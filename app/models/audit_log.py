"""AuditLog ORM model.

Append-only record of every write operation against domain entities.
The application database user is granted no DELETE permission on this table —
enforced in the migration via REVOKE statement.

before_state is nullable to accommodate INSERT operations where no prior
state exists.
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin


class AuditLog(TimestampMixin, Base):
    """Immutable audit trail for all write operations."""

    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    operation: Mapped[str] = mapped_column(
        sa.String, nullable=False
    )  # e.g. "report.status_change"
    actor_id_hash: Mapped[str] = mapped_column(sa.String, nullable=False)
    record_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), nullable=False)
    before_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict] = mapped_column(JSONB, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog id={self.id} op={self.operation} record={self.record_id}>"
