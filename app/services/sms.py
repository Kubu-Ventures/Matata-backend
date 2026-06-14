"""SMS Gateway abstraction layer.

Defines the ``SMSGateway`` Protocol that all SMS back-ends must satisfy,
plus two concrete implementations:

* ``ConsoleSMSGateway``  — prints the OTP to stdout; used in development
  and automated tests so no real SMS account is needed.
* ``AfricasTalkingSMSGateway`` — production gateway via the Africa's Talking
  SMS API, used when ``SMS_GATEWAY=africastalking`` in the environment.

A factory function ``get_sms_gateway()`` returns the correct implementation
based on the ``SMS_GATEWAY`` setting, keeping all gateway selection logic in
one place.

Design notes
------------
* Using ``typing.Protocol`` (structural subtyping) rather than an ABC keeps
  the contract explicit without requiring inheritance, making it easy to add
  a third gateway (e.g. Twilio) without modifying this file.
* ``send_otp`` is intentionally synchronous in its signature so that a simple
  test implementation does not need to set up an async runtime.  The
  AfricasTalking implementation uses httpx with a sync client; if the API
  ever needs high concurrency, swap to ``async def`` and ``AsyncClient``.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol — the interface every gateway must satisfy
# ---------------------------------------------------------------------------


@runtime_checkable
class SMSGateway(Protocol):
    """Structural interface for all SMS back-ends.

    Any object that implements ``send_otp`` with this exact signature is a
    valid ``SMSGateway`` — no inheritance required.
    """

    def send_otp(self, phone_number: str, otp_code: str) -> None:
        """Dispatch a one-time password to *phone_number*.

        Args:
            phone_number: E.164-formatted phone number, e.g. ``+254700123456``.
                          This is the *plaintext* number; the gateway is
                          responsible for discarding it after dispatch.
            otp_code:     Six-digit numeric string, e.g. ``"483920"``.

        Raises:
            SMSDeliveryError: If the downstream gateway returns an error
                              response or a network failure occurs.
        """
        ...


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class SMSDeliveryError(RuntimeError):
    """Raised when an SMS gateway fails to deliver a message."""


# ---------------------------------------------------------------------------
# ConsoleSMSGateway — development / test
# ---------------------------------------------------------------------------


class ConsoleSMSGateway:
    """Development gateway that prints the OTP to stdout.

    This implementation never makes any network call and is safe to use in
    automated tests.  The OTP is written to stdout (not a log file) so that
    ``pytest -s`` captures it and the test can extract it when needed.

    The plaintext phone number is deliberately NOT printed to stdout or any
    log output, in keeping with the system-wide PII policy.
    """

    def send_otp(self, phone_number: str, otp_code: str) -> None:  # noqa: ARG002
        # phone_number intentionally unused — never log PII.
        print(f"[ConsoleSMSGateway] OTP code: {otp_code}")
        logger.debug("ConsoleSMSGateway dispatched OTP (phone number suppressed)")


# ---------------------------------------------------------------------------
# AfricasTalkingSMSGateway — production
# ---------------------------------------------------------------------------

_AT_SMS_URL = "https://api.africastalking.com/version1/messaging"


class AfricasTalkingSMSGateway:
    """Production SMS gateway backed by Africa's Talking.

    Requires ``AFRICASTALKING_API_KEY`` and ``AFRICASTALKING_USERNAME`` to be
    present in the environment (see ``.env.example``).

    The phone number is passed to the downstream API but is never stored or
    logged by this service.  The gateway client does not retain the number
    after the HTTP call completes.
    """

    def __init__(self) -> None:
        self._api_key = settings.AFRICASTALKING_API_KEY
        self._username = settings.AFRICASTALKING_USERNAME

        if not self._api_key or not self._username:
            raise SMSDeliveryError(
                "AFRICASTALKING_API_KEY and AFRICASTALKING_USERNAME must be set "
                "when SMS_GATEWAY=africastalking."
            )

    def send_otp(self, phone_number: str, otp_code: str) -> None:
        """Send the OTP via the Africa's Talking REST API.

        Args:
            phone_number: E.164-formatted number.  Forwarded to AT; never
                          stored or logged after this call returns.
            otp_code:     Six-digit OTP string.

        Raises:
            SMSDeliveryError: On HTTP error or non-success AT response code.
        """
        message = (
            f"Your CrisisMap verification code is: {otp_code}. " "Valid for 5 minutes."
        )

        payload: dict = {
            "username": self._username,
            "to": phone_number,
            "message": message,
        }
        sender_id = settings.AFRICASTALKING_SENDER_ID
        if sender_id:
            payload["from"] = sender_id

        try:
            response = httpx.post(
                _AT_SMS_URL,
                headers={
                    "apiKey": self._api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=payload,
                timeout=10.0,
            )
            response.raise_for_status()

            payload = response.json()
            # AT wraps responses in SMSMessageData; check for delivery errors.
            recipients = payload.get("SMSMessageData", {}).get("Recipients", [])
            if not recipients:
                raise SMSDeliveryError(
                    "Africa's Talking returned no recipients in response."
                )

            # AT reports per-recipient status; anything other than "Success" means
            # the carrier rejected or queued the message — treat it as a failure
            # so the caller gets a 503 rather than a silent no-op.
            recipient = recipients[0]
            status = recipient.get("status", "unknown")
            status_code = recipient.get("statusCode", "unknown")
            logger.info(
                "Africa's Talking OTP dispatch — status: %s, statusCode: %s",
                status,
                status_code,
            )
            if status != "Success":
                raise SMSDeliveryError(
                    f"Africa's Talking rejected the message "
                    f"(status: {status}, statusCode: {status_code}). "
                    "Check the AT dashboard for details."
                )

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Africa's Talking HTTP error: %s %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise SMSDeliveryError(
                f"SMS delivery failed with HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            logger.error("Africa's Talking network error: %s", type(exc).__name__)
            raise SMSDeliveryError(
                "SMS delivery failed due to a network error."
            ) from exc


# ---------------------------------------------------------------------------
# Gateway factory
# ---------------------------------------------------------------------------


def get_sms_gateway() -> SMSGateway:
    """Return the SMS gateway implementation selected by ``SMS_GATEWAY``.

    Supported values for ``SMS_GATEWAY``:
    * ``console``        — ``ConsoleSMSGateway`` (default, development/test)
    * ``africastalking`` — ``AfricasTalkingSMSGateway`` (production)

    Returns:
        An object that satisfies the ``SMSGateway`` Protocol.

    Raises:
        ValueError: If ``SMS_GATEWAY`` is set to an unknown value.
    """
    gateway_name = settings.SMS_GATEWAY.lower()

    if gateway_name == "console":
        return ConsoleSMSGateway()
    if gateway_name == "africastalking":
        return AfricasTalkingSMSGateway()

    raise ValueError(
        f"Unknown SMS_GATEWAY value: '{gateway_name}'. "
        "Supported options: 'console', 'africastalking'."
    )
