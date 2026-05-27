"""Declarative base and shared mixins.

All domain models must be imported at the bottom of this module so that
Alembic autogenerate can detect them. Add new model imports here as they
are created.
"""

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ── Model imports for Alembic autogenerate detection ─────────────────────────
# Each import must remain even if the symbol is not used directly here.
from app.models.analyst_note import AnalystNote  # noqa: E402, F401
from app.models.audit_log import AuditLog  # noqa: E402, F401
from app.models.building import Building  # noqa: E402, F401
from app.models.notification import Notification  # noqa: E402, F401
from app.models.report import Report  # noqa: E402, F401
