"""AnalystNote ORM model.

Internal notes attached to a Report by a Matata analyst. Notes are never
included in any data export. Cascade delete ensures notes are
removed when their parent Report is deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.report import Report


class AnalystNote(TimestampMixin, Base):
    """Analyst-authored internal commentary on a Report."""

    __tablename__ = "analyst_note"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    report_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("report.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analyst_id_hash: Mapped[str] = mapped_column(sa.String, nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    report: Mapped["Report"] = relationship(
        "Report", back_populates="analyst_notes", lazy="raise"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AnalystNote id={self.id} report_id={self.report_id}>"
