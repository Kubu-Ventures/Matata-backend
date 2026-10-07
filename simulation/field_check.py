"""Score automatic matching against a real-phone field test.

    python -m simulation.field_check --since 2026-10-10T06:00:00Z

Field protocol (see the prep book, section 10a): stand at a mapped building,
start a report, and on the building picker tap the building you are really
at. That tap is the ground truth (``reporter_confirmed_building_id``). If
your building isn't among the three offered, choose "Not sure" (counts as a
top-3 miss) or "My building isn't on this map" (counts as unmapped).

This script re-runs the automatic matcher on each report's real GPS fix and
accuracy, exactly as if the reporter had not tapped anything, and compares
the result with the tap. Nothing is written to the database.
"""

from __future__ import annotations

import argparse
import os
import statistics
from typing import Optional

import psycopg2
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# GISService reads app settings at import time; no secrets are needed here.
for _var in ("SECRET_KEY", "JWT_SECRET_KEY", "PHONE_HASH_SALT"):
    os.environ.setdefault(_var, "field-check-placeholder-not-a-secret")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://field:field@localhost:5432/field"
)

from app.services.gis_service import GISService  # noqa: E402

DEFAULT_DSN = os.environ.get(
    "SIM_DSN",
    "dbname=matata_db user=matata password=matata host=localhost port=5432",
)


def _pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.0f}%" if den else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument(
        "--since", required=True, help="only reports created after this ISO time"
    )
    args = ap.parse_args()

    engine = create_engine(
        "postgresql+psycopg2://", creator=lambda: psycopg2.connect(args.dsn)
    )
    with Session(engine) as db:
        rows = db.execute(
            text("""
                SELECT id, lat, lng, gps_accuracy_m,
                       reporter_confirmed_building_id::text AS truth,
                       reporter_building_missing AS missing
                FROM report
                WHERE created_at >= CAST(:since AS timestamptz)
                  AND lat IS NOT NULL AND lng IS NOT NULL
                ORDER BY created_at
                """),
            {"since": args.since},
        ).fetchall()
        gis = GISService(db)

        scored, top1, top3, unsure, missing = 0, 0, 0, 0, 0
        accuracies: list[float] = []
        confident, confident_right = 0, 0
        print(f"{'report':8} {'acc m':>6} {'auto':>5} {'top3':>5} {'conf':>5}")
        for r in rows:
            if r.missing:
                missing += 1
                continue
            if r.gps_accuracy_m is not None:
                accuracies.append(float(r.gps_accuracy_m))
            if r.truth is None:  # "Not sure": true building not offered
                unsure += 1
                scored += 1
                continue
            match = gis.match_building(
                lat=r.lat, lng=r.lng, accuracy_m=r.gps_accuracy_m, with_candidates=True
            )
            auto: Optional[str] = str(match.building_id) if match.building_id else None
            in3 = any(str(c.building_id) == r.truth for c in match.candidates)
            scored += 1
            top1 += auto == r.truth
            top3 += in3
            if match.confidence >= 0.8:
                confident += 1
                confident_right += auto == r.truth
            print(
                f"{str(r.id)[:8]:8} {r.gps_accuracy_m or 0:6.1f} "
                f"{'yes' if auto == r.truth else 'no':>5} "
                f"{'yes' if in3 else 'no':>5} {match.confidence:5.2f}"
            )
        db.rollback()

    print()
    print(f"Reports scored: {scored} (plus {missing} marked 'not on this map')")
    if accuracies:
        print(f"Median reported GPS accuracy: {statistics.median(accuracies):.1f} m")
    print(f"Automatic match right (top-1): {_pct(top1, scored)}")
    print(f"Right building among the 3 offered (top-3): {_pct(top3, scored)}")
    print(f"'Not sure' (true building not offered): {unsure}")
    print(
        f"Confidence >= 0.8: {confident} matches, "
        f"right {_pct(confident_right, confident)}"
    )


if __name__ == "__main__":
    main()
