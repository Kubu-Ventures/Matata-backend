"""Notification Celery task definitions — spec §11 / issue #17.

This module implements two notification types dispatched by the CrisisMap
worker pipeline:

**Type 1 — Analyst alert (new critical-severity report)**
Triggered when a report with ``damage_severity="destroyed"`` enters
``status="pending"``.  Delivered via email to all active analyst accounts.
Rate-limited to 1 alert per analyst per 10-minute window; subsequent critical
reports within the window are accumulated and sent as a batched digest at the
end of the window.

**Type 2 — Reporter photo replacement request**
Triggered by the AI worker (issue #13) when ``quality_flag="unusable"``.
Delivered via SMS to the reporter if and only if they have a verified phone
number (i.e. their ``reporter_token_hash`` resolves to an OTP-verified
session).  Idempotent — a second call for the same report does not re-send.

**Notification log**
Every outbound notification attempt is written to the ``notification`` table:
``type``, ``recipient_hash``, ``report_id``, ``status``, ``sent_at``.  Failed
sends are retried once after 5 minutes.  After two failures, ``status``
is set to ``"failed"`` and the error is logged.

All database access uses **synchronous** SQLAlchemy (``Session``) because
Celery workers run in regular threads, not an async event loop.  A fresh
session is opened per task invocation and committed or rolled-back before
exit.

Retry policy
------------
* Analyst alert: up to 2 retries with 300-second (5-minute) backoff.
* Reporter SMS: up to 2 retries with 300-second backoff.

Both tasks use ``acks_late=True`` so that jobs are re-queued if the worker
crashes mid-execution.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional, cast
from uuid import UUID

import redis as sync_redis
from celery import Task
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.services.email_service import EmailDeliveryError, get_email_provider
from app.services.sms import SMSDeliveryError, get_sms_gateway
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
# Redis rate-limit key helpers
# ---------------------------------------------------------------------------

_NS = "crisismap:notifications"

# Rate limit: 1 alert per analyst per N minutes window.
_ALERT_RATE_LIMIT_MINUTES: int = getattr(
    settings, "ANALYST_ALERT_RATE_LIMIT_MINUTES", 10
)
_ALERT_RATE_LIMIT_SECONDS: int = _ALERT_RATE_LIMIT_MINUTES * 60

# Key for the per-analyst rate limit sentinel.
_RATE_LIMIT_KEY_TEMPLATE = f"{_NS}:analyst_alert_sent:{{analyst_hash}}"

# Key for the per-analyst digest accumulator: stores pending report IDs.
_DIGEST_KEY_TEMPLATE = f"{_NS}:analyst_alert_digest:{{analyst_hash}}"

# Key for the idempotency guard on reporter photo replacement requests.
_PHOTO_REQUEST_SENT_KEY_TEMPLATE = f"{_NS}:photo_request_sent:{{report_id}}"


def _rate_limit_key(analyst_hash: str) -> str:
    return _RATE_LIMIT_KEY_TEMPLATE.format(analyst_hash=analyst_hash)


def _digest_key(analyst_hash: str) -> str:
    return _DIGEST_KEY_TEMPLATE.format(analyst_hash=analyst_hash)


def _photo_request_key(report_id: str) -> str:
    return _PHOTO_REQUEST_SENT_KEY_TEMPLATE.format(report_id=report_id)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _get_active_analysts(db: Session) -> List[dict]:
    """Return all active analyst accounts (email + hashed ID).

    Queries the ``analyst_account`` table which stores provisioned analyst
    records.  Each row has at minimum:
        * ``id_hash``  — anonymised analyst identifier (used as recipient_hash)
        * ``email``    — delivery address

    If the project has not yet introduced a dedicated analyst_account table,
    this function falls back gracefully and returns an empty list, which means
    no alert emails are sent (safe-fail).

    Returns:
        List of dicts with keys ``id_hash`` and ``email``.
    """
    try:
        rows = db.execute(
            text("SELECT id_hash, email FROM analyst_account WHERE is_active = TRUE")
        ).fetchall()
        return [{"id_hash": row.id_hash, "email": row.email} for row in rows]
    except Exception as exc:  # noqa: BLE001
        # Table may not exist yet — log and degrade gracefully.
        logger.warning(
            "Could not query analyst_account table: %s — no alerts will be sent.",
            type(exc).__name__,
        )
        return []


def _fetch_report(db: Session, report_id: UUID) -> Optional[dict]:
    """Load the minimal report fields needed for notification copy.

    Returns:
        Dict with ``id``, ``crisis_type``, ``damage_severity``, ``lat``,
        ``lng``, ``landmark_description``, ``ai_confidence``,
        ``reporter_token_hash``, or ``None`` if not found.
    """
    row = db.execute(
        text("""
            SELECT
                id,
                crisis_type,
                damage_severity,
                lat,
                lng,
                landmark_description,
                ai_confidence,
                reporter_token_hash
            FROM report
            WHERE id = :report_id
            """),
        {"report_id": str(report_id)},
    ).fetchone()

    if row is None:
        return None

    return {
        "id": str(row.id),
        "crisis_type": row.crisis_type,
        "damage_severity": row.damage_severity,
        "lat": row.lat,
        "lng": row.lng,
        "landmark_description": row.landmark_description,
        "ai_confidence": row.ai_confidence,
        "reporter_token_hash": row.reporter_token_hash,
    }


def _is_verified_reporter(db: Session, reporter_token_hash: str) -> bool:
    """Return True if the reporter completed OTP verification.

    A verified reporter has a ``reporter_session`` row with
    ``is_otp_verified=TRUE`` for their token hash.  If the table does not
    exist, returns False (safe-fail — no SMS sent).
    """
    try:
        row = db.execute(
            text("""
                SELECT 1 FROM reporter_session
                WHERE token_hash = :token_hash
                  AND is_otp_verified = TRUE
                LIMIT 1
                """),
            {"token_hash": reporter_token_hash},
        ).fetchone()
        return row is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not query reporter_session table: %s — treating as unverified.",
            type(exc).__name__,
        )
        return False


def _write_notification_log(
    db: Session,
    *,
    notification_type: str,
    recipient_hash: str,
    report_id: UUID,
    status: str,
    sent_at: Optional[datetime] = None,
) -> None:
    """Insert a row into the ``notification`` table.

    Uses raw SQL to avoid a hard dependency on the ORM model being importable
    from this worker module (the worker may run in a separate container).

    """
    now = sent_at or datetime.now(tz=timezone.utc)
    db.execute(
        text("""
            INSERT INTO notification
                (type, recipient_hash, report_id, status,
                 sent_at, created_at, updated_at)
            VALUES
                (:type, :recipient_hash, :report_id, :status,
                 :sent_at, :now, :now)
            """),
        {
            "type": notification_type,
            "recipient_hash": recipient_hash,
            "report_id": str(report_id),
            "status": status,
            "sent_at": now if status == "sent" else None,
            "now": now,
        },
    )


def _update_notification_status(
    db: Session,
    *,
    report_id: UUID,
    notification_type: str,
    recipient_hash: str,
    status: str,
    sent_at: Optional[datetime] = None,
) -> None:
    """Update the most recent notification row matching the given criteria."""
    db.execute(
        text("""
            UPDATE notification
            SET status = :status,
                sent_at = :sent_at,
                updated_at = :now
            WHERE report_id = :report_id
              AND type = :type
              AND recipient_hash = :recipient_hash
              AND id = (
                  SELECT id FROM notification
                  WHERE report_id = :report_id
                    AND type = :type
                    AND recipient_hash = :recipient_hash
                  ORDER BY created_at DESC
                  LIMIT 1
              )
            """),
        {
            "status": status,
            "sent_at": sent_at,
            "report_id": str(report_id),
            "type": notification_type,
            "recipient_hash": recipient_hash,
            "now": datetime.now(tz=timezone.utc),
        },
    )


# ---------------------------------------------------------------------------
# Email copy helpers
# ---------------------------------------------------------------------------


def _location_summary(report: dict) -> str:
    """Build a human-readable location string from report fields."""
    if report.get("landmark_description"):
        return report["landmark_description"]
    lat = report.get("lat")
    lng = report.get("lng")
    if lat is not None and lng is not None:
        return f"{lat:.5f}°, {lng:.5f}°"
    return "Location not available"


def _build_alert_subject(report: dict) -> str:
    """Construct the alert email subject line per spec §11."""
    crisis_type = (report.get("crisis_type") or "unknown").replace("_", " ").title()
    location = _location_summary(report)
    # Truncate location for subject line readability.
    location_short = location[:60] + "…" if len(location) > 60 else location
    return f"[CrisisMap] Critical damage report — {crisis_type} at {location_short}"


def _build_alert_body(report: dict, dashboard_base_url: str) -> str:
    """Construct the alert email body."""
    report_id = report["id"]
    crisis_type = (report.get("crisis_type") or "unknown").replace("_", " ").title()
    severity = (report.get("damage_severity") or "unknown").title()
    location = _location_summary(report)
    ai_confidence = report.get("ai_confidence")
    confidence_str = (
        f"{ai_confidence:.0%}" if ai_confidence is not None else "not yet assessed"
    )
    detail_url = f"{dashboard_base_url}/analyst/reports/{report_id}"

    return (
        "A critical-severity damage report has been submitted"
        " and requires analyst review.\n\n"
        f"Report ID    : {report_id}\n"
        f"Crisis Type  : {crisis_type}\n"
        f"Severity     : {severity}\n"
        f"Location     : {location}\n"
        f"AI Confidence: {confidence_str}\n\n"
        f"Review this report in the CrisisMap dashboard:\n{detail_url}\n\n"
        "---\n"
        "This is an automated alert from CrisisMap. Do not reply to this email.\n"
        "To update your notification preferences, contact your system administrator."
    )


def _build_digest_body(report_ids: List[str], dashboard_base_url: str) -> str:
    """Construct the digest email body for batched critical reports."""
    count = len(report_ids)
    ids_block = "\n".join(f"  • {rid}" for rid in report_ids)
    _query = "status=pending&damage_severity=destroyed"
    dashboard_url = f"{dashboard_base_url}/analyst/reports?{_query}"
    return (
        f"{count} additional critical-severity report(s) were submitted during the "
        f"last {_ALERT_RATE_LIMIT_MINUTES}-minute notification window.\n\n"
        f"Report IDs:\n{ids_block}\n\n"
        f"Review all pending critical reports:\n{dashboard_url}\n\n"
        f"---\n"
        f"This is an automated digest from CrisisMap. Do not reply to this email."
    )


# ---------------------------------------------------------------------------
# Implementation — analyst alert
# ---------------------------------------------------------------------------


def _send_analyst_alert_impl(report_id: str) -> dict:
    """Core analyst alert logic, decoupled from the Celery task wrapper.

    For each active analyst:
    1. Check the per-analyst rate-limit key in Redis.
    2. If within the rate-limit window → accumulate the report ID in the
       digest key and skip immediate send.
    3. If outside the rate-limit window → send the immediate alert email,
       set the rate-limit key, and flush any accumulated digest.
    4. Write a ``notification`` row for every delivery attempt.

    Args:
        report_id: UUID string of the ``report`` record to alert on.

    Returns:
        Dict with ``sent_count``, ``digested_count``, and ``skipped_count``.
    """
    _report_id = UUID(report_id)
    logger.info("Notification task: analyst alert for report %s", _report_id)

    db: Session = _SyncSessionLocal()
    redis_client = sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

    try:
        report = _fetch_report(db, _report_id)
        if report is None:
            logger.error(
                "Notification task: report %s not found — skipping", _report_id
            )
            return {"sent_count": 0, "digested_count": 0, "skipped_count": 0}

        analysts = _get_active_analysts(db)
        if not analysts:
            logger.info("Notification task: no active analysts found — no alerts sent.")
            return {"sent_count": 0, "digested_count": 0, "skipped_count": 0}

        # Resolve dashboard base URL from settings (safe default for dev).
        dashboard_base_url: str = getattr(
            settings, "DASHBOARD_BASE_URL", "https://crisismap.matata.org"
        )

        email_provider = get_email_provider()
        sent_count = 0
        digested_count = 0

        for analyst in analysts:
            analyst_hash: str = analyst["id_hash"]
            analyst_email: str = analyst["email"]
            rl_key = _rate_limit_key(analyst_hash)
            dg_key = _digest_key(analyst_hash)

            already_alerted: bool = bool(redis_client.exists(rl_key))

            if already_alerted:
                # Accumulate in digest list for this analyst.
                redis_client.rpush(dg_key, report_id)
                # Ensure digest key expires with the rate-limit window.
                redis_client.expire(dg_key, _ALERT_RATE_LIMIT_SECONDS)
                digested_count += 1
                logger.debug(
                    "Alert for report %s queued in digest for analyst %s…",
                    _report_id,
                    analyst_hash[:8],
                )
                continue

            # Outside rate-limit window — send immediate alert.
            subject = _build_alert_subject(report)
            body = _build_alert_body(report, dashboard_base_url)

            _write_notification_log(
                db,
                notification_type="analyst_alert",
                recipient_hash=analyst_hash,
                report_id=_report_id,
                status="pending",
            )
            db.commit()

            try:
                asyncio.run(
                    email_provider.send(
                        to=analyst_email,
                        subject=subject,
                        body=body,
                    )
                )
                sent_at = datetime.now(tz=timezone.utc)
                _update_notification_status(
                    db,
                    report_id=_report_id,
                    notification_type="analyst_alert",
                    recipient_hash=analyst_hash,
                    status="sent",
                    sent_at=sent_at,
                )
                db.commit()
                sent_count += 1
                logger.info(
                    "Analyst alert sent to %s…  (report %s)",
                    analyst_hash[:8],
                    _report_id,
                )
            except EmailDeliveryError as exc:
                logger.error(
                    "Analyst alert delivery failed for analyst %s…: %s",
                    analyst_hash[:8],
                    exc,
                )
                _update_notification_status(
                    db,
                    report_id=_report_id,
                    notification_type="analyst_alert",
                    recipient_hash=analyst_hash,
                    status="failed",
                )
                db.commit()
                raise  # Propagate so the Celery task wrapper can retry.

            # Set rate-limit sentinel (TTL = window length).
            redis_client.set(rl_key, "1", ex=_ALERT_RATE_LIMIT_SECONDS)

            # If there are pending digest items from a *previous* window that
            # was never flushed, send them now and clear the key.
            pending_ids: List[str] = cast(List[str], redis_client.lrange(dg_key, 0, -1))
            if pending_ids:
                n_pending = len(pending_ids)
                digest_subject = (
                    f"[CrisisMap] Digest — {n_pending}" " additional critical reports"
                )
                digest_body = _build_digest_body(pending_ids, dashboard_base_url)
                try:
                    asyncio.run(
                        email_provider.send(
                            to=analyst_email,
                            subject=digest_subject,
                            body=digest_body,
                        )
                    )
                    redis_client.delete(dg_key)
                    logger.info(
                        "Digest sent to analyst %s… (%d reports)",
                        analyst_hash[:8],
                        len(pending_ids),
                    )
                except EmailDeliveryError:
                    logger.error(
                        "Digest delivery failed for analyst %s…"
                        " — will retry next window",
                        analyst_hash[:8],
                    )

        return {
            "sent_count": sent_count,
            "digested_count": digested_count,
            "skipped_count": 0,
        }

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
        redis_client.close()


# ---------------------------------------------------------------------------
# Implementation — reporter photo replacement request
# ---------------------------------------------------------------------------


def _send_photo_replacement_request_impl(report_id: str) -> dict:
    """Core photo replacement SMS logic, decoupled from the Celery task wrapper.

    1. Idempotency check — if a replacement request has already been sent for
       this report, return immediately without re-sending.
    2. Load report and verify the reporter has a verified phone number.
    3. Send the SMS via the configured ``SMSGateway``.
    4. Write a ``notification`` row recording the delivery attempt.
    5. Set the idempotency key in Redis.

    Args:
        report_id: UUID string of the ``report`` record to notify on.

    Returns:
        Dict with ``sent``: bool and ``reason``: str.
    """
    _report_id = UUID(report_id)
    logger.info(
        "Notification task: photo replacement request for report %s", _report_id
    )

    redis_client = sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    db: Session = _SyncSessionLocal()

    try:
        # ── Idempotency guard ────────────────────────────────────────────────
        idempotency_key = _photo_request_key(report_id)
        if redis_client.exists(idempotency_key):
            logger.info(
                "Photo replacement request already sent for report %s — skipping.",
                _report_id,
            )
            return {"sent": False, "reason": "already_sent"}

        # ── Load report ──────────────────────────────────────────────────────
        report = _fetch_report(db, _report_id)
        if report is None:
            logger.error(
                "Notification task: report %s not found — skipping", _report_id
            )
            return {"sent": False, "reason": "report_not_found"}

        reporter_token_hash: str = report.get("reporter_token_hash") or ""

        # ── Verify reporter has an OTP-verified phone number ─────────────────
        is_verified = _is_verified_reporter(db, reporter_token_hash)
        if not reporter_token_hash or not is_verified:
            logger.info(
                "Reporter for report %s is not OTP-verified — no SMS sent.",
                _report_id,
            )
            return {"sent": False, "reason": "reporter_not_verified"}

        # ── Lookup the reporter's hashed phone number ────────────────────────
        # The SMS gateway sends to a phone number; we look up the hashed phone
        # from the reporter_session row so we can retrieve it (in E.164 format)
        # for dispatch.  The plaintext phone is retrieved from the SMS-gateway
        # lookup table only for the duration of the call.
        phone_number: Optional[str] = _get_reporter_phone(db, reporter_token_hash)
        if not phone_number:
            logger.info(
                "Could not resolve phone number for report %s — no SMS sent.",
                _report_id,
            )
            return {"sent": False, "reason": "phone_not_resolvable"}

        # ── Compose message ──────────────────────────────────────────────────
        message = (
            f"Your CrisisMap report {report_id} needs a clearer photo. "
            "Please reopen the app and submit a replacement image. Thank you."
        )

        # ── Write pending notification log ────────────────────────────────────
        _write_notification_log(
            db,
            notification_type="photo_replacement_request",
            recipient_hash=reporter_token_hash,
            report_id=_report_id,
            status="pending",
        )
        db.commit()

        # ── Dispatch SMS ──────────────────────────────────────────────────────
        sms_gateway = get_sms_gateway()
        try:
            sms_gateway.send_otp(phone_number, message)  # reuses the send interface
        except TypeError:
            # Some gateways have a ``send`` method instead of ``send_otp``.
            # Attempt a generic send if available.
            if hasattr(sms_gateway, "send"):
                sms_gateway.send(phone_number, message)  # type: ignore[attr-defined]
            else:
                raise

        sent_at = datetime.now(tz=timezone.utc)
        _update_notification_status(
            db,
            report_id=_report_id,
            notification_type="photo_replacement_request",
            recipient_hash=reporter_token_hash,
            status="sent",
            sent_at=sent_at,
        )
        db.commit()

        # ── Set idempotency key (TTL 30 days) ────────────────────────────────
        redis_client.set(idempotency_key, "1", ex=30 * 86_400)

        logger.info(
            "Photo replacement SMS sent for report %s (reporter: %s…)",
            _report_id,
            reporter_token_hash[:8],
        )
        return {"sent": True, "reason": "ok"}

    except SMSDeliveryError as exc:
        logger.error(
            "Photo replacement SMS delivery failed for report %s: %s",
            _report_id,
            exc,
        )
        db.execute(
            text("""
                UPDATE notification
                SET status = 'failed', updated_at = :now
                WHERE report_id = :report_id
                  AND type = 'photo_replacement_request'
                  AND id = (
                      SELECT id FROM notification
                      WHERE report_id = :report_id
                        AND type = 'photo_replacement_request'
                      ORDER BY created_at DESC LIMIT 1
                  )
                """),
            {"report_id": str(_report_id), "now": datetime.now(tz=timezone.utc)},
        )
        db.commit()
        raise  # Re-raise so the Celery task wrapper can retry.

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
        redis_client.close()


def _get_reporter_phone(db: Session, reporter_token_hash: str) -> Optional[str]:
    """Retrieve the E.164 phone number for a verified reporter.

    Looks up the ``reporter_session`` table for a row with the matching
    ``token_hash`` that has been OTP-verified.  The phone number is stored as
    a hashed value in the auth service — this function fetches the *original*
    E.164 number from the ``phone_lookup`` table if one exists (a separate,
    strongly access-controlled table that stores the mapping from hash → E.164
    for SMS dispatch purposes only).

    If the system does not maintain a ``phone_lookup`` table, returns ``None``
    and no SMS is sent (safe-fail).
    """
    try:
        row = db.execute(
            text("""
                SELECT pl.phone_number
                FROM reporter_session rs
                JOIN phone_lookup pl ON pl.phone_hash = rs.phone_hash
                WHERE rs.token_hash = :token_hash
                  AND rs.is_otp_verified = TRUE
                LIMIT 1
                """),
            {"token_hash": reporter_token_hash},
        ).fetchone()
        return row.phone_number if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not resolve phone number for token hash %s…: %s",
            reporter_token_hash[:8],
            type(exc).__name__,
        )
        return None


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.notification_tasks.send_analyst_alert",
    bind=True,
    max_retries=2,
    default_retry_delay=300,  # 5 minutes
    acks_late=True,
)
def send_analyst_alert(self: Task, report_id: str) -> dict:
    """Send a critical-severity report alert to all active analyst accounts.

    Triggered when a ``damage_severity="destroyed"`` report enters
    ``status="pending"``.  Rate-limited per analyst; subsequent critical
    reports within the window are batched into a digest.

    Args:
        report_id: UUID string of the triggering ``report`` record.

    Returns:
        Dict with ``sent_count``, ``digested_count``, and ``skipped_count``.

    Raises:
        celery.exceptions.Retry: On transient email delivery failures
                                 (up to 2 retries with 5-minute backoff).
    """
    try:
        return _send_analyst_alert_impl(report_id)
    except EmailDeliveryError as exc:
        logger.warning(
            "Analyst alert delivery error for report %s (%s) — will retry",
            report_id,
            type(exc).__name__,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error(
                "Analyst alert permanently failed for report %s after %d retries",
                report_id,
                self.max_retries,
            )
            return {"sent_count": 0, "digested_count": 0, "skipped_count": 1}
    except Exception as exc:
        logger.error(
            "Unexpected error in analyst alert task for report %s: %s",
            report_id,
            exc,
            exc_info=True,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error(
                "Analyst alert permanently failed for report %s after %d retries",
                report_id,
                self.max_retries,
            )
            return {"sent_count": 0, "digested_count": 0, "skipped_count": 1}


@celery_app.task(
    name="app.workers.notification_tasks.send_photo_replacement_request",
    bind=True,
    max_retries=2,
    default_retry_delay=300,  # 5 minutes
    acks_late=True,
)
def send_photo_replacement_request(self: Task, report_id: str) -> dict:
    """Send an SMS requesting a clearer photo from a verified reporter.

    Triggered by the AI worker when ``quality_flag="unusable"``.  Idempotent
    — calling this task a second time for the same report is a no-op.

    Args:
        report_id: UUID string of the ``report`` record with insufficient photo.

    Returns:
        Dict with ``sent``: bool and ``reason``: str.

    Raises:
        celery.exceptions.Retry: On transient SMS delivery failures
                                 (up to 2 retries with 5-minute backoff).
    """
    try:
        return _send_photo_replacement_request_impl(report_id)
    except SMSDeliveryError as exc:
        logger.warning(
            "Photo replacement SMS delivery error for report %s (%s) — will retry",
            report_id,
            type(exc).__name__,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error(
                "Photo replacement SMS permanently failed for"
                " report %s after %d retries",
                report_id,
                self.max_retries,
            )
            return {"sent": False, "reason": "max_retries_exceeded"}
    except Exception as exc:
        logger.error(
            "Unexpected error in photo replacement task for report %s: %s",
            report_id,
            exc,
            exc_info=True,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error(
                "Photo replacement SMS permanently failed for"
                " report %s after %d retries",
                report_id,
                self.max_retries,
            )
            return {"sent": False, "reason": "max_retries_exceeded"}
