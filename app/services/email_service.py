"""Email provider abstraction layer.

Defines the ``EmailProvider`` Protocol that all email back-ends must satisfy,
plus two concrete implementations:

* ``ConsoleEmailProvider`` — prints the email to stdout; used in development
  and automated tests so no real email account is needed.
* ``SMTPEmailProvider``    — sends via any SMTP server using Python's stdlib
  ``smtplib``.  Works with:
    - Mailpit (local dev / CI, zero config, catches all mail)
    - Postal   (self-hosted production MTA, fully open-source)
    - Any other standard SMTP server

No third-party email API keys are required.  All configuration is done via
the standard SMTP_* environment variables documented in ``.env.example``.

A factory function ``get_email_provider()`` returns the correct implementation
based on the ``EMAIL_PROVIDER`` setting, keeping all provider selection logic
in one place.

Design notes
------------
* Using ``typing.Protocol`` (structural subtyping) rather than an ABC keeps
  the contract explicit without requiring inheritance, making it easy to add a
  new provider without modifying this file.
* ``send`` is ``async`` throughout for consistency with the rest of the async
  codebase.  The SMTP implementation runs the blocking ``smtplib`` call in a
  thread-pool executor so it does not block the event loop.
* ``smtplib`` is Python stdlib — zero extra dependencies for email sending.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Protocol, runtime_checkable

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class EmailDeliveryError(RuntimeError):
    """Raised when an email provider fails to deliver a message."""


# ---------------------------------------------------------------------------
# Protocol — the interface every provider must satisfy
# ---------------------------------------------------------------------------


@runtime_checkable
class EmailProvider(Protocol):
    """Structural interface for all email back-ends.

    Any object that implements ``send`` with this exact signature is a valid
    ``EmailProvider`` — no inheritance required.
    """

    async def send(self, to: str, subject: str, body: str) -> None:
        """Dispatch an email message.

        Args:
            to:      Recipient email address.
            subject: Email subject line.
            body:    Plain-text email body.

        Raises:
            EmailDeliveryError: If the downstream provider returns an error
                                response or a network failure occurs.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# ConsoleEmailProvider — development / test
# ---------------------------------------------------------------------------


class ConsoleEmailProvider:
    """Development provider that prints emails to stdout.

    This implementation never makes any network call and is safe to use in
    automated tests.  Output is written to stdout (not a log file) so that
    ``pytest -s`` captures it and tests can inspect it when needed.
    """

    async def send(self, to: str, subject: str, body: str) -> None:
        """Print the email to stdout in a readable format."""
        separator = "─" * 60
        print(
            f"\n[ConsoleEmailProvider] {separator}\n"
            f"  To      : {to}\n"
            f"  Subject : {subject}\n"
            f"  Body    :\n{body}\n"
            f"[ConsoleEmailProvider] {separator}\n"
        )
        logger.debug(
            "ConsoleEmailProvider dispatched email to %s subject=%r", to, subject
        )


# ---------------------------------------------------------------------------
# SMTPEmailProvider — works with Mailpit (dev) and Postal (production)
# ---------------------------------------------------------------------------


class SMTPEmailProvider:
    """Production-ready email provider using standard SMTP.

    Compatible with any SMTP server:
    - **Mailpit** (dev/CI): ``SMTP_HOST=mailpit``, ``SMTP_PORT=1025``,
      no credentials required.  Catches all mail; web UI at port 8025.
    - **Postal** (self-hosted production): ``SMTP_HOST=<postal-host>``,
      ``SMTP_PORT=587``, ``SMTP_USE_STARTTLS=true``, plus credentials.
    - Any other standards-compliant SMTP MTA.

    Uses only Python stdlib ``smtplib`` — no extra pip dependencies.

    The blocking SMTP call is executed in a thread-pool executor so it does
    not block the asyncio event loop.
    """

    def __init__(self) -> None:
        self._host = settings.SMTP_HOST
        self._port = settings.SMTP_PORT
        self._username = settings.SMTP_USERNAME
        self._password = settings.SMTP_PASSWORD
        self._use_tls = settings.SMTP_USE_TLS  # SMTP_SSL (port 465)
        self._use_starttls = settings.SMTP_USE_STARTTLS  # STARTTLS (port 587)
        self._from_email = settings.NOTIFICATION_FROM_EMAIL

        if not self._from_email:
            raise EmailDeliveryError(
                "NOTIFICATION_FROM_EMAIL must be set when EMAIL_PROVIDER=smtp."
            )

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        """Blocking SMTP send — called from a thread-pool executor."""
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self._from_email
        msg["To"] = to

        try:
            if self._use_tls:
                # Implicit TLS — typically port 465.
                smtp_cls = smtplib.SMTP_SSL
                with smtp_cls(self._host, self._port, timeout=10) as smtp:
                    if self._username:
                        smtp.login(self._username, self._password)
                    smtp.sendmail(self._from_email, [to], msg.as_string())
            else:
                with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
                    if self._use_starttls:
                        smtp.starttls()
                    if self._username:
                        smtp.login(self._username, self._password)
                    smtp.sendmail(self._from_email, [to], msg.as_string())
        except smtplib.SMTPException as exc:
            raise EmailDeliveryError(f"SMTP error delivering to {to}: {exc}") from exc
        except OSError as exc:
            # Covers ConnectionRefusedError, TimeoutError, etc.
            raise EmailDeliveryError(
                f"Network error connecting to SMTP server "
                f"{self._host}:{self._port}: {exc}"
            ) from exc

    async def send(self, to: str, subject: str, body: str) -> None:
        """Send an email via SMTP, running the blocking call in a thread executor.

        Args:
            to:      Recipient email address.
            subject: Email subject line.
            body:    Plain-text email body.

        Raises:
            EmailDeliveryError: On SMTP error or network failure.
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._send_sync, to, subject, body)
        logger.info("SMTP email dispatched to %s subject=%r", to, subject)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_email_provider() -> EmailProvider:
    """Return the email provider implementation selected by ``EMAIL_PROVIDER``.

    Supported values for ``EMAIL_PROVIDER``:
    * ``console`` — ``ConsoleEmailProvider`` (default, development/test)
    * ``smtp``    — ``SMTPEmailProvider`` (Mailpit in dev, Postal in production)

    Returns:
        An object that satisfies the ``EmailProvider`` Protocol.

    Raises:
        ValueError: If ``EMAIL_PROVIDER`` is set to an unknown value.
    """
    provider_name = getattr(settings, "EMAIL_PROVIDER", "console").lower()

    if provider_name == "console":
        return ConsoleEmailProvider()
    if provider_name == "smtp":
        return SMTPEmailProvider()

    raise ValueError(
        f"Unknown EMAIL_PROVIDER value: '{provider_name}'. "
        "Supported options: 'console', 'smtp'."
    )
