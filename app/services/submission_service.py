"""Report submission service.

This module is the **single orchestrator** for the complete report submission
flow.  Route handlers in ``app/api/v1/routes/reports.py`` call this service
and must not contain any database operations, storage calls, queue publishes,
or moderation logic directly.

Responsibilities
----------------
* Input sanitisation (HTML stripping and field truncation).
* Rate limit enforcement (per-token, via Redis).
* Stage 2 safety moderation (synchronous — check before store).
* Perceptual hash (pHash) computation for duplicate detection.
* Object storage upload (only on moderation pass).
* Database record creation.
* Audit log write for moderation rejections.
* Job dispatch to GIS and AI queues.
* Photo upload for the offline sync path (``PATCH /reports/{id}/photo``).

Separation of concerns
-----------------------
``submission_service`` owns the *orchestration* logic.  It delegates to:
* ``moderation_service`` — content safety evaluation.
* ``storage_service``    — binary upload to S3/MinIO.
* ``queue_service``      — Redis Stream job publishing.

The service layer never imports from ``app/api``.  Route handlers never import
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
from app.services.moderation_service import ModerationProvider, get_moderation_provider
from app.services.queue_service import QueueService, RedisQueueService
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


def _compute_phash(image_bytes: bytes) -> Optional[str]:
    """Compute a 64-bit perceptual hash (pHash) of *image_bytes*.

    The pHash is used by the duplicate detection system (§10) to identify
    visually similar images by comparing Hamming distances.

    This implementation requires ``Pillow``.  If Pillow is unavailable (e.g.
    in a minimal CI environment), the function returns ``None`` and duplicate
    detection falls back to GPS-only scoring.

    Args:
        image_bytes: Raw JPEG/PNG binary.

    Returns:
        64-character hex string encoding the 64-bit pHash, or ``None``.
    """
    try:
        from io import BytesIO

        from PIL import Image  # type: ignore[import]
    except ImportError:
        logger.warning("Pillow not installed — pHash computation skipped")
        return None

    try:
        with Image.open(BytesIO(image_bytes)) as _src:
            # DCT-based pHash: resize to 32×32, convert to greyscale, then
            # apply a simplified DCT by averaging 8×8 blocks from the 32×32
            # grid.  This is a fast, dependency-free approximation sufficient
            # for Hamming-distance duplicate detection.
            #
            # FIX: open into _src (ImageFile), then assign the converted result
            # to a new variable typed as Image.Image to avoid the
            # "Incompatible types in assignment" error (ImageFile vs Image).
            #
            # FIX: Image.LANCZOS moved to Image.Resampling.LANCZOS in Pillow
            # 10.  getattr fallback keeps compatibility with Pillow 9.x.
            _resample = getattr(Image, "Resampling", Image).LANCZOS
            img: Image.Image = _src.convert("L").resize((32, 32), _resample)
            pixels = list(img.getdata())

        # Compute 8×8 block averages (64 values total).
        block_avgs = []
        for block_row in range(8):
            for block_col in range(8):
                total = 0
                for r in range(4):
                    for c in range(4):
                        idx = (block_row * 4 + r) * 32 + (block_col * 4 + c)
                        total += pixels[idx]
                block_avgs.append(total / 16.0)

        mean_val = sum(block_avgs) / len(block_avgs)
        bits = [1 if avg >= mean_val else 0 for avg in block_avgs]

        # Pack 64 bits into 8 bytes and format as a 16-char hex string.
        byte_vals = []
        for i in range(0, 64, 8):
            byte_val = sum(bits[i + j] << (7 - j) for j in range(8))
            byte_vals.append(byte_val)
        return "".join(f"{b:02x}" for b in byte_vals)

    except Exception as exc:  # noqa: BLE001
        logger.warning("pHash computation failed: %s", type(exc).__name__)
        return None


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
    queue_service: Optional[QueueService] = None,
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
    7. Dispatch GIS and AI queue jobs.

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
        queue_service:         Injected for testing; defaults to factory instance.

    Returns:
        The created ``Report`` ORM instance (not yet committed — caller commits).

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
    _queue: QueueService = queue_service or RedisQueueService(redis)

    photo_url: Optional[str] = None
    photo_status = PhotoStatus.pending
    photo_phash: Optional[str] = None

    # ── 4. Stage 2 moderation + storage ─────────────────────────────────────
    if image_bytes:
        # Compute pHash before moderation so we have it regardless of outcome.
        photo_phash = _compute_phash(image_bytes)

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

        # Moderation passed — safe to write to storage.
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
        photo_phash=photo_phash,
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

    await db.flush()  # Obtain the server-generated timestamps before dispatch.

    # ── 7. Dispatch queue jobs ───────────────────────────────────────────────
    # Both publishes are fire-and-forget; errors are logged, not re-raised,
    # so a Redis blip does not abort an otherwise successful submission.
    await _queue.publish_gis_job(report_id)
    if photo_url:
        await _queue.publish_ai_job(report_id)

    logger.info(
        "Report %s created (actor: %s…, photo: %s)",
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
    queue_service: Optional[QueueService] = None,
) -> Report:
    """Attach a photo to an existing report (offline sync path).

    This implements the ``PATCH /reports/{id}/photo`` endpoint — used when
    the offline sync protocol uploads metadata and photo as two separate
    requests.

    Orchestration order:
    1. Load the Report, verify it belongs to the requesting token.
    2. Run Stage 2 moderation synchronously.
    3. Upload to storage on pass.
    4. Update ``photo_url``, ``photo_phash``, and ``photo_status``.
    5. Write audit log.
    6. Dispatch AI queue job.

    Args:
        report_id:          UUID of the existing Report to update.
        image_bytes:        Raw photo binary.
        image_content_type: MIME type.
        reporter_token:     Raw token of the requesting reporter.
        db:                 Async database session.
        redis:              Async Redis client.
        moderation_provider: Injected for testing.
        storage_service:    Injected for testing.
        queue_service:      Injected for testing.

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
    _queue: QueueService = queue_service or RedisQueueService(redis)

    # ── 2. pHash + Stage 2 moderation ────────────────────────────────────────
    photo_phash = _compute_phash(image_bytes)
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

    # ── 3. Upload ─────────────────────────────────────────────────────────────
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
    report.photo_phash = photo_phash
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

    # ── 6. AI queue job ───────────────────────────────────────────────────────
    await _queue.publish_ai_job(report_id)

    logger.info("Photo added to report %s (actor: %s…)", report_id, token_hash[:8])
    return report


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
