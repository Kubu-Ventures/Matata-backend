"""HealthFacility ORM model.

Hospitals, clinics and health centres imported from OpenStreetMap, used to
show a reporter the nearest places to refer someone who needs medical help.
OSM data is ODbL-licensed; anything that shows these records must credit
"© OpenStreetMap contributors".
"""

from __future__ import annotations

import uuid
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin


class HealthFacility(TimestampMixin, Base):
    """A health facility point."""

    __tablename__ = "health_facility"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # e.g. "osm:node/123" so each row links back to its source object.
    external_id: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    # hospital | clinic | health_centre | doctors
    facility_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    lat: Mapped[float] = mapped_column(sa.Float, nullable=False)
    lng: Mapped[float] = mapped_column(sa.Float, nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    opening_hours: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    source: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, default="osm", server_default="osm"
    )

    __table_args__ = (sa.Index("ix_health_facility_lat_lng", "lat", "lng"),)
