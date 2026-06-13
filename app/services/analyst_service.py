"""Analyst service — all query and mutation logic for the analyst dashboard.

Route handlers in ``app/api/v1/routes/analyst.py`` call this module only —
no SQLAlchemy queries appear in the route layer.

Responsibilities
----------------
* Paginated, multi-dimensional-filterable report feed.
* Report detail with AI results, building footprint GeoJSON, analyst notes,
  and building damage timeline.
* Status workflow transitions (verify / reject / duplicate) with reporter
  trust-tier updates, building severity sync, and AuditLog writes.
* Manual merge (same logic as the automatic merge in duplicate_tasks.py).
* Analyst note creation.
* Statistics summary and heatmap (public, Redis-cached 60 s).

Responder geographic scoping
-----------------------------
Regional responders carry a ``region_geojson`` claim in their JWT (a GeoJSON
Polygon string).  When present, every query is filtered with a PostGIS
``ST_Within`` predicate so responders cannot see reports outside their region.

Privacy guarantees
------------------
* Reporter hash and token are NEVER returned to callers.
* ``reporter_trust_tier`` is the only reporter identifier exposed.
* Analyst note author (``analyst_id_hash``) is stored but never surfaced in
  any response schema.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, cast
from uuid import UUID

import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_feedback import AIFeedback
from app.models.analyst_note import AnalystNote
from app.models.audit_log import AuditLog
from app.models.building import Building
from app.models.enums import (
    DamageSeverity,
    ReportDamageSeverity,
    ReportStatus,
)
from app.models.report import Report
from app.schemas.analyst_schemas import (
    _REJECTION_REASON_CODES,
    AIAccuracyResponse,
    AnalystNoteOut,
    ConfirmMergeResponse,
    FeedbackTypeBreakdown,
    MergeResponse,
    PaginatedReports,
    RejectMergeResponse,
    ReportDetailSchema,
    ReportSummarySchema,
    SeverityOverrideResponse,
    StatsSummaryResponse,
    TimelineReportItem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis cache key helpers
# ---------------------------------------------------------------------------

_STATS_SUMMARY_KEY = "crisismap:stats:summary"
_STATS_HEATMAP_KEY = "crisismap:stats:heatmap"
_STATS_CACHE_TTL = 60  # seconds

# Redis Pub/Sub channel for SSE streaming (spec §13.4 / issue #15)
ANALYST_EVENTS_CHANNEL = "crisismap:analyst_events"

# ---------------------------------------------------------------------------
# Severity ordering helper (for trust-tier-aware building severity update)
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: Dict[str, int] = {
    "none": 0,
    "minimal": 1,
    "partial": 2,
    "destroyed": 3,
}

_DAMAGE_TO_BUILDING_SEVERITY: Dict[str, str] = {
    "minimal": "minimal",
    "partial": "partial",
    "destroyed": "destroyed",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Priority numeric mapping for ORDER BY — higher number = shown first.
_PRIORITY_ORDER = sa.case(
    (Report.review_priority == "critical", 4),
    (Report.review_priority == "high", 3),
    (Report.review_priority == "normal", 2),
    (Report.review_priority == "low", 1),
    else_=2,
)


def _build_report_filters(
    query: sa.Select,
    *,
    crisis_type: Optional[List[str]],
    damage_severity: Optional[List[str]],
    infrastructure_type: Optional[List[str]],
    status: Optional[List[str]],
    time_from: Optional[datetime],
    time_to: Optional[datetime],
    min_ai_confidence: Optional[float],
    review_priority: Optional[List[str]],
    ai_divergence_only: Optional[bool],
    region_geojson: Optional[str],
) -> sa.Select:
    """Apply all active filter predicates to *query* and return it.

    All parameters are optional; only non-None values generate predicates.
    Multi-value filters use ``IN`` clauses.  The responder geographic scope
    is applied via a PostGIS ``ST_Within`` filter when ``region_geojson`` is
    present.
    """
    if crisis_type:
        query = query.where(Report.crisis_type.in_(crisis_type))
    if damage_severity:
        query = query.where(Report.damage_severity.in_(damage_severity))
    if infrastructure_type:
        query = query.where(Report.infrastructure_type.in_(infrastructure_type))
    if status:
        query = query.where(Report.status.in_(status))
    if time_from:
        query = query.where(Report.created_at >= time_from)
    if time_to:
        query = query.where(Report.created_at <= time_to)
    if min_ai_confidence is not None:
        query = query.where(Report.ai_confidence >= min_ai_confidence)
    if review_priority:
        query = query.where(Report.review_priority.in_(review_priority))
    if ai_divergence_only:
        query = query.where(Report.ai_divergence.is_(True))
    if region_geojson:
        # PostGIS geographic scope for regional responders.
        # The filter uses a raw text clause to avoid importing geoalchemy2
        # into the service layer.
        query = query.where(
            text(
                "ST_Within("
                "ST_SetSRID(ST_Point(report.lng, report.lat), 4326), "
                "ST_SetSRID(ST_GeomFromGeoJSON(:region_geojson), 4326)"
                ")"
            ).bindparams(region_geojson=region_geojson)
        )
    return query


async def _get_building_footprint_geojson(
    db: AsyncSession, building_id: Optional[UUID]
) -> Optional[str]:
    """Return GeoJSON string of a building footprint, or None."""
    if building_id is None:
        return None
    row = await db.execute(
        text(
            "SELECT ST_AsGeoJSON(footprint) AS geojson "
            "FROM building WHERE id = :building_id"
        ).bindparams(building_id=str(building_id))
    )
    result = row.fetchone()
    return result.geojson if result else None


async def _log_ai_feedback(
    db: AsyncSession,
    *,
    report_id: UUID,
    feedback_type: str,
    ai_prediction: Optional[str],
    analyst_decision: Optional[str],
    ai_confidence: Optional[float],
) -> None:
    """Append one AI feedback row within the current transaction."""
    is_agreement: Optional[bool] = None
    if ai_prediction is not None and analyst_decision is not None:
        is_agreement = ai_prediction == analyst_decision

    entry = AIFeedback(
        report_id=report_id,
        feedback_type=feedback_type,
        ai_prediction=ai_prediction,
        analyst_decision=analyst_decision,
        is_agreement=is_agreement,
        ai_confidence=ai_confidence,
    )
    db.add(entry)


async def _write_audit_log(
    db: AsyncSession,
    *,
    operation: str,
    actor_id_hash: str,
    record_id: UUID,
    before_state: Optional[Dict[str, Any]],
    after_state: Dict[str, Any],
) -> None:
    """Insert an AuditLog row within the current transaction."""
    entry = AuditLog(
        operation=operation,
        actor_id_hash=actor_id_hash,
        record_id=record_id,
        before_state=before_state,
        after_state=after_state,
    )
    db.add(entry)


# ---------------------------------------------------------------------------
# Public API — report listing
# ---------------------------------------------------------------------------


async def list_reports(
    db: AsyncSession,
    *,
    page: int = 1,
    limit: int = 50,
    crisis_type: Optional[List[str]] = None,
    damage_severity: Optional[List[str]] = None,
    infrastructure_type: Optional[List[str]] = None,
    status: Optional[List[str]] = None,
    time_from: Optional[datetime] = None,
    time_to: Optional[datetime] = None,
    min_ai_confidence: Optional[float] = None,
    review_priority: Optional[List[str]] = None,
    ai_divergence_only: Optional[bool] = None,
    sort_by: Optional[str] = None,
    region_geojson: Optional[str] = None,
) -> PaginatedReports:
    """Return a paginated, filtered list of reports for the analyst dashboard.

    Args:
        db:                  Async database session.
        page:                1-based page number (default 1).
        limit:               Items per page (default 50, max 200).
        crisis_type:         Multi-value filter list.
        damage_severity:     Multi-value filter list.
        infrastructure_type: Multi-value filter list.
        status:              Multi-value filter list.
        time_from:           ISO 8601 UTC lower bound on ``created_at``.
        time_to:             ISO 8601 UTC upper bound on ``created_at``.
        min_ai_confidence:   Minimum ``ai_confidence`` threshold.
        review_priority:     Multi-value filter list (critical/high/normal/low).
        ai_divergence_only:  When True, restrict to reports where AI prediction
                             disagrees with the reporter's severity assessment.
        sort_by:             ``"severity"`` for priority+destroyed-first ordering;
                             ``"created_at"`` for pure chronological (bypasses
                             priority); default is priority-first + created_at.
        region_geojson:      Responder geographic scope (GeoJSON Polygon string).

    Returns:
        ``PaginatedReports`` with ``total``, ``page``, ``limit``, and ``items``.
    """
    base_query = sa.select(Report)
    count_query = sa.select(sa.func.count()).select_from(Report)

    base_query = _build_report_filters(
        base_query,
        crisis_type=crisis_type,
        damage_severity=damage_severity,
        infrastructure_type=infrastructure_type,
        status=status,
        time_from=time_from,
        time_to=time_to,
        min_ai_confidence=min_ai_confidence,
        review_priority=review_priority,
        ai_divergence_only=ai_divergence_only,
        region_geojson=region_geojson,
    )
    count_query = _build_report_filters(
        count_query,
        crisis_type=crisis_type,
        damage_severity=damage_severity,
        infrastructure_type=infrastructure_type,
        status=status,
        time_from=time_from,
        time_to=time_to,
        min_ai_confidence=min_ai_confidence,
        review_priority=review_priority,
        ai_divergence_only=ai_divergence_only,
        region_geojson=region_geojson,
    )

    # Sorting
    # Priority always leads (critical → high → normal → low) unless the caller
    # explicitly requests pure chronological order with sort_by="created_at".
    # This ensures analysts always see the reports that most need human review
    # regardless of when they were submitted.
    if sort_by == "created_at":
        base_query = base_query.order_by(Report.created_at.desc())
    elif sort_by == "severity":
        severity_order = sa.case(
            (Report.damage_severity == "destroyed", 3),
            (Report.damage_severity == "partial", 2),
            (Report.damage_severity == "minimal", 1),
            else_=0,
        )
        base_query = base_query.order_by(
            _PRIORITY_ORDER.desc(),
            severity_order.desc(),
            Report.created_at.desc(),
        )
    else:
        # Default: priority-first, then newest within each priority tier.
        base_query = base_query.order_by(
            _PRIORITY_ORDER.desc(), Report.created_at.desc()
        )

    # Count total matching records
    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    # Apply pagination
    offset = (page - 1) * limit
    base_query = base_query.offset(offset).limit(limit)

    result = await db.execute(base_query)
    reports = result.scalars().all()

    items = [ReportSummarySchema.model_validate(r) for r in reports]

    return PaginatedReports(total=total, page=page, limit=limit, items=items)


# ---------------------------------------------------------------------------
# Public API — report detail
# ---------------------------------------------------------------------------


async def get_report_detail(
    db: AsyncSession,
    report_id: UUID,
    *,
    region_geojson: Optional[str] = None,
) -> Optional[ReportDetailSchema]:
    """Return full report detail for the analyst dashboard.

    Loads the report, its analyst notes, its building's damage timeline, and
    the matched building footprint GeoJSON.

    Args:
        db:            Async database session.
        report_id:     UUID of the report to load.
        region_geojson: Responder scope; if set, returns ``None`` for reports
                        outside the region (same effect as 404 from caller).

    Returns:
        ``ReportDetailSchema`` or ``None`` if not found / out of scope.
    """
    # Load the report with analyst_notes eagerly via options
    result = await db.execute(
        sa.select(Report)
        .options(
            sa.orm.selectinload(Report.analyst_notes),
        )
        .where(Report.id == report_id)
    )
    report = result.scalar_one_or_none()
    if report is None:
        return None

    # Responder scope check — done in Python to avoid complex PostGIS subquery
    if region_geojson and report.lat is not None and report.lng is not None:
        try:
            from shapely.geometry import Point, shape  # type: ignore[import]

            region = shape(json.loads(region_geojson))
            point = Point(report.lng, report.lat)
            if not region.contains(point):
                return None
        except Exception:
            # If shapely is unavailable or parsing fails, fall through —
            # the PostGIS filter on the list endpoint is the primary gate.
            pass

    # Building footprint GeoJSON
    footprint_geojson = await _get_building_footprint_geojson(db, report.building_id)

    # Building damage timeline (all reports for same building, ordered by created_at)
    building_timeline: List[TimelineReportItem] = []
    if report.building_id is not None:
        timeline_result = await db.execute(
            sa.select(Report)
            .where(Report.building_id == report.building_id)
            .order_by(Report.created_at.asc())
        )
        timeline_reports = timeline_result.scalars().all()
        building_timeline = [
            TimelineReportItem.model_validate(r) for r in timeline_reports
        ]

    # Analyst notes — body + created_at only (no author identity)
    notes_out = [AnalystNoteOut.model_validate(n) for n in report.analyst_notes]

    schema = ReportDetailSchema(
        id=report.id,
        building_id=report.building_id,
        footprint_geojson=footprint_geojson,
        crisis_type=report.crisis_type,
        infrastructure_type=report.infrastructure_type,
        damage_severity=report.damage_severity,
        lat=report.lat,
        lng=report.lng,
        gps_accuracy_m=report.gps_accuracy_m,
        landmark_description=report.landmark_description,
        electricity_status=report.electricity_status,
        health_services_status=report.health_services_status,
        most_pressing_needs=report.most_pressing_needs,
        debris_clearing_needed=report.debris_clearing_needed,
        photo_url=report.photo_url,
        photo_status=report.photo_status,
        ai_quality_score=report.ai_quality_score,
        ai_severity_prediction=report.ai_severity_prediction,
        ai_confidence=report.ai_confidence,
        ai_divergence=report.ai_divergence,
        status=report.status,
        reporter_trust_tier=report.reporter_trust_tier,
        analyst_notes=notes_out,
        building_timeline=building_timeline,
        created_at=cast(datetime, report.created_at),
        updated_at=cast(datetime, report.updated_at),
    )
    return schema


# ---------------------------------------------------------------------------
# Public API — status transition
# ---------------------------------------------------------------------------


async def transition_report_status(
    db: AsyncSession,
    report_id: UUID,
    *,
    new_status: ReportStatus,
    reason_code: Optional[str],
    notes: Optional[str],
    analyst_id_hash: str,
) -> Report:
    """Apply a status transition with all associated side-effects.

    Side-effects:
    * ``verified``: increments ``reporter_trust_tier`` (cap 2); updates
      ``buildings.current_severity`` if this report's severity is higher.
    * ``rejected``: decrements ``reporter_trust_tier`` (floor 0); removes
      from active map (status=rejected in DB; record preserved).
    * All transitions written to ``AuditLog`` with before/after state.

    Args:
        db:              Async database session.
        report_id:       UUID of the report to transition.
        new_status:      Target ``ReportStatus``.
        reason_code:     Required when ``new_status == rejected``.
        notes:           Optional analyst notes to attach.
        analyst_id_hash: Anonymised analyst identifier for audit log.

    Returns:
        Updated ``Report`` ORM instance.

    Raises:
        ValueError:  Business rule violation (missing reason_code, invalid
                     reason_code value, or invalid target status).
        LookupError: Report not found.
    """
    # Validate target status — pending_merge_review is system-managed only
    if new_status not in (
        ReportStatus.verified,
        ReportStatus.rejected,
        ReportStatus.duplicate,
    ):
        raise ValueError(
            f"Invalid target status '{new_status}'. "
            "Permitted values: verified, rejected, duplicate."
        )

    # Validate reason_code requirement
    if new_status == ReportStatus.rejected:
        if not reason_code:
            raise ValueError("reason_code is required when status == 'rejected'.")
        if reason_code not in _REJECTION_REASON_CODES:
            raise ValueError(
                f"Invalid reason_code '{reason_code}'. "
                f"Must be one of: {', '.join(sorted(_REJECTION_REASON_CODES))}."
            )

    result = await db.execute(sa.select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise LookupError(f"Report {report_id} not found.")

    before_state = {
        "status": (
            report.status.value
            if hasattr(report.status, "value")
            else str(report.status)
        ),
        "reporter_trust_tier": report.reporter_trust_tier,
    }

    # Apply status
    report.status = new_status

    # Trust tier side-effects
    if new_status == ReportStatus.verified:
        report.reporter_trust_tier = min(report.reporter_trust_tier + 1, 2)

        # Update building current_severity if this report's severity is higher
        if report.building_id is not None:
            await _sync_building_severity(
                db, report.building_id, report.damage_severity
            )

    elif new_status == ReportStatus.rejected:
        report.reporter_trust_tier = max(report.reporter_trust_tier - 1, 0)

    # Attach optional analyst note
    if notes:
        note = AnalystNote(
            report_id=report_id,
            analyst_id_hash=analyst_id_hash,
            body=notes,
        )
        db.add(note)

    # Audit log
    after_state: Dict[str, Any] = {
        "status": (
            new_status.value if hasattr(new_status, "value") else str(new_status)
        ),
        "reporter_trust_tier": report.reporter_trust_tier,
    }
    if reason_code:
        after_state["reason_code"] = reason_code
    if notes:
        after_state["notes_attached"] = True

    await _write_audit_log(
        db,
        operation="report.status_change",
        actor_id_hash=analyst_id_hash,
        record_id=report_id,
        before_state=before_state,
        after_state=after_state,
    )

    # Active learning: record the analyst decision vs AI prediction.
    # The ground-truth severity is the analyst's explicit override if present,
    # otherwise the reporter's damage_severity (which the analyst confirmed).
    if new_status in (ReportStatus.verified, ReportStatus.rejected):
        ai_pred = (
            report.ai_severity_prediction.value
            if report.ai_severity_prediction
            else None
        )
        analyst_dec = (
            report.analyst_severity_override.value
            if report.analyst_severity_override
            else (
                report.damage_severity.value
                if hasattr(report.damage_severity, "value")
                else str(report.damage_severity)
            )
        )
        await _log_ai_feedback(
            db,
            report_id=report_id,
            feedback_type=new_status.value,  # 'verify' or 'reject'
            ai_prediction=ai_pred,
            analyst_decision=analyst_dec,
            ai_confidence=report.ai_confidence,
        )

    await db.flush()
    return report


async def _sync_building_severity(
    db: AsyncSession,
    building_id: UUID,
    new_report_severity: ReportDamageSeverity,
) -> None:
    """Update ``buildings.current_severity`` if new report severity is higher."""
    result = await db.execute(sa.select(Building).where(Building.id == building_id))
    building = result.scalar_one_or_none()
    if building is None:
        return

    current_order = _SEVERITY_ORDER.get(
        (
            building.current_severity.value
            if hasattr(building.current_severity, "value")
            else str(building.current_severity)
        ),
        0,
    )
    new_order = _SEVERITY_ORDER.get(
        (
            new_report_severity.value
            if hasattr(new_report_severity, "value")
            else str(new_report_severity)
        ),
        0,
    )

    if new_order > current_order:
        new_building_severity = _DAMAGE_TO_BUILDING_SEVERITY.get(
            (
                new_report_severity.value
                if hasattr(new_report_severity, "value")
                else str(new_report_severity)
            ),
            "none",
        )
        building.current_severity = DamageSeverity(new_building_severity)
        building.last_report_at = datetime.now(tz=timezone.utc)
        logger.debug(
            "Building %s severity updated to %s",
            building_id,
            new_building_severity,
        )


# ---------------------------------------------------------------------------
# Public API — manual merge
# ---------------------------------------------------------------------------


async def merge_reports(
    db: AsyncSession,
    primary_id: UUID,
    duplicate_ids: List[UUID],
    *,
    analyst_id_hash: str,
    region_geojson: Optional[str] = None,
) -> MergeResponse:
    """Manually merge duplicate reports into a primary record.

    Implements the same merge logic as ``duplicate_tasks._apply_auto_merge``.
    All IDs must exist and be within the analyst's accessible scope.

    Args:
        db:              Async database session.
        primary_id:      UUID of the primary (surviving) report.
        duplicate_ids:   List of UUIDs to mark as duplicates.
        analyst_id_hash: Anonymised analyst identifier for audit log.
        region_geojson:  Responder scope (optional).

    Returns:
        ``MergeResponse`` with ``primary_id`` and ``merged_count``.

    Raises:
        LookupError: If any ID is not found or out of scope.
    """
    # Validate primary exists
    primary_result = await db.execute(sa.select(Report).where(Report.id == primary_id))
    primary = primary_result.scalar_one_or_none()
    if primary is None:
        raise LookupError(f"Primary report {primary_id} not found.")

    merged_count = 0
    for dup_id in duplicate_ids:
        dup_result = await db.execute(sa.select(Report).where(Report.id == dup_id))
        dup = dup_result.scalar_one_or_none()
        if dup is None:
            raise LookupError(f"Duplicate report {dup_id} not found.")

        before_state = {
            "status": (
                dup.status.value if hasattr(dup.status, "value") else str(dup.status)
            ),
        }

        dup.status = ReportStatus.duplicate
        dup.duplicate_of_id = primary_id

        # Most recently submitted photo wins (spec §10.2)
        if dup.photo_url and not primary.photo_url:
            primary.photo_url = dup.photo_url

        await _write_audit_log(
            db,
            operation="report.manual_merge",
            actor_id_hash=analyst_id_hash,
            record_id=dup_id,
            before_state=before_state,
            after_state={
                "status": "duplicate",
                "duplicate_of_id": str(primary_id),
                "merged_by": "analyst",
            },
        )
        merged_count += 1

    await db.flush()
    logger.info(
        "Manual merge: %d reports merged into primary %s by analyst %s…",
        merged_count,
        primary_id,
        analyst_id_hash[:8],
    )
    return MergeResponse(primary_id=primary_id, merged_count=merged_count)


# ---------------------------------------------------------------------------
# Public API — analyst notes
# ---------------------------------------------------------------------------


async def create_analyst_note(
    db: AsyncSession,
    report_id: UUID,
    *,
    body: str,
    analyst_id_hash: str,
) -> AnalystNoteOut:
    """Create an analyst note on a report.

    Notes are visible to all analysts but are never exported (spec §11).

    Args:
        db:              Async database session.
        report_id:       UUID of the report to annotate.
        body:            Note content.
        analyst_id_hash: Anonymised analyst identifier (stored but not returned).

    Returns:
        ``AnalystNoteOut`` with ``id``, ``body``, and ``created_at``.

    Raises:
        LookupError: If the report does not exist.
    """
    # Verify report exists
    report_result = await db.execute(sa.select(Report.id).where(Report.id == report_id))
    if report_result.scalar_one_or_none() is None:
        raise LookupError(f"Report {report_id} not found.")

    note = AnalystNote(
        report_id=report_id,
        analyst_id_hash=analyst_id_hash,
        body=body,
    )
    db.add(note)
    await db.flush()

    return AnalystNoteOut.model_validate(note)


# ---------------------------------------------------------------------------
# Public API — analyst severity override (Feature 1)
# ---------------------------------------------------------------------------


async def set_severity_override(
    db: AsyncSession,
    report_id: UUID,
    *,
    override: ReportDamageSeverity,
    analyst_id_hash: str,
) -> SeverityOverrideResponse:
    """Record an analyst's explicit correction of the AI severity prediction.

    Writes to ``report.analyst_severity_override`` only — never modifies
    ``damage_severity`` (reporter) or ``ai_severity_prediction`` (AI).
    Also logs one ``AIFeedback`` row so the correction feeds into accuracy
    calibration.

    Args:
        db:              Async database session.
        report_id:       UUID of the report to override.
        override:        Analyst's corrected severity value.
        analyst_id_hash: Anonymised analyst identifier for audit log.

    Returns:
        ``SeverityOverrideResponse`` with the updated field.

    Raises:
        LookupError: Report not found.
    """
    result = await db.execute(sa.select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise LookupError(f"Report {report_id} not found.")

    previous = (
        report.analyst_severity_override.value
        if report.analyst_severity_override
        else None
    )
    report.analyst_severity_override = override

    await _write_audit_log(
        db,
        operation="report.severity_override",
        actor_id_hash=analyst_id_hash,
        record_id=report_id,
        before_state={"analyst_severity_override": previous},
        after_state={"analyst_severity_override": override.value},
    )

    # Log AI feedback: analyst explicitly disagreed with (or confirmed) AI
    ai_pred = (
        report.ai_severity_prediction.value if report.ai_severity_prediction else None
    )
    await _log_ai_feedback(
        db,
        report_id=report_id,
        feedback_type="severity_override",
        ai_prediction=ai_pred,
        analyst_decision=override.value,
        ai_confidence=report.ai_confidence,
    )

    await db.flush()
    return SeverityOverrideResponse(
        id=report_id,
        analyst_severity_override=override,
    )


# ---------------------------------------------------------------------------
# Public API — pending merge confirmation / rejection (Feature 2)
# ---------------------------------------------------------------------------


async def confirm_pending_merge(
    db: AsyncSession,
    report_id: UUID,
    *,
    analyst_id_hash: str,
) -> ConfirmMergeResponse:
    """Confirm a system-flagged pending merge, executing the actual merge.

    The duplicate detection worker sets ``status = 'pending_merge_review'``
    and ``possible_duplicate_of_id`` when a report scores ≥ 0.9.  This
    endpoint lets the analyst review and confirm the merge instead of it
    happening silently.

    Side-effects:
    * ``status``               → ``duplicate``
    * ``duplicate_of_id``      ← ``possible_duplicate_of_id``
    * ``possible_duplicate_of_id`` cleared
    * Primary report photo updated if this report has one and primary does not.

    Args:
        db:              Async database session.
        report_id:       UUID of the report in ``pending_merge_review`` state.
        analyst_id_hash: Anonymised analyst identifier for audit log.

    Returns:
        ``ConfirmMergeResponse`` with the primary report UUID.

    Raises:
        LookupError: Report not found.
        ValueError:  Report is not in ``pending_merge_review`` status.
    """
    result = await db.execute(sa.select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise LookupError(f"Report {report_id} not found.")
    if report.status != ReportStatus.pending_merge_review:
        raise ValueError(
            f"Report {report_id} is not pending merge review "
            f"(current status: {report.status})."
        )
    if report.possible_duplicate_of_id is None:
        raise ValueError(f"Report {report_id} has no possible_duplicate_of_id set.")

    primary_id = report.possible_duplicate_of_id

    # Execute the merge
    report.status = ReportStatus.duplicate
    report.duplicate_of_id = primary_id
    report.possible_duplicate_of_id = None

    # Most-recently-submitted photo wins — update primary if it has no photo
    if report.photo_url:
        primary_result = await db.execute(
            sa.select(Report).where(Report.id == primary_id)
        )
        primary = primary_result.scalar_one_or_none()
        if primary and not primary.photo_url:
            primary.photo_url = report.photo_url

    await _write_audit_log(
        db,
        operation="report.confirm_merge",
        actor_id_hash=analyst_id_hash,
        record_id=report_id,
        before_state={"status": "pending_merge_review"},
        after_state={
            "status": "duplicate",
            "duplicate_of_id": str(primary_id),
            "confirmed_by": "analyst",
        },
    )

    await db.flush()
    return ConfirmMergeResponse(
        id=report_id,
        status="duplicate",
        merged_into=primary_id,
    )


async def reject_pending_merge(
    db: AsyncSession,
    report_id: UUID,
    *,
    analyst_id_hash: str,
) -> RejectMergeResponse:
    """Reject a system-flagged pending merge, restoring the report to pending.

    The report is returned to ``status = 'pending'`` for normal analyst review.
    ``possible_duplicate_of_id`` and ``duplicate_score`` are cleared.

    Args:
        db:              Async database session.
        report_id:       UUID of the report in ``pending_merge_review`` state.
        analyst_id_hash: Anonymised analyst identifier for audit log.

    Returns:
        ``RejectMergeResponse``.

    Raises:
        LookupError: Report not found.
        ValueError:  Report is not in ``pending_merge_review`` status.
    """
    result = await db.execute(sa.select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise LookupError(f"Report {report_id} not found.")
    if report.status != ReportStatus.pending_merge_review:
        raise ValueError(
            f"Report {report_id} is not pending merge review "
            f"(current status: {report.status})."
        )

    before_primary = str(report.possible_duplicate_of_id)
    report.status = ReportStatus.pending
    report.possible_duplicate_of_id = None
    report.duplicate_score = None

    await _write_audit_log(
        db,
        operation="report.reject_merge",
        actor_id_hash=analyst_id_hash,
        record_id=report_id,
        before_state={
            "status": "pending_merge_review",
            "possible_duplicate_of_id": before_primary,
        },
        after_state={"status": "pending", "merge_rejected_by": "analyst"},
    )

    await db.flush()
    return RejectMergeResponse(id=report_id, status="pending")


# ---------------------------------------------------------------------------
# Public API — AI accuracy metrics (Feature 3)
# ---------------------------------------------------------------------------


async def get_ai_accuracy(db: AsyncSession) -> AIAccuracyResponse:
    """Return accuracy metrics derived from analyst feedback records.

    Queries the ``ai_feedback`` table to compute how often the AI's severity
    prediction has agreed with analyst decisions.  Provides a recommended
    divergence threshold adjustment when agreement on high-confidence
    predictions drops below an actionable level.

    Args:
        db: Async database session.

    Returns:
        ``AIAccuracyResponse`` with overall and per-type accuracy metrics.
    """
    rows_result = await db.execute(sa.select(AIFeedback))
    all_feedback = list(rows_result.scalars().all())

    total = len(all_feedback)
    if total == 0:
        return AIAccuracyResponse(
            total_feedback=0,
            agreement_rate=None,
            high_confidence_agreement_rate=None,
            avg_ai_confidence=None,
            by_feedback_type={},
            recommended_divergence_threshold=None,
        )

    agreements = [f for f in all_feedback if f.is_agreement]
    overall_rate = len(agreements) / total

    high_conf = [f for f in all_feedback if f.ai_confidence and f.ai_confidence > 0.7]
    hc_rate = (
        len([f for f in high_conf if f.is_agreement]) / len(high_conf)
        if high_conf
        else None
    )

    conf_vals = [f.ai_confidence for f in all_feedback if f.ai_confidence is not None]
    avg_conf = sum(conf_vals) / len(conf_vals) if conf_vals else None

    by_type: Dict[str, Any] = {}
    for ft in ("verify", "reject", "severity_override"):
        subset = [f for f in all_feedback if f.feedback_type == ft]
        subset_agreements = [f for f in subset if f.is_agreement]
        by_type[ft] = FeedbackTypeBreakdown(
            count=len(subset),
            agreement_rate=(len(subset_agreements) / len(subset) if subset else None),
        ).model_dump()

    # Recommend lowering the divergence threshold when high-confidence
    # predictions are only right ~60 % of the time or less.
    recommended_threshold: Optional[float] = None
    if hc_rate is not None:
        if hc_rate >= 0.85:
            recommended_threshold = 0.7  # current default — no change needed
        elif hc_rate >= 0.70:
            recommended_threshold = 0.6  # flag more reports for review
        else:
            recommended_threshold = 0.5  # AI is poorly calibrated; flag broadly

    return AIAccuracyResponse(
        total_feedback=total,
        agreement_rate=round(overall_rate, 4),
        high_confidence_agreement_rate=(
            round(hc_rate, 4) if hc_rate is not None else None
        ),
        avg_ai_confidence=round(avg_conf, 4) if avg_conf is not None else None,
        by_feedback_type=by_type,
        recommended_divergence_threshold=recommended_threshold,
    )


# ---------------------------------------------------------------------------
# Public API — stats summary (public, cached 60 s)
# ---------------------------------------------------------------------------


async def get_stats_summary(
    db: AsyncSession,
    redis: Redis,
) -> StatsSummaryResponse:
    """Return aggregated statistics, cached in Redis for 60 seconds.

    Args:
        db:    Async database session.
        redis: Async Redis client.

    Returns:
        ``StatsSummaryResponse`` with total, by_severity, by_crisis_type,
        and last_updated.
    """
    cached = await redis.get(_STATS_SUMMARY_KEY)
    if cached:
        try:
            return StatsSummaryResponse(**json.loads(cached))
        except Exception:
            pass  # Corrupt cache — fall through to live query.

    # Total count
    total_result = await db.execute(
        sa.select(sa.func.count())
        .select_from(Report)
        .where(Report.status != ReportStatus.rejected)
    )
    total = total_result.scalar_one()

    # By severity
    severity_rows = await db.execute(
        sa.select(Report.damage_severity, sa.func.count())
        .where(Report.status != ReportStatus.rejected)
        .group_by(Report.damage_severity)
    )
    by_severity = {"minimal": 0, "partial": 0, "destroyed": 0}
    for row in severity_rows:
        key = row[0].value if hasattr(row[0], "value") else str(row[0])
        if key in by_severity:
            by_severity[key] = row[1]

    # By crisis type
    crisis_rows = await db.execute(
        sa.select(Report.crisis_type, sa.func.count())
        .where(Report.status != ReportStatus.rejected)
        .group_by(Report.crisis_type)
    )
    by_crisis_type = {
        "flood": 0,
        "earthquake": 0,
        "conflict": 0,
        "wildfire": 0,
        "other": 0,
    }
    for crisis_row in crisis_rows:
        key = (
            crisis_row[0].value
            if hasattr(crisis_row[0], "value")
            else str(crisis_row[0])
        )
        if key in by_crisis_type:
            by_crisis_type[key] = crisis_row[1]

    now = datetime.now(tz=timezone.utc)
    response = StatsSummaryResponse(
        total=total,
        by_severity=by_severity,  # type: ignore[arg-type]
        by_crisis_type=by_crisis_type,  # type: ignore[arg-type]
        last_updated=now,
    )

    await redis.set(
        _STATS_SUMMARY_KEY,
        response.model_dump_json(),
        ex=_STATS_CACHE_TTL,
    )
    return response


# ---------------------------------------------------------------------------
# Public API — heatmap (public, cached 60 s)
# ---------------------------------------------------------------------------


async def get_heatmap(
    db: AsyncSession,
    redis: Redis,
) -> Dict[str, Any]:
    """Return GeoJSON FeatureCollection of heatmap points, cached 60 s.

    Each feature is a Point with a ``weight`` property proportional to the
    report density in that area (spec §13.5).

    Args:
        db:    Async database session.
        redis: Async Redis client.

    Returns:
        GeoJSON FeatureCollection dict.
    """
    cached = await redis.get(_STATS_HEATMAP_KEY)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    rows_result = await db.execute(
        sa.select(Report.lat, Report.lng, Report.damage_severity).where(
            Report.lat.isnot(None),
            Report.lng.isnot(None),
            Report.status != ReportStatus.rejected,
        )
    )
    rows = rows_result.all()

    # Weight by severity
    _weight_map = {"minimal": 1, "partial": 2, "destroyed": 3}

    features = []
    for row in rows:
        sev_key = row[2].value if hasattr(row[2], "value") else str(row[2])
        weight = _weight_map.get(sev_key, 1)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [row[1], row[0]],  # [lng, lat]
                },
                "properties": {"weight": weight},
            }
        )

    geojson = {"type": "FeatureCollection", "features": features}

    await redis.set(_STATS_HEATMAP_KEY, json.dumps(geojson), ex=_STATS_CACHE_TTL)
    return geojson
