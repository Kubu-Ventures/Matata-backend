"""Report submission service.

This module is the **single orchestrator** for the complete report submission
flow.  Route handlers in ``app/api/v1/routes/reports.py`` call this service
and must not contain any database operations, storage calls, or moderation
logic directly.

Responsibilities
----------------
* Input sanitisation (HTML stripping and field truncation).
* Rate limit enforcement (per-token, via Redis).
* Stage 2 safety moderation (synchronous — check before store).
* Object storage upload (only on moderation pass).
* Database record creation.
* Audit log write for moderation rejections.
* Photo upload for the offline sync path (``PATCH /reports/{id}/photo``).

Background job dispatch — moved to the route layer (fixed)
------------------------------------------------------------
Earlier versions of this module published the GIS and AI Celery jobs
directly from ``create_report`` / ``add_photo_to_report``, immediately after
``await db.flush()`` but BEFORE the caller's ``await db.commit()``. Because
Celery workers use a completely separate, synchronous database connection,
this created a race: Redis pub/sub is fast enough that a worker could query
the report row before the FastAPI request's transaction had actually
committed, get "not found", and silently give up (visible in logs as
"report ... not found — skipping"), leaving ``building_id`` / ``photo_phash``
unset for that report.

Job dispatch is therefore now the **caller's** responsibility, to be done
strictly *after* ``await db.commit()`` succeeds. See
``app/api/v1/routes/reports.py`` — ``submit_report`` and
``upload_report_photo`` — for the dispatch code, including seeding the
duplicate-scoring "pending steps" gate consumed by
``app/workers/duplicate_dispatch.py``.

Separation of concerns
-----------------------
``submission_service`` owns the *orchestration* logic up to and including
the committable database write. It delegates to:
* ``moderation_service`` — content safety evaluation.
* ``storage_service``    — binary upload to S3/MinIO.

The service layer never imports from ``app/api``. Route handlers never import
from ``sqlalchemy`` or ``redis`` directly — they import from this module.

Privacy
-------
The reporter's hashed token is stored as ``reporter_token_hash``; the raw token
is never written to any database column or log statement.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Optional, cast  # cast added here
from uuid import UUID

import bleach
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.enums import PhotoStatus, ReportStatus
from app.models.report import Report
from app.services.image_service import compress_image
from app.services.moderation_service import ModerationProvider, get_moderation_provider
from app.services.storage_service import StorageService, get_storage_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiting constants
# ---------------------------------------------------------------------------

# Spec §5.4: maximum 10 submissions per token per hour.
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW_SECONDS = 3600  # 1 hour

_NS = "crisismap:submission"

# Tags whose entire content (not just the tag) should be discarded.
_DANGEROUS_TAG_RE = re.compile(
    r"<(script|style|iframe|object|embed|form)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def _rate_limit_key(token_hash: str) -> str:
    """Redis key for the per-token submission rate limit counter."""
    return f"{_NS}:rate:{token_hash}"


# ---------------------------------------------------------------------------
# Input sanitisation constants
# ---------------------------------------------------------------------------

# Field max lengths as specified in §13.3 and §6.2.
_MAX_LANDMARK = 500
_MAX_PRESSING_NEEDS = 1000

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class SubmissionError(Exception):
    """Base class for submission-layer errors."""


class RateLimitExceededError(SubmissionError):
    """The per-token submission rate limit has been exceeded."""


class ModerationRejectionError(SubmissionError):
    """The submitted image was rejected by the content moderation gate.

    The rejection reason is intentionally not exposed to callers so that the
    error message cannot be used to iteratively bypass moderation.
    """


class ReportNotFoundError(SubmissionError):
    """The requested Report record does not exist."""


class ReportOwnershipError(SubmissionError):
    """The caller does not own the requested Report record."""


# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------


def _sanitise_text(value: Optional[str], max_length: int) -> Optional[str]:
    """Strip HTML/script tags AND their content from *value*, then truncate."""
    if value is None:
        return None
    # First pass: remove dangerous tags *and* their inner content entirely.
    without_dangerous = _DANGEROUS_TAG_RE.sub("", value)
    # Second pass: strip any remaining HTML tags (safe tags, stray markup).
    cleaned = bleach.clean(without_dangerous, tags=[], strip=True)
    return cleaned[:max_length]


def _hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of *token*.

    Used to derive ``reporter_token_hash`` — the raw JWT/session token is
    never stored in the database.

    Args:
        token: Raw bearer or session token string.

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Perceptual hash
# ---------------------------------------------------------------------------
#
# The submission path deliberately does NOT compute ``photo_phash``.  The AI
# worker (``ai_tasks.process_report_image``) is the single writer of that
# column, hashing the compressed image that is canonically stored in object
# storage with the one shared algorithm in ``image_service.compute_phash``.
# The duplicate-scoring gate guarantees the AI step finishes before
# ``score_report`` runs, so the hash is always present when it is needed.
# See audit finding H-2 for why two writers / two algorithms was a bug.


# ---------------------------------------------------------------------------
# Rate limit helper
# ---------------------------------------------------------------------------


async def _check_rate_limit(token_hash: str, redis: Redis) -> None:
    """Increment the submission counter and raise if the limit is exceeded.

    Uses a Redis INCR + EXPIRE pattern.  The key expires automatically after
    the window so counters do not need manual cleanup.

    Args:
        token_hash: SHA-256 hash of the reporter's session/JWT token.
        redis:      Async Redis client.

    Raises:
        RateLimitExceededError: If the reporter has exceeded 10 submissions/hour.
    """
    key = _rate_limit_key(token_hash)
    count = await redis.incr(key)
    if count == 1:
        # First submission in this window — set the expiry.
        await redis.expire(key, _RATE_LIMIT_WINDOW_SECONDS)

    if count > _RATE_LIMIT_MAX:
        logger.warning(
            "Rate limit exceeded for token hash %s… (count=%d)",
            token_hash[:8],
            count,
        )
        raise RateLimitExceededError(
            f"Maximum {_RATE_LIMIT_MAX} submissions per hour exceeded."
        )


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------


async def _write_audit_log(
    db: AsyncSession,
    operation: str,
    actor_id_hash: str,
    record_id: UUID,
    before_state: Optional[dict],
    after_state: dict,
) -> None:
    """Insert an AuditLog row (fire-and-forget within the current transaction).

    Args:
        db:            Active async database session.
        operation:     Short operation name, e.g. ``"report.moderation_rejection"``.
        actor_id_hash: Anonymised identifier of the actor.
        record_id:     UUID of the affected domain record.
        before_state:  State before the operation (``None`` for inserts).
        after_state:   State after the operation.
    """
    entry = AuditLog(
        operation=operation,
        actor_id_hash=actor_id_hash,
        record_id=record_id,
        before_state=before_state,
        after_state=after_state,
    )
    db.add(entry)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_report(
    *,
    # Reporter metadata fields
    crisis_type: str,
    infrastructure_type: str,
    damage_severity: str,
    lat: Optional[float],
    lng: Optional[float],
    gps_accuracy_m: Optional[float],
    landmark_description: Optional[str],
    electricity_status: Optional[str],
    health_services_status: Optional[str],
    most_pressing_needs: Optional[str],
    debris_clearing_needed: Optional[bool],
    offline_queued_at,
    # Photo
    image_bytes: Optional[bytes],
    image_content_type: str = "image/jpeg",
    # Auth
    reporter_token: str,
    reporter_trust_tier: int = 0,
    # Infrastructure
    db: AsyncSession,
    redis: Redis,
    moderation_provider: Optional[ModerationProvider] = None,
    storage_service: Optional[StorageService] = None,
) -> Report:
    """Create a new damage report — the primary submission endpoint handler.

    Orchestration order:
    1. Sanitise all text inputs.
    2. Hash the reporter token.
    3. Enforce per-token rate limit.
    4. If a photo is present: run Stage 2 moderation synchronously.
       - On rejection: write audit log entry and raise ``ModerationRejectionError``.
       - On pass: upload to object storage.
    5. Create the ``Report`` database record.
    6. Write audit log entry for the creation event.
    7. Flush so server-generated timestamps are available to the caller.

    IMPORTANT: this function deliberately does **not** publish the GIS/AI
    background jobs. Dispatch must happen in the caller, strictly after
    ``await db.commit()`` succeeds — see the module docstring and
    ``app/api/v1/routes/reports.py`` for why (commit-race fix) and how
    (including seeding the duplicate-scoring coordination gate).

    Args:
        crisis_type:           Enum value string.
        infrastructure_type:   Enum value string.
        damage_severity:       Enum value string.
        lat:                   Decimal degrees WGS84 latitude.
        lng:                   Decimal degrees WGS84 longitude.
        gps_accuracy_m:        Device-reported horizontal accuracy in metres.
        landmark_description:  Free-text landmark (required when lat/lng absent).
        electricity_status:    Optional enum value string.
        health_services_status: Optional enum value string.
        most_pressing_needs:   Optional free text.
        debris_clearing_needed: Optional boolean.
        offline_queued_at:     Timestamp from offline queue (None for live submissions).
        image_bytes:           Raw photo binary (None for metadata-only offline path).
        image_content_type:    MIME type of the photo.
        reporter_token:        Raw JWT/session token — hashed on receipt, never stored.
        reporter_trust_tier:   Tier from JWT payload (0 for anonymous).
        db:                    Async database session.
        redis:                 Async Redis client.
        moderation_provider:   Injected for testing; defaults to factory instance.
        storage_service:       Injected for testing; defaults to factory instance.

    Returns:
        The created ``Report`` ORM instance (not yet committed — caller commits
        and then dispatches background jobs).

    Raises:
        RateLimitExceededError:   Reporter has exceeded 10 submissions/hour.
        ModerationRejectionError: Image failed Stage 2 content moderation.
        SubmissionError:          Other submission-layer failure.
    """
    # ── 1. Sanitise text fields ──────────────────────────────────────────────
    landmark_description = _sanitise_text(landmark_description, _MAX_LANDMARK)
    most_pressing_needs = _sanitise_text(most_pressing_needs, _MAX_PRESSING_NEEDS)

    # ── 2. Hash reporter token ───────────────────────────────────────────────
    token_hash = _hash_token(reporter_token)

    # ── 3. Rate limit ────────────────────────────────────────────────────────
    await _check_rate_limit(token_hash, redis)

    # Resolve injectable dependencies (factory defaults used in production).
    _moderation = moderation_provider or get_moderation_provider()
    _storage = storage_service or get_storage_service()

    photo_url: Optional[str] = None
    photo_status = PhotoStatus.pending

    # ── 4. Stage 2 moderation + storage ─────────────────────────────────────
    if image_bytes:
        # CRITICAL: check before store — spec §8.2 principle.
        mod_result = await _moderation.moderate(image_bytes)

        if not mod_result.passed:
            # Write a detailed audit log entry (internal only — never exposed
            # to the reporter, as specified in §8.2.1).
            import uuid as _uuid

            rejection_record_id = _uuid.uuid4()
            await _write_audit_log(
                db=db,
                operation="report.moderation_rejection",
                actor_id_hash=token_hash,
                record_id=rejection_record_id,
                before_state=None,
                after_state={
                    "reason": "moderation_rejection",
                    "categories": mod_result.categories,
                },
            )
            await db.flush()  # Persist audit log within the current transaction.

            logger.warning("Stage 2 moderation rejection (actor: %s…)", token_hash[:8])
            # Generic message — do NOT disclose which category triggered rejection.
            raise ModerationRejectionError("Image could not be accepted")

        # Moderation passed — compress before writing to storage.
        # Moderation ran on the original bytes for maximum fidelity; the
        # perceptual hash is computed later by the AI worker on the stored
        # (compressed) image.
        image_bytes, image_content_type = compress_image(image_bytes)

        # We need a report ID for the object key, so generate one now.
        import uuid as _uuid

        report_id = _uuid.uuid4()

        try:
            photo_url = await _storage.upload_image(
                report_id=str(report_id),
                image_bytes=image_bytes,
                content_type=image_content_type,
            )
            photo_status = PhotoStatus.processing
        except Exception as exc:
            logger.error(
                "Storage upload failed for report %s: %s",
                report_id,
                type(exc).__name__,
            )
            # Re-raise as SubmissionError to keep infrastructure details
            # out of the HTTP response.
            raise SubmissionError("Photo upload failed. Please try again.") from exc
    else:
        # Metadata-only path (offline sync: metadata POST, then photo PATCH).
        import uuid as _uuid

        report_id = _uuid.uuid4()

    # ── 5. Create Report record ──────────────────────────────────────────────
    report = Report(
        id=report_id,
        crisis_type=crisis_type,
        infrastructure_type=infrastructure_type,
        damage_severity=damage_severity,
        lat=lat,
        lng=lng,
        gps_accuracy_m=gps_accuracy_m,
        landmark_description=landmark_description,
        electricity_status=electricity_status,
        health_services_status=health_services_status,
        most_pressing_needs=most_pressing_needs,
        debris_clearing_needed=debris_clearing_needed,
        photo_url=photo_url,
        photo_status=photo_status,
        status=ReportStatus.pending,
        reporter_token_hash=token_hash,
        reporter_trust_tier=reporter_trust_tier,
        offline_queued_at=offline_queued_at,
    )
    db.add(report)

    # ── 6. Audit log — creation event ────────────────────────────────────────
    await _write_audit_log(
        db=db,
        operation="report.create",
        actor_id_hash=token_hash,
        record_id=report_id,
        before_state=None,
        after_state={
            "status": ReportStatus.pending.value,
            "photo_status": photo_status.value,
            "crisis_type": crisis_type,
            "infrastructure_type": infrastructure_type,
            "damage_severity": damage_severity,
        },
    )

    await db.flush()  # Obtain server-generated timestamps before the caller commits.

    # NOTE: GIS/AI job dispatch intentionally happens in the route handler,
    # after `await db.commit()` — see module docstring. Dispatching here
    # (before commit) was the cause of the "report ... not found — skipping"
    # race previously seen in celery-gis/celery-ai logs.

    logger.info(
        "Report %s created (actor: %s…, photo: %s) — awaiting commit + job dispatch",
        report_id,
        token_hash[:8],
        "yes" if photo_url else "no",
    )
    return report


async def add_photo_to_report(
    *,
    report_id: UUID,
    image_bytes: bytes,
    image_content_type: str = "image/jpeg",
    reporter_token: str,
    db: AsyncSession,
    redis: Redis,
    moderation_provider: Optional[ModerationProvider] = None,
    storage_service: Optional[StorageService] = None,
) -> Report:
    """Attach a photo to an existing report (offline sync path).

    This implements the ``PATCH /reports/{id}/photo`` endpoint — used when
    the offline sync protocol uploads metadata and photo as two separate
    requests.

    Orchestration order:
    1. Load the Report, verify it belongs to the requesting token.
    2. Run Stage 2 moderation synchronously.
    3. Upload to storage on pass.
    4. Update ``photo_url`` and ``photo_status`` (``photo_phash`` is written
       later by the AI worker, the single writer of that column).
    5. Write audit log.

    IMPORTANT: this function deliberately does **not** publish the AI
    background job. Dispatch must happen in the caller, strictly after
    ``await db.commit()`` succeeds — see the module docstring and
    ``app/api/v1/routes/reports.py`` for why and how (including reseeding the
    duplicate-scoring coordination gate to 1, so scoring re-runs with the
    newly available image-similarity signal).

    Args:
        report_id:          UUID of the existing Report to update.
        image_bytes:        Raw photo binary.
        image_content_type: MIME type.
        reporter_token:     Raw token of the requesting reporter.
        db:                 Async database session.
        redis:              Async Redis client.
        moderation_provider: Injected for testing.
        storage_service:    Injected for testing.

    Returns:
        The updated ``Report`` ORM instance.

    Raises:
        ReportNotFoundError:      Report does not exist.
        ReportOwnershipError:     Report belongs to a different token.
        ModerationRejectionError: Image failed Stage 2 moderation.
        SubmissionError:          Other failure.
    """
    token_hash = _hash_token(reporter_token)

    # ── 1. Load and verify ownership ─────────────────────────────────────────
    result = await db.execute(sa.select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()

    if report is None:
        raise ReportNotFoundError(f"Report {report_id} not found.")

    if report.reporter_token_hash != token_hash:
        logger.warning(
            "Photo upload ownership violation — report %s, actor %s…",
            report_id,
            token_hash[:8],
        )
        raise ReportOwnershipError("You do not have permission to update this report.")

    _moderation = moderation_provider or get_moderation_provider()
    _storage = storage_service or get_storage_service()

    # ── 2. Stage 2 moderation ───────────────────────────────────────────────
    # ``photo_phash`` is not written here — the AI job the caller dispatches
    # after commit is the single writer, hashing the stored compressed image.
    mod_result = await _moderation.moderate(image_bytes)

    if not mod_result.passed:
        await _write_audit_log(
            db=db,
            operation="report.photo_moderation_rejection",
            actor_id_hash=token_hash,
            record_id=report_id,
            before_state=None,
            after_state={
                "reason": "moderation_rejection",
                "categories": mod_result.categories,
            },
        )
        await db.flush()
        raise ModerationRejectionError("Image could not be accepted")

    # ── 3. Compress then upload ───────────────────────────────────────────────
    # Moderation ran on the original bytes for maximum fidelity.
    image_bytes, image_content_type = compress_image(image_bytes)

    try:
        photo_url = await _storage.upload_image(
            report_id=str(report_id),
            image_bytes=image_bytes,
            content_type=image_content_type,
        )
    except Exception as exc:
        raise SubmissionError("Photo upload failed. Please try again.") from exc

    # ── 4. Update record ─────────────────────────────────────────────────────
    report.photo_url = photo_url
    report.photo_status = PhotoStatus.processing

    # ── 5. Audit log ─────────────────────────────────────────────────────────
    await _write_audit_log(
        db=db,
        operation="report.photo_added",
        actor_id_hash=token_hash,
        record_id=report_id,
        before_state={
            "photo_url": None,
            "photo_status": PhotoStatus.pending.value,
        },
        after_state={
            "photo_url": photo_url,
            "photo_status": PhotoStatus.processing.value,
        },
    )

    await db.flush()

    # NOTE: AI job dispatch intentionally happens in the route handler, after
    # `await db.commit()` — see module docstring.

    logger.info(
        "Photo added to report %s (actor: %s…) — awaiting commit + job dispatch",
        report_id,
        token_hash[:8],
    )
    return report


async def list_own_reports(
    *,
    reporter_token: str,
    page: int = 1,
    limit: int = 20,
    db: AsyncSession,
) -> tuple[list[Report], int]:
    """Return a paginated list of reports submitted under *reporter_token*.

    Filters by ``reporter_token_hash``, the same hash used to enforce
    ownership on ``GET /reports/{id}``. Works for both anonymous sessions
    (scoped to that session's ephemeral identity) and phone-verified
    reporters (scoped to their persistent hashed phone identity).

    Args:
        reporter_token: Raw JWT ``sub`` claim of the requesting caller.
        page:           1-based page number.
        limit:          Items per page (max enforced by the caller/route).
        db:             Async database session.

    Returns:
        Tuple of (reports for this page, total matching count).
    """
    token_hash = _hash_token(reporter_token)

    count_result = await db.execute(
        sa.select(sa.func.count())
        .select_from(Report)
        .where(Report.reporter_token_hash == token_hash)
    )
    total = count_result.scalar_one()

    offset = (page - 1) * limit
    result = await db.execute(
        sa.select(Report)
        .where(Report.reporter_token_hash == token_hash)
        .order_by(Report.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    reports = result.scalars().all()

    return list(reports), total


async def get_own_report(
    *,
    report_id: UUID,
    reporter_token: str,
    db: AsyncSession,
) -> Report:
    """Return a reporter's own report, enforcing ownership.

    Args:
        report_id:      UUID of the report.
        reporter_token: Raw token of the requesting reporter.
        db:             Async database session.

    Returns:
        The ``Report`` ORM instance.

    Raises:
        ReportNotFoundError:  Report does not exist.
        ReportOwnershipError: Report belongs to a different token.
    """
    token_hash = _hash_token(reporter_token)
    result = await db.execute(sa.select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()

    if report is None:
        raise ReportNotFoundError(f"Report {report_id} not found.")

    if report.reporter_token_hash != token_hash:
        raise ReportOwnershipError("You do not have permission to view this report.")

    return report


async def get_nearby_reports(
    *,
    lat: float,
    lng: float,
    radius_m: float,
    db: AsyncSession,
    redis: Redis,
) -> list[dict]:
    """Return nearby reports with GPS-proximity similarity scores.

    Used by the frontend pre-submission duplicate check (spec §10.3).
    Results are cached per coordinate cell (rounded to 4 decimal places) for
    30 seconds to reduce database load during active events.

    Args:
        lat:       WGS84 latitude of the query point.
        lng:       WGS84 longitude of the query point.
        radius_m:  Search radius in metres (max 100, default 30).
        db:        Async database session.
        redis:     Async Redis client.

    Returns:
        List of dicts with keys: ``id``, ``lat``, ``lng``, ``status``,
        ``damage_severity``, ``created_at``, ``similarity_score``.
    """
    import json
    import math

    # Clamp radius to spec maximum.
    radius_m = min(radius_m, 100.0)

    # Round coordinates to 4 decimal places (~11 m precision) for cache key.
    cell_lat = round(lat, 4)
    cell_lng = round(lng, 4)
    cache_key = f"crisismap:nearby:{cell_lat}:{cell_lng}:{int(radius_m)}"

    cached = await redis.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            pass  # Fall through to database query on corrupt cache.

    # Approximate degree delta for the search radius.
    # 1 degree latitude ≈ 111,320 m; longitude varies by cos(lat).
    lat_delta = radius_m / 111_320.0
    lng_delta = radius_m / (111_320.0 * math.cos(math.radians(lat)))

    result = await db.execute(
        sa.select(Report)
        .where(
            Report.lat.between(lat - lat_delta, lat + lat_delta),
            Report.lng.between(lng - lng_delta, lng + lng_delta),
            Report.status.in_([ReportStatus.pending.value, "verified"]),
        )
        .order_by(Report.created_at.desc())
        .limit(50)
    )
    reports = result.scalars().all()

    def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Return the great-circle distance between two WGS84 points in metres."""
        R = 6_371_000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lng2 - lng1)
        a = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        )
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    output = []
    for rpt in reports:
        # rpt.lat/lng are Optional[float] on the model, but the BETWEEN
        # filter above excludes NULL rows at the SQL level (BETWEEN against
        # NULL evaluates to NULL, which WHERE treats as false) — this can
        # never be None here.
        assert rpt.lat is not None and rpt.lng is not None
        dist = _haversine_m(lat, lng, rpt.lat, rpt.lng)
        if dist > radius_m:
            continue  # Bounding-box overshoot — exclude.
        # Similarity score: 1.0 at distance 0, 0.0 at 50 m (spec §10.1).
        score = max(0.0, 1.0 - dist / 50.0) * 0.30  # GPS proximity weight
        output.append(
            {
                "id": str(rpt.id),
                "lat": rpt.lat,
                "lng": rpt.lng,
                "status": (
                    rpt.status.value if hasattr(rpt.status, "value") else rpt.status
                ),
                "damage_severity": (
                    rpt.damage_severity.value
                    if hasattr(rpt.damage_severity, "value")
                    else rpt.damage_severity
                ),
                "created_at": cast(datetime, rpt.created_at).isoformat(),
                "similarity_score": round(score, 4),
            }
        )

    # Cache for 30 seconds.
    await redis.set(cache_key, json.dumps(output), ex=30)

    return output
