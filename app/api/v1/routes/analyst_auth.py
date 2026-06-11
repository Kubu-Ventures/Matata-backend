"""Analyst authentication route handlers — Supabase Auth integration.

All routes are registered under the ``/api/v1/auth/analyst`` prefix.

Why Supabase?
-------------
Analyst accounts require proper email + password login, password reset,
and invite-based onboarding. Supabase Auth provides all of this as a
SOC 2 Type 2 certified, GDPR-compliant service. No analyst credentials
or PII are stored in the CrisisMap database — only an opaque hashed
identifier is embedded in the issued CrisisMap JWT.

Flow
----
Invite (admin only):
    POST /api/v1/auth/analyst/invite
      → Creates account in Supabase, sends invite email to analyst
      → Analyst clicks email link, sets password
      → No further admin action needed

Login:
    POST /api/v1/auth/analyst/login
      → Calls Supabase Auth with email + password
      → Backend verifies Supabase JWT, reads crisismap_role from user_metadata
      → Issues a standard CrisisMap JWT + refresh token
      → Frontend uses these tokens identically to reporter tokens

Refresh / logout:
    Use the existing /api/v1/auth/refresh and /api/v1/auth/logout endpoints
    — no changes required.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis

from app.api.v1.routes.auth import require_role
from app.core.dependencies import get_redis
from app.services import auth_service
from app.services.auth_service import Role
from app.services.supabase_service import (
    SupabaseAuthError,
    SupabaseInvalidCredentialsError,
    SupabaseNotConfiguredError,
    extract_crisismap_claims,
    invite_analyst,
    sign_in,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/analyst", tags=["Analyst Authentication"])

_ELEVATED_ROLES = {Role.analyst.value, Role.responder.value, Role.admin.value}


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AnalystLoginRequest(BaseModel):
    email: str = Field(..., description="Analyst Matata email address.")
    password: str = Field(
        ...,
        min_length=8,
        description="Account password (minimum 8 characters).",
    )


class AnalystInviteRequest(BaseModel):
    email: str = Field(..., description="Email address for the new account.")
    role: str = Field(
        default="analyst",
        description="CrisisMap role to assign: analyst | responder | admin.",
    )
    region_geojson: Optional[str] = Field(
        default=None,
        description=(
            "GeoJSON string defining the responder's geographic scope. "
            "Required for role=responder; ignored for analyst and admin."
        ),
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in _ELEVATED_ROLES:
            raise ValueError(
                f"Invalid role '{value}'. "
                f"Must be one of: {', '.join(sorted(_ELEVATED_ROLES))}."
            )
        return value


class AnalystTokenResponse(BaseModel):
    token: str = Field(..., description="CrisisMap JWT access token.")
    refresh_token: str = Field(..., description="Opaque refresh token.")
    role: str = Field(..., description="CrisisMap role embedded in the token.")


class InviteResponse(BaseModel):
    message: str
    email: str
    role: str


# ---------------------------------------------------------------------------
# POST /auth/analyst/login
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=AnalystTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyst login via Supabase Auth",
    description=(
        "Authenticates an analyst using Supabase Auth (email + password). "
        "On success, issues a standard CrisisMap JWT and refresh token that "
        "work identically to reporter tokens across all protected endpoints. "
        "The CrisisMap role is read from the analyst's Supabase user_metadata — "
        "set when the account is created via POST /auth/analyst/invite. "
        "Use POST /auth/refresh to rotate the token and DELETE /auth/logout "
        "to revoke it."
    ),
    responses={
        200: {"description": "Login successful — CrisisMap tokens returned."},
        401: {"description": "Invalid email or password."},
        403: {"description": "Account exists but has no CrisisMap role assigned."},
        503: {"description": "Supabase Auth is not configured or unavailable."},
    },
)
async def analyst_login(
    body: AnalystLoginRequest,
    redis: Redis = Depends(get_redis),
) -> AnalystTokenResponse:
    """Authenticate analyst via Supabase and issue CrisisMap tokens."""

    # ── Call Supabase Auth ────────────────────────────────────────────────────
    try:
        supabase_response = await sign_in(email=body.email, password=body.password)
    except SupabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analyst authentication is not configured on this server.",
        ) from exc
    except SupabaseInvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except SupabaseAuthError as exc:
        logger.error("supabase_login_error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is temporarily unavailable.",
        ) from exc

    # ── Extract CrisisMap role from Supabase JWT ──────────────────────────────
    supabase_token = supabase_response.get("access_token", "")
    try:
        crisismap_role_str, region_geojson = extract_crisismap_claims(supabase_token)
    except SupabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analyst authentication is not configured on this server.",
        ) from exc
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    try:
        role = Role(crisismap_role_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Unknown CrisisMap role '{crisismap_role_str}'. "
                "Contact your administrator."
            ),
        )

    # ── Issue CrisisMap tokens ────────────────────────────────────────────────
    access_token, refresh_token = await auth_service.issue_analyst_token(
        email=body.email,
        role=role,
        redis=redis,
    )

    logger.info("analyst_login_success role=%s", role.value)
    return AnalystTokenResponse(
        token=access_token,
        refresh_token=refresh_token,
        role=role.value,
    )


# ---------------------------------------------------------------------------
# POST /auth/analyst/invite
# ---------------------------------------------------------------------------


@router.post(
    "/invite",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a new analyst (admin only)",
    description=(
        "Creates a new analyst account in Supabase Auth and sends an invite "
        "email to the specified address. "
        "The analyst clicks the email link to set their own password, then "
        "logs in normally via POST /auth/analyst/login. "
        "The CrisisMap role (and optional region_geojson for responders) are "
        "stored in Supabase user_metadata using the service role key — "
        "the analyst cannot modify these values. "
        "Requires admin role."
    ),
    responses={
        201: {"description": "Invite sent — analyst will receive an email."},
        400: {"description": "Invalid role value."},
        403: {"description": "Caller does not have admin role."},
        503: {"description": "Supabase Auth is not configured or unavailable."},
    },
)
async def invite_analyst_account(
    body: AnalystInviteRequest,
    current_user: dict = Depends(require_role(Role.admin)),
) -> InviteResponse:
    """Create an analyst account in Supabase and send an invite email."""
    try:
        await invite_analyst(
            email=body.email,
            crisismap_role=body.role,
            region_geojson=body.region_geojson,
        )
    except SupabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analyst authentication is not configured on this server.",
        ) from exc
    except SupabaseAuthError as exc:
        logger.error("supabase_invite_error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    logger.info("analyst_invite_sent role=%s", body.role)
    return InviteResponse(
        message=(
            "Invite email sent. "
            "The analyst will receive instructions to set their password."
        ),
        email=body.email,
        role=body.role,
    )
