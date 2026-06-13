"""Duplicate detection Celery task — spec §10.

This module contains the ``score_report`` task, consumed by the default Celery
worker queue.  It is triggered after both the GIS worker (``gis_tasks``) and
the AI worker (``ai_tasks``) have completed, so that ``building_id`` and
``photo_phash`` are present on the report record.

Task responsibilities
---------------------
1. Load the target report from the database.
2. Query up to 500 candidate reports from the same building or within 100 m.
3. Delegate scoring to ``DuplicateScorer``.
4. Apply the recommended action inside an atomic database transaction:
   - AUTO_MERGE  → mark new report as duplicate; update primary photo if newer.
   - ANALYST_FLAG → set ``possible_duplicate_of_id`` and ``duplicate_score``.
   - INDEPENDENT  → no-op (report already stored normally).
5. Write an ``AuditLog`` entry for merge events.

Atomicity guarantee
-------------------
The merge update and the audit log write share a single SQLAlchemy transaction.
If the audit log write raises (e.g., database constraint violation), the entire
transaction is rolled back so the report retains its original ``pending`` status.

All database access is **synchronous** (Celery runs in threads, not an async
event loop).  A fresh session is opened per invocation and always closed in the
``finally`` block.

Retry policy
------------
Up to 3 retries with 30-second backoff on any unexpected exception.
IntegrityError is not retried — it indicates a data problem.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from uuid import UUID

from celery import Task
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.services.duplicate_service import (
    CandidateReport,
    DuplicateAction,
    DuplicateScorer,
)
from app.workers.celery_app import celery_app

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
# Query constants
# ---------------------------------------------------------------------------

# Maximum number of candidate reports to evaluate (spec §10)
_MAX_CANDIDATES: int = 500

# Candidate search radius in metres when no building match is available
_CANDIDATE_SEARCH_RADIUS_M: float = 100.0


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _load_report(db: Session, report_id: UUID) -> Optional[Any]:
    """Load target report row from the database.

    Returns:
        A SQLAlchemy ``Row`` with the required fields, or ``None``.
    """
    return db.execute(
        text("""
            SELECT
                id,
                building_id,
                lat,
                lng,
                photo_phash,
                photo_url,
                crisis_type,
                infrastructure_type,
                status,
                created_at
            FROM report
            WHERE id = :report_id
        """),
        {"report_id": str(report_id)},
    ).fetchone()


def _load_candidates(
    db: Session,
    report_id: UUID,
    building_id: Optional[str],
    lat: Optional[float],
    lng: Optional[float],
    report_created_at: datetime,
) -> list[CandidateReport]:
    """Fetch up to ``_MAX_CANDIDATES`` candidate reports to score against.

    Candidates are drawn from reports that share the same building footprint
    OR that fall within ``_CANDIDATE_SEARCH_RADIUS_M`` metres of the incoming
    report's coordinates — whichever set is broader.

    **Time-window filter:** only reports whose ``created_at`` falls within
    ±``DUPLICATE_TIME_WINDOW_HOURS`` of the incoming report are considered.
    This prevents cross-event false positives where the same building is
    damaged in two separate disaster events (e.g. a flood report from three
    months ago matching today's earthquake report). A "duplicate" by definition
    means the same damage event was reported more than once; two reports
    separated by more than the window represent distinct events.

    The 72-hour default accommodates delayed offline submissions and slow
    cellular sync common in the field regions this system targets.

    The incoming report itself is always excluded.

    Args:
        db:                Active synchronous session.
        report_id:         UUID of the report being scored (excluded).
        building_id:       Matched building UUID string, or ``None``.
        lat:               WGS84 latitude, or ``None``.
        lng:               WGS84 longitude, or ``None``.
        report_created_at: ``created_at`` timestamp of the report being scored.
                           Candidates outside ±DUPLICATE_TIME_WINDOW_HOURS are
                           excluded.

    Returns:
        List of ``CandidateReport`` dataclass instances.
    """
    window_hours = settings.DUPLICATE_TIME_WINDOW_HOURS
    # Ensure the reference timestamp is timezone-aware for consistent
    # comparison against the timezone-aware `created_at` column.
    if report_created_at.tzinfo is None:
        report_created_at = report_created_at.replace(tzinfo=timezone.utc)
    time_lower = report_created_at - timedelta(hours=window_hours)
    time_upper = report_created_at + timedelta(hours=window_hours)

    # Use an explicit list[Row[Any]] so mypy is satisfied when we call
    # list() on fetchall() (which returns Sequence[Row[Any]]).
    rows: List[Row[Any]] = []

    if building_id is not None:
        rows = list(
            db.execute(
                text("""
                    SELECT
                        id,
                        building_id,
                        lat,
                        lng,
                        photo_phash,
                        crisis_type,
                        infrastructure_type
                    FROM report
                    WHERE building_id = :building_id
                      AND id          != :report_id
                      AND status      NOT IN ('duplicate')
                      AND created_at  >= :time_lower
                      AND created_at  <= :time_upper
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {
                    "building_id": building_id,
                    "report_id": str(report_id),
                    "time_lower": time_lower,
                    "time_upper": time_upper,
                    "limit": _MAX_CANDIDATES,
                },
            ).fetchall()
        )

    # If no building match OR fewer candidates than the cap, also add
    # GPS-proximity candidates (using PostGIS ST_DWithin if PostGIS is
    # available, falling back to a bounding-box pre-filter otherwise).
    if len(rows) < _MAX_CANDIDATES and lat is not None and lng is not None:
        remaining = _MAX_CANDIDATES - len(rows)
        existing_ids = {str(r.id) for r in rows}

        try:
            geo_rows: List[Row[Any]] = list(
                db.execute(
                    text("""
                        SELECT
                            id,
                            building_id,
                            lat,
                            lng,
                            photo_phash,
                            crisis_type,
                            infrastructure_type
                        FROM report
                        WHERE ST_DWithin(
                            ST_SetSRID(ST_Point(lng, lat), 4326)::geography,
                            ST_SetSRID(ST_Point(:lng, :lat), 4326)::geography,
                            :radius_m
                        )
                          AND id         != :report_id
                          AND id         != ALL(:existing_ids)
                          AND status     NOT IN ('duplicate')
                          AND created_at >= :time_lower
                          AND created_at <= :time_upper
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    {
                        "lat": lat,
                        "lng": lng,
                        "radius_m": _CANDIDATE_SEARCH_RADIUS_M,
                        "report_id": str(report_id),
                        "existing_ids": list(existing_ids),
                        "time_lower": time_lower,
                        "time_upper": time_upper,
                        "limit": remaining,
                    },
                ).fetchall()
            )
            rows.extend(geo_rows)
        except Exception as exc:  # noqa: BLE001
            # PostGIS may not be available in the test SQLite environment.
            # Fall back to a bounding-box approximation (1° ≈ 111 320 m).
            logger.warning(
                "PostGIS ST_DWithin unavailable (%s) — using bounding-box " "fallback",
                type(exc).__name__,
            )
            import math

            lat_delta = _CANDIDATE_SEARCH_RADIUS_M / 111_320.0
            lng_delta = _CANDIDATE_SEARCH_RADIUS_M / (
                111_320.0 * math.cos(math.radians(lat))
            )
            fallback_rows: List[Row[Any]] = list(
                db.execute(
                    text("""
                        SELECT
                            id,
                            building_id,
                            lat,
                            lng,
                            photo_phash,
                            crisis_type,
                            infrastructure_type
                        FROM report
                        WHERE lat        BETWEEN :min_lat AND :max_lat
                          AND lng        BETWEEN :min_lng AND :max_lng
                          AND id         != :report_id
                          AND status     NOT IN ('duplicate')
                          AND created_at >= :time_lower
                          AND created_at <= :time_upper
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    {
                        "min_lat": lat - lat_delta,
                        "max_lat": lat + lat_delta,
                        "min_lng": lng - lng_delta,
                        "max_lng": lng + lng_delta,
                        "report_id": str(report_id),
                        "time_lower": time_lower,
                        "time_upper": time_upper,
                        "limit": remaining,
                    },
                ).fetchall()
            )
            rows.extend(r for r in fallback_rows if str(r.id) not in existing_ids)

    candidates = []
    for row in rows:
        candidates.append(
            CandidateReport(
                id=UUID(str(row.id)),
                building_id=UUID(str(row.building_id)) if row.building_id else None,
                lat=float(row.lat) if row.lat is not None else None,
                lng=float(row.lng) if row.lng is not None else None,
                photo_phash=row.photo_phash,
                crisis_type=str(row.crisis_type),
                infrastructure_type=str(row.infrastructure_type),
            )
        )

    logger.debug("Loaded %d candidates for report %s", len(candidates), report_id)
    return candidates


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _apply_pending_merge_review(
    db: Session,
    report_id: UUID,
    primary_id: UUID,
    composite_score: float,
) -> None:
    """Queue a high-confidence duplicate for analyst confirmation.

    Instead of silently merging (old AUTO_MERGE behaviour), the report is set
    to ``pending_merge_review`` with ``possible_duplicate_of_id`` pointing at
    the likely primary.  An analyst must confirm or reject the merge via
    ``POST /analyst/reports/{id}/confirm-merge`` or ``…/reject-merge``.

    All writes share the caller's transaction.

    Args:
        db:              Active synchronous session.
        report_id:       The new report to hold for review.
        primary_id:      The most probable primary report.
        composite_score: Composite duplicate score (≥ 0.9).
    """
    db.execute(
        text("""
            UPDATE report
            SET
                status                   = 'pending_merge_review',
                possible_duplicate_of_id = :primary_id,
                duplicate_score          = :score,
                updated_at               = NOW()
            WHERE id = :report_id
        """),
        {
            "primary_id": str(primary_id),
            "report_id": str(report_id),
            "score": composite_score,
        },
    )

    db.execute(
        text("""
            INSERT INTO audit_log (
                operation,
                actor_id_hash,
                record_id,
                before_state,
                after_state
            ) VALUES (
                'report.pending_merge_review',
                'system',
                :record_id,
                :before_state,
                :after_state
            )
        """),
        {
            "record_id": str(report_id),
            "before_state": '{"status": "pending"}',
            "after_state": json.dumps(
                {
                    "status": "pending_merge_review",
                    "possible_duplicate_of_id": str(primary_id),
                    "duplicate_score": composite_score,
                    "awaiting": "analyst_confirmation",
                }
            ),
        },
    )

    logger.info(
        "Pending merge review queued: report %s → possible primary %s (score=%.4f)",
        report_id,
        primary_id,
        composite_score,
    )


def _apply_analyst_flag(
    db: Session,
    report_id: UUID,
    possible_primary_id: UUID,
    composite_score: float,
) -> None:
    """Flag ``report_id`` as a possible duplicate for analyst review.

    Args:
        db:                  Active synchronous session.
        report_id:           The report to flag.
        possible_primary_id: The most likely primary report.
        composite_score:     Composite duplicate score.
    """
    db.execute(
        text("""
            UPDATE report
            SET
                possible_duplicate_of_id = :primary_id,
                duplicate_score          = :score,
                updated_at               = NOW()
            WHERE id = :report_id
        """),
        {
            "primary_id": str(possible_primary_id),
            "report_id": str(report_id),
            "score": composite_score,
        },
    )

    logger.info(
        "Analyst flag: report %s possible duplicate of %s (score=%.4f)",
        report_id,
        possible_primary_id,
        composite_score,
    )


# ---------------------------------------------------------------------------
# Core implementation — decoupled from Celery for unit testability
# ---------------------------------------------------------------------------


def _score_report_impl(report_id: str) -> dict:  # type: ignore[return]
    """Core duplicate detection logic, decoupled from the Celery task wrapper."""
    _report_id = UUID(report_id)
    logger.info("Duplicate detection started for report %s", _report_id)

    db: Session = _SyncSessionLocal()
    _rolled_back: bool = False
    try:
        # ── 1. Load the target report ─────────────────────────────────────────
        row = _load_report(db, _report_id)
        if row is None:
            logger.error("Duplicate task: report %s not found — skipping", _report_id)
            return {"action": "skipped", "best_score": 0.0, "primary_id": None}

        building_id_str: Optional[str] = (
            str(row.building_id) if row.building_id else None
        )
        lat: Optional[float] = float(row.lat) if row.lat is not None else None
        lng: Optional[float] = float(row.lng) if row.lng is not None else None
        report_created_at: datetime = row.created_at

        # ── 2. Fetch candidate reports ────────────────────────────────────────
        candidates = _load_candidates(
            db,
            _report_id,
            building_id_str,
            lat,
            lng,
            report_created_at,
        )

        if not candidates:
            logger.info(
                "Duplicate task: no candidates found for report %s — independent",
                _report_id,
            )
            return {
                "action": DuplicateAction.INDEPENDENT,
                "best_score": 0.0,
                "primary_id": None,
            }

        # ── 3. Score ──────────────────────────────────────────────────────────
        scorer = DuplicateScorer()
        result = scorer.score(
            incoming_building_id=(UUID(building_id_str) if building_id_str else None),
            incoming_lat=lat,
            incoming_lng=lng,
            incoming_phash=row.photo_phash,
            incoming_crisis_type=str(row.crisis_type),
            incoming_infrastructure_type=str(row.infrastructure_type),
            candidates=candidates,
        )

        # ── 4. Apply threshold action (inside a transaction) ──────────────────
        primary_id: Optional[UUID] = None

        if result.action == DuplicateAction.AUTO_MERGE and result.best_candidate:
            primary_id = result.best_candidate.candidate.id
            try:
                # Human-in-the-loop: queue for analyst review instead of
                # merging silently. The analyst confirms or rejects via API.
                _apply_pending_merge_review(
                    db=db,
                    report_id=_report_id,
                    primary_id=primary_id,
                    composite_score=result.best_score,
                )
                db.commit()
            except Exception:
                db.rollback()
                _rolled_back = True
                logger.error(
                    "Duplicate task: pending-merge-review write failed for "
                    "report %s — rolling back; report status unchanged",
                    _report_id,
                )
                raise

        elif result.action == DuplicateAction.ANALYST_FLAG and result.best_candidate:
            primary_id = result.best_candidate.candidate.id
            _apply_analyst_flag(
                db=db,
                report_id=_report_id,
                possible_primary_id=primary_id,
                composite_score=result.best_score,
            )
            db.commit()

        else:
            logger.info(
                "Duplicate task: report %s is independent (best_score=%.4f)",
                _report_id,
                result.best_score,
            )

        return {
            "action": result.action.value,
            "best_score": result.best_score,
            "primary_id": str(primary_id) if primary_id else None,
        }

    except IntegrityError as exc:
        if not _rolled_back:
            db.rollback()
        logger.error(
            "Duplicate task: integrity error for report %s: %s",
            _report_id,
            exc,
        )
        return {"action": "error", "best_score": 0.0, "primary_id": None}

    except Exception:
        if not _rolled_back:
            db.rollback()
        raise

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Celery task — thin retry wrapper around _score_report_impl
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.duplicate_tasks.score_report",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def score_report(self: Task, report_id: str) -> dict:
    """Score a report against existing records for duplicate detection.

    Triggered after the GIS worker resolves the building match and the AI
    worker writes ``photo_phash``.  Delegates all logic to
    ``_score_report_impl`` and handles the Celery retry policy.

    Args:
        report_id: UUID string of the ``report`` record to process.

    Returns:
        Dict with keys: ``action``, ``best_score``, ``primary_id``.

    Raises:
        celery.exceptions.Retry: On transient errors (up to 3 retries).
    """
    try:
        return _score_report_impl(report_id)
    except Exception as exc:
        logger.warning(
            "Duplicate task error for report %s (%s) — will retry",
            report_id,
            type(exc).__name__,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error(
                "Duplicate task permanently failed for report %s after %d retries",
                report_id,
                self.max_retries,
            )
            return {"action": "error", "best_score": 0.0, "primary_id": None}
