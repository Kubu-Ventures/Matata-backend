"""Seed synthetic building footprints into PostGIS.

The duplicate-detection building signal (40% weight) and the
point-in-polygon / nearest-neighbour GIS match only fire when the ``building``
table is populated (audit finding M-1). This module lays out a regular grid
of small rectangular footprints inside a bounding box and inserts them, then
returns a representative interior point for each so ``scenario.py`` can place
reports on them.
"""

from __future__ import annotations

import psycopg2

# Nairobi-ish bounding box (arbitrary; only relative geometry matters).
BBOX = (-1.330, 36.790, -1.250, 36.870)  # (min_lat, min_lng, max_lat, max_lng)

_FOOTPRINT_M = 12.0        # ~12 m square buildings
_DEG_PER_M_LAT = 1.0 / 111_320.0


def _grid(n_cells: int) -> list[tuple[str, float, float, float, float]]:
    """Return (ext_id, min_lat, min_lng, max_lat, max_lng) for n_cells^2 lots."""
    min_lat, min_lng, max_lat, max_lng = BBOX
    out = []
    dlat = (max_lat - min_lat) / n_cells
    dlng = (max_lng - min_lng) / n_cells
    half = _FOOTPRINT_M * _DEG_PER_M_LAT / 2
    for r in range(n_cells):
        for c in range(n_cells):
            clat = min_lat + (r + 0.5) * dlat
            clng = min_lng + (c + 0.5) * dlng
            out.append((
                f"sim-fp-{r:03d}-{c:03d}",
                clat - half, clng - half, clat + half, clng + half,
            ))
    return out


def seed(dsn: str, count_needed: int) -> tuple[list[str], dict[str, tuple[float, float]]]:
    """Insert >= ``count_needed`` footprints; return (ext_ids, interior_point)."""
    import math

    n_cells = max(4, math.ceil(math.sqrt(count_needed * 1.15)))
    lots = _grid(n_cells)

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    ext_ids: list[str] = []
    points: dict[str, tuple[float, float]] = {}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM report WHERE reporter_token_hash LIKE 'sim:%';")
        cur.execute("DELETE FROM building WHERE external_id LIKE 'sim-fp-%';")
        for ext_id, mnla, mnlo, mxla, mxlo in lots:
            # POLYGON ring (lng lat order), centroid as interior point.
            ring = (
                f"{mnlo} {mnla},{mxlo} {mnla},{mxlo} {mxla},"
                f"{mnlo} {mxla},{mnlo} {mnla}"
            )
            cur.execute(
                """
                INSERT INTO building (footprint, centroid, source, external_id,
                                      current_severity)
                VALUES (
                    ST_SetSRID(ST_GeomFromText(%s), 4326),
                    ST_SetSRID(ST_GeomFromText(%s), 4326),
                    'manual', %s, 'none'
                )
                """,
                (
                    f"POLYGON(({ring}))",
                    f"POINT({(mnlo + mxlo) / 2} {(mnla + mxla) / 2})",
                    ext_id,
                ),
            )
            ext_ids.append(ext_id)
            points[ext_id] = ((mnla + mxla) / 2, (mnlo + mxlo) / 2)
    conn.close()
    return ext_ids, points
