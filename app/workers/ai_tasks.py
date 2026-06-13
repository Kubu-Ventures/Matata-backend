"""AI Celery task definitions — Stage 3 image processing (spec §8.3).

This module implements the ``process_report_image`` Celery task, consumed by
the ``celery-ai`` worker from the ``ai`` queue.

The task is non-blocking from the reporter's perspective: it is enqueued by
the submission service after a photo has been safely stored in object storage,
and its results are written back to the ``report`` record for display on the
analyst dashboard.

Stage 3.1 — Image quality assessment
--------------------------------------
Downloads the stored image, calls the configured VisionProvider with a
structured prompt, and writes ``ai_quality_score`` and ``photo_status``.
If the image is unusable, ``photo_status`` is set to ``insufficient_quality``
and a notification job is dispatched to request a replacement photo.

Stage 3.2 — Damage classification
--------------------------------------
Batched into the same VisionProvider call (no double API cost).
Writes ``ai_severity_prediction`` and ``ai_confidence``.  The AI prediction
NEVER modifies ``report.damage_severity``.

Divergence flag
---------------
If ``ai_severity_prediction != damage_severity`` AND ``ai_confidence > 0.7``,
``ai_divergence`` is set to ``True`` to surface the report for analyst review.

Perceptual hash
---------------
A pHash is computed from the downloaded image bytes and stored in
``report.photo_phash`` (after confirming the image is usable or borderline).

Error handling
--------------
On VisionAPIError: exponential backoff retries (30 s → 2 min → 10 min).
After all retries: ``photo_status = "ai_processing_failed"``.
Errors are NEVER surfaced to reporters.

All database access is synchronous (Celery runs in threads, not an async loop).
"""

from __future__ import annotations

import asyncio
import json
import logging
from io import BytesIO
from typing import Optional
from uuid import UUID

import requests
from celery import Task
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.services.vision_service import (
    ImageAnalysisResult,
    VisionAPIError,
    VisionProvider,
    get_vision_provider,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Divergence threshold — Redis-backed, calibrated by active learning
# ---------------------------------------------------------------------------

# Redis key written by analyst_service.get_ai_accuracy().
# Must match analyst_service.AI_DIVERGENCE_THRESHOLD_KEY.
_AI_DIVERGENCE_THRESHOLD_KEY = "crisismap:ai:divergence_threshold"

# Redis Pub/Sub channel consumed by the analyst SSE stream.
# Must match analyst_service.ANALYST_EVENTS_CHANNEL.
_ANALYST_EVENTS_CHANNEL = "crisismap:analyst_events"


def _get_divergence_threshold() -> float:
    """Return the active divergence confidence threshold.

    Reads the calibrated value written by ``get_ai_accuracy()`` from Redis.
    Falls back to ``settings.AI_DIVERGENCE_THRESHOLD_DEFAULT`` (0.70) when:
    - No analyst feedback has been collected yet (key absent).
    - Redis is temporarily unavailable.

    A fresh synchronous connection is opened and closed per call so this
    function is safe under both forked and threaded Celery worker models.
    The read is cheap (single GET) and performed once per task invocation.
    """
    try:
        import redis as _redis_sync

        r = _redis_sync.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            value = r.get(_AI_DIVERGENCE_THRESHOLD_KEY)
        finally:
            r.close()
        if value is not None:
            threshold = float(value)
            logger.debug("Divergence threshold from Redis: %.2f", threshold)
            return threshold
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Could not read divergence threshold from Redis (%s) — using default",
            type(exc).__name__,
        )
    return settings.AI_DIVERGENCE_THRESHOLD_DEFAULT


# ---------------------------------------------------------------------------
# Analyst SSE event publisher
# ---------------------------------------------------------------------------


def _publish_analyst_event(payload: dict) -> None:
    """Publish *payload* to the analyst SSE Redis channel.

    Creates a fresh synchronous Redis connection per call so this function is
    safe under both forked and threaded Celery worker models.  Failures are
    logged as warnings and never propagate — the DB write has already committed
    so the report is consistent even if the SSE push is dropped.

    Args:
        payload: Dict that will be JSON-serialised and published.  Must include
                 an ``"event"`` key so the SSE handler emits the correct event
                 type to connected dashboard clients.
    """
    try:
        import redis as _redis_sync  # local import keeps startup lightweight

        r = _redis_sync.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            r.publish(_ANALYST_EVENTS_CHANNEL, json.dumps(payload))
        finally:
            r.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not publish analyst event %r for report %s: %s",
            payload.get("event"),
            payload.get("report_id"),
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Confidence-based analyst routing
# ---------------------------------------------------------------------------


def _compute_review_priority(
    *,
    ai_confidence: Optional[float],
    ai_quality_score: Optional[float],
    ai_divergence: bool,
) -> str:
    """Map AI output metrics to an analyst queue priority string.

    Priority rules (evaluated top-to-bottom; first match wins):

    ``critical``
        - Image quality below the unusable threshold (score < 0.30): any
          severity prediction derived from this image is unreliable noise.
        - AI confidence below 0.60: the model is insufficiently certain to
          support an operational decision without a human second opinion.
          Threshold follows responsible-AI guidance for humanitarian
          decision-support systems.

    ``high``
        - AI disagrees with the reporter's own classification (divergence).
          One party is wrong; an analyst must adjudicate.
        - AI confidence in the 0.60–0.79 band: model is uncertain enough that
          divergence or image issues could tip the prediction either way.

    ``low``
        - AI confidence ≥ 0.80, no divergence, quality above the unusable
          threshold.  The model is confident and agrees with the reporter; this
          report can be safely deprioritised in the analyst queue.

    ``normal``
        - Fallback for any case not matched above (e.g. confidence is None
          because the report is still being processed).

    Args:
        ai_confidence:   Scalar 0.0–1.0 from the vision provider, or None.
        ai_quality_score: Scalar 0.0–1.0 image quality score, or None.
        ai_divergence:   True when AI prediction ≠ reporter severity AND
                         ai_confidence > _DIVERGENCE_CONFIDENCE_THRESHOLD.

    Returns:
        One of ``"critical"``, ``"high"``, ``"normal"``, ``"low"``.
    """
    # Unusable image — severity prediction is noise regardless of confidence.
    if (
        ai_quality_score is not None
        and ai_quality_score < settings.AI_QUALITY_CRITICAL_THRESHOLD
    ):
        return "critical"

    # Confidence too low for autonomous action.
    if (
        ai_confidence is not None
        and ai_confidence < settings.AI_CONFIDENCE_CRITICAL_THRESHOLD
    ):
        return "critical"

    # AI and reporter disagree — one of them is wrong; analyst must decide.
    if ai_divergence:
        return "high"

    # Medium confidence band — uncertain enough to warrant a second look.
    if (
        ai_confidence is not None
        and ai_confidence < settings.AI_CONFIDENCE_HIGH_PRIORITY_THRESHOLD
    ):
        return "high"

    # High confidence, no divergence, acceptable image quality → safe to defer.
    if (
        ai_confidence is not None
        and ai_confidence >= settings.AI_CONFIDENCE_HIGH_PRIORITY_THRESHOLD
    ):
        return "low"

    return "normal"


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
# Retry intervals (spec §8.3 error handling)
# ---------------------------------------------------------------------------
# Celery countdown values in seconds for each retry attempt:
#   attempt 1 → 30 s
#   attempt 2 → 120 s (2 min)
#   attempt 3 → 600 s (10 min)
_RETRY_COUNTDOWNS = [30, 120, 600]


# ---------------------------------------------------------------------------
# Image download helper
# ---------------------------------------------------------------------------


def _download_image(photo_url: str) -> bytes:
    """Download image bytes from object storage using the stored photo_url.

    Args:
        photo_url: Full URL or storage key of the stored image.

    Returns:
        Raw image bytes.

    Raises:
        VisionAPIError: If the download fails.
    """
    try:
        resp = requests.get(photo_url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        raise VisionAPIError(
            f"Image download failed for {photo_url!r}: {type(exc).__name__}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Perceptual hash computation
# ---------------------------------------------------------------------------


def _compute_phash(image_bytes: bytes) -> Optional[str]:
    """Compute a 64-bit DCT perceptual hash using the ``imagehash`` library.

    Imports are deferred so that a missing ``imagehash`` package degrades
    gracefully (returns ``None``) rather than breaking the module import.

    Args:
        image_bytes: Raw image binary.

    Returns:
        Hex string representation of the pHash, or ``None`` on failure.
    """
    try:
        import imagehash  # type: ignore[import]
        from PIL import Image as PILImage  # type: ignore[import]

        img = PILImage.open(BytesIO(image_bytes))
        ph = imagehash.phash(img)
        # imagehash returns a custom ImageHash object; str() gives the hex form.
        return str(ph)
    except ImportError:
        logger.warning("imagehash/Pillow not installed — pHash computation skipped")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("pHash computation failed: %s", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Notification queue helper
# ---------------------------------------------------------------------------


def _publish_photo_request_notification(report_id: str, db: Session) -> None:
    """Insert a ``reporter_photo_request`` notification record.

    Reads the reporter's token hash from the report to use as ``recipient_hash``.
    The notification worker picks this up and dispatches an SMS if the reporter
    has a verified phone number.

    Args:
        report_id: UUID string of the affected report.
        db:        Active synchronous database session.
    """
    row = db.execute(
        text("SELECT reporter_token_hash FROM report WHERE id = :rid"),
        {"rid": report_id},
    ).fetchone()

    if row is None:
        logger.warning("Cannot dispatch photo request: report %s not found", report_id)
        return

    db.execute(
        text("""
            INSERT INTO notification (type, recipient_hash, report_id, status)
            VALUES (
                'reporter_photo_request',
                :recipient_hash,
                :report_id,
                'pending'
            )
        """),
        {"recipient_hash": row.reporter_token_hash, "report_id": report_id},
    )
    logger.info("Photo-request notification queued for report %s", report_id)


# ---------------------------------------------------------------------------
# Core implementation — decoupled from Celery for unit testability
# ---------------------------------------------------------------------------


def _process_report_image_impl(
    report_id: str,
    vision_provider: Optional[VisionProvider] = None,
) -> dict:
    """Stage 3 AI processing logic, decoupled from the Celery task wrapper.

    Extracted into a standalone function so tests can call it directly without
    a Celery worker context.  The Celery task ``process_report_image`` delegates
    here and handles the retry policy.

    Args:
        report_id:       UUID string of the ``report`` record to process.
        vision_provider: Injected provider for testing; defaults to factory.

    Returns:
        Dict with keys: ``photo_status``, ``ai_quality_score``,
        ``ai_severity_prediction``, ``ai_confidence``, ``ai_divergence``,
        ``photo_phash``.

    Raises:
        VisionAPIError: Propagated so the Celery wrapper can apply retries.
        Exception:      Any other unexpected error is also propagated.
    """
    _report_id = UUID(report_id)
    logger.info("AI task started for report %s", _report_id)

    provider = vision_provider or get_vision_provider()

    db: Session = _SyncSessionLocal()
    try:
        # ── 1. Load report ────────────────────────────────────────────────────
        row = db.execute(
            text("""
                SELECT id, photo_url, damage_severity, lat, lng
                FROM report
                WHERE id = :report_id
            """),
            {"report_id": str(_report_id)},
        ).fetchone()

        if row is None:
            logger.error("AI task: report %s not found — skipping", _report_id)
            return {
                "photo_status": "ai_processing_failed",
                "ai_quality_score": None,
                "ai_severity_prediction": None,
                "ai_confidence": None,
                "ai_divergence": None,
                "photo_phash": None,
            }

        photo_url: Optional[str] = row.photo_url
        reporter_severity: str = (
            row.damage_severity.value
            if hasattr(row.damage_severity, "value")
            else str(row.damage_severity)
        )

        if not photo_url:
            logger.warning("AI task: report %s has no photo_url — skipping", _report_id)
            return {
                "photo_status": "ai_processing_failed",
                "ai_quality_score": None,
                "ai_severity_prediction": None,
                "ai_confidence": None,
                "ai_divergence": None,
                "photo_phash": None,
            }

        # ── 2. Download image ─────────────────────────────────────────────────
        image_bytes = _download_image(photo_url)

        # ── 3. Call vision provider (Stage 3.1 + 3.2 in one batched call) ─────
        result: ImageAnalysisResult = asyncio.run(
            provider.analyse_damage_image(
                image_bytes=image_bytes,
                reporter_severity=reporter_severity,
            )
        )

        logger.info(
            "AI analysis complete for report %s — quality=%s (%.2f), "
            "severity=%s (conf=%.2f)",
            _report_id,
            result.quality_flag,
            result.quality_score,
            result.ai_severity_prediction,
            result.ai_confidence,
        )

        # ── 4. Handle unusable image (Stage 3.1) ──────────────────────────────
        if result.quality_flag == "unusable":
            # Unusable image → critical priority: no reliable AI data at all.
            priority = _compute_review_priority(
                ai_confidence=None,
                ai_quality_score=result.quality_score,
                ai_divergence=False,
            )
            db.execute(
                text("""
                    UPDATE report
                    SET
                        ai_quality_score = :score,
                        photo_status     = :photo_status,
                        review_priority  = :priority,
                        updated_at       = CURRENT_TIMESTAMP
                    WHERE id = :report_id
                """),
                {
                    "score": result.quality_score,
                    "photo_status": "insufficient_quality",
                    "priority": priority,
                    "report_id": str(_report_id),
                },
            )
            _publish_photo_request_notification(str(_report_id), db)
            db.commit()

            logger.info(
                "AI task: report %s photo marked insufficient_quality "
                "(review_priority=%s)",
                _report_id,
                priority,
            )
            return {
                "photo_status": "insufficient_quality",
                "ai_quality_score": result.quality_score,
                "ai_severity_prediction": None,
                "ai_confidence": None,
                "ai_divergence": None,
                "photo_phash": None,
                "review_priority": priority,
            }

        # ── 5. Compute perceptual hash (usable / borderline only) ─────────────
        photo_phash = _compute_phash(image_bytes)

        # ── 6. Evaluate divergence flag ────────────────────────────────────────
        # Threshold is read from Redis each task run so the active learning
        # loop can adjust sensitivity without a worker restart.
        divergence_threshold = _get_divergence_threshold()
        divergence = (
            result.ai_severity_prediction != reporter_severity
            and result.ai_confidence > divergence_threshold
        )

        # ── 7. Compute analyst queue priority ─────────────────────────────────
        priority = _compute_review_priority(
            ai_confidence=result.ai_confidence,
            ai_quality_score=result.quality_score,
            ai_divergence=divergence,
        )

        # ── 8. Write AI results back to report (Stage 3.2) ────────────────────
        # CRITICAL: ai_severity_prediction NEVER overwrites damage_severity.
        db.execute(
            text("""
                UPDATE report
                SET
                    ai_quality_score        = :quality_score,
                    ai_severity_prediction  = :severity,
                    ai_confidence           = :confidence,
                    ai_divergence           = :divergence,
                    photo_phash             = :phash,
                    photo_status            = :photo_status,
                    review_priority         = :priority,
                    updated_at              = CURRENT_TIMESTAMP
                WHERE id = :report_id
            """),
            {
                "quality_score": result.quality_score,
                "severity": result.ai_severity_prediction,
                "confidence": result.ai_confidence,
                "divergence": divergence,
                "phash": photo_phash,
                "photo_status": "accepted",
                "priority": priority,
                "report_id": str(_report_id),
            },
        )
        db.commit()

        logger.info(
            "AI task complete for report %s — divergence=%s phash=%s "
            "review_priority=%s",
            _report_id,
            divergence,
            photo_phash is not None,
            priority,
        )

        # ── 9. Real-time SSE alert for divergent predictions ──────────────────
        # Published AFTER commit so the report is readable when an analyst
        # clicks through.  Fire-and-forget: Redis failure never fails the task.
        if divergence:
            _publish_analyst_event(
                {
                    "event": "report.ai_divergence",
                    "report_id": str(_report_id),
                    "ai_severity_prediction": result.ai_severity_prediction,
                    "reporter_severity": reporter_severity,
                    "ai_confidence": result.ai_confidence,
                    "review_priority": priority,
                    "lat": float(row.lat) if row.lat is not None else None,
                    "lng": float(row.lng) if row.lng is not None else None,
                }
            )

        return {
            "photo_status": "accepted",
            "ai_quality_score": result.quality_score,
            "ai_severity_prediction": result.ai_severity_prediction,
            "ai_confidence": result.ai_confidence,
            "ai_divergence": divergence,
            "photo_phash": photo_phash,
            "review_priority": priority,
        }

    except VisionAPIError:
        db.rollback()
        raise  # Re-raised so the Celery wrapper can apply the retry policy.

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Celery task — thin retry wrapper around _process_report_image_impl
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.ai_tasks.process_report_image",
    bind=True,
    max_retries=3,
    # Default retry delay overridden per-attempt via self.retry(countdown=...).
    default_retry_delay=30,
    acks_late=True,
)
def process_report_image(
    self: Task,
    report_id: str,
    # vision_provider is not passed via Celery messages; it is used only in
    # tests that call the task directly via .apply() with task_always_eager=True.
    _vision_provider: Optional[VisionProvider] = None,
) -> dict:
    """Stage 3 AI processing for a submitted report image.

    Triggered by the submission service after a photo has been confirmed
    stored in object storage.  Delegates all logic to
    ``_process_report_image_impl`` and handles the retry policy.

    Retry schedule (spec §8.3 error handling):
        Attempt 1 → wait 30 s
        Attempt 2 → wait 2 min
        Attempt 3 → wait 10 min
        All retries exhausted → ``photo_status = "ai_processing_failed"``

    Args:
        report_id: UUID string of the ``report`` record to process.

    Returns:
        Dict summarising the processing outcome.
    """
    try:
        return _process_report_image_impl(report_id, vision_provider=_vision_provider)

    except (VisionAPIError, Exception) as exc:
        attempt = self.request.retries  # 0-indexed; 0 means first failure
        countdown = (
            _RETRY_COUNTDOWNS[attempt] if attempt < len(_RETRY_COUNTDOWNS) else 600
        )
        logger.warning(
            "AI task error for report %s (attempt %d/%d, retry in %ds): %s",
            report_id,
            attempt + 1,
            self.max_retries,
            countdown,
            type(exc).__name__,
        )

        try:
            raise self.retry(exc=exc, countdown=countdown)
        except MaxRetriesExceededError:
            logger.error(
                "AI task permanently failed for report %s after %d retries — "
                "setting photo_status=ai_processing_failed",
                report_id,
                self.max_retries,
            )
            # Best-effort final update — errors here are swallowed so the
            # report remains accessible to analysts.
            try:
                db: Session = _SyncSessionLocal()
                try:
                    # No AI data at all → critical: analyst must review before
                    # any action is taken on this report.
                    db.execute(
                        text("""
                            UPDATE report
                            SET
                                photo_status    = :photo_status,
                                review_priority = 'critical',
                                updated_at      = CURRENT_TIMESTAMP
                            WHERE id = :report_id
                        """),
                        {
                            "photo_status": "ai_processing_failed",
                            "report_id": report_id,
                        },
                    )
                    db.commit()
                finally:
                    db.close()
            except Exception as db_exc:  # noqa: BLE001
                logger.error(
                    "AI task: could not write ai_processing_failed for "
                    "report %s: %s",
                    report_id,
                    type(db_exc).__name__,
                )

            return {
                "photo_status": "ai_processing_failed",
                "ai_quality_score": None,
                "ai_severity_prediction": None,
                "ai_confidence": None,
                "ai_divergence": None,
                "photo_phash": None,
                "review_priority": "critical",
            }
