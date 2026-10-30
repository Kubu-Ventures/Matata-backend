"""FieldReporter ORM model.

A field reporter is a paid community reporter registered for a pilot. Each
one gets a short code such as ``KIB-01``. When they sign in by email, every
report they send is tagged with that code so the organisation running the
pilot can count verified reports per person and pay them.

Matata never stores the person's name, phone number or email. Only the
salted email hash (same scheme as ``analyst_accounts``) and the code are
kept. The organisation running the pilot holds the list that matches codes
to people.
"""

from __future__ import annotations

import uuid
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin


class FieldReporter(TimestampMixin, Base):
    """A registered field reporter, identified only by a code."""

    __tablename__ = "field_reporters"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    code: Mapped[str] = mapped_column(
        sa.String(32),
        unique=True,
        nullable=False,
        index=True,
    )

    email_hash: Mapped[str] = mapped_column(
        sa.Text,
        unique=True,
        nullable=False,
        index=True,
    )

    created_by_sub: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        default=True,
        server_default=sa.true(),
    )
