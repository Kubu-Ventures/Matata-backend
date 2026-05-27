"""Building ORM model.

Represents a physical structure matched from the Microsoft Africa Building
Footprints dataset, OSM, or manual entry. Multiple Reports may reference a
single Building, enabling damage timeline analysis across events.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
from uuid import UUID

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import BuildingSource, DamageSeverity

if TYPE_CHECKING:
    from app.models.report import Report


class Building(TimestampMixin, Base):
    """Persistent record of a physical structure.

    The centroid is derived and stored at write time for query performance —
    avoids repeated ST_Centroid calls on the polygon geometry.
    """

    __tablename__ = "building"

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    footprint: Mapped[bytes] = mapped_column(
        Geometry("POLYGON", srid=4326), nullable=False
    )
    centroid: Mapped[bytes] = mapped_column(
        Geometry("POINT", srid=4326), nullable=False
    )
    source: Mapped[BuildingSource] = mapped_column(
        sa.Enum(BuildingSource, name="building_source_enum"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(sa.String, nullable=False, unique=True)
    current_severity: Mapped[DamageSeverity] = mapped_column(
        sa.Enum(DamageSeverity, name="damage_severity_enum"),
        nullable=False,
        default=DamageSeverity.none,
        server_default=DamageSeverity.none.value,
    )
    last_report_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    # ── Relationships ────────────────────────────────────────────────────────
    reports: Mapped[List["Report"]] = relationship(
        "Report", back_populates="building", lazy="raise"
    )

    # ── Indexes ──────────────────────────────────────────────────────────────
    __table_args__ = (
        sa.Index("ix_building_footprint", "footprint", postgresql_using="gist"),
        sa.Index("ix_building_centroid", "centroid", postgresql_using="gist"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Building id={self.id} source={self.source} severity={self.current_severity}>"
