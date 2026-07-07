"""Report ORM model.

A Report is a single community-submitted damage observation. It may or may
not be resolved to a Building footprint (nullable FK until the GIS worker
processes the submission). Self-referential FKs capture both confirmed and
possible duplicate relationships.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin

# Building must be imported at runtime so SQLAlchemy's mapper can resolve the
# "Building" string in relationship().  The other two are forward-reference-only
# and can stay behind TYPE_CHECKING to avoid circular import issues.
from app.models.analyst_note import AnalystNote  # noqa: F401
from app.models.building import Building  # noqa: F401
from app.models.enums import (
    CrisisType,
    ElectricityStatus,
    HealthServicesStatus,
    InfrastructureType,
    PhotoStatus,
    ReportDamageSeverity,
    ReportStatus,
    ReviewPriority,
)
from app.models.notification import Notification  # noqa: F401


class Report(TimestampMixin, Base):
    """Community-submitted infrastructure damage observation.

    The reporter's lat/lng is stored as raw floats in addition to the PostGIS
    spatial columns on Building, giving a fast composite index for proximity
    queries before the GIS worker resolves the footprint match.
    """

    __tablename__ = "report"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )

    # ── Building relationship (nullable until GIS worker resolves) ───────────
    building_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("building.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── Reporter-supplied classification ─────────────────────────────────────
    crisis_type: Mapped[CrisisType] = mapped_column(
        sa.Enum(CrisisType, name="crisis_type_enum"), nullable=False
    )
    infrastructure_type: Mapped[InfrastructureType] = mapped_column(
        sa.Enum(InfrastructureType, name="infrastructure_type_enum"), nullable=False
    )
    damage_severity: Mapped[ReportDamageSeverity] = mapped_column(
        sa.Enum(ReportDamageSeverity, name="report_damage_severity_enum"),
        nullable=False,
    )

    # ── Location ─────────────────────────────────────────────────────────────
    # Nullable: a report may carry a landmark_description instead of GPS
    # coordinates (see ReportCreateSchema.require_location) when GPS is
    # unavailable to the reporter.
    lat: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    lng: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    gps_accuracy_m: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    landmark_description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)

    # ── Optional operational detail fields ───────────────────────────────────
    electricity_status: Mapped[Optional[ElectricityStatus]] = mapped_column(
        sa.Enum(ElectricityStatus, name="electricity_status_enum"), nullable=True
    )
    health_services_status: Mapped[Optional[HealthServicesStatus]] = mapped_column(
        sa.Enum(HealthServicesStatus, name="health_services_status_enum"), nullable=True
    )
    most_pressing_needs: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    debris_clearing_needed: Mapped[Optional[bool]] = mapped_column(
        sa.Boolean, nullable=True
    )

    # ── Photo pipeline ────────────────────────────────────────────────────────
    photo_url: Mapped[Optional[str]] = mapped_column(sa.String, nullable=True)
    photo_phash: Mapped[Optional[str]] = mapped_column(sa.String(64), nullable=True)
    photo_status: Mapped[PhotoStatus] = mapped_column(
        sa.Enum(PhotoStatus, name="photo_status_enum"),
        nullable=False,
        default=PhotoStatus.pending,
        server_default=PhotoStatus.pending.value,
    )

    # ── Workflow status ───────────────────────────────────────────────────────
    status: Mapped[ReportStatus] = mapped_column(
        sa.Enum(ReportStatus, name="report_status_enum"),
        nullable=False,
        default=ReportStatus.pending,
        server_default=ReportStatus.pending.value,
        index=True,
    )

    # ── Reporter identity (anonymised) ────────────────────────────────────────
    reporter_token_hash: Mapped[str] = mapped_column(sa.String, nullable=False)
    reporter_trust_tier: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )

    # ── Offline sync ──────────────────────────────────────────────────────────
    offline_queued_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # ── AI worker results ─────────────────────────────────────────────────────
    ai_severity_prediction: Mapped[Optional[ReportDamageSeverity]] = mapped_column(
        sa.Enum(ReportDamageSeverity, name="report_damage_severity_enum"),
        nullable=True,
    )
    ai_confidence: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    ai_quality_score: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    ai_divergence: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)

    # ── Confidence-based analyst queue priority ───────────────────────────────
    # Set by the AI worker after image analysis. Drives default sort order in
    # the analyst feed so critical items surface immediately.
    # Values: critical > high > normal > low (see ReviewPriority docstring).
    review_priority: Mapped[ReviewPriority] = mapped_column(
        sa.Enum(ReviewPriority, name="review_priority_enum"),
        nullable=False,
        default=ReviewPriority.normal,
        server_default=ReviewPriority.normal.value,
        index=True,
    )

    # ── Analyst AI override ───────────────────────────────────────────────────
    # Explicit analyst correction of the AI's severity prediction.
    # Populated via POST /analyst/reports/{id}/severity-override.
    # Never touches damage_severity (reporter) or ai_severity_prediction (AI).
    analyst_severity_override: Mapped[Optional[ReportDamageSeverity]] = mapped_column(
        sa.Enum(ReportDamageSeverity, name="report_damage_severity_enum"),
        nullable=True,
    )

    # ── GIS worker results ────────────────────────────────────────────────────
    footprint_match_confidence: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )

    # ── Duplicate detection ───────────────────────────────────────────────────
    duplicate_of_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("report.id", ondelete="SET NULL"),
        nullable=True,
    )
    possible_duplicate_of_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("report.id", ondelete="SET NULL"),
        nullable=True,
    )
    duplicate_score: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    building: Mapped[Optional[Building]] = relationship(
        "Building", back_populates="reports", lazy="raise"
    )
    analyst_notes: Mapped[List["AnalystNote"]] = relationship(
        "AnalystNote",
        back_populates="report",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    notifications: Mapped[List["Notification"]] = relationship(
        "Notification", back_populates="report", lazy="raise"
    )
    duplicate_of: Mapped[Optional["Report"]] = relationship(
        "Report",
        foreign_keys=[duplicate_of_id],
        remote_side="Report.id",
        lazy="raise",
    )
    possible_duplicate_of: Mapped[Optional["Report"]] = relationship(
        "Report",
        foreign_keys=[possible_duplicate_of_id],
        remote_side="Report.id",
        lazy="raise",
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    __table_args__ = (
        sa.Index("ix_report_lat_lng", "lat", "lng"),
        sa.Index("ix_report_created_at", sa.text("created_at DESC")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Report id={self.id} status={self.status}"
            f" severity={self.damage_severity}>"
        )
