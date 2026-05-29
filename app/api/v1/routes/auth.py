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

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis

from app.core.dependencies import get_redis
from app.services import auth_service
from app.services.auth_service import (
    AuthError,
    InvalidOTPError,
    InvalidTokenError,
    OTPLockedOutError,
    OTPNotFoundError,
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return await auth_service.verify_access_token(raw_token, redis)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------------------
# Role-based access control dependency factory
# ---------------------------------------------------------------------------


def require_role(*roles: Role):
    """Dependency factory that enforces role-based access control.

    Usage::

        @router.get("/analyst/reports", dependencies=[Depends(require_role(Role.analyst))])
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


def _auth_error_to_http(exc: AuthError) -> HTTPException:
    """Convert a service-layer ``AuthError`` to a FastAPI ``HTTPException``."""
    if isinstance(exc, OTPLockedOutError):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": "900"},
        )
    if isinstance(exc, (OTPNotFoundError, InvalidOTPError)):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    if isinstance(exc, InvalidTokenError):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Generic auth failure — do not leak internal detail.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication failed.",
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
) -> MessageResponse:
    """Dispatch a one-time password to the supplied phone number."""
    try:
        await auth_service.send_otp(phone_number=body.phone, redis=redis)
    except SMSDeliveryError as exc:
        logger.error("SMS delivery failure (phone suppressed): %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to send OTP. Please try again later.",
        ) from exc

    return MessageResponse(message="OTP sent successfully.", )


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
) -> TokenResponse:
    """Verify an OTP and return an access + refresh token pair."""
    try:
        access_token, refresh_token = await auth_service.verify_otp(
            phone_number=body.phone,
            otp_code=body.otp,
            redis=redis,
        )
    except AuthError as exc:
        raise _auth_error_to_http(exc) from exc

    return TokenResponse(
        token=access_token,
        refresh_token=refresh_token,
        role=Role.reporter.value,
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
) -> MessageResponse:
    """Revoke the current access token."""
    raw_token: str | None = (
        credentials.credentials if credentials else x_session_token
    )

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        await auth_service.logout(token=raw_token, redis=redis)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return MessageResponse(message="Logged out successfully.")
