"""Top-level simulation orchestrator.

    python -m simulation.run --n 800 --concurrency 40 --out simulation/out

Steps: seed synthetic footprints -> generate planned reports (with ground
truth) -> drive the load -> wait for the async pipeline to drain -> collect
outcomes from Postgres -> write results.json + CSVs + summary.md.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import time
from datetime import datetime, timezone

import psycopg2

from simulation import collect as collect_mod
from simulation import footprints as fp_mod
from simulation import scenario as scen
from simulation.runner import run_load

# Defaults target the local docker-compose stack (docker-compose.yml):
#   postgres service  → POSTGRES_USER/PASSWORD/DB = matata / matata / matata_db,
#                       published on host port 5432
#   app service       → published on host port 8081
# Override with SIM_DSN / SIM_BASE for any other deployment.
DEFAULT_DSN = os.environ.get(
    "SIM_DSN",
    "dbname=matata_db user=matata password=matata host=localhost port=5432",
)
DEFAULT_BASE = os.environ.get("SIM_BASE", "http://localhost:8081")


def wait_for_drain(dsn: str, report_ids: list[str], *, timeout_s: int = 600,
                   poll_s: int = 5) -> dict:
    """Poll until GIS + AI stages settle for all reports, or timeout."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE footprint_match_confidence IS NOT NULL) AS gis,
                  count(*) FILTER (WHERE photo_status IN
                        ('accepted','insufficient_quality','ai_processing_failed')
                        OR photo_url IS NULL) AS ai_or_nophoto,
                  count(*) FILTER (WHERE possible_duplicate_of_id IS NOT NULL
                        OR duplicate_score IS NOT NULL) AS dedup,
                  count(*) AS total
                FROM report WHERE id = ANY(%s::uuid[])
                """,
                (report_ids,),
            )
            g, a, d, t = cur.fetchone()
            last = {"gis": g, "ai_or_nophoto": a, "dedup_touched": d, "total": t}
            print(f"  drain: gis={g}/{t}  ai/nophoto={a}/{t}  dedup_touched={d}",
                  flush=True)
            if t and g >= t and a >= t:
                # give dedup a moment after AI/GIS settle
                time.sleep(poll_s * 2)
                break
        time.sleep(poll_s)
    conn.close()
    return last


def _write_outputs(outdir: str, metrics, run, plans) -> None:
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "results.json"), "w") as fh:
        json.dump({"summary": metrics.summary, "findings": metrics.findings},
                  fh, indent=2)
    with open(os.path.join(outdir, "requests.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["idx", "identity", "http_status", "report_id", "latency_ms",
                    "moderation_blocked", "error"])
        for r in run.results:
            w.writerow([r.idx, r.identity, r.http_status, r.report_id or "",
                        f"{r.latency_ms:.1f}", r.moderation_blocked,
                        (r.error or "")[:120]])
    for name in ("submit_latency_ms", "e2e_latency_s", "gis_confidence"):
        xs = metrics.series.get(name, [])
        with open(os.path.join(outdir, f"series_{name}.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([name])
            for x in xs:
                w.writerow([f"{x:.4f}"])
    with open(os.path.join(outdir, "summary.md"), "w") as fh:
        fh.write(f"# Simulation summary\n\n_generated {datetime.now(timezone.utc)}_\n\n")
        fh.write("```json\n")
        fh.write(json.dumps(metrics.summary, indent=2))
        fh.write("\n```\n\n## Findings\n\n")
        for x in metrics.findings:
            fh.write(f"- {x}\n")


async def _amain(args) -> None:
    print(f"[1/5] seeding footprints (need >= {args.n}) ...", flush=True)
    ext_ids, points = fp_mod.seed(args.dsn, args.n)
    print(f"      seeded {len(ext_ids)} footprints", flush=True)

    print("[2/5] generating scenario ...", flush=True)
    plans = scen.generate(
        n=args.n, seed=args.seed, footprint_ids=ext_ids,
        footprint_points=points, photo_fraction=args.photo_fraction,
        dup_fraction=args.dup_fraction,
    )
    n_dup = sum(1 for p in plans if p.dup_cluster is not None and not p.is_canonical)
    print(f"      {len(plans)} planned; {n_dup} planted non-canonical duplicates",
          flush=True)

    print(f"[3/5] load: concurrency={args.concurrency} ...", flush=True)
    if args.faults:
        drv = asyncio.create_task(run_load(
            base=args.base, plans=plans, concurrency=args.concurrency))
        await asyncio.sleep(args.fault_at_s)
        print("      >>> fault injection: killing celery-ai", flush=True)
        os.system("docker compose kill celery-ai >/dev/null 2>&1")
        await asyncio.sleep(8)
        os.system("docker compose up -d celery-ai >/dev/null 2>&1")
        print("      >>> celery-ai restarted", flush=True)
        run = await drv
    else:
        run = await run_load(base=args.base, plans=plans,
                             concurrency=args.concurrency)
    ok_ids = [r.report_id for r in run.results if r.report_id]
    print(f"      created={len(ok_ids)} blocked={sum(1 for r in run.results if r.moderation_blocked)} "
          f"wall={run.wall_end-run.wall_start:.1f}s "
          f"rps={run.throughput_rps:.1f}", flush=True)

    print("[4/5] waiting for pipeline drain ...", flush=True)
    drain = wait_for_drain(args.dsn, ok_ids, timeout_s=args.drain_timeout_s)
    print(f"      drain state: {drain}", flush=True)

    print("[5/5] collecting + grading ...", flush=True)
    metrics = collect_mod.collect(dsn=args.dsn, plans=plans, run=run)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = os.path.join(args.out, ts)
    _write_outputs(outdir, metrics, run, plans)
    print(f"\n==== wrote {outdir} ====\n", flush=True)
    print(json.dumps(metrics.summary, indent=2), flush=True)
    print("\nFindings:", flush=True)
    for x in metrics.findings:
        print("  -", x, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--concurrency", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--photo-fraction", type=float, default=0.85)
    ap.add_argument("--dup-fraction", type=float, default=0.25)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out", default="simulation/out")
    ap.add_argument("--drain-timeout-s", type=int, default=900)
    ap.add_argument("--faults", action="store_true",
                    help="kill+restart celery-ai mid-run to test recovery")
    ap.add_argument("--fault-at-s", type=float, default=6.0)
    args = ap.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
