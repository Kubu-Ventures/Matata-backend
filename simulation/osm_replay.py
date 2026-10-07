"""Offline replay of the footprint matcher against real building footprints.

    python -m simulation.osm_replay --sample 500 --out simulation/out

Answers: when a report's GPS fix is off by a realistic amount, how often does
``GISService.match_building`` pick the right building, and how does that
change with GPS error and with how tightly packed the buildings are?

Method (pilot phase A):
1. Sample buildings already loaded into PostGIS (``--source osm`` by default,
   see ``python -m app.cli.import_footprints --source-type osm``).
2. For each one take a true point that is guaranteed to lie inside it
   (``ST_PointOnSurface``) and count its neighbours within ``--density-m``.
3. Hold out ``--unmapped-fraction`` of the sample by deleting them, to test
   that reports on buildings missing from the map end unmatched.
4. For each GPS error level, jitter the true point with a 2-D Gaussian and run
   the real matcher with ``accuracy_m`` set to that level. Android reports
   accuracy as a 68 % radius, so the per-axis sigma is ``accuracy / 1.515``.
5. Score top-1, top-3 (from ``match.candidates``), false matches and
   confidence calibration.

Everything runs in one transaction that is always rolled back: the held-out
buildings are never really deleted and nothing is written. Point it at a
local copy of the database anyway.

Reference areas
---------------
``--area`` names a preset bounding box (``AREAS`` below) so a run can be
repeated exactly. To reproduce one from scratch::

    python -m simulation.osm_replay --area kibera --overpass-query > q.txt
    curl -sS --data-urlencode data@q.txt \
        https://overpass-api.de/api/interpreter -o kibera.json
    python -m app.cli.import_footprints --source-type osm --source kibera.json
    python -m simulation.osm_replay --area kibera --sample 1000 --trials 2

OSM data is ODbL: credit "© OpenStreetMap contributors" wherever results
derived from it are shown. OSM changes over time, so record the Overpass
``timestamp_osm_base`` alongside any published numbers.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# GISService reads app settings at import time; the replay needs none of the
# secrets, so fill placeholders the same way tests/conftest.py does.
for _var in ("SECRET_KEY", "JWT_SECRET_KEY", "PHONE_HASH_SALT"):
    os.environ.setdefault(_var, "replay-placeholder-not-a-secret")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://replay:replay@localhost:5432/replay"
)

from app.services.gis_service import GISService  # noqa: E402

DEFAULT_DSN = os.environ.get(
    "SIM_DSN",
    "dbname=matata_db user=matata password=matata host=localhost port=5432",
)

_M_PER_DEG_LAT = 111_320.0
# 68 % of a 2-D isotropic Gaussian lies within 1.515 per-axis sigmas.
_ACCURACY_TO_AXIS_SIGMA = 1.515
_FALSE_MATCH_CONFIDENCE = 0.8
_DENSITY_BUCKETS = [(0, 0, "0"), (1, 2, "1-2"), (3, 5, "3-5"), (6, 10**9, "6+")]
_CALIBRATION_BINS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]

Bbox = tuple[float, float, float, float]  # min_lng, min_lat, max_lng, max_lat


@dataclass(frozen=True)
class Area:
    bbox: Bbox
    description: str


# Reference areas, each about 1 km x 1 km. First run 7 Oct 2026 against OSM
# as of 2026-10-07T08:31Z (Kibera 4,896 buildings, Kilimani 1,249).
AREAS: dict[str, Area] = {
    "kibera": Area(
        (36.7850, -1.3165, 36.7950, -1.3075),
        "Kibera, Nairobi: dense informal settlement, extensively mapped "
        "(median 19 buildings within 20 m)",
    ),
    "kilimani": Area(
        (36.7800, -1.2930, 36.7900, -1.2840),
        "Kilimani, Nairobi: formal residential and commercial, less dense "
        "(median 5 buildings within 20 m)",
    ),
}


def overpass_query(bbox: Bbox) -> str:
    """Overpass QL for every building way in ``bbox`` (``out geom`` JSON).

    Overpass takes the box as south,west,north,east, the reverse of ours.
    Relations (multipolygon buildings) are left out because the importer
    does not assemble them from Overpass JSON.
    """
    min_lng, min_lat, max_lng, max_lat = bbox
    return (
        "[out:json][timeout:120];"
        f'way["building"]({min_lat},{min_lng},{max_lat},{max_lng});'
        "out geom;"
    )


@dataclass
class Target:
    building_id: str
    external_id: str
    lat: float
    lng: float
    neighbours: int
    held_out: bool


@dataclass
class Trial:
    external_id: str
    held_out: bool
    neighbours: int
    density_bucket: str
    accuracy_m: float
    offset_m: float
    matched_id: Optional[str]
    confidence: float
    correct: bool
    in_top3: bool
    error_m: Optional[float]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def jitter(
    lat: float, lng: float, accuracy_m: float, rng: random.Random
) -> tuple[float, float, float]:
    """Return a GPS fix around (lat, lng) and its distance from it in metres."""
    sigma = accuracy_m / _ACCURACY_TO_AXIS_SIGMA
    dx, dy = rng.gauss(0, sigma), rng.gauss(0, sigma)
    new_lat = lat + dy / _M_PER_DEG_LAT
    new_lng = lng + dx / (_M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return new_lat, new_lng, math.hypot(dx, dy)


def density_bucket(neighbours: int) -> str:
    for lo, hi, label in _DENSITY_BUCKETS:
        if lo <= neighbours <= hi:
            return label
    raise ValueError(neighbours)


def _rate(num: int, den: int) -> Optional[float]:
    return round(100.0 * num / den, 1) if den else None


def summarise(trials: list[Trial]) -> dict:
    """Aggregate trials into the pilot metrics (section 8 of the prep book)."""
    mapped = [t for t in trials if not t.held_out]
    held = [t for t in trials if t.held_out]

    def block(ts: list[Trial]) -> dict:
        errors = [t.error_m for t in ts if t.error_m is not None]
        wrong_confident = [
            t
            for t in ts
            if t.matched_id
            and not t.correct
            and t.confidence >= _FALSE_MATCH_CONFIDENCE
        ]
        return {
            "n": len(ts),
            "match_rate_pct": _rate(sum(1 for t in ts if t.matched_id), len(ts)),
            "top1_pct": _rate(sum(1 for t in ts if t.correct), len(ts)),
            "top3_pct": _rate(sum(1 for t in ts if t.in_top3), len(ts)),
            "false_match_pct": _rate(len(wrong_confident), len(ts)),
            "median_wrong_match_error_m": (
                round(statistics.median(errors), 1) if errors else None
            ),
        }

    by_accuracy: dict[float, list[Trial]] = defaultdict(list)
    by_cell: dict[tuple[float, str], list[Trial]] = defaultdict(list)
    for t in mapped:
        by_accuracy[t.accuracy_m].append(t)
        by_cell[(t.accuracy_m, t.density_bucket)].append(t)

    unmapped: dict[float, list[Trial]] = defaultdict(list)
    for t in held:
        unmapped[t.accuracy_m].append(t)

    calibration = []
    matched = [t for t in mapped if t.matched_id]
    for lo, hi in zip(_CALIBRATION_BINS, _CALIBRATION_BINS[1:]):
        last = hi == _CALIBRATION_BINS[-1]
        ts = [
            t
            for t in matched
            if lo <= t.confidence < hi or (last and t.confidence == hi)
        ]
        calibration.append(
            {
                "bin": f"{lo:.1f}-{hi:.1f}",
                "n": len(ts),
                "mean_confidence": (
                    round(statistics.fmean(t.confidence for t in ts), 3) if ts else None
                ),
                "share_correct_pct": _rate(sum(1 for t in ts if t.correct), len(ts)),
            }
        )

    return {
        "overall": block(mapped),
        "by_accuracy_m": {str(a): block(ts) for a, ts in sorted(by_accuracy.items())},
        "by_accuracy_and_density": {
            f"{a}m/{d}": block(ts) for (a, d), ts in sorted(by_cell.items())
        },
        "unmapped_detection": {
            str(a): {
                "n": len(ts),
                "correctly_unmatched_pct": _rate(
                    sum(1 for t in ts if not t.matched_id), len(ts)
                ),
            }
            for a, ts in sorted(unmapped.items())
        },
        "calibration": calibration,
    }


# ---------------------------------------------------------------------------
# Database steps
# ---------------------------------------------------------------------------


def sample_targets(
    db: Session,
    *,
    source: Optional[str],
    bbox: Optional[Bbox],
    sample: int,
    density_m: float,
    seed: int,
) -> list[Target]:
    db.execute(text("SELECT setseed(:s)"), {"s": (seed % 1000) / 1000.0})
    rows = db.execute(
        text("""
            WITH picked AS (
                SELECT id, external_id, footprint,
                       ST_PointOnSurface(footprint) AS p
                FROM building
                WHERE (CAST(:source AS text) IS NULL
                       OR source::text = CAST(:source AS text))
                  AND (CAST(:min_lng AS float8) IS NULL OR footprint &&
                       ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326))
                ORDER BY random()
                LIMIT :sample
            )
            SELECT
                picked.id::text AS id,
                picked.external_id,
                ST_Y(picked.p) AS lat,
                ST_X(picked.p) AS lng,
                (
                    SELECT count(*) FROM building b
                    WHERE b.id <> picked.id
                      AND ST_DWithin(b.footprint::geography,
                                     picked.footprint::geography, :density_m)
                ) AS neighbours
            FROM picked
            """),
        {
            "source": source,
            "min_lng": bbox[0] if bbox else None,
            "min_lat": bbox[1] if bbox else None,
            "max_lng": bbox[2] if bbox else None,
            "max_lat": bbox[3] if bbox else None,
            "sample": sample,
            "density_m": density_m,
        },
    ).fetchall()
    return [
        Target(r.id, r.external_id, r.lat, r.lng, int(r.neighbours), False)
        for r in rows
    ]


def hold_out(db: Session, targets: list[Target], fraction: float, seed: int) -> None:
    """Delete a share of the targets inside the (rolled-back) transaction."""
    rng = random.Random(seed)
    chosen = rng.sample(targets, int(len(targets) * fraction))
    for t in chosen:
        t.held_out = True
    if chosen:
        db.execute(
            text("DELETE FROM building WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [t.building_id for t in chosen]},
        )


def footprint_gap_m(db: Session, a: str, b: str) -> float:
    return float(
        db.execute(
            text("""
                SELECT ST_Distance(x.footprint::geography, y.footprint::geography)
                FROM building x, building y
                WHERE x.id = CAST(:a AS uuid) AND y.id = CAST(:b AS uuid)
                """),
            {"a": a, "b": b},
        ).scalar_one()
    )


def replay(
    db: Session, targets: list[Target], accuracies: list[float], trials: int, seed: int
) -> list[Trial]:
    rng = random.Random(seed)
    gis = GISService(db)
    out: list[Trial] = []
    for target in targets:
        for acc in accuracies:
            for _ in range(trials):
                lat, lng, offset = jitter(target.lat, target.lng, acc, rng)
                match = gis.match_building(
                    lat=lat, lng=lng, accuracy_m=acc, with_candidates=True
                )
                matched = str(match.building_id) if match.building_id else None
                correct = matched == target.building_id
                error = None
                if matched and not correct and not target.held_out:
                    error = round(footprint_gap_m(db, matched, target.building_id), 1)
                out.append(
                    Trial(
                        external_id=target.external_id,
                        held_out=target.held_out,
                        neighbours=target.neighbours,
                        density_bucket=density_bucket(target.neighbours),
                        accuracy_m=acc,
                        offset_m=round(offset, 1),
                        matched_id=matched,
                        confidence=round(match.confidence, 3),
                        correct=correct,
                        in_top3=any(
                            str(c.building_id) == target.building_id
                            for c in match.candidates
                        ),
                        error_m=error,
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _table(rows: dict[str, dict], cols: list[str], first: str) -> list[str]:
    lines = [
        "| " + " | ".join([first, *cols]) + " |",
        "|" + "---|" * (len(cols) + 1),
    ]
    for key, row in rows.items():
        cells = ["" if row.get(c) is None else str(row[c]) for c in cols]
        lines.append("| " + " | ".join([key, *cells]) + " |")
    return lines


def write_outputs(out_dir: str, trials: list[Trial], summary: dict, meta: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "trials.csv"), "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(trials[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(t) for t in trials)
    with open(os.path.join(out_dir, "results.json"), "w") as fh:
        json.dump({"run": meta, **summary}, fh, indent=2)

    cols = [
        "n",
        "match_rate_pct",
        "top1_pct",
        "top3_pct",
        "false_match_pct",
        "median_wrong_match_error_m",
    ]
    md = [
        "# OSM footprint-matching replay",
        "",
        f"_generated {meta['generated']}_ · area `{meta['area'] or 'custom'}` · "
        f"source `{meta['source']}` · "
        f"{meta['buildings']} buildings ({meta['held_out']} held out) · "
        f"{meta['trials_per_level']} trial(s) per accuracy level · seed {meta['seed']}",
        "",
        "Percentages are of trials on mapped buildings. A false match is a wrong "
        f"building returned with confidence >= {_FALSE_MATCH_CONFIDENCE}.",
        "",
        "## By GPS accuracy",
        "",
        *_table(
            {f"{k} m": v for k, v in summary["by_accuracy_m"].items()}, cols, "accuracy"
        ),
        "",
        f"## By accuracy and density (neighbours within {meta['density_m']} m)",
        "",
        *_table(summary["by_accuracy_and_density"], cols, "accuracy/neighbours"),
        "",
        "## Unmapped detection (held-out buildings)",
        "",
        *_table(
            {f"{k} m": v for k, v in summary["unmapped_detection"].items()},
            ["n", "correctly_unmatched_pct"],
            "accuracy",
        ),
        "",
        "## Confidence calibration",
        "",
        *_table(
            {c["bin"]: c for c in summary["calibration"]},
            ["n", "mean_confidence", "share_correct_pct"],
            "confidence",
        ),
        "",
    ]
    with open(os.path.join(out_dir, "summary.md"), "w") as fh:
        fh.write("\n".join(md))


def _bbox(value: str) -> Bbox:
    parts = [float(v) for v in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox is min_lng,min_lat,max_lng,max_lat")
    return parts[0], parts[1], parts[2], parts[3]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument(
        "--source",
        default="osm",
        help="building.source to sample; 'any' for all sources",
    )
    where = ap.add_mutually_exclusive_group()
    where.add_argument(
        "--area",
        choices=sorted(AREAS),
        help="a reference area (see AREAS); sets the bounding box",
    )
    where.add_argument(
        "--bbox",
        type=_bbox,
        default=None,
        help="limit the sample: min_lng,min_lat,max_lng,max_lat",
    )
    ap.add_argument(
        "--overpass-query",
        action="store_true",
        help="print the Overpass query for --area/--bbox and exit",
    )
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument(
        "--accuracies",
        default="5,10,20,30",
        help="comma-separated reported GPS accuracies in metres",
    )
    ap.add_argument(
        "--trials",
        type=int,
        default=1,
        help="jittered fixes per building per accuracy level",
    )
    ap.add_argument("--unmapped-fraction", type=float, default=0.1)
    ap.add_argument("--density-m", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default="simulation/out")
    args = ap.parse_args()
    bbox: Optional[Bbox] = AREAS[args.area].bbox if args.area else args.bbox

    if args.overpass_query:
        if bbox is None:
            ap.error("--overpass-query needs --area or --bbox")
        print(overpass_query(bbox))
        return

    accuracies = [float(a) for a in args.accuracies.split(",")]
    source = None if args.source == "any" else args.source
    engine = create_engine(
        "postgresql+psycopg2://", creator=lambda: psycopg2.connect(args.dsn)
    )

    with Session(engine) as db:
        try:
            targets = sample_targets(
                db,
                source=source,
                bbox=bbox,
                sample=args.sample,
                density_m=args.density_m,
                seed=args.seed,
            )
            if not targets:
                raise SystemExit(
                    f"No buildings with source={args.source!r} in range. Import "
                    "some first: python -m app.cli.import_footprints "
                    "--source-type osm --source <file>"
                )
            hold_out(db, targets, args.unmapped_fraction, args.seed)
            trials = replay(db, targets, accuracies, args.trials, args.seed)
        finally:
            db.rollback()  # never persist the hold-out deletes

    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "area": args.area,
        "area_description": AREAS[args.area].description if args.area else None,
        "source": args.source,
        "bbox": bbox,
        "buildings": len(targets),
        "held_out": sum(1 for t in targets if t.held_out),
        "accuracies_m": accuracies,
        "trials_per_level": args.trials,
        "density_m": args.density_m,
        "seed": args.seed,
    }
    summary = summarise(trials)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = f"_{args.area}" if args.area else ""
    out_dir = os.path.join(args.out, f"osm_replay{label}_{stamp}")
    write_outputs(out_dir, trials, summary, meta)
    print(f"Wrote {len(trials)} trials to {out_dir}")
    print(json.dumps(summary["by_accuracy_m"], indent=2))


if __name__ == "__main__":
    main()
