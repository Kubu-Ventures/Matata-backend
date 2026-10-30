"""Areas phones save for offline use, and which ones a given phone needs."""

from __future__ import annotations

from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.offline_area import MAX_AREA_DEG, OfflineArea
from app.services.field_reporter_service import code_for_subject


def validate_bbox(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> None:
    """Raise ``ValueError`` unless the box is valid and at most 0.05 degrees."""
    if not (-90 <= min_lat < max_lat <= 90 and -180 <= min_lng < max_lng <= 180):
        raise ValueError("The box must have min below max on both axes.")
    if max_lat - min_lat > MAX_AREA_DEG or max_lng - min_lng > MAX_AREA_DEG:
        raise ValueError(
            f"An area can be at most {MAX_AREA_DEG} degrees (about 5.5 km) on each "
            "side. Split bigger places into several areas."
        )


async def areas_for_subject(subject: str, db: AsyncSession) -> dict:
    """Every active area, plus the ids of this caller's home areas.

    A field reporter's home areas are those whose ``code_prefix`` matches
    the start of their code (``KIB`` for ``KIB-01``). The phone downloads
    home areas always and nearby risk areas when on Wi-Fi.
    """
    rows = (
        (
            await db.execute(
                sa.select(OfflineArea).where(OfflineArea.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    code: Optional[str] = await code_for_subject(subject, db)
    prefix = code.split("-", 1)[0] if code else None
    home: List[str] = [
        str(a.id) for a in rows if prefix and a.code_prefix and a.code_prefix == prefix
    ]
    return {
        "areas": [
            {
                "id": str(a.id),
                "name": a.name,
                "kind": a.kind,
                "bbox": [a.min_lat, a.min_lng, a.max_lat, a.max_lng],
            }
            for a in rows
        ],
        "home_area_ids": home,
    }
