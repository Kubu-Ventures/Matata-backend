"""Pipeline reconciliation sweep — closes audit findings M-8 and M-9.

Why this exists
---------------
A report is durably stored by ``POST /reports`` *before* its GIS / AI /
duplicate-scoring jobs are dispatched (the commit-before-dispatch race fix).
Two failure modes leave a report stored but never processed:

* **M-8** — ``redis.set(gate)`` or ``queue.publish_*`` raises *after*
  ``db.commit()``.  The client gets a 500, the row sits ``pending`` forever,
  and the coordination gate's 1-hour TTL erases the only trace.
* **M-9** — Redis is unavailable or flushed when a worker reaches a terminal
  state, so ``mark_step_done_and_maybe_dispatch`` cannot decrement the gate
  and ``score_report`` is never dispatched.

``reconcile_stuck_reports`` runs on a schedule (Celery beat, every 5 minutes
by default — see ``celery_app.conf.beat_schedule``) or on demand
(``python -m app.cli reconcile``).  It finds ``pending`` reports older than a
grace period whose pipeline is demonstrably incomplete and re-drives only the
missing stages.  Every re-driven task is idempotent:

* ``gis_tasks.match_building`` / ``ai_tasks.process_report_image`` overwrite
  their own columns and call ``mark_step_done_and_maybe_dispatch`` on every
  terminal path.
* ``duplicate_tasks.score_report`` has an explicit "only act while still
  ``pending``" guard (M-10) and writes a durable ``report.duplicate_scored``
  audit row, which this sweep checks with ``NOT EXISTS`` so it never loops on
  an already-scored report.

The grace period (default 15 min, vs. seconds of normal processing) makes it
practically impossible to re-drive a report that is merely slow rather than
stuck.  The SQL is PostgreSQL-specific (``make_interval``); unit tests mock
the query the same way the PostGIS paths are mocked elsewhere.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from celery import Task
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Synchronous engine — Celery/CLI context, mirrors duplicate_tasks.
_sync_url = settings.DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
_sync_engine = create_engine(_sync_url, pool_pre_ping=True, pool_size=2, max_overflow=2)
_SyncSessionLocal = sessionmaker(
    bind=_sync_engine, autoflush=False, autocommit=False, expire_on_commit=False
)

# Photo-processing states that count as "AI stage finished".
_AI_TERMINAL_PHOTO_STATES = ("accepted", "insufficient_quality", "ai_processing_failed")

DEFAULT_GRACE_MINUTES = 15
DEFAULT_LIMIT = 500
# Older than this: leave it for a human — re-driving forever is pointless.
_MAX_AGE_HOURS = 72

_DEDUP_DONE_EXISTS = """
    EXISTS (
        SELECT 1 FROM audit_log al
        WHERE al.record_id = report.id
          AND al.operation IN
              ('report.duplicate_scored', 'report.pending_merge_review')
    )
"""

_STUCK_QUERY = text(f"""
    SELECT
        id,
        (footprint_match_confidence IS NULL) AS needs_gis,
        (
            photo_url IS NOT NULL
            AND (photo_status IS NULL OR photo_status NOT IN :ai_terminal)
        ) AS needs_ai,
        (NOT {_DEDUP_DONE_EXISTS}) AS needs_dedup
    FROM report
    WHERE status = 'pending'
      AND created_at < (NOW() - make_interval(mins => :grace_minutes))
      AND created_at > (NOW() - make_interval(hours => :max_age_hours))
      AND (
            footprint_match_confidence IS NULL
         OR (
              photo_url IS NOT NULL
              AND (photo_status IS NULL OR photo_status NOT IN :ai_terminal)
            )
         OR NOT {_DEDUP_DONE_EXISTS}
      )
    ORDER BY created_at ASC
    LIMIT :limit
    """).bindparams(bindparam("ai_terminal", expanding=True))


def _pending_dup_steps_key(report_id: UUID) -> str:
    # Must match app/api/v1/routes/reports.py.
    return f"crisismap:report:{report_id}:pending_dup_steps"


def _reconcile_impl(
    *,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Find stuck ``pending`` reports and re-drive their missing stages.

    Returns ``{scanned, gis, ai, dedup_direct}``.
    """
    from redis import Redis as SyncRedis

    db: Session = _SyncSessionLocal()
    redis_client: Optional[Any] = None
    summary: dict[str, Any] = {"scanned": 0, "gis": 0, "ai": 0, "dedup_direct": 0}
    try:
        rows = db.execute(
            _STUCK_QUERY,
            {
                "grace_minutes": grace_minutes,
                "max_age_hours": _MAX_AGE_HOURS,
                "limit": limit,
                "ai_terminal": list(_AI_TERMINAL_PHOTO_STATES),
            },
        ).fetchall()
        db.rollback()  # read-only — release the snapshot

        if not rows:
            logger.debug("reconcile: nothing stuck")
            return summary

        try:
            redis_client = SyncRedis.from_url(settings.REDIS_URL)
            redis_client.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reconcile: Redis unavailable (%s) — aborting sweep",
                type(exc).__name__,
            )
            return summary

        for row in rows:
            summary["scanned"] += 1
            report_id = UUID(str(row.id))
            needs_gis, needs_ai = bool(row.needs_gis), bool(row.needs_ai)
            steps = int(needs_gis) + int(needs_ai)

            if steps:
                try:
                    redis_client.set(_pending_dup_steps_key(report_id), steps, ex=3600)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "reconcile: gate seed failed for %s (%s) — skipping",
                        report_id,
                        type(exc).__name__,
                    )
                    continue
                if needs_gis:
                    celery_app.send_task(
                        "app.workers.gis_tasks.match_building",
                        args=[str(report_id)],
                        queue="gis",
                    )
                    summary["gis"] += 1
                if needs_ai:
                    celery_app.send_task(
                        "app.workers.ai_tasks.process_report_image",
                        args=[str(report_id)],
                        queue="ai",
                    )
                    summary["ai"] += 1
                logger.info(
                    "reconcile: re-drove %s (gis=%s ai=%s)",
                    report_id,
                    needs_gis,
                    needs_ai,
                )
            elif bool(row.needs_dedup):
                # GIS + AI complete but score_report was lost (M-9).
                celery_app.send_task(
                    "app.workers.duplicate_tasks.score_report",
                    args=[str(report_id)],
                    queue="duplicate",
                )
                summary["dedup_direct"] += 1
                logger.info("reconcile: re-dispatched score_report for %s", report_id)

        logger.info("reconcile sweep complete: %s", summary)
        return summary
    finally:
        db.close()
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:  # noqa: BLE001
                pass


@celery_app.task(
    name="app.workers.reconciliation_tasks.reconcile_stuck_reports",
    bind=True,
    max_retries=0,
    acks_late=True,
)
def reconcile_stuck_reports(
    self: Task,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Celery entry point — see ``_reconcile_impl``.  Never retries; the next
    scheduled run is the retry."""
    try:
        return _reconcile_impl(grace_minutes=grace_minutes, limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception("reconcile sweep failed")
        return {"scanned": 0, "gis": 0, "ai": 0, "dedup_direct": 0, "error": True}
