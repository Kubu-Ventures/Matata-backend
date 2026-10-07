"""GIS service — building footprint matching and geospatial layer management.

All PostGIS queries use parameterised ``text()`` expressions.  No user-supplied
coordinate values are interpolated into SQL strings under any circumstances.

This module provides:
* ``GISService`` — synchronous SQLAlchemy queries suitable for the Celery
  worker context (which cannot use async I/O).
* ``match_building`` — the public entry point called by both the Celery task
  and the synchronous ``GET /gis/building/match`` endpoint (via a shared
  ``GISService`` instance obtained from the FastAPI dependency).

Matching sequence (spec §9.2):
  1. Point-in-polygon  ``ST_Contains``          → confidence 1.0
  2. Nearest-neighbour ``ST_DWithin``            → confidence ∝ 1 − dist/radius
  3. Landmark geocoding (when GPS absent)        → confidence ≤ 0.5
  4. Unmapped structure                          → building_id = None

With ``with_candidates=True`` the result also lists up to three buildings
nearest the query point (within the same search radius), so a client can let
the reporter confirm which one they meant. In dense settlements GPS error is
often larger than the gap between buildings, and the nearest building is
then frequently the wrong one. ``confirm_building`` validates the building
the reporter picked from that list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class BuildingCandidate:
    """One of the buildings nearest a query point.

    Attributes:
        building_id:       UUID of the building.
        external_id:       Source dataset id (e.g. ``osm:way/123``).
        distance_m:        Metres from the query point to the footprint edge;
                           0.0 when the point lies inside the footprint.
        footprint_geojson: GeoJSON string of the footprint polygon.
    """

    building_id: UUID
    external_id: str
    distance_m: float
    footprint_geojson: str


@dataclass
class BuildingMatch:
    """Result returned by every matching path.

    Attributes:
        building_id:      UUID of the matched building, or ``None`` if unmapped.
        confidence:       Float in [0, 1]. 1.0 for exact polygon match.
        distance_m:       Metres from the query point to the matched centroid,
                          or ``None`` for an exact polygon match.
        footprint_geojson: GeoJSON string of the building footprint polygon,
                           or ``None`` when no match is found.
        candidates:        Up to ``MAX_CANDIDATES`` nearest buildings, nearest
                           first. Empty unless requested with
                           ``with_candidates=True``.
    """

    building_id: Optional[UUID]
    confidence: float
    distance_m: Optional[float]
    footprint_geojson: Optional[str]
    candidates: List[BuildingCandidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# GISService
# ---------------------------------------------------------------------------


MAX_CANDIDATES = 3
_LANDMARK_RADIUS_M = 100.0


class GISService:
    """Synchronous PostGIS service for building footprint matching.

    Uses a synchronous SQLAlchemy ``Session`` so it can be called from the
    Celery worker context (which runs in a regular thread, not an async event
    loop).  The FastAPI endpoint obtains a sync session via
    ``get_sync_db`` and shares the same service class.

    Args:
        db: A synchronous SQLAlchemy ``Session`` (not ``AsyncSession``).
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match_building(
        self,
        lat: Optional[float],
        lng: Optional[float],
        accuracy_m: Optional[float] = None,
        landmark_description: Optional[str] = None,
        geocoding_provider=None,
        with_candidates: bool = False,
    ) -> BuildingMatch:
        """Run the four-step matching sequence and return the best result.

        Args:
            lat:                   WGS84 latitude (may be ``None`` when GPS absent).
            lng:                   WGS84 longitude (may be ``None`` when GPS absent).
            accuracy_m:            Device-reported horizontal GPS accuracy in metres.
            landmark_description:  Free-text landmark (used when GPS absent).
            geocoding_provider:    ``GeocodingProvider`` instance; required for the
                                   landmark path.  Defaults to ``None``
                                   (skip geocoding).
            with_candidates:       Also fill ``candidates`` with the nearest
                                   buildings (one extra query per match).

        Returns:
            ``BuildingMatch`` with the best available result.
        """
        # Step 1 & 2 — GPS-based matching
        if lat is not None and lng is not None:
            search_radius_m = self._compute_search_radius(accuracy_m)

            # Step 1: point-in-polygon
            match = self._point_in_polygon(lat, lng)

            # Step 2: nearest-neighbour with dynamic radius
            if match is None:
                match = self._nearest_neighbour(lat, lng, search_radius_m)

            if match:
                if with_candidates:
                    match.candidates = self._candidates(lat, lng, search_radius_m)
                return match

        # Step 3: landmark geocoding fallback
        if landmark_description and geocoding_provider is not None:
            match = self._landmark_geocoding(
                landmark_description, geocoding_provider, with_candidates
            )
            if match:
                return match

        # Step 4: unmapped structure
        logger.info(
            "No building match found (lat=%s, lng=%s, landmark=%s)",
            lat,
            lng,
            bool(landmark_description),
        )
        return BuildingMatch(
            building_id=None,
            confidence=0.0,
            distance_m=None,
            footprint_geojson=None,
        )

    def confirm_building(
        self,
        building_id: UUID,
        lat: float,
        lng: float,
        accuracy_m: Optional[float] = None,
    ) -> Optional[BuildingMatch]:
        """Accept the building a reporter picked, if it is plausibly theirs.

        The pick is adopted only when the building exists and its footprint
        lies within the same search radius ``match_building`` would use for
        this fix — the same set the candidates were drawn from. A pick from
        anywhere else (stale form, tampered request) returns ``None`` and the
        caller falls back to normal matching.

        Returns:
            ``BuildingMatch`` with confidence 1.0, or ``None`` if rejected.
        """
        search_radius_m = self._compute_search_radius(accuracy_m)
        row = self._db.execute(
            text("""
                SELECT
                    id,
                    ST_AsGeoJSON(footprint)        AS footprint_geojson,
                    ST_Distance(
                        footprint::geography,
                        ST_SetSRID(
                            ST_Point(:lng, :lat), 4326
                        )::geography
                    )                              AS distance_m
                FROM building
                WHERE id = :building_id
                  AND ST_DWithin(
                    footprint::geography,
                    ST_SetSRID(ST_Point(:lng, :lat), 4326)::geography,
                    :radius_m
                  )
                """),
            {
                "building_id": str(building_id),
                "lat": lat,
                "lng": lng,
                "radius_m": search_radius_m,
            },
        ).fetchone()

        if row is None:
            return None

        return BuildingMatch(
            building_id=UUID(str(row.id)),
            confidence=1.0,
            distance_m=float(row.distance_m),
            footprint_geojson=row.footprint_geojson,
        )

    def update_building_severity(self, building_id: UUID) -> None:
        """Recompute and persist ``current_severity`` for a building.

        Sets ``current_severity`` to the MAX enum ordinal across all linked
        reports with a confirmed (non-null) damage_severity, and sets
        ``last_report_at`` to NOW().

        Args:
            building_id: UUID of the building to update.
        """
        # The damage_severity_enum ordinal order is:
        # none(0) < minimal(1) < partial(2) < destroyed(3)
        # PostgreSQL enum comparison uses declaration order.
        self._db.execute(
            text("""
                UPDATE building
                SET
                    current_severity = COALESCE(
                        (
                            SELECT damage_severity::text::damage_severity_enum
                            FROM report
                            WHERE building_id = :building_id
                              AND damage_severity IS NOT NULL
                            ORDER BY
                                CASE damage_severity::text
                                    WHEN 'destroyed' THEN 3
                                    WHEN 'partial'   THEN 2
                                    WHEN 'minimal'   THEN 1
                                    ELSE 0
                                END DESC
                            LIMIT 1
                        ),
                        current_severity
                    ),
                    last_report_at = NOW()
                WHERE id = :building_id
                """),
            {"building_id": str(building_id)},
        )
        self._db.flush()

    # ------------------------------------------------------------------
    # Private matching helpers
    # ------------------------------------------------------------------

    def _point_in_polygon(self, lat: float, lng: float) -> Optional[BuildingMatch]:
        """Step 1 — exact point-in-polygon query."""
        row = self._db.execute(
            text("""
                SELECT
                    id,
                    ST_AsGeoJSON(footprint) AS footprint_geojson
                FROM building
                WHERE ST_Contains(
                    footprint,
                    ST_SetSRID(ST_Point(:lng, :lat), 4326)
                )
                LIMIT 1
                """),
            {"lat": lat, "lng": lng},
        ).fetchone()

        if row is None:
            return None

        logger.debug("Point-in-polygon match: building_id=%s", row.id)
        return BuildingMatch(
            building_id=UUID(str(row.id)),
            confidence=1.0,
            distance_m=None,
            footprint_geojson=row.footprint_geojson,
        )

    def _nearest_neighbour(
        self, lat: float, lng: float, search_radius_m: float
    ) -> Optional[BuildingMatch]:
        """Step 2 — nearest building footprint edge within ``search_radius_m`` metres.

        Measures distance to the polygon boundary (not the centroid) so that
        large institutional buildings — hospitals, schools, government offices —
        are matched correctly even when the GPS point is near an edge rather
        than the geometric centre.
        """
        row = self._db.execute(
            text("""
                SELECT
                    id,
                    ST_AsGeoJSON(footprint)        AS footprint_geojson,
                    ST_Distance(
                        footprint::geography,
                        ST_SetSRID(
                            ST_Point(:lng, :lat), 4326
                        )::geography
                    )                              AS distance_m
                FROM building
                WHERE ST_DWithin(
                    footprint::geography,
                    ST_SetSRID(ST_Point(:lng, :lat), 4326)::geography,
                    :radius_m
                )
                ORDER BY distance_m ASC
                LIMIT 1
                """),
            {"lat": lat, "lng": lng, "radius_m": search_radius_m},
        ).fetchone()

        if row is None:
            return None

        distance_m: float = float(row.distance_m)
        confidence = 1.0 - (distance_m / search_radius_m)

        if confidence <= 0.0:
            return None

        logger.debug(
            "Nearest-neighbour match: building_id=%s distance=%.1fm confidence=%.3f",
            row.id,
            distance_m,
            confidence,
        )
        return BuildingMatch(
            building_id=UUID(str(row.id)),
            confidence=confidence,
            distance_m=distance_m,
            footprint_geojson=row.footprint_geojson,
        )

    def _candidates(
        self, lat: float, lng: float, search_radius_m: float
    ) -> List[BuildingCandidate]:
        """Up to ``MAX_CANDIDATES`` footprints within the radius, nearest first.

        Uses the same edge distance as ``_nearest_neighbour``, so for a
        nearest-neighbour match the first candidate is the matched building.
        """
        rows = self._db.execute(
            text("""
                SELECT
                    id,
                    external_id,
                    ST_AsGeoJSON(footprint)        AS footprint_geojson,
                    ST_Distance(
                        footprint::geography,
                        ST_SetSRID(
                            ST_Point(:lng, :lat), 4326
                        )::geography
                    )                              AS distance_m
                FROM building
                WHERE ST_DWithin(
                    footprint::geography,
                    ST_SetSRID(ST_Point(:lng, :lat), 4326)::geography,
                    :radius_m
                )
                ORDER BY distance_m ASC
                LIMIT :limit
                """),
            {
                "lat": lat,
                "lng": lng,
                "radius_m": search_radius_m,
                "limit": MAX_CANDIDATES,
            },
        ).fetchall()

        return [
            BuildingCandidate(
                building_id=UUID(str(row.id)),
                external_id=row.external_id,
                distance_m=float(row.distance_m),
                footprint_geojson=row.footprint_geojson,
            )
            for row in rows
        ]

    def _landmark_geocoding(
        self,
        landmark_description: str,
        geocoding_provider,
        with_candidates: bool = False,
    ) -> Optional[BuildingMatch]:
        """Step 3 — geocode the landmark text, then run nearest-neighbour."""
        from app.services.geocoding_service import GeocodingError

        try:
            coords = geocoding_provider.geocode(landmark_description)
        except GeocodingError as exc:
            logger.warning("Geocoding failed for landmark: %s", exc)
            return None

        if coords is None:
            return None

        geo_lat, geo_lng = coords

        match = self._nearest_neighbour(geo_lat, geo_lng, _LANDMARK_RADIUS_M)
        if match is None:
            return None

        # Cap confidence at 0.5 for landmark-derived matches (spec §9.2).
        return BuildingMatch(
            building_id=match.building_id,
            confidence=min(match.confidence, 0.5),
            distance_m=match.distance_m,
            footprint_geojson=match.footprint_geojson,
            candidates=(
                self._candidates(geo_lat, geo_lng, _LANDMARK_RADIUS_M)
                if with_candidates
                else []
            ),
        )

    # ------------------------------------------------------------------
    # Radius helper
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_search_radius(accuracy_m: Optional[float]) -> float:
        """Return the effective nearest-neighbour search radius.

        If the device-reported accuracy exceeds 50 m, the radius is expanded
        to ``accuracy_m * 1.5``, capped at 100 m (spec §9.2).

        Args:
            accuracy_m: Device-reported horizontal accuracy in metres, or ``None``.

        Returns:
            Search radius in metres.
        """
        default = float(getattr(settings, "BUILDING_FOOTPRINT_SEARCH_RADIUS_M", 30))
        if accuracy_m is not None and accuracy_m > 50:
            return min(accuracy_m * 1.5, 100.0)
        return default
