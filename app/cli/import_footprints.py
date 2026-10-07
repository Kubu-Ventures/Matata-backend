"""Management command — import building footprints (Microsoft or OpenStreetMap).

Usage
-----
    python -m app.cli.import_footprints --source /path/to/footprints.geojson
    python -m app.cli.import_footprints --source https://example.com/ke.geojson
    python -m app.cli.import_footprints --source /path/to/footprints.ndjson
    python -m app.cli.import_footprints --source-type osm --source buildings.geojson
    python -m app.cli.import_footprints --source-type osm --source overpass.json

What it does
------------
1. Reads building footprint geometries from a GeoJSON FeatureCollection or
   NDJSON (newline-delimited JSON) file at a local path or remote URL.
2. Upserts each record into the ``building`` table keyed on ``external_id``,
   so re-running with the same source is idempotent (no duplicates created).
3. Computes the polygon centroid via PostGIS ``ST_Centroid`` during INSERT/UPDATE.
4. Verifies GIST spatial indexes after import.
5. Logs a summary of records inserted / updated.

Source format
-------------
The Microsoft Africa Building Footprints dataset is distributed as NDJSON
(one GeoJSON Feature per line).  Each Feature must have:
* ``geometry.type == "Polygon"``
* ``geometry.coordinates`` — standard GeoJSON polygon ring(s)
* ``properties`` — any additional metadata (stored as-is, not parsed)
* ``id`` (optional) — used as ``external_id``; if absent, a hash of the
  geometry is used instead.

OpenStreetMap (``--source-type osm``)
-------------------------------------
Accepts GeoJSON from the HOT Export Tool, ``osmtogeojson`` or ``ogr2ogr`` /
``osmium export``, and raw Overpass API JSON (``out geom;``, closed ways only).
Rows are stored with ``source = 'osm'`` and ``external_id = 'osm:<type>/<id>'``
(e.g. ``osm:way/123456789``) so every match links back to the OSM object.
Features without a ``building`` tag (or tagged ``building=no``) are skipped.
OSM data is ODbL-licensed: exports that include these footprints must carry
"© OpenStreetMap contributors" attribution.

Security notes
--------------
* Database credentials are read from ``settings.DATABASE_URL`` — no
  credentials are accepted on the command line.
* All SQL parameters are passed as bound values; no string interpolation of
  user data is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from typing import Iterator
from urllib.request import urlopen

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


# CLI --source-type value → building_source_enum value.
_SOURCE_TYPES = {
    "microsoft": "microsoft_africa",
    "osm": "osm",
    "manual": "manual",
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.import_footprints",
        description="Import building footprints (Microsoft or OSM) into PostGIS.",
    )
    parser.add_argument(
        "--source-type",
        choices=sorted(_SOURCE_TYPES),
        default="microsoft",
        help="Dataset the footprints come from (default microsoft).",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path or URL to a GeoJSON FeatureCollection or NDJSON file.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of features to upsert per database transaction (default 500).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate without writing to the database.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Source reader
# ---------------------------------------------------------------------------


def _open_source(source: str):
    """Return a file-like object for *source* (local path or URL)."""
    if source.startswith("http://") or source.startswith("https://"):
        return urlopen(source)  # noqa: S310 — URL validated by scheme check
    return open(source, "rb")


def _iter_features(source: str) -> Iterator[dict]:
    """Yield GeoJSON Feature dicts from *source*.

    Supports:
    * GeoJSON FeatureCollection (single JSON object with ``features`` array)
    * NDJSON (one Feature JSON object per line)
    """
    with _open_source(source) as fh:
        raw = fh.read()

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    # Try to parse as a single JSON object first.
    try:
        obj = json.loads(raw)
        if obj.get("type") == "FeatureCollection":
            yield from obj.get("features", [])
            return
        if obj.get("type") == "Feature":
            yield obj
            return
        if isinstance(obj.get("elements"), list):
            yield from _iter_overpass_elements(obj["elements"])
            return
    except json.JSONDecodeError:
        pass  # Fall through to NDJSON parsing.

    # NDJSON — one feature per non-empty line.
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            feature = json.loads(line)
            if feature.get("type") == "Feature":
                yield feature
        except json.JSONDecodeError as exc:
            logger.warning("Skipping invalid JSON line: %s", exc)


def _iter_overpass_elements(elements: list) -> Iterator[dict]:
    """Yield GeoJSON Features for closed ways in an Overpass ``out geom`` result.

    Relations (multipolygon buildings) are not assembled here; export them
    through the HOT Export Tool or ``osmtogeojson`` instead.
    """
    skipped_relations = 0
    for el in elements:
        if el.get("type") == "relation":
            skipped_relations += 1
            continue
        if el.get("type") != "way":
            continue
        ring = [[pt["lon"], pt["lat"]] for pt in el.get("geometry") or []]
        if len(ring) < 4 or ring[0] != ring[-1]:
            continue  # open way — not a polygon
        yield {
            "type": "Feature",
            "id": f"way/{el['id']}",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": el.get("tags") or {},
        }
    if skipped_relations:
        logger.warning(
            "Skipped %d Overpass relation(s); use a GeoJSON export to import "
            "multipolygon buildings.",
            skipped_relations,
        )


def _is_osm_building(properties: dict) -> bool:
    """True if the feature carries a ``building`` tag other than ``no``."""
    value = properties.get("building")
    tags = properties.get("tags")
    if value is None and isinstance(tags, dict):
        value = tags.get("building")
    return value not in (None, "", "no")


def _osm_external_id(feature: dict, geometry: dict) -> str:
    """Return ``osm:<type>/<id>`` for an OSM feature.

    Recognises the id conventions of osmtogeojson / Overpass (``way/123``,
    ``@id``), ogr2ogr (``osm_way_id`` for closed ways, ``osm_id`` for
    relations on the multipolygons layer) and the HOT Export Tool
    (``osm_id`` + ``osm_type``). Falls back to a geometry hash.
    """
    props = feature.get("properties") or {}

    for candidate in (feature.get("id"), props.get("@id"), props.get("id")):
        if isinstance(candidate, str) and "/" in candidate:
            return f"osm:{candidate}"

    if props.get("osm_way_id"):
        return f"osm:way/{props['osm_way_id']}"

    osm_id = props.get("osm_id")
    if osm_id:
        osm_type = str(props.get("osm_type") or "").lower()
        for kind in ("way", "relation", "node"):
            if kind in osm_type:
                return f"osm:{kind}/{osm_id}"
        if "osm_way_id" in props:  # ogr2ogr multipolygons layer: a relation
            return f"osm:relation/{osm_id}"
        return f"osm:{osm_id}"

    return f"osm:geom-{_geometry_hash(geometry)}"


def _geometry_hash(geometry: dict) -> str:
    """Return a stable SHA-256 hex digest of the geometry coordinates."""
    canonical = json.dumps(geometry.get("coordinates", []), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Upsert logic
# ---------------------------------------------------------------------------

_UPSERT_SQL = text("""
    INSERT INTO building (
        footprint,
        centroid,
        source,
        external_id,
        current_severity
    )
    VALUES (
        ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)),
        ST_Centroid(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326))),
        CAST(:source AS building_source_enum),
        :external_id,
        'none'::damage_severity_enum
    )
    ON CONFLICT (external_id) DO UPDATE SET
        footprint        = EXCLUDED.footprint,
        centroid         = EXCLUDED.centroid,
        updated_at       = NOW()
    RETURNING (xmax = 0) AS inserted
    """)


def _upsert_batch(
    db: Session,
    batch: list[dict],
    counters: dict,
    dry_run: bool,
) -> None:
    """Upsert a list of prepared feature dicts into the database."""
    if dry_run:
        counters["parsed"] += len(batch)
        return

    for item in batch:
        result = db.execute(_UPSERT_SQL, item).fetchone()
        if result and result.inserted:
            counters["inserted"] += 1
        else:
            counters["updated"] += 1

    db.commit()


# ---------------------------------------------------------------------------
# Index verification
# ---------------------------------------------------------------------------


def _verify_indexes(db: Session) -> None:
    """Log the status of GIST spatial indexes on the building table."""
    rows = db.execute(text("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'building'
              AND indexdef ILIKE '%gist%'
            ORDER BY indexname
            """)).fetchall()

    if rows:
        for row in rows:
            logger.info("  GIST index verified: %s", row.indexname)
    else:
        logger.warning(
            "No GIST indexes found on 'building' table — "
            "spatial queries may be slow.  Run the migration to create them."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run(
    source: str,
    batch_size: int,
    dry_run: bool,
    source_type: str = "microsoft",
) -> int:
    """Core import logic.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    from app.core.config import settings  # Deferred to avoid import-time env validation

    if dry_run:
        logger.info("DRY RUN — no database writes will be performed.")

    # Build a synchronous engine (Celery/CLI context; no async).
    sync_url = settings.DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
    engine = create_engine(sync_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db: Session = SessionLocal()

    building_source = _SOURCE_TYPES[source_type]
    is_osm = source_type == "osm"

    counters: dict[str, int] = {
        "parsed": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "not_building": 0,
    }
    batch: list[dict] = []
    errors = 0

    try:
        logger.info("Reading %s features from: %s", building_source, source)

        for feature in _iter_features(source):
            if is_osm and not _is_osm_building(feature.get("properties") or {}):
                counters["not_building"] += 1
                continue

            geometry = feature.get("geometry") or {}
            geom_type = geometry.get("type")

            if geom_type == "MultiPolygon":
                # Extract the sub-polygon with the most exterior-ring vertices
                # (a reliable proxy for largest area without PostGIS round-trips).
                # Building complexes recorded as MultiPolygon (e.g. hospital
                # campuses) are retained rather than silently discarded.
                polygons = geometry.get("coordinates") or []
                if not polygons:
                    counters["skipped"] += 1
                    continue
                largest = max(polygons, key=lambda p: len(p[0]) if p else 0)
                geometry = {"type": "Polygon", "coordinates": largest}
                geom_type = "Polygon"

            if geom_type != "Polygon":
                counters["skipped"] += 1
                continue

            geojson_str = json.dumps(geometry)
            if is_osm:
                external_id = _osm_external_id(feature, geometry)
            else:
                # Derive external_id from feature id → property id → geometry hash.
                external_id = str(
                    feature.get("id")
                    or (feature.get("properties") or {}).get("id")
                    or _geometry_hash(geometry)
                )

            batch.append(
                {
                    "geojson": geojson_str,
                    "external_id": external_id,
                    "source": building_source,
                }
            )

            if len(batch) >= batch_size:
                _upsert_batch(db, batch, counters, dry_run)
                total = counters["inserted"] + counters["updated"]
                logger.info(
                    "  Progress: %d processed (%d inserted, %d updated)",
                    total,
                    counters["inserted"],
                    counters["updated"],
                )
                batch = []

        # Flush remaining records.
        if batch:
            _upsert_batch(db, batch, counters, dry_run)

        if not dry_run:
            logger.info("Verifying GIST spatial indexes…")
            _verify_indexes(db)
            # Update planner statistics so PostGIS GIST indexes are used
            # efficiently after a bulk load.
            logger.info("Running ANALYZE on building table…")
            db.execute(text("ANALYZE building"))
            db.commit()

    except Exception as exc:  # noqa: BLE001
        logger.error("Import failed: %s", exc, exc_info=True)
        db.rollback()
        errors += 1
    finally:
        db.close()
        engine.dispose()

    # ── Summary ───────────────────────────────────────────────────────────────
    if dry_run:
        logger.info(
            "[DRY RUN] Parsed %d features (%d skipped non-polygon, "
            "%d skipped without a building tag).",
            counters["parsed"],
            counters["skipped"],
            counters["not_building"],
        )
    else:
        logger.info(
            "Import complete — inserted: %d, updated: %d, skipped: %d, "
            "not a building: %d.",
            counters["inserted"],
            counters["updated"],
            counters["skipped"],
            counters["not_building"],
        )

    return 0 if errors == 0 else 1


def main(argv: list[str] | None = None) -> None:
    """Entry point called by ``python -m app.cli.import_footprints``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = _parse_args(argv)
    exit_code = _run(
        source=args.source,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        source_type=args.source_type,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
