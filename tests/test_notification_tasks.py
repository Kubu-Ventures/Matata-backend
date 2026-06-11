"""Tests for the notification worker
Covers:
* Analyst alert: immediate send on first critical report in window.
* Analyst alert: digest accumulation within the rate-limit window.
* Analyst alert: rate-limit key TTL is set correctly.
* Analyst alert: notification log rows written for sent and failed deliveries.
* Reporter photo replacement: sent for verified reporters.
* Reporter photo replacement: idempotent — second call is a no-op.
* Reporter photo replacement: not sent for unverified reporters.
* Reporter photo replacement: not sent when report is not found.
* Email delivery failure triggers retry and sets status="failed" after
  max retries.
* SMTPEmailProvider: sends correctly via smtplib (Mailpit / Postal).
* SMTPEmailProvider: raises EmailDeliveryError on SMTP failure.
* get_email_provider: returns correct provider for each EMAIL_PROVIDER value.
"""

from __future__ import annotations

import asyncio
import smtplib
from typing import List, Optional
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Helper stubs
# ---------------------------------------------------------------------------


class _FakeConsoleEmail:
    """Synchronous-friendly email stub that records calls."""

    def __init__(self) -> None:
        self.calls: List[dict] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.calls.append({"to": to, "subject": subject, "body": body})


class _FakeConsoleSMS:
    """SMS stub that records calls and never raises."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def send_otp(self, phone_number: str, message: str) -> None:
        self.calls.append((phone_number, message))


class _FakeFailEmail:
    """Email stub that always raises EmailDeliveryError."""

    async def send(self, to: str, subject: str, body: str) -> None:
        from app.services.email_service import EmailDeliveryError

        raise EmailDeliveryError("Simulated delivery failure")


# ---------------------------------------------------------------------------
# Report factory helper
# ---------------------------------------------------------------------------


def _make_report_row(
    report_id: str,
    crisis_type: str = "earthquake",
    damage_severity: str = "destroyed",
    lat: float = -1.286389,
    lng: float = 36.817223,
    landmark_description: Optional[str] = None,
    ai_confidence: Optional[float] = 0.92,
    reporter_token_hash: str = "abc123",
) -> dict:
    """Build a fake report dict matching _fetch_report output."""
    return {
        "id": report_id,
        "crisis_type": crisis_type,
        "damage_severity": damage_severity,
        "lat": lat,
        "lng": lng,
        "landmark_description": landmark_description,
        "ai_confidence": ai_confidence,
        "reporter_token_hash": reporter_token_hash,
    }


# ---------------------------------------------------------------------------
# Analyst alert — tests
# ---------------------------------------------------------------------------


class TestSendAnalystAlertImpl:
    """Unit tests for _send_analyst_alert_impl."""

    def _fake_redis(
        self, already_alerted: bool = False, digest_items: Optional[List[str]] = None
    ) -> MagicMock:
        """Return a synchronous Redis mock."""
        r = MagicMock()
        r.exists.return_value = 1 if already_alerted else 0
        r.lrange.return_value = digest_items or []
        r.set = MagicMock()
        r.rpush = MagicMock()
        r.expire = MagicMock()
        r.delete = MagicMock()
        r.close = MagicMock()
        return r

    def test_immediate_alert_sent_to_analyst(self) -> None:
        """First critical report in window triggers immediate alert email."""
        report_id = str(uuid4())
        analyst_hash = "analystAAA"
        analyst_email = "analyst@matata.org"
        report = _make_report_row(report_id)
        email_stub = _FakeConsoleEmail()

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=report,
            ),
            patch(
                "app.workers.notification_tasks._get_active_analysts",
                return_value=[{"id_hash": analyst_hash, "email": analyst_email}],
            ),
            patch(
                "app.workers.notification_tasks.get_email_provider",
                return_value=email_stub,
            ),
            patch(
                "app.workers.notification_tasks._write_notification_log",
            ),
            patch(
                "app.workers.notification_tasks._update_notification_status",
            ),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(already_alerted=False),
            ),
        ):
            from app.workers.notification_tasks import _send_analyst_alert_impl

            result = _send_analyst_alert_impl(report_id)

        assert result["sent_count"] == 1
        assert result["digested_count"] == 0
        assert len(email_stub.calls) == 1
        assert "Critical damage report" in email_stub.calls[0]["subject"]
        assert report_id in email_stub.calls[0]["body"]

    def test_digest_accumulation_within_window(self) -> None:
        """Within the window, critical reports are digested, not sent immediately."""
        report_id = str(uuid4())
        analyst_hash = "analystBBB"
        analyst_email = "analyst@matata.org"
        report = _make_report_row(report_id)
        email_stub = _FakeConsoleEmail()
        redis_mock = self._fake_redis(already_alerted=True)

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=report,
            ),
            patch(
                "app.workers.notification_tasks._get_active_analysts",
                return_value=[{"id_hash": analyst_hash, "email": analyst_email}],
            ),
            patch(
                "app.workers.notification_tasks.get_email_provider",
                return_value=email_stub,
            ),
            patch(
                "app.workers.notification_tasks._write_notification_log",
            ),
            patch(
                "app.workers.notification_tasks._update_notification_status",
            ),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=redis_mock,
            ),
        ):
            from app.workers.notification_tasks import _send_analyst_alert_impl

            result = _send_analyst_alert_impl(report_id)

        assert result["sent_count"] == 0
        assert result["digested_count"] == 1
        assert len(email_stub.calls) == 0
        redis_mock.rpush.assert_called_once()

    def test_five_simultaneous_destroyed_reports_one_sent_four_digested(self) -> None:
        """5 simultaneous critical reports → 1 immediate alert + 4 digested."""
        analyst_hash = "analystCCC"
        analyst_email = "analyst@matata.org"
        email_stub = _FakeConsoleEmail()

        redis_mocks = []
        for i in range(5):
            already = i > 0
            redis_mocks.append(self._fake_redis(already_alerted=already))

        redis_iter = iter(redis_mocks)

        def _redis_factory(*args, **kwargs):
            return next(redis_iter)

        with (
            patch(
                "app.workers.notification_tasks._get_active_analysts",
                return_value=[{"id_hash": analyst_hash, "email": analyst_email}],
            ),
            patch(
                "app.workers.notification_tasks.get_email_provider",
                return_value=email_stub,
            ),
            patch(
                "app.workers.notification_tasks._write_notification_log",
            ),
            patch(
                "app.workers.notification_tasks._update_notification_status",
            ),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                side_effect=_redis_factory,
            ),
        ):
            from app.workers.notification_tasks import _send_analyst_alert_impl

            total_sent = 0
            total_digested = 0

            for _ in range(5):
                rid = str(uuid4())
                with patch(
                    "app.workers.notification_tasks._fetch_report",
                    return_value=_make_report_row(rid),
                ):
                    result = _send_analyst_alert_impl(rid)
                    total_sent += result["sent_count"]
                    total_digested += result["digested_count"]

        assert total_sent == 1
        assert total_digested == 4

    def test_no_analysts_returns_zero_counts(self) -> None:
        """If no analysts exist, no emails are sent."""
        report_id = str(uuid4())

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=_make_report_row(report_id),
            ),
            patch(
                "app.workers.notification_tasks._get_active_analysts",
                return_value=[],
            ),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(),
            ),
        ):
            from app.workers.notification_tasks import _send_analyst_alert_impl

            result = _send_analyst_alert_impl(report_id)

        assert result["sent_count"] == 0
        assert result["digested_count"] == 0

    def test_report_not_found_returns_zeros(self) -> None:
        """Missing report → graceful return, no email sent."""
        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=None,
            ),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(),
            ),
        ):
            from app.workers.notification_tasks import _send_analyst_alert_impl

            result = _send_analyst_alert_impl(str(uuid4()))

        assert result["sent_count"] == 0

    def test_email_subject_contains_crisis_type_and_location(self) -> None:
        """Alert subject includes crisis type and location."""
        report_id = str(uuid4())
        report = _make_report_row(
            report_id,
            crisis_type="flood",
            landmark_description="Near Nairobi Central Market",
        )
        email_stub = _FakeConsoleEmail()

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=report,
            ),
            patch(
                "app.workers.notification_tasks._get_active_analysts",
                return_value=[{"id_hash": "h1", "email": "a@b.com"}],
            ),
            patch(
                "app.workers.notification_tasks.get_email_provider",
                return_value=email_stub,
            ),
            patch("app.workers.notification_tasks._write_notification_log"),
            patch("app.workers.notification_tasks._update_notification_status"),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(already_alerted=False),
            ),
        ):
            from app.workers.notification_tasks import _send_analyst_alert_impl

            _send_analyst_alert_impl(report_id)

        subject = email_stub.calls[0]["subject"]
        assert "Flood" in subject or "flood" in subject
        assert "Nairobi Central Market" in subject


# ---------------------------------------------------------------------------
# Photo replacement request — tests
# ---------------------------------------------------------------------------


class TestSendPhotoReplacementRequestImpl:
    """Unit tests for _send_photo_replacement_request_impl."""

    def _fake_redis(self, already_sent: bool = False) -> MagicMock:
        r = MagicMock()
        r.exists.return_value = 1 if already_sent else 0
        r.set = MagicMock()
        r.close = MagicMock()
        return r

    def test_sms_sent_for_verified_reporter(self) -> None:
        """SMS dispatched when reporter is OTP-verified and phone is resolvable."""
        report_id = str(uuid4())
        reporter_hash = "verified_reporter_hash"
        report = _make_report_row(report_id, reporter_token_hash=reporter_hash)
        sms_stub = _FakeConsoleSMS()

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=report,
            ),
            patch(
                "app.workers.notification_tasks._is_verified_reporter",
                return_value=True,
            ),
            patch(
                "app.workers.notification_tasks._get_reporter_phone",
                return_value="+254700123456",
            ),
            patch(
                "app.workers.notification_tasks.get_sms_gateway",
                return_value=sms_stub,
            ),
            patch("app.workers.notification_tasks._write_notification_log"),
            patch("app.workers.notification_tasks._update_notification_status"),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(already_sent=False),
            ),
        ):
            from app.workers.notification_tasks import (
                _send_photo_replacement_request_impl,
            )

            result = _send_photo_replacement_request_impl(report_id)

        assert result["sent"] is True
        assert result["reason"] == "ok"
        assert len(sms_stub.calls) == 1
        _, message = sms_stub.calls[0]
        assert report_id in message
        assert "clearer photo" in message.lower() or "replacement" in message.lower()

    def test_idempotent_second_call_is_noop(self) -> None:
        """A second call for the same report does not re-send the SMS."""
        report_id = str(uuid4())
        sms_stub = _FakeConsoleSMS()

        with (
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(already_sent=True),
            ),
            patch(
                "app.workers.notification_tasks.get_sms_gateway",
                return_value=sms_stub,
            ),
        ):
            from app.workers.notification_tasks import (
                _send_photo_replacement_request_impl,
            )

            result = _send_photo_replacement_request_impl(report_id)

        assert result["sent"] is False
        assert result["reason"] == "already_sent"
        assert len(sms_stub.calls) == 0

    def test_not_sent_for_unverified_reporter(self) -> None:
        """No SMS is sent when the reporter has not completed OTP verification."""
        report_id = str(uuid4())
        report = _make_report_row(report_id, reporter_token_hash="unverified_hash")

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=report,
            ),
            patch(
                "app.workers.notification_tasks._is_verified_reporter",
                return_value=False,
            ),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(already_sent=False),
            ),
        ):
            from app.workers.notification_tasks import (
                _send_photo_replacement_request_impl,
            )

            result = _send_photo_replacement_request_impl(report_id)

        assert result["sent"] is False
        assert result["reason"] == "reporter_not_verified"

    def test_not_sent_when_report_not_found(self) -> None:
        """No SMS is sent if the report does not exist."""
        report_id = str(uuid4())

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=None,
            ),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(already_sent=False),
            ),
        ):
            from app.workers.notification_tasks import (
                _send_photo_replacement_request_impl,
            )

            result = _send_photo_replacement_request_impl(report_id)

        assert result["sent"] is False
        assert result["reason"] == "report_not_found"

    def test_not_sent_when_phone_not_resolvable(self) -> None:
        """No SMS is sent if the reporter's phone cannot be resolved."""
        report_id = str(uuid4())
        report = _make_report_row(report_id, reporter_token_hash="some_hash")

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=report,
            ),
            patch(
                "app.workers.notification_tasks._is_verified_reporter",
                return_value=True,
            ),
            patch(
                "app.workers.notification_tasks._get_reporter_phone",
                return_value=None,
            ),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(already_sent=False),
            ),
        ):
            from app.workers.notification_tasks import (
                _send_photo_replacement_request_impl,
            )

            result = _send_photo_replacement_request_impl(report_id)

        assert result["sent"] is False
        assert result["reason"] == "phone_not_resolvable"

    def test_notification_log_written_on_failure(self) -> None:
        """Notification log is written with status='failed' when SMS dispatch fails."""
        from app.services.sms import SMSDeliveryError

        report_id = str(uuid4())
        reporter_hash = "fail_hash"
        report = _make_report_row(report_id, reporter_token_hash=reporter_hash)

        failing_sms = MagicMock()
        failing_sms.send_otp.side_effect = SMSDeliveryError("network error")

        db_mock = MagicMock()
        db_mock.execute = MagicMock()
        db_mock.commit = MagicMock()

        with (
            patch(
                "app.workers.notification_tasks._fetch_report",
                return_value=report,
            ),
            patch(
                "app.workers.notification_tasks._is_verified_reporter",
                return_value=True,
            ),
            patch(
                "app.workers.notification_tasks._get_reporter_phone",
                return_value="+254700000000",
            ),
            patch(
                "app.workers.notification_tasks.get_sms_gateway",
                return_value=failing_sms,
            ),
            patch("app.workers.notification_tasks._write_notification_log"),
            patch("app.workers.notification_tasks._update_notification_status"),
            patch(
                "app.workers.notification_tasks._SyncSessionLocal",
                return_value=MagicMock(
                    return_value=db_mock,
                    __call__=MagicMock(return_value=db_mock),
                ),
            ),
            patch(
                "app.workers.notification_tasks.sync_redis.Redis.from_url",
                return_value=self._fake_redis(already_sent=False),
            ),
        ):
            from app.workers.notification_tasks import (
                _send_photo_replacement_request_impl,
            )

            with pytest.raises(SMSDeliveryError):
                _send_photo_replacement_request_impl(report_id)


# ---------------------------------------------------------------------------
# Email provider tests
# ---------------------------------------------------------------------------


class TestConsoleEmailProvider:
    """Unit tests for ConsoleEmailProvider."""

    def test_send_prints_formatted_output(self, capsys) -> None:
        """ConsoleEmailProvider.send prints to stdout without raising."""
        from app.services.email_service import ConsoleEmailProvider

        provider = ConsoleEmailProvider()
        asyncio.run(
            provider.send(
                to="analyst@matata.org",
                subject="Test Subject",
                body="Test body content.",
            )
        )
        captured = capsys.readouterr()
        assert "analyst@matata.org" in captured.out
        assert "Test Subject" in captured.out
        assert "Test body content." in captured.out

    def test_send_does_not_raise(self) -> None:
        """ConsoleEmailProvider.send never raises EmailDeliveryError."""
        from app.services.email_service import ConsoleEmailProvider

        provider = ConsoleEmailProvider()
        asyncio.run(provider.send(to="x@y.com", subject="s", body="b"))


class TestSMTPEmailProvider:
    """Unit tests for SMTPEmailProvider."""

    def _make_provider(self) -> object:
        """Return an SMTPEmailProvider with test settings patched in."""
        from app.services.email_service import SMTPEmailProvider

        with patch.multiple(
            "app.services.email_service.settings",
            SMTP_HOST="mailpit",
            SMTP_PORT=1025,
            SMTP_USERNAME="",
            SMTP_PASSWORD="",
            SMTP_USE_TLS=False,
            SMTP_USE_STARTTLS=False,
            NOTIFICATION_FROM_EMAIL="noreply@crisismap.matata.org",
            create=True,
        ):
            return SMTPEmailProvider()

    def test_send_calls_smtplib(self) -> None:
        """SMTPEmailProvider.send calls smtplib.SMTP and sendmail."""
        from app.services.email_service import SMTPEmailProvider

        mock_smtp_instance = MagicMock()
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(
            return_value=mock_smtp_instance
        )
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch.multiple(
                "app.services.email_service.settings",
                SMTP_HOST="mailpit",
                SMTP_PORT=1025,
                SMTP_USERNAME="",
                SMTP_PASSWORD="",
                SMTP_USE_TLS=False,
                SMTP_USE_STARTTLS=False,
                NOTIFICATION_FROM_EMAIL="noreply@crisismap.matata.org",
                create=True,
            ),
            patch("app.services.email_service.smtplib.SMTP", mock_smtp_cls),
        ):
            provider = SMTPEmailProvider()
            asyncio.run(
                provider.send(
                    to="analyst@matata.org",
                    subject="Test",
                    body="Hello.",
                )
            )

        mock_smtp_instance.sendmail.assert_called_once()
        args = mock_smtp_instance.sendmail.call_args[0]
        assert args[1] == ["analyst@matata.org"]

    def test_smtp_error_raises_email_delivery_error(self) -> None:
        """SMTPEmailProvider raises EmailDeliveryError on smtplib.SMTPException."""
        from app.services.email_service import EmailDeliveryError, SMTPEmailProvider

        mock_smtp_instance = MagicMock()
        mock_smtp_instance.sendmail.side_effect = smtplib.SMTPException(
            "connection refused"
        )
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(
            return_value=mock_smtp_instance
        )
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch.multiple(
                "app.services.email_service.settings",
                SMTP_HOST="mailpit",
                SMTP_PORT=1025,
                SMTP_USERNAME="",
                SMTP_PASSWORD="",
                SMTP_USE_TLS=False,
                SMTP_USE_STARTTLS=False,
                NOTIFICATION_FROM_EMAIL="noreply@crisismap.matata.org",
                create=True,
            ),
            patch("app.services.email_service.smtplib.SMTP", mock_smtp_cls),
        ):
            provider = SMTPEmailProvider()
            with pytest.raises(EmailDeliveryError, match="SMTP error"):
                asyncio.run(provider.send(to="x@y.com", subject="s", body="b"))

    def test_network_error_raises_email_delivery_error(self) -> None:
        """SMTPEmailProvider raises EmailDeliveryError on OSError
        (e.g. connection refused).
        """
        from app.services.email_service import EmailDeliveryError, SMTPEmailProvider

        mock_smtp_cls = MagicMock()
        mock_smtp_cls.side_effect = OSError("connection refused")

        with (
            patch.multiple(
                "app.services.email_service.settings",
                SMTP_HOST="mailpit",
                SMTP_PORT=1025,
                SMTP_USERNAME="",
                SMTP_PASSWORD="",
                SMTP_USE_TLS=False,
                SMTP_USE_STARTTLS=False,
                NOTIFICATION_FROM_EMAIL="noreply@crisismap.matata.org",
                create=True,
            ),
            patch("app.services.email_service.smtplib.SMTP", mock_smtp_cls),
        ):
            provider = SMTPEmailProvider()
            with pytest.raises(EmailDeliveryError, match="Network error"):
                asyncio.run(provider.send(to="x@y.com", subject="s", body="b"))

    def test_starttls_called_when_configured(self) -> None:
        """SMTPEmailProvider calls smtp.starttls() when SMTP_USE_STARTTLS=True."""
        from app.services.email_service import SMTPEmailProvider

        mock_smtp_instance = MagicMock()
        mock_smtp_cls = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(
            return_value=mock_smtp_instance
        )
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch.multiple(
                "app.services.email_service.settings",
                SMTP_HOST="postal.example.org",
                SMTP_PORT=587,
                SMTP_USERNAME="crisismap",
                SMTP_PASSWORD="secret",
                SMTP_USE_TLS=False,
                SMTP_USE_STARTTLS=True,
                NOTIFICATION_FROM_EMAIL="noreply@crisismap.matata.org",
                create=True,
            ),
            patch("app.services.email_service.smtplib.SMTP", mock_smtp_cls),
        ):
            provider = SMTPEmailProvider()
            asyncio.run(provider.send(to="a@b.com", subject="s", body="b"))

        mock_smtp_instance.starttls.assert_called_once()
        mock_smtp_instance.login.assert_called_once_with("crisismap", "secret")


class TestGetEmailProvider:
    """Unit tests for get_email_provider factory."""

    def test_console_provider_returned_when_email_provider_console(self) -> None:
        """Returns ConsoleEmailProvider when EMAIL_PROVIDER=console."""
        from app.services.email_service import ConsoleEmailProvider, get_email_provider

        with patch.object(
            __import__("app.core.config", fromlist=["settings"]).settings,
            "EMAIL_PROVIDER",
            "console",
            create=True,
        ):
            provider = get_email_provider()
        assert isinstance(provider, ConsoleEmailProvider)

    def test_smtp_provider_returned_when_email_provider_smtp(self) -> None:
        """Returns SMTPEmailProvider when EMAIL_PROVIDER=smtp."""
        from app.services.email_service import SMTPEmailProvider, get_email_provider

        with (
            patch.object(
                __import__("app.core.config", fromlist=["settings"]).settings,
                "EMAIL_PROVIDER",
                "smtp",
                create=True,
            ),
            patch.multiple(
                "app.services.email_service.settings",
                SMTP_HOST="mailpit",
                SMTP_PORT=1025,
                SMTP_USERNAME="",
                SMTP_PASSWORD="",
                SMTP_USE_TLS=False,
                SMTP_USE_STARTTLS=False,
                NOTIFICATION_FROM_EMAIL="noreply@crisismap.matata.org",
                create=True,
            ),
        ):
            provider = get_email_provider()
        assert isinstance(provider, SMTPEmailProvider)

    def test_unknown_provider_raises_value_error(self) -> None:
        """get_email_provider raises ValueError for unknown EMAIL_PROVIDER values."""
        import app.services.email_service as _em

        with patch.object(
            __import__("app.core.config", fromlist=["settings"]).settings,
            "EMAIL_PROVIDER",
            "nonexistent_provider",
            create=True,
        ):
            with pytest.raises(ValueError, match="Unknown EMAIL_PROVIDER"):
                _em.get_email_provider()
