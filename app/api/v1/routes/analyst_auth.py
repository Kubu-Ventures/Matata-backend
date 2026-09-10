"""Analyst account provisioning routes.

Analyst, responder, and admin accounts are stored as hashed email addresses in
the ``analyst_accounts`` table.  Login works through the Privy email OTP flow:

    POST /api/v1/auth/privy/verify { "privy_token": "...", "identity_token": "..." }

The verify endpoint hashes the email from the Privy identity token; if a
provisioned account exists the returned JWT carries the stored elevated role
(analyst / responder / admin) instead of the default reporter role.  No separate
login endpoint is required.

Admin-only management routes in this module:

    POST   /auth/analyst/register           — provision a new account
    DELETE /auth/analyst/accounts/{id}      — deactivate an account
    GET    /auth/analyst/accounts           — list all accounts (active only)
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routes.auth import require_role
from app.core.dependencies import get_db
from app.services import auth_service
from app.services.auth_service import Role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/analyst", tags=["Analyst Account Management"])

_ELEVATED_ROLES = {Role.analyst.value, Role.responder.value, Role.admin.value}
# Deliberately simple — reject obvious garbage, not implement RFC 5322.
# Same pattern as app/cli/create_analyst.py.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str = Field(
        ...,
        description="Email address of the analyst to provision.",
        examples=["analyst@example.org"],
    )
    role: str = Field(
        default="analyst",
        description="CrisisMap role: analyst | responder | admin.",
    )
    region_geojson: Optional[str] = Field(
        default=None,
        description=(
            "GeoJSON string defining the responder's geographic scope. "
            "Required for role=responder; ignored for analyst and admin."
        ),
    )

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError("A valid email address is required.")
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in _ELEVATED_ROLES:
            raise ValueError(
                f"Invalid role '{value}'. "
                f"Must be one of: {', '.join(sorted(_ELEVATED_ROLES))}."
            )
        return value


class AccountResponse(BaseModel):
    id: uuid.UUID
    role: str
    region_geojson: Optional[str]
    is_active: bool
    created_by_sub: str

    model_config = {"from_attributes": True}


class RegisterResponse(BaseModel):
    message: str
    account: AccountResponse


# ---------------------------------------------------------------------------
# POST /auth/analyst/register
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision an analyst account (admin only)",
    description=(
        "Registers an email address as an analyst, responder, or admin account. "
        "The email is normalised and hashed immediately and never stored in "
        "plaintext. Once registered, the user logs in through the Privy email OTP "
        "flow (POST /auth/privy/verify) and receives a JWT with the provisioned "
        "elevated role. "
        "Requires admin role."
    ),
    responses={
        201: {"description": "Account provisioned."},
        400: {"description": "Email address already registered."},
        403: {"description": "Caller does not have admin role."},
        422: {"description": "Invalid role or email format."},
    },
)
async def register_analyst(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role(Role.admin)),
) -> RegisterResponse:
    """Provision an analyst account from an admin-supplied email address."""
    try:
        account = await auth_service.register_analyst_account(
            email=body.email,
            role=Role(body.role),
            created_by_sub=current_user["sub"],
            db=db,
            region_geojson=body.region_geojson,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    logger.info(
        "analyst_account_registered role=%s by admin=%s…",
        body.role,
        current_user["sub"][:8],
    )
    return RegisterResponse(
        message=(
            "Account provisioned. "
            "The analyst can now log in via POST /auth/privy/verify."
        ),
        account=AccountResponse.model_validate(account),
    )


# ---------------------------------------------------------------------------
# DELETE /auth/analyst/accounts/{account_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_200_OK,
    summary="Deactivate an analyst account (admin only)",
    responses={
        200: {"description": "Account deactivated."},
        404: {"description": "Account not found or already inactive."},
        403: {"description": "Caller does not have admin role."},
    },
)
async def deactivate_analyst(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role(Role.admin)),
) -> dict:
    """Deactivate a provisioned account. Existing tokens remain valid until expiry."""
    deactivated = await auth_service.deactivate_analyst_account(
        account_id=str(account_id),
        db=db,
    )
    if not deactivated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active analyst account found with that ID.",
        )
    logger.info(
        "analyst_account_deactivated id=%s by admin=%s…",
        account_id,
        current_user["sub"][:8],
    )
    return {"message": "Account deactivated. Existing tokens will expire naturally."}


# ---------------------------------------------------------------------------
# GET /auth/analyst/accounts
# ---------------------------------------------------------------------------


@router.get(
    "/accounts",
    response_model=List[AccountResponse],
    status_code=status.HTTP_200_OK,
    summary="List active analyst accounts (admin only)",
    responses={
        200: {"description": "List of active provisioned accounts."},
        403: {"description": "Caller does not have admin role."},
    },
)
async def list_analyst_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_role(Role.admin)),
) -> List[AccountResponse]:
    """Return all active analyst, responder, and admin accounts."""
    import sqlalchemy as sa

    from app.models.analyst_account import AnalystAccount

    result = await db.execute(
        sa.select(AnalystAccount)
        .where(AnalystAccount.is_active.is_(True))
        .order_by(AnalystAccount.created_at)
    )
    accounts = result.scalars().all()
    return [AccountResponse.model_validate(a) for a in accounts]
