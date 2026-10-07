"""GIS route handlers.

All routes in this module are registered under the ``/api/v1/gis`` prefix.
Route handlers are thin: they validate query parameters, call ``GISService``
via a shared synchronous session, cache the result in Redis, and return the
response.

Spec §13.5 — ``GET /gis/building/match``
----------------------------------------
Synchronous endpoint used by the frontend location step.
* Accepts ``lat``, ``lng``, ``accuracy_m`` query parameters.
* Returns ``{ building_id, footprint_geojson, confidence, distance_m,
  candidates }`` — ``candidates`` lists up to three nearest buildings so the
  client can let the reporter confirm which one they meant.
* Response is cached per ``(lat, lng)`` rounded to 5 decimal places, TTL 30 s.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.core.dependencies import get_redis, get_sync_db
from app.services.geocoding_service import get_geocoding_provider
from app.services.gis_service import BuildingMatch, GISService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gis", tags=["GIS"])

# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class BuildingCandidateResponse(BaseModel):
    building_id: UUID
    external_id: str
    distance_m: float
    footprint_geojson: str
    # Probability this is the reporter's building, given the GPS accuracy.
    probability: float = 0.0


class BuildingMatchResponse(BaseModel):
    building_id: Optional[UUID]
    footprint_geojson: Optional[str]
    confidence: float
    distance_m: Optional[float]
    candidates: List[BuildingCandidateResponse] = []


# ---------------------------------------------------------------------------
# Cache key helper
# ---------------------------------------------------------------------------

_CACHE_TTL_S = 30


def _cache_key(lat: float, lng: float) -> str:
    return f"gis:match:{round(lat, 5)}:{round(lng, 5)}"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/building/match",
    response_model=BuildingMatchResponse,
    summary="Match GPS coordinates to a building footprint",
    description=(
        "Returns the best-matching building footprint for the supplied GPS "
        "coordinates, using point-in-polygon → nearest-neighbour matching "
        "against the loaded building footprints (Microsoft or OSM), plus up "
        "to three nearest candidate buildings.  "
        "Results are cached per ``(lat, lng)`` rounded to 5 decimal places "
        "for 30 seconds."
    ),
)
async def match_building(
    lat: float = Query(..., ge=-90.0, le=90.0, description="WGS84 latitude."),
    lng: float = Query(..., ge=-180.0, le=180.0, description="WGS84 longitude."),
    accuracy_m: Optional[float] = Query(
        default=None,
        ge=0.0,
        description="Device-reported horizontal GPS accuracy in metres.",
    ),
    db: Session = Depends(get_sync_db),
    redis: Redis = Depends(get_redis),
) -> BuildingMatchResponse:
    """Return the nearest building footprint for the supplied GPS coordinates."""
    cache_key = _cache_key(lat, lng)

    # ── Cache check ──────────────────────────────────────────────────────────
    cached = await redis.get(cache_key)
    if cached:
        try:
            return BuildingMatchResponse(**json.loads(cached))
        except Exception:
            pass  # Corrupt cache entry — fall through to live query.

    # ── Live GIS query ───────────────────────────────────────────────────────
    gis = GISService(db)
    match: BuildingMatch = gis.match_building(
        lat=lat,
        lng=lng,
        accuracy_m=accuracy_m,
        geocoding_provider=get_geocoding_provider(),
        with_candidates=True,
    )

    candidates = [BuildingCandidateResponse(**asdict(c)) for c in match.candidates]
    response = BuildingMatchResponse(
        building_id=match.building_id,
        footprint_geojson=match.footprint_geojson,
        confidence=match.confidence,
        distance_m=match.distance_m,
        candidates=candidates,
    )

    # ── Cache result ─────────────────────────────────────────────────────────
    await redis.set(
        cache_key,
        json.dumps(
            {
                "building_id": str(match.building_id) if match.building_id else None,
                "footprint_geojson": match.footprint_geojson,
                "confidence": match.confidence,
                "distance_m": match.distance_m,
                "candidates": [c.model_dump(mode="json") for c in candidates],
            }
        ),
        ex=_CACHE_TTL_S,
    )

    return response
