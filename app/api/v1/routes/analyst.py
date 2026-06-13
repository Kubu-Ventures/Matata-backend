"""Analyst dashboard route handlers.

All routes in this module are registered under the ``/api/v1/analyst`` prefix
(and ``/api/v1/stats`` for the public statistics endpoints).

Route handlers are intentionally thin: they validate request shapes, resolve
the authenticated user, call ``analyst_service``, and map service exceptions
to appropriate HTTP responses.

No SQLAlchemy queries appear here — all query and mutation logic lives in
``app/services/analyst_service.py``.

Role enforcement
----------------
* ``GET  /analyst/reports``              — analyst | responder
* ``GET  /analyst/reports/{id}``         — analyst | responder
* ``PATCH /analyst/reports/{id}/status`` — analyst only (responders cannot modify)
* ``POST /analyst/reports/merge``        — analyst only
* ``POST /analyst/reports/{id}/notes``   — analyst | responder
* ``GET  /analyst/stream``               — analyst | responder (SSE)
* ``GET  /stats/summary``                — public
* ``GET  /stats/heatmap``                — public

Responder geographic scoping
-----------------------------
Regional responders carry an optional ``region_geojson`` claim in their JWT.
When present, the service layer filters results via PostGIS ``ST_Within``.

SSE streaming
-------------
``GET /analyst/stream`` subscribes to the ``crisismap:analyst_events`` Redis
Pub/Sub channel and streams events to connected clients using Server-Sent
Events (text/event-stream).  A heartbeat comment is emitted every 30 seconds
to keep the connection alive through proxies.  The handler uses ``asyncio``
and ``aioredis`` (redis-py async) so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routes.auth import require_role
from app.core.dependencies import get_db, get_redis
from app.schemas.analyst_schemas import (
    AIAccuracyResponse,
    AnalystNoteCreateRequest,
    AnalystNoteOut,
    ConfirmMergeResponse,
    MergeRequest,
    MergeResponse,
    PaginatedReports,
    RejectMergeResponse,
    ReportDetailSchema,
    SeverityOverrideRequest,
    SeverityOverrideResponse,
    StatsSummaryResponse,
    StatusTransitionRequest,
)
from app.services import analyst_service
from app.services.auth_service import Role

logger = logging.getLogger(__name__)

analyst_router = APIRouter(prefix="/analyst", tags=["Analyst Dashboard"])
stats_router = APIRouter(prefix="/stats", tags=["Statistics"])

# ---------------------------------------------------------------------------
# Small response wrapper for status transitions
# ---------------------------------------------------------------------------


class StatusTransitionResponse(BaseModel):
    id: UUID
    status: str
    reporter_trust_tier: int


# ---------------------------------------------------------------------------
# Helper — extract region_geojson claim from JWT payload
# ---------------------------------------------------------------------------


def _region_geojson(current_user: dict) -> Optional[str]:
    """Extract the optional ``region_geojson`` claim from the JWT payload."""
    return current_user.get("region_geojson")


def _analyst_id_hash(current_user: dict) -> str:
    """Extract the anonymised analyst subject claim."""
    return current_user.get("sub", "")


# ---------------------------------------------------------------------------
# GET /analyst/reports — paginated, filterable report feed
# ---------------------------------------------------------------------------


@analyst_router.get(
    "/reports",
    response_model=PaginatedReports,
    status_code=status.HTTP_200_OK,
    summary="List reports (paginated + filtered)",
    description=(
        "Returns a paginated, multi-dimensional-filterable list of reports. "
        "Requires ``analyst`` or ``responder`` role. "
        "Regional responders automatically receive results scoped to their "
        "assigned geographic area via PostGIS ``ST_Within``."
    ),
)
async def list_reports(
    page: int = Query(default=1, ge=1, description="Page number (1-based)."),
    limit: int = Query(
        default=50, ge=1, le=200, description="Items per page (max 200)."
    ),
    crisis_type: Optional[str] = Query(
        default=None,
        description="Comma-separated crisis types to include.",
    ),
    damage_severity: Optional[str] = Query(
        default=None,
        description="Comma-separated severity levels to include.",
    ),
    infrastructure_type: Optional[str] = Query(
        default=None,
        description="Comma-separated infrastructure types to include.",
    ),
    report_status: Optional[str] = Query(
        default=None,
        alias="status",
        description="Comma-separated report statuses to include.",
    ),
    time_from: Optional[datetime] = Query(
        default=None,
        description="ISO 8601 UTC lower bound for created_at.",
    ),
    time_to: Optional[datetime] = Query(
        default=None,
        description="ISO 8601 UTC upper bound for created_at.",
    ),
    min_ai_confidence: Optional[float] = Query(
        default=None,
        ge=0.0,
        le=1.0,
        description="Minimum AI confidence threshold.",
    ),
    review_priority: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated priority levels to include: "
            "critical, high, normal, low. "
            "Example: ?review_priority=critical,high"
        ),
    ),
    divergence_only: Optional[bool] = Query(
        default=None,
        description=(
            "When true, restrict to reports where the AI's severity prediction "
            "disagrees with the reporter's classification (ai_divergence=true). "
            "These are the cases most in need of analyst adjudication."
        ),
    ),
    sort_by: Optional[str] = Query(
        default=None,
        description=(
            "Sort order: 'severity' (priority+destroyed first), "
            "'created_at' (pure chronological), "
            "or omit for default priority-first ordering."
        ),
    ),
    current_user: dict = Depends(require_role(Role.analyst, Role.responder)),
    db: AsyncSession = Depends(get_db),
) -> PaginatedReports:
    """Return a paginated, filtered report list."""

    def _split(value: Optional[str]) -> Optional[List[str]]:
        if not value:
            return None
        return [v.strip() for v in value.split(",") if v.strip()]

    return await analyst_service.list_reports(
        db,
        page=page,
        limit=limit,
        crisis_type=_split(crisis_type),
        damage_severity=_split(damage_severity),
        infrastructure_type=_split(infrastructure_type),
        status=_split(report_status),
        time_from=time_from,
        time_to=time_to,
        min_ai_confidence=min_ai_confidence,
        review_priority=_split(review_priority),
        ai_divergence_only=divergence_only,
        sort_by=sort_by,
        region_geojson=_region_geojson(current_user),
    )


# ---------------------------------------------------------------------------
# POST /analyst/reports/merge — must be registered BEFORE /{id}
# ---------------------------------------------------------------------------


@analyst_router.post(
    "/reports/merge",
    response_model=MergeResponse,
    status_code=status.HTTP_200_OK,
    summary="Manually merge duplicate reports",
    description=(
        "Merges one or more duplicate reports into a primary record. "
        "Executes the same merge logic as the automatic duplicate detection "
        "worker. All IDs must exist and be within the analyst's accessible "
        "scope. Requires ``analyst`` role."
    ),
)
async def merge_reports(
    body: MergeRequest,
    current_user: dict = Depends(require_role(Role.analyst)),
    db: AsyncSession = Depends(get_db),
) -> MergeResponse:
    """Merge duplicate reports into a primary record."""
    try:
        result = await analyst_service.merge_reports(
            db,
            primary_id=body.primary_id,
            duplicate_ids=body.duplicate_ids,
            analyst_id_hash=_analyst_id_hash(current_user),
            region_geojson=_region_geojson(current_user),
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# GET /analyst/reports/{id} — report detail
# ---------------------------------------------------------------------------


@analyst_router.get(
    "/reports/{report_id}",
    response_model=ReportDetailSchema,
    status_code=status.HTTP_200_OK,
    summary="Get report detail",
    description=(
        "Returns full report detail including AI results, matched building "
        "footprint GeoJSON, analyst notes (body + timestamp only — no author "
        "identity), and the building damage timeline. "
        "Requires ``analyst`` or ``responder`` role."
    ),
)
async def get_report_detail(
    report_id: UUID,
    current_user: dict = Depends(require_role(Role.analyst, Role.responder)),
    db: AsyncSession = Depends(get_db),
) -> ReportDetailSchema:
    """Return full analyst report detail."""
    detail = await analyst_service.get_report_detail(
        db,
        report_id,
        region_geojson=_region_geojson(current_user),
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report {report_id} not found.",
        )
    return detail


# ---------------------------------------------------------------------------
# PATCH /analyst/reports/{id}/status — status workflow transition
# ---------------------------------------------------------------------------


@analyst_router.patch(
    "/reports/{report_id}/status",
    response_model=StatusTransitionResponse,
    status_code=status.HTTP_200_OK,
    summary="Transition report status",
    description=(
        "Transitions a report to verified, rejected, or duplicate. "
        "``reason_code`` is required when status == 'rejected'. "
        "Verified reports increment the reporter trust tier (cap 2); "
        "rejected reports decrement it (floor 0). "
        "All transitions are written to the AuditLog. "
        "Requires ``analyst`` role — responders cannot modify status."
    ),
)
async def transition_status(
    report_id: UUID,
    body: StatusTransitionRequest,
    current_user: dict = Depends(require_role(Role.analyst)),
    db: AsyncSession = Depends(get_db),
) -> StatusTransitionResponse:
    """Apply a status transition with audit logging and trust-tier side-effects."""
    try:
        report = await analyst_service.transition_report_status(
            db,
            report_id,
            new_status=body.status,
            reason_code=body.reason_code,
            notes=body.notes,
            analyst_id_hash=_analyst_id_hash(current_user),
        )
        await db.commit()
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return StatusTransitionResponse(
        id=report.id,
        status=(
            report.status.value
            if hasattr(report.status, "value")
            else str(report.status)
        ),
        reporter_trust_tier=report.reporter_trust_tier,
    )


# ---------------------------------------------------------------------------
# POST /analyst/reports/{id}/notes — create analyst note
# ---------------------------------------------------------------------------


@analyst_router.post(
    "/reports/{report_id}/notes",
    response_model=AnalystNoteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add analyst note",
    description=(
        "Attaches an internal analyst note to a report. "
        "Notes are visible to all analysts but are never exported "
        "(enforced at the export layer). "
        "Requires ``analyst`` or ``responder`` role."
    ),
)
async def create_note(
    report_id: UUID,
    body: AnalystNoteCreateRequest,
    current_user: dict = Depends(require_role(Role.analyst, Role.responder)),
    db: AsyncSession = Depends(get_db),
) -> AnalystNoteOut:
    """Create an analyst note on a report."""
    try:
        note = await analyst_service.create_analyst_note(
            db,
            report_id,
            body=body.body,
            analyst_id_hash=_analyst_id_hash(current_user),
        )
        await db.commit()
        return note
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# POST /analyst/reports/{id}/severity-override — Feature 1
# ---------------------------------------------------------------------------


@analyst_router.post(
    "/reports/{report_id}/severity-override",
    response_model=SeverityOverrideResponse,
    status_code=status.HTTP_200_OK,
    summary="Override AI severity prediction",
    description=(
        "Records the analyst's explicit correction of the AI's damage severity "
        "prediction. Writes to ``analyst_severity_override`` only — never "
        "modifies the reporter's ``damage_severity`` or the AI's "
        "``ai_severity_prediction``. Also logs an ``AIFeedback`` entry for "
        "accuracy calibration. Requires ``analyst`` role."
    ),
)
async def set_severity_override(
    report_id: UUID,
    body: SeverityOverrideRequest,
    current_user: dict = Depends(require_role(Role.analyst)),
    db: AsyncSession = Depends(get_db),
) -> SeverityOverrideResponse:
    """Record an analyst's corrected severity assessment."""
    try:
        result = await analyst_service.set_severity_override(
            db,
            report_id,
            override=body.analyst_severity_override,
            analyst_id_hash=_analyst_id_hash(current_user),
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# POST /analyst/reports/{id}/confirm-merge — Feature 2
# ---------------------------------------------------------------------------


@analyst_router.post(
    "/reports/{report_id}/confirm-merge",
    response_model=ConfirmMergeResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm a pending duplicate merge",
    description=(
        "Confirms a system-suggested duplicate merge. The report must be in "
        "``pending_merge_review`` status (set by the duplicate detection worker "
        "when the similarity score ≥ 0.9). Executes the actual merge: sets "
        "``status=duplicate``, links ``duplicate_of_id``, and optionally "
        "promotes the photo to the primary record. Requires ``analyst`` role."
    ),
)
async def confirm_merge(
    report_id: UUID,
    current_user: dict = Depends(require_role(Role.analyst)),
    db: AsyncSession = Depends(get_db),
) -> ConfirmMergeResponse:
    """Confirm and execute a pending duplicate merge."""
    try:
        result = await analyst_service.confirm_pending_merge(
            db,
            report_id,
            analyst_id_hash=_analyst_id_hash(current_user),
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# POST /analyst/reports/{id}/reject-merge — Feature 2
# ---------------------------------------------------------------------------


@analyst_router.post(
    "/reports/{report_id}/reject-merge",
    response_model=RejectMergeResponse,
    status_code=status.HTTP_200_OK,
    summary="Reject a pending duplicate merge",
    description=(
        "Rejects a system-suggested duplicate merge. The report is returned to "
        "``pending`` status for normal analyst review. ``possible_duplicate_of_id`` "
        "and ``duplicate_score`` are cleared. Requires ``analyst`` role."
    ),
)
async def reject_merge(
    report_id: UUID,
    current_user: dict = Depends(require_role(Role.analyst)),
    db: AsyncSession = Depends(get_db),
) -> RejectMergeResponse:
    """Reject a pending duplicate merge and restore the report to pending."""
    try:
        result = await analyst_service.reject_pending_merge(
            db,
            report_id,
            analyst_id_hash=_analyst_id_hash(current_user),
        )
        await db.commit()
        return result
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# GET /analyst/ai-accuracy — Feature 3
# ---------------------------------------------------------------------------


@analyst_router.get(
    "/ai-accuracy",
    response_model=AIAccuracyResponse,
    status_code=status.HTTP_200_OK,
    summary="AI accuracy metrics",
    description=(
        "Returns accuracy metrics derived from analyst feedback records. "
        "Shows how often the AI's severity prediction has agreed with analyst "
        "decisions (verify, reject, severity override). Includes a recommended "
        "divergence threshold adjustment to calibrate the auto-flagging system "
        "based on observed accuracy. Requires ``analyst`` role."
    ),
)
async def ai_accuracy(
    current_user: dict = Depends(require_role(Role.analyst)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AIAccuracyResponse:
    """Return AI accuracy metrics and apply the recommended divergence threshold."""
    return await analyst_service.get_ai_accuracy(db, redis)


# ---------------------------------------------------------------------------
# GET /analyst/stream — Server-Sent Events
# ---------------------------------------------------------------------------

_SSE_HEARTBEAT_INTERVAL = 30  # seconds


async def _sse_event_generator(
    redis: Redis,
    current_user: dict,
) -> AsyncGenerator[str, None]:
    """Subscribe to the analyst events Redis channel and yield SSE frames.

    Events emitted: ``report.created``, ``report.updated``, ``report.critical``.
    A heartbeat comment (```: heartbeat\\n\\n``) is emitted every 30 seconds
    to keep the connection alive through reverse proxies.

    Uses ``asyncio`` throughout; the event loop is never blocked.

    Args:
        redis:        Async Redis client (connection re-used from dependency).
        current_user: JWT payload; used to scope events for responders.
    """
    region_geojson = _region_geojson(current_user)

    # Create a new pubsub object from the existing client
    pubsub = redis.pubsub()
    await pubsub.subscribe(analyst_service.ANALYST_EVENTS_CHANNEL)

    logger.info("SSE client connected (sub: %s…)", current_user.get("sub", "")[:8])

    try:
        heartbeat_task = asyncio.create_task(_heartbeat_ticker())

        while True:
            # Race: next message vs next heartbeat tick
            message_task = asyncio.create_task(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            )
            done, pending = await asyncio.wait(
                {message_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if heartbeat_task in done:
                yield ": heartbeat\n\n"
                heartbeat_task = asyncio.create_task(_heartbeat_ticker())
                # Cancel the pending message task only if it isn't done
                if message_task in pending:
                    message_task.cancel()
                    try:
                        await message_task
                    except asyncio.CancelledError:
                        pass
                continue

            # message_task is done
            if heartbeat_task in pending:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                heartbeat_task = asyncio.create_task(_heartbeat_ticker())

            try:
                message = message_task.result()
            except Exception:
                message = None

            if message and message.get("type") == "message":
                raw_data = message.get("data", "")
                try:
                    event_data = json.loads(raw_data)
                except (json.JSONDecodeError, TypeError):
                    event_data = {"raw": str(raw_data)}

                event_type = event_data.get("event", "report.updated")

                # Responder geographic scope filter (best-effort in Python)
                if region_geojson and "lat" in event_data and "lng" in event_data:
                    try:
                        from shapely.geometry import (  # type: ignore[import]
                            Point,
                            shape,
                        )

                        region = shape(json.loads(region_geojson))
                        point = Point(event_data["lng"], event_data["lat"])
                        if not region.contains(point):
                            continue
                    except Exception:
                        pass  # If shapely unavailable, forward all events

                yield (f"event: {event_type}\n" f"data: {json.dumps(event_data)}\n\n")

    except asyncio.CancelledError:
        logger.info(
            "SSE client disconnected (sub: %s…)",
            current_user.get("sub", "")[:8],
        )
    finally:
        await pubsub.unsubscribe(analyst_service.ANALYST_EVENTS_CHANNEL)
        await pubsub.aclose()


async def _heartbeat_ticker() -> None:
    """Coroutine that resolves after ``_SSE_HEARTBEAT_INTERVAL`` seconds."""
    await asyncio.sleep(_SSE_HEARTBEAT_INTERVAL)


@analyst_router.get(
    "/stream",
    summary="SSE real-time event stream",
    description=(
        "Server-Sent Events stream of analyst dashboard events. "
        "Emits ``report.created``, ``report.updated``, and ``report.critical`` "
        "(complete destruction) events published to the "
        "``crisismap:analyst_events`` Redis Pub/Sub channel. "
        "A heartbeat comment is sent every 30 seconds. "
        "Requires ``analyst`` or ``responder`` role."
    ),
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE stream of analyst events.",
        }
    },
)
async def analyst_stream(
    current_user: dict = Depends(require_role(Role.analyst, Role.responder)),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    """Stream real-time analyst events via Server-Sent Events."""
    return StreamingResponse(
        _sse_event_generator(redis, current_user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


# ---------------------------------------------------------------------------
# GET /stats/summary — public, cached 60 s
# ---------------------------------------------------------------------------


@stats_router.get(
    "/summary",
    response_model=StatsSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Statistics summary",
    description=(
        "Returns aggregated dashboard counters: total active reports, "
        "breakdown by severity, breakdown by crisis type, and last_updated "
        "timestamp. Public endpoint, cached 60 seconds."
    ),
)
async def stats_summary(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StatsSummaryResponse:
    """Return cached statistics summary."""
    return await analyst_service.get_stats_summary(db, redis)


# ---------------------------------------------------------------------------
# GET /stats/heatmap — public, cached 60 s
# ---------------------------------------------------------------------------


@stats_router.get(
    "/heatmap",
    status_code=status.HTTP_200_OK,
    summary="Heatmap data",
    description=(
        "Returns a GeoJSON FeatureCollection of report points with a ``weight`` "
        "property for heatmap rendering. Public endpoint, cached 60 seconds."
    ),
)
async def stats_heatmap(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Return cached GeoJSON heatmap feature collection."""
    return await analyst_service.get_heatmap(db, redis)
