"""AIFeedback ORM model — active learning signal store.

Every time an analyst makes a decision that can be compared against the AI
prediction (verify, reject, or explicit severity override), a row is written
here.  The data drives the ``GET /analyst/ai-accuracy`` endpoint and can be
used to calibrate the divergence threshold over time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


class AIFeedback(Base):
    """One analyst decision compared against the AI severity prediction."""

    __tablename__ = "ai_feedback"

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
    # 'verify' | 'reject' | 'severity_override'
    feedback_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    ai_prediction: Mapped[Optional[str]] = mapped_column(sa.String(50), nullable=True)
    analyst_decision: Mapped[Optional[str]] = mapped_column(
        sa.String(50), nullable=True
    )
    is_agreement: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)
    ai_confidence: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
        index=True,
    )

    __table_args__ = (sa.Index("ix_ai_feedback_type", "feedback_type"),)
