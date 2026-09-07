"""Authentication route handlers.

All routes in this module are registered under the ``/api/v1/auth`` prefix.
Route handlers are intentionally thin: they validate request shapes, call
``auth_service``, and map service exceptions to appropriate HTTP responses.

No Redis operations, hashing, or JWT construction appear here — all of that
logic lives in ``app/services/auth_service.py``.

OpenAPI documentation
---------------------
Every endpoint is annotated with ``summary``, ``description``, and
``response_model`` so that the generated ``/docs`` schema is accurate and
self-describing for the frontend and third-party integrators.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, get_redis
from app.core.i18n import LocalisedHTTPException, get_locale, get_message
from app.core.rate_limits import (
    RATE_PRIVY_VERIFY_LIMIT,
    RATE_PRIVY_VERIFY_WINDOW_SECONDS,
)
from app.services import auth_service
from app.services.auth_service import (
    AuthError,
    InvalidOTPError,
    InvalidTokenError,
    OTPLockedOutError,
    OTPNotFoundError,
    RateLimitedError,
    Role,
)
from app.services.sms import SMSDeliveryError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])

# ---------------------------------------------------------------------------
# Security scheme (used by ``get_current_user`` dependency)
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)

# E.164 phone number pattern (international format)
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


# ---------------------------------------------------------------------------
# Request / response Pydantic models
# ---------------------------------------------------------------------------


class OTPSendRequest(BaseModel):
    phone: str = Field(
        ...,
        description="E.164-formatted phone number, e.g. +254700123456.",
        examples=["+254700123456"],
    )

    @field_validator("phone")
    @classmethod
    def validate_e164(cls, value: str) -> str:
        if not _E164_RE.match(value):
            raise ValueError(
                "Phone number must be in E.164 format, e.g. +254700123456."
            )
        return value


class OTPVerifyRequest(BaseModel):
    phone: str = Field(..., description="E.164-formatted phone number.")
    otp: str = Field(
        ...,
        min_length=6,
        max_length=6,
        pattern=r"^\d{6}$",
        description="Six-digit OTP received via SMS.",
    )

    @field_validator("phone")
    @classmethod
    def validate_e164(cls, value: str) -> str:
        if not _E164_RE.match(value):
            raise ValueError("Phone number must be in E.164 format.")
        return value


class PrivyVerifyRequest(BaseModel):
    privy_token: str = Field(
        ...,
        description=(
            "Privy access token (ES256 JWT) obtained from the frontend's "
            "email OTP login via Privy's useLoginWithEmail hook."
        ),
    )
    identity_token: str | None = Field(
        default=None,
        description=(
            "Privy identity token (ES256 JWT). Carries the verified email "
            "address, which is used to resolve a provisioned elevated role. "
            "Omit for a plain reporter login."
        ),
    )


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="Opaque refresh token.")


class AnonymousTokenResponse(BaseModel):
    session_token: str = Field(..., description="Short-lived anonymous JWT.")


class TokenResponse(BaseModel):
    token: str = Field(..., description="Signed JWT access token.")
    refresh_token: str = Field(..., description="Opaque refresh token for rotation.")
    role: str = Field(..., description="Role claim embedded in the JWT.")


class AccessTokenResponse(BaseModel):
    token: str = Field(..., description="New signed JWT access token.")
    refresh_token: str = Field(..., description="New opaque refresh token.")


class MessageResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Shared dependency — resolve current user from Authorization header
# ---------------------------------------------------------------------------


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    redis: Redis = Depends(get_redis),
    lang: str = Depends(get_locale),
) -> dict:
    """FastAPI dependency that resolves and validates the caller's JWT.

    Accepts tokens from either:
    * ``Authorization: Bearer <token>`` (standard OAuth2 bearer)
    * ``X-Session-Token: <token>``      (anonymous session fallback)

    Returns:
        Decoded JWT payload dictionary.

    Raises:
        HTTPException 401: If no token is present or the token is invalid.
    """
    raw_token: str | None = None

    if credentials:
        raw_token = credentials.credentials
    elif x_session_token:
        raw_token = x_session_token

    if not raw_token:
        raise LocalisedHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message_key="errors.auth_required",
            lang=lang,
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return await auth_service.verify_access_token(raw_token, redis)
    except InvalidTokenError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message_key="errors.auth_required",
            lang=lang,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------------------
# Role-based access control dependency factory
# ---------------------------------------------------------------------------


def require_role(*roles: Role):
    """Dependency factory that enforces role-based access control.

    Usage::

        @router.get(
            "/analyst/reports",
            dependencies=[Depends(require_role(Role.analyst))],
        )
        async def list_reports(): ...

    Args:
        *roles: One or more permitted ``Role`` values.

    Returns:
        A FastAPI dependency that raises HTTP 403 if the caller's role is not
        in the permitted set.
    """

    async def _check_role(
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        caller_role = current_user.get("role")
        permitted = {r.value for r in roles}
        if caller_role not in permitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"This endpoint requires one of the following roles: "
                    f"{', '.join(sorted(permitted))}."
                ),
            )
        return current_user

    return _check_role


# ---------------------------------------------------------------------------
# Helper — map AuthError subclasses to HTTP status codes
# ---------------------------------------------------------------------------


def _auth_error_to_http(exc: AuthError, lang: str = "en") -> HTTPException:
    """Convert a service-layer ``AuthError`` to a localised ``HTTPException``."""
    if isinstance(exc, OTPLockedOutError):
        return LocalisedHTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message_key="errors.otp_locked_out",
            lang=lang,
            headers={"Retry-After": "900"},
            minutes=15,
        )
    if isinstance(exc, (OTPNotFoundError, InvalidOTPError)):
        return LocalisedHTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            message_key="errors.otp_invalid",
            lang=lang,
        )
    if isinstance(exc, RateLimitedError):
        return LocalisedHTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message_key="errors.rate_limit_exceeded",
            lang=lang,
            headers={"Retry-After": str(RATE_PRIVY_VERIFY_WINDOW_SECONDS)},
        )
    if isinstance(exc, InvalidTokenError):
        return LocalisedHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message_key="errors.auth_required",
            lang=lang,
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Generic auth failure — do not leak internal detail.
    return LocalisedHTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        message_key="errors.auth_required",
        lang=lang,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/anonymous",
    response_model=AnonymousTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Issue anonymous session token",
    description=(
        "Issues a short-lived JWT with role ``anonymous_reporter``. "
        "No request body is required. No personally identifiable information "
        "is collected or stored."
    ),
)
async def issue_anonymous_token() -> AnonymousTokenResponse:
    """Issue an anonymous session token (zero-friction, no registration)."""
    token = await auth_service.issue_anonymous_token()
    return AnonymousTokenResponse(session_token=token)


@router.post(
    "/otp/send",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Send OTP to phone number",
    description=(
        "Hashes the provided phone number and dispatches a 6-digit OTP via SMS. "
        "The plaintext phone number is discarded immediately after dispatch and is "
        "never stored."
    ),
)
async def send_otp(
    body: OTPSendRequest,
    redis: Redis = Depends(get_redis),
    lang: str = Depends(get_locale),
) -> MessageResponse:
    """Dispatch a one-time password to the supplied phone number."""
    try:
        await auth_service.send_otp(phone_number=body.phone, redis=redis)
    except SMSDeliveryError as exc:
        logger.error("SMS delivery failure (phone suppressed): %s", type(exc).__name__)
        raise LocalisedHTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            message_key="errors.sms_failed",
            lang=lang,
        ) from exc

    return MessageResponse(message=get_message("info.otp_sent", lang))


@router.post(
    "/otp/verify",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify OTP and issue JWT",
    description=(
        "Validates the 6-digit OTP against the stored value. On success, issues a "
        "signed JWT with role ``reporter`` and an opaque refresh token. After 5 "
        "consecutive failures within 15 minutes the endpoint returns HTTP 429."
    ),
)
async def verify_otp(
    body: OTPVerifyRequest,
    redis: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    lang: str = Depends(get_locale),
) -> TokenResponse:
    """Verify an OTP and return an access + refresh token pair.

    For provisioned analysts, responders, and admins the returned JWT carries
    the elevated role stored in analyst_accounts.  All other callers receive
    role=reporter.
    """
    try:
        access_token, refresh_token, role = await auth_service.verify_otp(
            phone_number=body.phone,
            otp_code=body.otp,
            redis=redis,
            db=db,
        )
    except AuthError as exc:
        raise _auth_error_to_http(exc, lang) from exc

    return TokenResponse(
        token=access_token,
        refresh_token=refresh_token,
        role=role.value if hasattr(role, "value") else str(role),
    )


@router.post(
    "/privy/verify",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify Privy tokens and issue JWT",
    description=(
        "Exchanges the tokens from a completed Privy email OTP login for our own "
        "signed JWT + opaque refresh token. Send the Privy access token as "
        "``privy_token``; include the Privy identity token as ``identity_token`` so "
        "a provisioned analyst/responder/admin email resolves to its stored elevated "
        "role (otherwise the caller receives ``role=reporter``). Returns the same "
        "``{ token, refresh_token, role }`` shape as ``/auth/otp/verify``. An "
        "expired, tampered, or wrong-audience Privy token returns HTTP 401. The "
        "endpoint is rate limited per client IP."
    ),
)
async def verify_privy(
    body: PrivyVerifyRequest,
    request: Request,
    redis: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    lang: str = Depends(get_locale),
) -> TokenResponse:
    """Verify Privy access + identity tokens and return an access + refresh pair."""
    client_ip = request.client.host if request.client else "unknown"
    try:
        await auth_service.check_rate_limit(
            redis,
            bucket=f"privy_verify:{client_ip}",
            limit=RATE_PRIVY_VERIFY_LIMIT,
            window_seconds=RATE_PRIVY_VERIFY_WINDOW_SECONDS,
        )
        access_token, refresh_token, role = (
            await auth_service.verify_privy_and_issue_tokens(
                privy_token=body.privy_token,
                identity_token=body.identity_token,
                redis=redis,
                db=db,
            )
        )
    except AuthError as exc:
        raise _auth_error_to_http(exc, lang) from exc

    return TokenResponse(
        token=access_token,
        refresh_token=refresh_token,
        role=role.value if hasattr(role, "value") else str(role),
    )


@router.post(
    "/refresh",
    response_model=AccessTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate refresh token",
    description=(
        "Invalidates the supplied refresh token and issues a new access token + "
        "refresh token pair. Each refresh token is single-use."
    ),
)
async def refresh_token(
    body: RefreshRequest,
    redis: Redis = Depends(get_redis),
) -> AccessTokenResponse:
    """Rotate a refresh token and return a new token pair."""
    try:
        new_access, new_refresh = await auth_service.rotate_refresh_token(
            refresh_token=body.refresh_token,
            redis=redis,
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return AccessTokenResponse(token=new_access, refresh_token=new_refresh)


@router.delete(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Logout — revoke current token",
    description=(
        "Adds the current JWT to a Redis denylist with a TTL equal to the "
        "token's remaining lifetime, effectively invalidating it immediately."
    ),
)
async def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    redis: Redis = Depends(get_redis),
    lang: str = Depends(get_locale),
) -> MessageResponse:
    """Revoke the current access token."""
    raw_token: str | None = credentials.credentials if credentials else x_session_token

    if not raw_token:
        raise LocalisedHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message_key="errors.auth_required",
            lang=lang,
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        await auth_service.logout(token=raw_token, redis=redis)
    except InvalidTokenError as exc:
        raise LocalisedHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message_key="errors.auth_required",
            lang=lang,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return MessageResponse(message=get_message("info.logged_out", lang))
