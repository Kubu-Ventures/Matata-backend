"""Find the health facilities nearest to a point.

Uses a bounding-box query followed by an exact great-circle distance in
Python, so it works on both PostgreSQL and the SQLite test database. The
table holds a few hundred rows per city, so this is fast enough without
PostGIS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health_facility import HealthFacility

_EARTH_RADIUS_M = 6_371_000.0
_METRES_PER_DEGREE_LAT = 111_320.0


@dataclass
class NearbyFacility:
    name: Optional[str]
    facility_type: str
    lat: float
    lng: float
    distance_m: float
    phone: Optional[str] = None
    opening_hours: Optional[str] = None


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


async def nearest_facilities(
    db: AsyncSession,
    *,
    lat: float,
    lng: float,
    limit: int = 3,
    radius_m: float = 5000.0,
) -> List[NearbyFacility]:
    """Return up to *limit* facilities within *radius_m*, nearest first."""
    dlat = radius_m / _METRES_PER_DEGREE_LAT
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    dlng = radius_m / (_METRES_PER_DEGREE_LAT * cos_lat)

    result = await db.execute(
        sa.select(HealthFacility).where(
            HealthFacility.lat.between(lat - dlat, lat + dlat),
            HealthFacility.lng.between(lng - dlng, lng + dlng),
        )
    )
    found = []
    for f in result.scalars().all():
        distance = haversine_m(lat, lng, f.lat, f.lng)
        if distance <= radius_m:
            found.append(
                NearbyFacility(
                    name=f.name,
                    facility_type=f.facility_type,
                    lat=f.lat,
                    lng=f.lng,
                    distance_m=round(distance, 1),
                    phone=f.phone,
                    opening_hours=f.opening_hours,
                )
            )
    found.sort(key=lambda f: f.distance_m)
    return found[:limit]


async def all_facilities(db: AsyncSession) -> List[list]:
    """Every facility as ``[name, facility_type, lat, lng, phone]``.

    Compact on purpose: the phone saves the whole list (a few thousand rows
    for Kenya) so the nearest clinics can be shown without a connection.
    """
    result = await db.execute(
        sa.select(
            HealthFacility.name,
            HealthFacility.facility_type,
            HealthFacility.lat,
            HealthFacility.lng,
            HealthFacility.phone,
        )
    )
    return [
        [name, ftype, round(lat, 6), round(lng, 6), phone]
        for name, ftype, lat, lng, phone in result.all()
    ]
