"""Management command — import health facilities from OpenStreetMap.

Usage
-----
    python -m app.cli.import_health_facilities --source facilities.geojson
    python -m app.cli.import_health_facilities --source overpass.json [--dry-run]

Accepts GeoJSON (osmtogeojson, ogr2ogr or an OSM export tool) or raw Overpass API
JSON. For Overpass, ask for ``out center;`` so ways and relations come with a
centre point. A query for Nairobi's informal settlements looks like:

    [out:json][timeout:60];
    (
      nwr["amenity"~"^(hospital|clinic|doctors)$"](-1.345,36.74,-1.24,36.90);
      nwr["healthcare"~"^(hospital|clinic|centre|doctor)$"](-1.345,36.74,-1.24,36.90);
    );
    out center;

Pharmacies are skipped, since they are not places to refer someone for care.
Rows are upserted on ``external_id`` (``osm:<type>/<id>``), so re-running the
import updates names and positions without creating duplicates. OSM data is
ODbL-licensed: anything showing these records must credit
"© OpenStreetMap contributors".
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_AMENITY_TYPES = {"hospital": "hospital", "clinic": "clinic", "doctors": "doctors"}
_HEALTHCARE_TYPES = {
    "hospital": "hospital",
    "clinic": "clinic",
    "centre": "health_centre",
    "doctor": "doctors",
}


def facility_type(tags: dict) -> Optional[str]:
    """Map OSM tags to our facility type, or None to skip the feature."""
    amenity = str(tags.get("amenity", "")).strip().lower()
    if amenity in _AMENITY_TYPES:
        return _AMENITY_TYPES[amenity]
    healthcare = str(tags.get("healthcare", "")).strip().lower()
    return _HEALTHCARE_TYPES.get(healthcare)


def _centre(geometry: dict) -> Optional[tuple[float, float]]:
    """Return (lat, lng) for a GeoJSON geometry: the point, or the mean vertex."""
    gtype = geometry.get("type")
    coords: Any = geometry.get("coordinates")
    if gtype == "Point" and coords:
        return float(coords[1]), float(coords[0])
    points: list = []

    def walk(c: Any) -> None:
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            points.append(c)
        elif isinstance(c, (list, tuple)):
            for item in c:
                walk(item)

    walk(coords)
    if not points:
        return None
    return (
        sum(float(p[1]) for p in points) / len(points),
        sum(float(p[0]) for p in points) / len(points),
    )


def parse_features(data: dict) -> Iterator[dict]:
    """Yield facility rows from GeoJSON or Overpass JSON."""
    if "elements" in data:  # Overpass
        for el in data["elements"]:
            tags = el.get("tags") or {}
            ftype = facility_type(tags)
            if ftype is None:
                continue
            if "lat" in el and "lon" in el:
                lat, lng = float(el["lat"]), float(el["lon"])
            elif el.get("center"):
                lat, lng = float(el["center"]["lat"]), float(el["center"]["lon"])
            else:
                continue
            yield _row(f"osm:{el.get('type')}/{el.get('id')}", tags, ftype, lat, lng)
        return

    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        nested = props.get("tags")
        ftags: dict = nested if isinstance(nested, dict) else props
        ftype = facility_type(ftags)
        if ftype is None or not feature.get("geometry"):
            continue
        centre = _centre(feature["geometry"])
        if centre is None:
            continue
        osm_ref = (
            feature.get("id")
            or props.get("@id")
            or props.get("osm_id")
            or props.get("id")
        )
        if osm_ref is None:
            continue
        osm_ref = str(osm_ref)
        if "/" not in osm_ref:
            osm_type = props.get("osm_type", "node")
            osm_ref = f"{osm_type}/{osm_ref}"
        yield _row(f"osm:{osm_ref}", ftags, ftype, centre[0], centre[1])


def _row(external_id: str, tags: dict, ftype: str, lat: float, lng: float) -> dict:
    return {
        "external_id": external_id[:64],
        "name": tags.get("name") or tags.get("name:en"),
        "facility_type": ftype,
        "lat": lat,
        "lng": lng,
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "opening_hours": tags.get("opening_hours"),
    }


def run_import(source: str, dry_run: bool = False) -> int:
    """Import facilities from *source*. Returns a process exit code."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.core.config import settings
    from app.models.health_facility import HealthFacility

    try:
        with open(source, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read {source}: {exc}")
        return 1

    rows = list(parse_features(data))
    print(f"Found {len(rows)} health facilities in {source}.")
    if dry_run:
        for r in rows[:10]:
            print(f"  {r['facility_type']:<14} {r['name'] or '(no name)'}")
        print("Dry run: nothing written.")
        return 0

    sync_url = settings.DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
    engine = create_engine(sync_url, pool_pre_ping=True)
    inserted = updated = 0
    with Session(engine) as db:
        for r in rows:
            existing = db.execute(
                select(HealthFacility).where(
                    HealthFacility.external_id == r["external_id"]
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(HealthFacility(id=uuid.uuid4(), source="osm", **r))
                inserted += 1
            else:
                for key, value in r.items():
                    setattr(existing, key, value)
                updated += 1
        db.commit()
    engine.dispose()
    print(f"Done: {inserted} added, {updated} updated.")
    return 0


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.import_health_facilities",
        description="Import health facilities from OpenStreetMap data.",
    )
    parser.add_argument(
        "--source", required=True, help="GeoJSON or Overpass JSON file."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    sys.exit(run_import(args.source, args.dry_run))


if __name__ == "__main__":
    main()
