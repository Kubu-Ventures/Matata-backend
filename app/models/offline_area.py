"""OfflineArea ORM model.

An area whose building outlines phones save in advance, so the building
picker works there even if the phone has never been used there online.

Two kinds:
* ``home`` areas carry a ``code_prefix`` (e.g. ``KIB``). Field reporters
  whose code starts with that prefix always download the whole area.
* ``risk`` areas (flood plains, settlements prone to fire, places under an
  early warning). Phones within ``RISK_AREA_RADIUS_KM`` download them when
  on Wi-Fi.
"""

from __future__ import annotations

import uuid
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin

# Largest side of an area, in degrees (about 5.5 km). Bigger places are
# split into several areas so no single download is huge.
MAX_AREA_DEG = 0.05


class OfflineArea(TimestampMixin, Base):
    __tablename__ = "offline_area"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(sa.String(120), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="risk")
    code_prefix: Mapped[Optional[str]] = mapped_column(sa.String(8), nullable=True)
    min_lat: Mapped[float] = mapped_column(sa.Float, nullable=False)
    min_lng: Mapped[float] = mapped_column(sa.Float, nullable=False)
    max_lat: Mapped[float] = mapped_column(sa.Float, nullable=False)
    max_lng: Mapped[float] = mapped_column(sa.Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
