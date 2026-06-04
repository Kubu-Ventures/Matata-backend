"""Geographic utility functions.

This module provides pure-Python geospatial helpers that are safe to import
in any context — no database, no external dependencies.

Functions
---------
haversine_distance_m
    Great-circle distance between two WGS84 coordinate pairs, in metres.
    Used by the duplicate detection system (spec §10.1) and the nearby-reports
    endpoint (spec §10.3).
"""

from __future__ import annotations

import math


def haversine_distance_m(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
) -> float:
    """Return the great-circle distance between two WGS84 points in metres.

    Uses the Haversine formula, which gives sub-metre accuracy for the
    short distances (< 1 km) involved in building-level duplicate detection.
    For distances above ~1 000 km the formula introduces small errors due to
    Earth's oblateness, which is not a concern for this use-case.

    Args:
        lat1: Latitude of point A in decimal degrees.
        lng1: Longitude of point A in decimal degrees.
        lat2: Latitude of point B in decimal degrees.
        lng2: Longitude of point B in decimal degrees.

    Returns:
        Distance in metres (≥ 0.0).

    Examples:
        >>> round(haversine_distance_m(0.0, 0.0, 0.0, 0.0), 6)
        0.0
        >>> round(haversine_distance_m(-1.2921, 36.8219, -1.2921, 36.8219), 6)
        0.0
    """
    _EARTH_RADIUS_M = 6_371_000.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return _EARTH_RADIUS_M * c
