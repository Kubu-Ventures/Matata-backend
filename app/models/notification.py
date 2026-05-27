"""Notification ORM model.

Represents an outbound notification dispatched to either an analyst
(new critical-severity report alert) or a verified reporter (photo quality
improvement request). Status tracks delivery state through the notification
worker pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import NotificationStatus, NotificationType

if TYPE_CHECKING:
    from app.models.report import Report


class Notification(TimestampMixin, Base):
    """Outbound notification record for analyst alerts and reporter requests."""

    __tablename__ = "notification"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    type: Mapped[NotificationType] = mapped_column(
        sa.Enum(NotificationType, name="notification_type_enum"), nullable=False
    )
    recipient_hash: Mapped[str] = mapped_column(sa.String, nullable=False)
    report_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("report.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        sa.Enum(NotificationStatus, name="notification_status_enum"),
        nullable=False,
        default=NotificationStatus.pending,
        server_default=NotificationStatus.pending.value,
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    report: Mapped["Report"] = relationship(
        "Report", back_populates="notifications", lazy="raise"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Notification id={self.id} type={self.type} status={self.status}>"
