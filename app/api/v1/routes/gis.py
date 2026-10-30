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

import gzip
import json
import logging
from dataclasses import asdict
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.api.v1.routes.auth import get_current_user
from app.core.dependencies import get_db, get_redis, get_sync_db
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


# ---------------------------------------------------------------------------
# GET /gis/health-facilities/nearby
# ---------------------------------------------------------------------------


class HealthFacilityResponse(BaseModel):
    name: Optional[str]
    facility_type: str
    lat: float
    lng: float
    distance_m: float
    phone: Optional[str] = None
    opening_hours: Optional[str] = None


class NearbyHealthFacilitiesResponse(BaseModel):
    items: List[HealthFacilityResponse]
    attribution: str = "© OpenStreetMap contributors"


@router.get(
    "/health-facilities/nearby",
    response_model=NearbyHealthFacilitiesResponse,
    summary="Nearest health facilities",
    description=(
        "Returns the hospitals, clinics and health centres nearest to a "
        "point (within 5 km, nearest first), from OpenStreetMap data loaded "
        "with ``python -m app.cli.import_health_facilities``. Used on the "
        "report form when someone needs medical help. Public data, so no "
        "login is needed. Opening times may be missing or out of date."
    ),
)
async def nearby_health_facilities(
    lat: float = Query(..., ge=-90.0, le=90.0, description="WGS84 latitude."),
    lng: float = Query(..., ge=-180.0, le=180.0, description="WGS84 longitude."),
    limit: int = Query(default=3, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
) -> NearbyHealthFacilitiesResponse:
    """Return the health facilities closest to (lat, lng)."""
    from app.services.health_facility_service import nearest_facilities

    found = await nearest_facilities(db, lat=lat, lng=lng, limit=limit)
    return NearbyHealthFacilitiesResponse(
        items=[HealthFacilityResponse(**asdict(f)) for f in found]
    )


# ---------------------------------------------------------------------------
# Offline downloads — saved on the phone so the form works without a network
# ---------------------------------------------------------------------------

# Largest box the buildings download accepts, in degrees (about 1.1 km).
_MAX_BBOX_DEG = 0.011


def _json_response(request: Request, payload: dict, max_age: int) -> Response:
    """JSON, gzipped when the client accepts it, cacheable for *max_age* s."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Cache-Control": f"public, max-age={max_age}", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", "") and len(body) > 1024:
        body = gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return Response(content=body, media_type="application/json", headers=headers)


@router.get(
    "/health-facilities/all",
    summary="Every health facility, for offline use",
    description=(
        "The full health facility list as compact rows "
        "``[name, facility_type, lat, lng, phone]``. The app saves it on the "
        "phone so the nearest clinics can be shown without a connection. "
        "No login needed."
    ),
)
async def all_health_facilities(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    from app.services.health_facility_service import all_facilities

    rows = await all_facilities(db)
    return _json_response(
        request,
        {
            "fields": ["name", "facility_type", "lat", "lng", "phone"],
            "items": rows,
            "attribution": "© OpenStreetMap contributors",
        },
        max_age=86400,
    )


@router.get(
    "/buildings",
    summary="Building footprints in a small area, for offline use",
    description=(
        "Footprints whose centroid lies in the box, as rows "
        "``[building_id, external_id, footprint_geojson]``. The app saves "
        "them so the building picker works without a connection. The box "
        "may be at most 0.011 degrees (about 1.1 km) on each side. "
        "``truncated`` is true when the 20,000-building cap was hit."
    ),
)
async def buildings_in_area(
    request: Request,
    min_lat: float = Query(..., ge=-90.0, le=90.0),
    min_lng: float = Query(..., ge=-180.0, le=180.0),
    max_lat: float = Query(..., ge=-90.0, le=90.0),
    max_lng: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_sync_db),
) -> Response:
    if max_lat <= min_lat or max_lng <= min_lng:
        raise HTTPException(status_code=422, detail="The box is empty.")
    if max_lat - min_lat > _MAX_BBOX_DEG or max_lng - min_lng > _MAX_BBOX_DEG:
        raise HTTPException(status_code=422, detail="The box is too large.")

    limit = 20000
    rows = GISService(db).buildings_in_bbox(
        min_lat, min_lng, max_lat, max_lng, limit=limit
    )
    return _json_response(
        request,
        {
            "fields": ["building_id", "external_id", "footprint_geojson"],
            "items": rows,
            "truncated": len(rows) >= limit,
        },
        max_age=3600,
    )


@router.get(
    "/offline-areas",
    summary="Areas to save on the phone in advance",
    description=(
        "Every active offline area as ``{id, name, kind, bbox}`` with bbox "
        "``[min_lat, min_lng, max_lat, max_lng]``, plus ``home_area_ids``: "
        "the areas the caller must always keep (a field reporter's home "
        "area, matched by code prefix). The app downloads home areas always "
        "and nearby risk areas on Wi-Fi, so the building picker works there "
        "offline even if the phone was never used there online."
    ),
)
async def offline_areas(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services.offline_area_service import areas_for_subject

    return await areas_for_subject(current_user.get("sub", ""), db)
