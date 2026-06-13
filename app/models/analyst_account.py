"""AnalystAccount ORM model.

Stores provisioned analyst, responder, and admin identities as hashed phone
numbers.  No plaintext phone numbers are ever persisted — only the SHA-256
hash salted with PHONE_HASH_SALT, identical to the reporter identity model.

When a provisioned user completes the standard OTP flow (POST /auth/otp/send
+ POST /auth/otp/verify), auth_service looks up their phone hash here and
issues a JWT with the stored elevated role instead of the default reporter
role.  No separate login endpoint is required.
"""

from __future__ import annotations

import uuid
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin


class AnalystAccount(TimestampMixin, Base):
    """A provisioned analyst, responder, or admin identity."""

    __tablename__ = "analyst_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    phone_hash: Mapped[str] = mapped_column(
        sa.Text,
        unique=True,
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(
        sa.Text,
        nullable=False,
    )

    region_geojson: Mapped[Optional[str]] = mapped_column(
        sa.Text,
        nullable=True,
    )

    created_by_sub: Mapped[str] = mapped_column(
        sa.Text,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        default=True,
        server_default=sa.true(),
    )
