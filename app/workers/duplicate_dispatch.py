"""Shared helper — coordinates GIS + AI task completion to trigger duplicate scoring.

``duplicate_tasks.score_report`` was previously never invoked by anything:
neither ``gis_tasks.match_building`` nor ``ai_tasks.process_report_image``
dispatched it, and no other code path did either. This module closes that
gap.

Both the GIS worker and the AI worker call ``mark_step_done_and_maybe_dispatch``
after they finish handling a report — on every *terminal* return path
(success, "not found", "no photo", permanent failure after retries) — so
that ``score_report`` runs exactly once per report, once every step that was
scheduled for it has completed, regardless of which worker finishes last.

IMPORTANT — retry semantics:
    This is deliberately NOT called from a blanket ``try/finally`` around the
    whole task body. Both ``match_building`` and ``process_report_image``
    retry transient failures (network errors, ``VisionAPIError``, etc.) up
    to 3 times, and each retry re-invokes the task function. If the marker
    were called unconditionally on every invocation, a task that retries
    twice before succeeding would decrement the pending-step counter three
    times for what is logically a single step, causing duplicate scoring to
    fire before the report is actually ready (or firing an extra,
    unnecessary time later once the counter goes negative again).

    Call this ONLY at genuine terminal points:
      * a normal successful return,
      * an early "nothing to do" return (report/photo not found, etc.),
      * the MaxRetriesExceededError branch after retries are exhausted.
    Do NOT call it just before an exception is re-raised for a Celery retry.

The submission service (and the photo-upload route) seed the expected step
count in Redis at job-dispatch time: 1 if the report has no photo (GIS
only), 2 if it has a photo (GIS + AI). When a photo is attached later via
``PATCH /reports/{id}/photo``, the gate is reseeded to 1 (AI only) so
duplicate scoring re-runs with the new image-similarity signal included.
"""

from __future__ import annotations

import logging

import redis as sync_redis

from app.core.config import settings
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_PENDING_STEPS_KEY_FMT = "crisismap:report:{report_id}:pending_dup_steps"


def mark_step_done_and_maybe_dispatch(report_id: str) -> None:
    """Decrement the pending-step counter for *report_id*; dispatch scoring at 0.

    Safe to call from any Celery worker context — opens and closes its own
    synchronous Redis connection, matching the pattern already used elsewhere
    in this codebase (see ``ai_tasks._get_divergence_threshold`` and
    ``gis_tasks._invalidate_gis_caches``). Never raises: a coordination
    failure is logged and swallowed so it can never fail an otherwise
    successful GIS or AI task.

    Args:
        report_id: UUID string of the report whose step just reached a
                   terminal state (success or permanent failure).
    """
    key = _PENDING_STEPS_KEY_FMT.format(report_id=report_id)
    client = sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        # If the key is missing entirely (e.g. the gate was never seeded —
        # should not happen in normal operation), DECR creates it at -1,
        # which is still <= 0 and triggers dispatch. That is the safe
        # default: better to score once unexpectedly than to silently never
        # score at all.
        remaining = client.decr(key)
        if remaining <= 0:
            client.delete(key)
            celery_app.send_task(
                "app.workers.duplicate_tasks.score_report",
                args=[report_id],
                queue="duplicate",
            )
            logger.info("Duplicate scoring dispatched for report %s", report_id)
        else:
            logger.debug(
                "Report %s: %d step(s) still pending before duplicate scoring",
                report_id,
                remaining,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not coordinate duplicate-scoring dispatch for report %s: %s",
            report_id,
            type(exc).__name__,
        )
    finally:
        client.close()
