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
  1. Point-in-polygon  ``ST_Contains``
  2. Nearest-neighbour ``ST_DWithin``
  3. Landmark geocoding (when GPS absent)        → confidence ≤ 0.5
  4. Unmapped structure                          → building_id = None

Confidence is a probability, not a distance score: every building within the
search radius gets a likelihood from a 2-D Gaussian of its distance, with a
spread set by the phone's reported accuracy plus map error, and the matched
building's confidence is its share of the total. Two houses a metre apart
under a 10 m fix are each about 50 %, not a falsely certain 0.98. The
old ``1 - dist/radius`` score is kept only as a fallback when the
probability pool is empty.

With ``with_candidates=True`` the result also lists up to three buildings
nearest the query point (within the same search radius), so a client can let
the reporter confirm which one they meant. In dense settlements GPS error is
often larger than the gap between buildings, and the nearest building is
then frequently the wrong one. ``confirm_building`` validates the building
the reporter picked from that list.
"""

from __future__ import annotations

import logging
import math
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
        probability:       Share of the match likelihood this building holds
                           among those in range (0 until computed).
    """

    building_id: UUID
    external_id: str
    distance_m: float
    footprint_geojson: str
    probability: float = 0.0


@dataclass
class BuildingMatch:
    """Result returned by every matching path.

    Attributes:
        building_id:      UUID of the matched building, or ``None`` if unmapped.
        confidence:       Float in [0, 1]: the probability that this is the
                          right building given the GPS accuracy (1.0 when the
                          reporter confirmed it; capped at 0.5 for landmarks).
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
# Buildings considered when sharing out the match probability.
_PROBABILITY_POOL = 10
# Android reports accuracy as a 68 % radius; for a 2-D Gaussian that is
# 1.515 per-axis standard deviations (Rayleigh distribution).
_ACCURACY_TO_SIGMA = 1.515
# Positional error of the footprints themselves (imagery offset, tracing).
_MAP_SIGMA_M = 2.0
# Landmark text is vague: treat it like a fix with this reported accuracy.
_LANDMARK_ACCURACY_M = 50.0


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
                pool = self._candidates(
                    lat, lng, search_radius_m, limit=_PROBABILITY_POOL
                )
                self._apply_probabilities(match, pool, accuracy_m)
                if with_candidates:
                    match.candidates = pool[:MAX_CANDIDATES]
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

    @staticmethod
    def _sigma_m(accuracy_m: Optional[float]) -> float:
        """Per-axis spread (m) of where the reporter really is, around the fix."""
        acc = accuracy_m if accuracy_m and accuracy_m > 0 else None
        if acc is None:
            acc = float(getattr(settings, "GPS_DEFAULT_ACCURACY_M", 15.0))
        gps_sigma = acc / _ACCURACY_TO_SIGMA
        return math.hypot(gps_sigma, _MAP_SIGMA_M)

    @staticmethod
    def _probabilities(distances_m: List[float], sigma_m: float) -> List[float]:
        """Share out the match probability among buildings by distance.

        Each building's likelihood is exp(-d² / 2σ²): the chance a fix lands
        d metres from a building the reporter is really at. Normalised, so
        the shares sum to 1 over the buildings in range.
        """
        if not distances_m:
            return []
        weights = [math.exp(-(d * d) / (2 * sigma_m * sigma_m)) for d in distances_m]
        total = sum(weights)
        if total <= 0.0:  # every building far beyond the spread
            return [1.0 / len(weights)] * len(weights)
        return [w / total for w in weights]

    def _apply_probabilities(
        self,
        match: BuildingMatch,
        pool: List[BuildingCandidate],
        accuracy_m: Optional[float],
    ) -> None:
        """Fill each candidate's probability and set the match's confidence."""
        probs = self._probabilities(
            [c.distance_m for c in pool], self._sigma_m(accuracy_m)
        )
        for cand, prob in zip(pool, probs):
            cand.probability = prob
            if cand.building_id == match.building_id:
                match.confidence = prob

    def _candidates(
        self,
        lat: float,
        lng: float,
        search_radius_m: float,
        limit: int = MAX_CANDIDATES,
    ) -> List[BuildingCandidate]:
        """Up to ``limit`` footprints within the radius, nearest first.

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
                "limit": limit,
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

        pool = self._candidates(
            geo_lat, geo_lng, _LANDMARK_RADIUS_M, limit=_PROBABILITY_POOL
        )
        self._apply_probabilities(match, pool, _LANDMARK_ACCURACY_M)

        # Cap confidence at 0.5 for landmark-derived matches (spec §9.2).
        return BuildingMatch(
            building_id=match.building_id,
            confidence=min(match.confidence, 0.5),
            distance_m=match.distance_m,
            footprint_geojson=match.footprint_geojson,
            candidates=pool[:MAX_CANDIDATES] if with_candidates else [],
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
