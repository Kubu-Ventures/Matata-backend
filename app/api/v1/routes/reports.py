"""Report submission route handlers.

All routes in this module are registered under the ``/api/v1/reports`` prefix.
Route handlers are intentionally thin: they parse multipart/form data, call
``submission_service``, and map service exceptions to HTTP responses.

No database queries, storage calls, or moderation logic appear here — all of
that lives in ``app/services/submission_service.py``.

Background job dispatch (fixed)
--------------------------------
``POST /reports`` and ``PATCH /reports/{id}/photo`` are responsible for
dispatching the GIS/AI Celery jobs. This is done here — in the route layer —
rather than inside ``submission_service`` for a specific reason: it must
happen strictly AFTER ``await db.commit()`` succeeds.

Previously, ``submission_service`` published these jobs itself right after
``await db.flush()`` but before the route's ``await db.commit()``. Because
Celery workers query the report via a completely separate, synchronous DB
connection, and Redis pub/sub delivers almost instantly, workers would
sometimes query the row before the transaction had actually committed —
getting "not found" and giving up, leaving ``building_id`` / ``photo_phash``
unset. Dispatching here, after commit, eliminates that race.

This is also where the duplicate-scoring coordination gate (consumed by
``app/workers/duplicate_dispatch.py``) is seeded: 2 pending steps (GIS + AI)
when the report has a photo, 1 (GIS only) when it doesn't. When a photo is
attached later via the PATCH endpoint, the gate is reseeded to 1 (AI only) so
duplicate scoring re-runs with the newly available image-similarity signal.

OpenAPI documentation
---------------------
Every endpoint is annotated with ``summary``, ``description``, and
``response_model`` so the generated ``/docs`` schema is accurate and
self-describing for frontend and third-party integrators.

Multipart handling
------------------
``POST /reports`` and ``PATCH /reports/{id}/photo`` accept
``multipart/form-data``.  FastAPI's ``UploadFile`` + ``Form`` combination is
used throughout; the ``metadata`` field on ``POST /reports`` is a JSON string
that is parsed in-handler and validated against ``ReportCreateSchema``.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routes.auth import get_current_user
from app.core.dependencies import get_db, get_redis
from app.core.i18n import LocalisedHTTPException, get_locale
from app.schemas.report_submission import (
    NearbyReportItem,
    ReportCreateResponse,
    ReportCreateSchema,
    ReportDetailResponse,
    ReportPhotoResponse,
)
from app.services.queue_service import RedisQueueService
from app.services.submission_service import (
    ModerationRejectionError,
    RateLimitExceededError,
    ReportNotFoundError,
    ReportOwnershipError,
    SubmissionError,
    add_photo_to_report,
    create_report,
    get_nearby_reports,
    get_own_report,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["Reports"])

# ---------------------------------------------------------------------------
# Duplicate-scoring coordination gate — must match
# app/workers/duplicate_dispatch.py's _PENDING_STEPS_KEY_FMT
# ---------------------------------------------------------------------------


def _pending_dup_steps_key(report_id: UUID) -> str:
    return f"crisismap:report:{report_id}:pending_dup_steps"


# ---------------------------------------------------------------------------
# Helper — extract raw token from the authenticated user dict or headers
# ---------------------------------------------------------------------------


def _get_raw_token(
    credentials=None,
    x_session_token: Optional[str] = None,
) -> str:
    """Extract the raw token string from FastAPI request context.

    Tries Authorization Bearer first, then X-Session-Token.  Raises HTTP 401
    if neither is present.

    Args:
        credentials: HTTPAuthorizationCredentials from the Bearer scheme.
        x_session_token: Value of the X-Session-Token header.

    Returns:
        Raw token string.

    Raises:
        HTTPException 401: If no token is present.
    """
    if credentials and hasattr(credentials, "credentials"):
        return credentials.credentials
    if x_session_token:
        return x_session_token
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication credentials were not provided.",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# POST /reports — create a new damage report
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ReportCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a damage report",
    description=(
        "Accepts ``multipart/form-data`` with an optional ``photo`` file and a "
        "required ``metadata`` JSON string.  "
        "Stage 2 content moderation runs synchronously before any write to "
        "object storage — if the image is rejected, HTTP 422 is returned and "
        "nothing is stored.  "
        "GIS and AI jobs are dispatched asynchronously to their respective "
        "workers only after the database transaction has been committed, so "
        "the workers' separate DB connections can always see the new row.  "
        "Requires a valid anonymous or authenticated session token."
    ),
)
async def submit_report(
    metadata: Annotated[
        str,
        Form(
            description=(
                "JSON string conforming to ReportCreateSchema.  Must contain "
                "at least (lat, lng) or landmark_description."
            )
        ),
    ],
    photo: Annotated[
        Optional[UploadFile],
        File(
            description="Photo of the damaged structure (JPEG recommended, max 15 MB)."
        ),
    ] = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    lang: str = Depends(get_locale),
) -> ReportCreateResponse:
    """Create a damage report.

    The ``metadata`` form field must be a JSON string.  The ``photo`` field is
    optional — it may be sent as a separate ``PATCH`` request for offline-sync
    submissions.
    """
    # ── Parse metadata JSON ──────────────────────────────────────────────────
    try:
        parsed = json.loads(metadata)
        meta = ReportCreateSchema.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # ── Read photo bytes ─────────────────────────────────────────────────────
    image_bytes: Optional[bytes] = None
    image_content_type = "image/jpeg"
    if photo is not None:
        image_bytes = await photo.read()
        image_content_type = photo.content_type or "image/jpeg"

    # ── Extract token for rate limiting and ownership ────────────────────────
    reporter_token = current_user.get("sub", "")
    reporter_trust_tier = current_user.get("tier", 0)

    # ── Call service (creates + flushes, does NOT dispatch jobs) ─────────────
    try:
        report = await create_report(
            crisis_type=meta.crisis_type.value,
            infrastructure_type=meta.infrastructure_type.value,
            damage_severity=meta.damage_severity.value,
            lat=meta.lat,
            lng=meta.lng,
            gps_accuracy_m=meta.gps_accuracy_m,
            landmark_description=meta.landmark_description,
            electricity_status=(
                meta.electricity_status.value if meta.electricity_status else None
            ),
            health_services_status=(
                meta.health_services_status.value
                if meta.health_services_status
                else None
            ),
            most_pressing_needs=meta.most_pressing_needs,
            debris_clearing_needed=meta.debris_clearing_needed,
            offline_queued_at=meta.offline_queued_at,
            image_bytes=image_bytes,
            image_content_type=image_content_type,
            reporter_token=reporter_token,
            reporter_trust_tier=reporter_trust_tier,
            db=db,
            redis=redis,
        )
        await db.commit()
    except RateLimitExceededError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message_key="errors.rate_limit_exceeded",
            lang=lang,
            headers={"Retry-After": "3600"},
        ) from exc
    except ModerationRejectionError:
        # Generic message — do not disclose rejection reason (spec §8.2.1).
        raise LocalisedHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            message_key="errors.image_rejected",
            lang=lang,
        )
    except SubmissionError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message_key="errors.internal",
            lang=lang,
        ) from exc

    # ── Dispatch background jobs — ONLY after a successful commit ────────────
    # This ordering is the fix for the "report ... not found — skipping" race
    # previously seen in the celery-gis / celery-ai logs: dispatching before
    # commit let a worker query the row before it was durably visible on its
    # own DB connection.
    queue = RedisQueueService()

    # Seed the duplicate-scoring coordination gate BEFORE publishing jobs, so
    # there's no window where a very-fast worker could finish and decrement
    # the counter before it exists. 2 steps (GIS + AI) if there's a photo,
    # 1 step (GIS only) otherwise. See app/workers/duplicate_dispatch.py.
    await redis.set(
        _pending_dup_steps_key(report.id),
        2 if report.photo_url else 1,
        ex=3600,
    )

    await queue.publish_gis_job(report.id)
    if report.photo_url:
        await queue.publish_ai_job(report.id)

    return ReportCreateResponse(
        id=report.id,
        status=report.status,
        building_id=report.building_id,
    )


# ---------------------------------------------------------------------------
# PATCH /reports/{id}/photo — attach photo to existing report (offline sync)
# ---------------------------------------------------------------------------


@router.patch(
    "/{report_id}/photo",
    response_model=ReportPhotoResponse,
    status_code=status.HTTP_200_OK,
    summary="Upload photo to an existing report",
    description=(
        "Used by the offline sync protocol to upload a photo after the "
        "metadata has already been accepted by ``POST /reports``.  "
        "Stage 2 moderation runs synchronously.  "
        "The AI job is dispatched only after the database transaction has "
        "been committed, and duplicate scoring is re-triggered once it "
        "completes so the newly available image-similarity signal is taken "
        "into account.  "
        "The caller must be the original report submitter."
    ),
)
async def upload_report_photo(
    report_id: UUID,
    photo: Annotated[
        UploadFile,
        File(description="Replacement or initial photo for the report."),
    ],
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    lang: str = Depends(get_locale),
) -> ReportPhotoResponse:
    """Attach a photo to an existing report (offline sync path)."""
    image_bytes = await photo.read()
    image_content_type = photo.content_type or "image/jpeg"
    reporter_token = current_user.get("sub", "")

    try:
        report = await add_photo_to_report(
            report_id=report_id,
            image_bytes=image_bytes,
            image_content_type=image_content_type,
            reporter_token=reporter_token,
            db=db,
            redis=redis,
        )
        await db.commit()
    except ReportNotFoundError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            message_key="errors.report_not_found",
            lang=lang,
        ) from exc
    except ReportOwnershipError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            message_key="errors.report_ownership",
            lang=lang,
        ) from exc
    except ModerationRejectionError:
        raise LocalisedHTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            message_key="errors.image_rejected",
            lang=lang,
        )
    except SubmissionError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message_key="errors.internal",
            lang=lang,
        ) from exc

    # ── Dispatch background job — ONLY after a successful commit ─────────────
    queue = RedisQueueService()

    # Only the AI step is pending now (GIS already ran when the report was
    # first created). Reseed to 1 so mark_step_done_and_maybe_dispatch fires
    # duplicate scoring again once this AI run completes — this time with a
    # photo_phash available, which the original GIS-only scoring pass did not
    # have.
    await redis.set(_pending_dup_steps_key(report.id), 1, ex=3600)

    await queue.publish_ai_job(report.id)

    return ReportPhotoResponse(
        id=report.id,
        photo_url=report.photo_url or "",
        status=(
            report.photo_status.value
            if hasattr(report.photo_status, "value")
            else str(report.photo_status)
        ),
    )


# ---------------------------------------------------------------------------
# GET /reports/nearby — pre-submission duplicate check
# IMPORTANT: must be registered BEFORE /{report_id} so that the static path
# segment "nearby" is not swallowed by the UUID path parameter.
# ---------------------------------------------------------------------------


@router.get(
    "/nearby",
    response_model=list[NearbyReportItem],
    status_code=status.HTTP_200_OK,
    summary="Get nearby reports for duplicate check",
    description=(
        "Returns reports within *radius_m* metres of the given coordinates, "
        "ordered by GPS-proximity similarity score.  No authentication required.  "
        "Results are cached per coordinate cell (4 decimal places) for 30 seconds."
    ),
)
async def nearby_reports(
    lat: float = Query(..., ge=-90.0, le=90.0, description="WGS84 latitude."),
    lng: float = Query(..., ge=-180.0, le=180.0, description="WGS84 longitude."),
    radius_m: float = Query(
        default=30.0,
        ge=1.0,
        le=100.0,
        description="Search radius in metres (default 30, max 100).",
    ),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> list[NearbyReportItem]:
    """Return nearby reports for the frontend pre-submission duplicate check."""
    raw_results = await get_nearby_reports(
        lat=lat,
        lng=lng,
        radius_m=radius_m,
        db=db,
        redis=redis,
    )
    return [NearbyReportItem(**item) for item in raw_results]


# ---------------------------------------------------------------------------
# GET /reports/{id} — reporter's own submission
# IMPORTANT: must be registered AFTER /nearby (see note above).
# ---------------------------------------------------------------------------


@router.get(
    "/{report_id}",
    response_model=ReportDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get own report",
    description=(
        "Returns the full detail of a reporter's own submission.  "
        "HTTP 403 is returned if the report belongs to a different session token."
    ),
)
async def get_report(
    report_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    lang: str = Depends(get_locale),
) -> ReportDetailResponse:
    """Return a reporter's own report."""
    reporter_token = current_user.get("sub", "")
    try:
        report = await get_own_report(
            report_id=report_id,
            reporter_token=reporter_token,
            db=db,
        )
    except ReportNotFoundError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            message_key="errors.report_not_found",
            lang=lang,
        ) from exc
    except ReportOwnershipError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            message_key="errors.report_ownership",
            lang=lang,
        ) from exc

    return ReportDetailResponse.model_validate(report)
