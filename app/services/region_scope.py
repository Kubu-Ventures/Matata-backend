"""Geographic scope for regional responder accounts.

A responder account may carry a ``region_geojson`` (a GeoJSON Polygon or
MultiPolygon in WGS84). It is copied into the responder's JWT at sign-in, and
every analyst-side read of report data filters on it, so a responder only
ever sees reports whose GPS point lies inside their area.

``within_region`` is the one SQL predicate every scoped query uses; the
``bboxes_to_region`` / ``parse_bbox`` helpers let the CLI build a region from
the same ``min_lat,min_lng,max_lat,max_lng`` boxes used for offline areas.
"""

from __future__ import annotations

import json
from typing import Iterable, List, Tuple

import sqlalchemy as sa

BBox = Tuple[float, float, float, float]  # min_lat, min_lng, max_lat, max_lng


def within_region(region_geojson: str) -> sa.TextClause:
    """Return a WHERE predicate: the report's point lies inside the region.

    Written against the ``report`` table name, so it applies to any
    ``select(Report ...)`` that does not alias the table.
    """
    return sa.text(
        "ST_Within("
        "ST_SetSRID(ST_Point(report.lng, report.lat), 4326), "
        "ST_SetSRID(ST_GeomFromGeoJSON(:region_geojson), 4326)"
        ")"
    ).bindparams(region_geojson=region_geojson)


def _in_ring(lng: float, lat: float, ring: list) -> bool:
    """Ray-casting point-in-ring test (ring is a list of [lng, lat])."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_region(lat: float | None, lng: float | None, region_geojson: str) -> bool:
    """Return True only if the point is inside the region.

    Fails closed: a missing point, an unparsable region or an unsupported
    geometry type all return False, so a responder is never shown a report
    whose location cannot be confirmed to be inside their area. Accepts a
    bare Polygon / MultiPolygon geometry, or a Feature wrapping one.
    """
    if lat is None or lng is None:
        return False
    try:
        geom = json.loads(region_geojson)
        if geom.get("type") == "Feature":
            geom = geom.get("geometry") or {}
        gtype = geom.get("type")
        if gtype == "Polygon":
            polygons = [geom["coordinates"]]
        elif gtype == "MultiPolygon":
            polygons = geom["coordinates"]
        else:
            return False
        for rings in polygons:
            if not rings:
                continue
            outer, holes = rings[0], rings[1:]
            if _in_ring(lng, lat, outer) and not any(
                _in_ring(lng, lat, h) for h in holes
            ):
                return True
        return False
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


def parse_bbox(value: str) -> BBox:
    """Parse ``"min_lat,min_lng,max_lat,max_lng"`` into a validated box.

    Raises:
        ValueError: on a malformed, out-of-range or inverted box.
    """
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"'{value}' must be four numbers: min_lat,min_lng,max_lat,max_lng."
        )
    try:
        min_lat, min_lng, max_lat, max_lng = (float(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"'{value}' contains a value that is not a number.") from exc
    if not (-90 <= min_lat < max_lat <= 90):
        raise ValueError(f"'{value}': latitudes must satisfy -90 <= min < max <= 90.")
    if not (-180 <= min_lng < max_lng <= 180):
        raise ValueError(
            f"'{value}': longitudes must satisfy -180 <= min < max <= 180."
        )
    return min_lat, min_lng, max_lat, max_lng


def bboxes_to_region(boxes: Iterable[BBox]) -> str:
    """Return a GeoJSON MultiPolygon string covering every box."""
    polygons: List[list] = []
    for min_lat, min_lng, max_lat, max_lng in boxes:
        ring = [
            [min_lng, min_lat],
            [max_lng, min_lat],
            [max_lng, max_lat],
            [min_lng, max_lat],
            [min_lng, min_lat],
        ]
        polygons.append([ring])
    if not polygons:
        raise ValueError("At least one box is required.")
    return json.dumps({"type": "MultiPolygon", "coordinates": polygons})
