"""GIS Celery task definitions.

This module contains the ``match_building`` task, which is consumed by the
``celery-gis`` worker from the ``gis`` queue.

The task is triggered by the submission service after a report is created
AND after that report's database transaction has been committed (see
``app/services/submission_service.py`` and ``app/api/v1/routes/reports.py``
for the dispatch ordering fix — jobs are now published only after commit,
so this worker's separate DB connection can always see the row it's asked
to process).

It runs the four-step building footprint matching sequence defined in spec §9.2
via ``GISService``, updates the ``report`` and ``building`` records, invalidates
the affected Redis cache keys, logs the outcome, and — via
``duplicate_dispatch.mark_step_done_and_maybe_dispatch`` — signals that the
GIS step for this report is complete. Once both the GIS step and (if the
report has a photo) the AI step have signalled completion, duplicate
detection (``duplicate_tasks.score_report``) is automatically dispatched.
Previously nothing dispatched that task at all, so duplicate/merge-review
detection never ran.

All database access uses **synchronous** SQLAlchemy (``Session``) because Celery
workers run in regular threads, not an async event loop.  A fresh session is
opened per task invocation and committed or rolled-back before exit.

Retry policy:
    Up to 3 retries with 30-second backoff on any unexpected exception.
    Database integrity errors are not retried (they indicate a data problem).

    IMPORTANT: ``mark_step_done_and_maybe_dispatch`` is called only at
    genuine terminal points (success, "report not found", integrity error,
    and the final MaxRetriesExceededError branch) — never just before an
    exception is re-raised for a Celery retry. Calling it on every retry
    attempt would decrement the pending-step counter multiple times for a
    single logical step. See ``duplicate_dispatch`` module docstring.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple, cast
from uuid import UUID

import redis as sync_redis
from celery import Task
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.services.geocoding_service import get_geocoding_provider
from app.services.gis_service import GISService
from app.workers.celery_app import celery_app
from app.workers.duplicate_dispatch import mark_step_done_and_maybe_dispatch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synchronous SQLAlchemy engine (Celery context — no async)
# ---------------------------------------------------------------------------

_sync_url = settings.DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")

_sync_engine = create_engine(
    _sync_url,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=2,
)

_SyncSessionLocal = sessionmaker(
    bind=_sync_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

# ---------------------------------------------------------------------------
# Redis cache key helpers — must match the API layer
# ---------------------------------------------------------------------------

_CACHE_NS = "gis"


def _heatmap_key(bbox_hash: str) -> str:
    return f"{_CACHE_NS}:heatmap:{bbox_hash}"


def _stats_summary_key() -> str:
    return f"{_CACHE_NS}:stats:summary"


def _invalidate_gis_caches(redis_client: sync_redis.Redis) -> None:
    """Delete all heatmap cache keys and the stats summary key.

    The heatmap keys use a pattern scan so that all bbox variants are cleared.
    Pattern scans are done with SCAN (non-blocking) rather than KEYS.
    """
    cursor: int = 0
    pattern = f"{_CACHE_NS}:heatmap:*"
    while True:
        scan_result = cast(
            Tuple[int, List[bytes]],
            redis_client.scan(cursor=cursor, match=pattern, count=100),
        )
        cursor, keys = scan_result
        if keys:
            redis_client.delete(*keys)
        if cursor == 0:
            break

    redis_client.delete(_stats_summary_key())
    logger.debug("GIS cache invalidated (heatmap + stats summary)")


# ---------------------------------------------------------------------------
# Implementation — extracted so tests can call it directly without Celery
# ---------------------------------------------------------------------------


def _match_building_impl(report_id: str) -> dict:
    """Core matching logic, decoupled from the Celery task wrapper.

    Extracted into a standalone function so that unit tests can call it
    directly without needing a Celery worker context or ``self`` (Task).
    The Celery task ``match_building`` delegates here and handles retries.

    Every terminal return path calls
    ``duplicate_dispatch.mark_step_done_and_maybe_dispatch`` so that
    duplicate/merge-review scoring is triggered once this report's GIS step
    (and its AI step, if it has a photo) have both completed. The one
    exception is the generic-``Exception`` branch, which re-raises for a
    Celery retry — that is NOT a terminal state, so it must not fire the
    marker (see module docstring).

    Args:
        report_id: UUID string of the ``report`` record to process.

    Returns:
        Dict with ``building_id``, ``confidence``, and ``distance_m``.

    Raises:
        Exception: Any non-integrity error is re-raised so the Celery wrapper
                   can apply the retry policy.
    """
    _report_id = UUID(report_id)
    logger.info("GIS task started for report %s", _report_id)

    db: Session = _SyncSessionLocal()
    try:
        # ── 1. Load report ────────────────────────────────────────────────────
        row = db.execute(
            text("""
                SELECT id, lat, lng, gps_accuracy_m, landmark_description
                FROM report
                WHERE id = :report_id
                """),
            {"report_id": str(_report_id)},
        ).fetchone()

        if row is None:
            logger.error("GIS task: report %s not found — skipping", _report_id)
            mark_step_done_and_maybe_dispatch(str(_report_id))
            return {"building_id": None, "confidence": 0.0, "distance_m": None}

        lat: Optional[float] = row.lat
        lng: Optional[float] = row.lng
        accuracy_m: Optional[float] = row.gps_accuracy_m
        landmark: Optional[str] = row.landmark_description

        # ── 2. Run matching ───────────────────────────────────────────────────
        geocoding_provider = get_geocoding_provider()
        gis = GISService(db)
        match = gis.match_building(
            lat=lat,
            lng=lng,
            accuracy_m=accuracy_m,
            landmark_description=landmark,
            geocoding_provider=geocoding_provider,
        )

        # ── 3. Write match result to report ───────────────────────────────────
        if match.building_id is not None:
            db.execute(
                text("""
                    UPDATE report
                    SET
                        building_id                = :building_id,
                        footprint_match_confidence = :confidence
                    WHERE id = :report_id
                    """),
                {
                    "building_id": str(match.building_id),
                    "confidence": match.confidence,
                    "report_id": str(_report_id),
                },
            )
            gis.update_building_severity(match.building_id)
        else:
            db.execute(
                text("""
                    UPDATE report
                    SET footprint_match_confidence = 0.0
                    WHERE id = :report_id
                    """),
                {"report_id": str(_report_id)},
            )

        db.commit()

        # ── 4. Invalidate GIS cache ────────────────────────────────────────────
        redis_client = sync_redis.Redis.from_url(
            settings.REDIS_URL, decode_responses=True
        )
        try:
            _invalidate_gis_caches(redis_client)
        finally:
            redis_client.close()

        logger.info(
            "GIS task complete for report %s — building=%s confidence=%.3f",
            _report_id,
            match.building_id,
            match.confidence,
        )

        # GIS step is done — signal the coordinator. If this report has no
        # photo (no AI step was scheduled), this is the call that dispatches
        # duplicate scoring.
        mark_step_done_and_maybe_dispatch(str(_report_id))

        return {
            "building_id": str(match.building_id) if match.building_id else None,
            "confidence": match.confidence,
            "distance_m": match.distance_m,
        }

    except IntegrityError as exc:
        db.rollback()
        logger.error("GIS task integrity error for report %s: %s", _report_id, exc)
        # Do NOT retry integrity errors — they indicate a data problem.
        # This is a terminal state, so the coordinator must still be told.
        mark_step_done_and_maybe_dispatch(str(_report_id))
        return {"building_id": None, "confidence": 0.0, "distance_m": None}

    except Exception:
        db.rollback()
        # NOT terminal — this propagates to the Celery wrapper, which will
        # retry. Do not call mark_step_done_and_maybe_dispatch here; the
        # MaxRetriesExceededError branch in the task wrapper below handles
        # the eventual terminal (permanent-failure) case.
        raise

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Celery task — thin wrapper around _match_building_impl
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.gis_tasks.match_building",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def match_building(self: Task, report_id: str) -> dict:
    """Match a report's GPS coordinates to a building footprint.

    Triggered by the submission service after a report is created and its
    transaction has been committed.  Delegates all logic to
    ``_match_building_impl`` and handles the Celery retry policy.

    Args:
        report_id: UUID string of the ``report`` record to process.

    Returns:
        Dict with ``building_id``, ``confidence``, and ``distance_m``.

    Raises:
        celery.exceptions.Retry: On transient errors (up to 3 retries).
    """
    try:
        return _match_building_impl(report_id)
    except Exception as exc:
        logger.warning(
            "GIS task error for report %s (%s) — will retry",
            report_id,
            type(exc).__name__,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error(
                "GIS task permanently failed for report %s after %d retries",
                report_id,
                self.max_retries,
            )
            # Permanent failure — this IS a terminal state for the GIS step,
            # so the duplicate-scoring coordinator must be told, or a report
            # whose GIS step never succeeds would sit forever waiting for a
            # step that will never complete.
            mark_step_done_and_maybe_dispatch(report_id)
            return {"building_id": None, "confidence": 0.0, "distance_m": None}
        