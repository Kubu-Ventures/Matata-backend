"""SMS Gateway abstraction layer.

Defines the ``SMSGateway`` Protocol that all SMS back-ends must satisfy,
plus concrete implementations:

* ``ConsoleSMSGateway``        — prints OTP to stdout; used in dev/CI.
* ``AfricasTalkingSMSGateway`` — production SMS via Africa's Talking API.
* ``AtVoiceGateway``           — fallback: AT outbound voice call reads OTP
                                 via TTS when SMS is carrier-rejected.
* ``FallbackSMSGateway``       — tries SMS first; on failure automatically
                                 retries with the voice gateway.

A factory function ``get_sms_gateway()`` returns the correct implementation
based on the ``SMS_GATEWAY`` and ``AFRICASTALKING_VOICE_ENABLED`` settings.

Voice fallback design
---------------------
When ``AFRICASTALKING_VOICE_ENABLED=true`` the factory wraps the AT SMS
gateway in ``FallbackSMSGateway``.  If the SMS carrier rejects the message
(``SMSDeliveryError``), an outbound AT Voice call is placed to the same
number.  When the recipient answers, AT fetches
``GET {APP_PUBLIC_URL}/voice/otp/{session_id}`` and the handler returns
JSON actions that instruct AT to read the OTP digits via TTS.

The session → OTP mapping is stored in Redis under
``voice_otp:{session_id}`` with a 5-minute TTL so the plaintext OTP is
never embedded in the callback URL.
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol, runtime_checkable

import httpx
import redis as sync_redis

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
# AtVoiceGateway — voice call OTP via Africa's Talking Voice API
# ---------------------------------------------------------------------------

_AT_VOICE_URL = "https://voice.africastalking.com/call"
_VOICE_OTP_TTL = 300  # 5 minutes, matching OTP validity window


def _digits_spaced(otp_code: str) -> str:
    """Return OTP digits separated by pauses for clear TTS reading.

    '203153' → '2. 0. 3. 1. 5. 3.'
    """
    return ". ".join(otp_code) + "."


class AtVoiceGateway:
    """OTP delivery via AT outbound voice call with TTS readout.

    When called, AT phones the recipient.  On answer AT fetches the
    callback URL and reads the OTP aloud via text-to-speech.

    Requires:
        ``AFRICASTALKING_API_KEY``, ``AFRICASTALKING_USERNAME``,
        ``AFRICASTALKING_VOICE_NUMBER`` (AT-assigned virtual number),
        ``APP_PUBLIC_URL`` (publicly reachable base URL of this server).
    """

    def __init__(self) -> None:
        self._api_key = settings.AFRICASTALKING_API_KEY
        self._username = settings.AFRICASTALKING_USERNAME
        self._from_number = settings.AFRICASTALKING_VOICE_NUMBER
        self._app_url = settings.APP_PUBLIC_URL.rstrip("/")
        self._redis = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)

        if not self._from_number:
            raise SMSDeliveryError(
                "AFRICASTALKING_VOICE_NUMBER must be set when voice fallback is enabled."
            )
        if not self._app_url:
            raise SMSDeliveryError(
                "APP_PUBLIC_URL must be set when voice fallback is enabled."
            )

    def send_otp(self, phone_number: str, otp_code: str) -> None:
        """Place an outbound AT Voice call that reads *otp_code* via TTS.

        Stores the OTP in Redis under ``voice_otp:{session_id}`` with a
        5-minute TTL so the callback handler can retrieve it without
        embedding any sensitive data in the callback URL.

        Args:
            phone_number: E.164-formatted destination number.
            otp_code:     Six-digit OTP string.

        Raises:
            SMSDeliveryError: If the AT Voice API rejects the call request.
        """
        session_id = str(uuid.uuid4())
        self._redis.setex(f"voice_otp:{session_id}", _VOICE_OTP_TTL, otp_code)

        callback_url = f"{self._app_url}/voice/otp/{session_id}"

        try:
            response = httpx.post(
                _AT_VOICE_URL,
                headers={
                    "apiKey": self._api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "username": self._username,
                    "from": self._from_number,
                    "to": phone_number,
                    "callbackUrl": callback_url,
                },
                timeout=15.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "AT Voice API HTTP error: %s %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise SMSDeliveryError(
                f"Voice OTP call failed with HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            logger.error("AT Voice network error: %s", type(exc).__name__)
            raise SMSDeliveryError(
                "Voice OTP call failed due to a network error."
            ) from exc

        payload = response.json()
        entries = payload.get("entries", [])
        if not entries:
            raise SMSDeliveryError("AT Voice API returned no call entries.")

        call_status = entries[0].get("status", "unknown")
        if call_status not in ("Queued", "Success"):
            raise SMSDeliveryError(
                f"AT Voice call not queued (status: {call_status})."
            )

        logger.info("AT Voice OTP call queued (status: %s)", call_status)


# ---------------------------------------------------------------------------
# FallbackSMSGateway — SMS first, voice on carrier rejection
# ---------------------------------------------------------------------------


class FallbackSMSGateway:
    """Tries *primary* first; on ``SMSDeliveryError`` falls back to *fallback*.

    Used to wrap ``AfricasTalkingSMSGateway`` with ``AtVoiceGateway`` so
    that a carrier rejection automatically triggers a voice call without any
    change to the calling code.
    """

    def __init__(self, primary: SMSGateway, fallback: SMSGateway) -> None:
        self._primary = primary
        self._fallback = fallback

    def send_otp(self, phone_number: str, otp_code: str) -> None:
        try:
            self._primary.send_otp(phone_number, otp_code)
        except SMSDeliveryError as exc:
            logger.warning(
                "SMS gateway failed (%s); attempting voice fallback.", exc
            )
            self._fallback.send_otp(phone_number, otp_code)


# ---------------------------------------------------------------------------
# Gateway factory
# ---------------------------------------------------------------------------


def get_sms_gateway() -> SMSGateway:
    """Return the SMS gateway selected by ``SMS_GATEWAY``.

    When ``SMS_GATEWAY=africastalking`` and ``AFRICASTALKING_VOICE_ENABLED=true``
    the AT SMS gateway is wrapped in ``FallbackSMSGateway`` with ``AtVoiceGateway``
    so that a carrier rejection automatically retries via voice call.

    Supported values for ``SMS_GATEWAY``:
    * ``console``        — ``ConsoleSMSGateway`` (default, development/test)
    * ``africastalking`` — ``AfricasTalkingSMSGateway`` (+ voice fallback if enabled)

    Returns:
        An object that satisfies the ``SMSGateway`` Protocol.

    Raises:
        ValueError: If ``SMS_GATEWAY`` is set to an unknown value.
    """
    gateway_name = settings.SMS_GATEWAY.lower()

    if gateway_name == "console":
        return ConsoleSMSGateway()

    if gateway_name == "africastalking":
        sms = AfricasTalkingSMSGateway()
        if settings.AFRICASTALKING_VOICE_ENABLED:
            return FallbackSMSGateway(primary=sms, fallback=AtVoiceGateway())
        return sms

    raise ValueError(
        f"Unknown SMS_GATEWAY value: '{gateway_name}'. "
        "Supported options: 'console', 'africastalking'."
    )
