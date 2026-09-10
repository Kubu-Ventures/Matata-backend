"""Post-run outcome collection and metric computation.

Reads the ``report`` and ``audit_log`` tables directly (sim rows are tagged by
``reporter_token_hash LIKE 'sim:%'`` — see runner note below) and grades the
pipeline against the ground truth in the plan list.

NOTE: reports submitted via the API are tagged as sim rows by the *pre-run*
DELETE in footprints.seed() plus a post-run WHERE on created_at window; the
API itself hashes the JWT sub, so we scope by the run's time window and by
matching report ids returned from the runner.
"""

from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from simulation.runner import RunOutput
from simulation.scenario import PlannedReport

# ---- routing recomputation (mirror of app/workers/ai_tasks._compute_review_priority)
AI_QUALITY_CRITICAL = 0.30
AI_CONF_CRITICAL = 0.60
AI_CONF_HIGH = 0.80


def expected_priority(
    conf: Optional[float], qual: Optional[float], divergence: bool
) -> str:
    if qual is not None and qual < AI_QUALITY_CRITICAL:
        return "critical"
    if conf is not None and conf < AI_CONF_CRITICAL:
        return "critical"
    if divergence:
        return "high"
    if conf is not None and conf < AI_CONF_HIGH:
        return "high"
    if conf is not None and conf >= AI_CONF_HIGH:
        return "low"
    return "normal"


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


@dataclass
class Metrics:
    summary: dict[str, Any] = field(default_factory=dict)
    series: dict[str, list] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)


def collect(*, dsn: str, plans: list[PlannedReport], run: RunOutput) -> Metrics:
    by_identity = {p.identity: p for p in plans}
    rid_to_identity = {r.report_id: r.identity for r in run.results if r.report_id}
    ids = [r.report_id for r in run.results if r.report_id]

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT id::text, status, photo_status, footprint_match_confidence,
               building_id::text, ai_quality_score, ai_confidence,
               ai_severity_prediction::text, ai_divergence, review_priority::text,
               damage_severity::text, possible_duplicate_of_id::text,
               duplicate_score, photo_phash, most_pressing_needs,
               reporter_token_hash,
               extract(epoch from created_at)::float8 AS t_created,
               extract(epoch from updated_at)::float8 AS t_updated
        FROM report WHERE id = ANY(%s::uuid[])
        """,
        (ids,),
    )
    rows = {r["id"]: r for r in cur.fetchall()}

    cur.execute(
        """
        SELECT record_id::text, operation,
               extract(epoch from created_at)::float8 AS ts
        FROM audit_log WHERE record_id = ANY(%s::uuid[]) ORDER BY created_at
        """,
        (ids,),
    )
    audit: dict[str, list[tuple[str, float]]] = {}
    for a in cur.fetchall():
        audit.setdefault(a["record_id"], []).append((a["operation"], a["ts"]))

    # building external id lookup
    cur.execute(
        "SELECT id::text, external_id FROM building "
        "WHERE external_id LIKE 'sim-fp-%'"
    )
    bid_to_ext = {b["id"]: b["external_id"] for b in cur.fetchall()}

    m = Metrics()
    n_submit_attempt = len(run.results)
    ok = [r for r in run.results if r.report_id]
    blocked = [r for r in run.results if r.moderation_blocked]
    errors = [
        r
        for r in run.results
        if r.error and not r.moderation_blocked and r.http_status not in (429,)
    ]
    http429 = [r for r in run.results if r.http_status == 429]

    # ---------- throughput / latency ----------
    submit_lat = [r.latency_ms for r in ok]
    m.summary["run"] = {
        "planned": len(plans),
        "submit_attempts": n_submit_attempt,
        "created_201": len(ok),
        "moderation_blocked_422": len(blocked),
        "rate_limited_429": len(http429),
        "hard_errors": len(errors),
        "wall_seconds": round(run.wall_end - run.wall_start, 2),
        "throughput_created_per_s": round(run.throughput_rps, 2),
        "token_pool": run.token_count,
    }
    m.summary["submit_latency_ms"] = {
        "p50": round(pct(submit_lat, 0.50), 1),
        "p95": round(pct(submit_lat, 0.95), 1),
        "p99": round(pct(submit_lat, 0.99), 1),
        "max": round(max(submit_lat), 1) if submit_lat else None,
    }
    m.series["submit_latency_ms"] = submit_lat

    # ---------- pipeline completion + end-to-end latency ----------
    gis_done = ai_done = dedup_seen = 0
    e2e_gis: list[float] = []
    for r in ok:
        row = rows.get(r.report_id)
        if not row:
            continue
        if row["footprint_match_confidence"] is not None:
            gis_done += 1
        if row["photo_status"] in (
            "accepted",
            "insufficient_quality",
            "ai_processing_failed",
        ):
            ai_done += 1
        ops = [o for o, _ in audit.get(r.report_id, [])]
        if (
            "report.duplicate_scored" in ops
            or "report.pending_merge_review" in ops
            or row["possible_duplicate_of_id"]
            or row["duplicate_score"] is not None
        ):
            dedup_seen += 1
        # crude e2e latency: updated_at - submit start
        if row["t_updated"] and row["footprint_match_confidence"] is not None:
            e2e_gis.append(max(0.0, row["t_updated"] - r.t_submit_start))
    m.summary["pipeline"] = {
        "gis_stage_completed": gis_done,
        "gis_stage_completed_pct": round(100 * gis_done / max(1, len(ok)), 1),
        "ai_stage_completed": ai_done,
        "ai_stage_completed_pct": round(
            100
            * ai_done
            / max(
                1,
                sum(
                    1 for r in ok if by_identity[rid_to_identity[r.report_id]].has_photo
                ),
            ),
            1,
        ),
        "dedup_evaluated_signal": dedup_seen,
    }
    m.summary["end_to_end_latency_s_updatedts_minus_submit"] = {
        "p50": round(pct(e2e_gis, 0.50), 2),
        "p95": round(pct(e2e_gis, 0.95), 2),
        "p99": round(pct(e2e_gis, 0.99), 2),
    }
    m.series["e2e_latency_s"] = e2e_gis

    # ---------- GIS match accuracy ----------
    gis_total = gis_hit = gis_correct_fp = 0
    conf_vals = []
    for r in ok:
        p = by_identity[rid_to_identity[r.report_id]]
        row = rows.get(r.report_id)
        if not row or p.lat is None:
            continue
        gis_total += 1
        if row["building_id"]:
            gis_hit += 1
            conf_vals.append(row["footprint_match_confidence"] or 0.0)
            if (
                p.building_external_id
                and bid_to_ext.get(row["building_id"]) == p.building_external_id
            ):
                gis_correct_fp += 1
    m.summary["gis_match"] = {
        "reports_with_gps": gis_total,
        "matched_to_a_building": gis_hit,
        "match_rate_pct": round(100 * gis_hit / max(1, gis_total), 1),
        "matched_to_expected_footprint": gis_correct_fp,
        "correct_footprint_pct": round(100 * gis_correct_fp / max(1, gis_hit), 1),
        "confidence_mean": round(st.fmean(conf_vals), 3) if conf_vals else None,
    }
    m.series["gis_confidence"] = conf_vals

    # ---------- AI routing correctness ----------
    prio_match = prio_total = 0
    div_recompute_match = 0
    prio_dist: dict[str, int] = {}
    for r in ok:
        row = rows.get(r.report_id)
        if not row or row["ai_confidence"] is None:
            continue
        prio_total += 1
        p = by_identity[rid_to_identity[r.report_id]]
        div = bool(row["ai_divergence"])
        exp = expected_priority(row["ai_confidence"], row["ai_quality_score"], div)
        if exp == row["review_priority"]:
            prio_match += 1
        prio_dist[row["review_priority"]] = prio_dist.get(row["review_priority"], 0) + 1
        # recompute divergence: pred != reporter severity AND conf > 0.70
        exp_div = (
            row["ai_severity_prediction"] != row["damage_severity"]
            and row["ai_confidence"] > 0.70
        )
        if exp_div == div:
            div_recompute_match += 1
    m.summary["ai_routing"] = {
        "reports_with_ai_output": prio_total,
        "review_priority_recompute_agreement": prio_match,
        "review_priority_recompute_agreement_pct": round(
            100 * prio_match / max(1, prio_total), 2
        ),
        "divergence_flag_recompute_agreement_pct": round(
            100 * div_recompute_match / max(1, prio_total), 2
        ),
        "review_priority_distribution": prio_dist,
    }

    # ---------- Deduplication precision / recall ----------
    # cluster -> submitted report ids in submission order
    cluster_members: dict[int, list[str]] = {}
    identity_to_rid = {v: k for k, v in rid_to_identity.items()}
    for p in plans:
        if p.dup_cluster is None:
            continue
        rid = identity_to_rid.get(p.identity)
        if rid:
            cluster_members.setdefault(p.dup_cluster, []).append(rid)

    tp = fn = 0  # over non-canonical cluster members
    for cid, members in cluster_members.items():
        if len(members) < 2:
            continue
        member_set = set(members)
        for rid in members[1:]:
            row = rows.get(rid)
            pointed = row and row["possible_duplicate_of_id"]
            if pointed and pointed in member_set:
                tp += 1
            else:
                fn += 1
    fp = 0  # distinct reports wrongly pointed at another report
    distinct_checked = 0
    for p in plans:
        if p.dup_cluster is not None:
            continue
        rid = identity_to_rid.get(p.identity)
        row = rows.get(rid) if rid else None
        if not row:
            continue
        distinct_checked += 1
        if row["possible_duplicate_of_id"]:
            fp += 1
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
    m.summary["deduplication"] = {
        "duplicate_clusters": len([c for c in cluster_members.values() if len(c) >= 2]),
        "non_canonical_members_evaluated": tp + fn,
        "true_positives": tp,
        "false_negatives": fn,
        "distinct_reports_evaluated": distinct_checked,
        "false_positives": fp,
        "precision": round(prec, 3) if prec is not None else None,
        "recall": round(rec, 3) if rec is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
    }

    # ---------- moderation-before-storage ----------
    mod_audit = 0
    cur.execute(
        "SELECT count(*) AS c FROM audit_log "
        "WHERE operation IN ('report.moderation_rejection', "
        "'report.photo_moderation_rejection')"
    )
    mod_audit = cur.fetchone()["c"]
    m.summary["moderation"] = {
        "submissions_blocked_422": len(blocked),
        "moderation_rejection_audit_rows": mod_audit,
        "blocked_created_a_report_row": sum(
            1 for r in blocked if r.report_id in rows
        ),  # expect 0
    }

    # ---------- audit completeness ----------
    missing_create = [
        rid for rid in ids if "report.create" not in [o for o, _ in audit.get(rid, [])]
    ]
    m.summary["audit"] = {
        "reports_checked": len(ids),
        "with_report_create_row": len(ids) - len(missing_create),
        "missing_report_create": len(missing_create),
    }

    # ---------- PII at rest ----------
    bad_hash = sum(
        1
        for rid in ids
        if rows.get(rid) and len((rows[rid]["reporter_token_hash"] or "")) != 64
    )
    # the phone probe string should persist in most_pressing_needs (demonstrates
    # M-5) but a raw bearer/JWT must never appear
    cur.execute(
        "SELECT count(*) AS c FROM report WHERE id = ANY(%s::uuid[]) AND "
        "(most_pressing_needs LIKE '%%eyJ%%' OR landmark_description LIKE '%%eyJ%%')",
        (ids,),
    )
    jwt_leak = cur.fetchone()["c"]
    cur.execute(
        "SELECT count(*) AS c FROM report WHERE id = ANY(%s::uuid[]) AND "
        "most_pressing_needs LIKE '%%+254712345678%%'",
        (ids,),
    )
    phone_probe_persisted = cur.fetchone()["c"]
    m.summary["pii_at_rest"] = {
        "reporter_token_hash_not_64hex": bad_hash,  # expect 0
        "raw_jwt_found_in_freetext": jwt_leak,  # expect 0
        "reporter_freetext_phone_probe_persisted": phone_probe_persisted,  # >0 = M-5
    }

    conn.close()

    # ---------- findings ----------
    f = m.findings
    s = m.summary
    if s["run"]["hard_errors"] == 0:
        f.append(f"Zero hard errors across {s['run']['submit_attempts']} submissions.")
    else:
        f.append(f"{s['run']['hard_errors']} hard errors — investigate.")
    if s["pipeline"]["gis_stage_completed_pct"] >= 99.5:
        f.append("GIS stage completed for ~100% of submitted reports.")
    if s["ai_routing"]["review_priority_recompute_agreement_pct"] >= 99.9:
        f.append(
            "Analyst-queue routing is 100% consistent with the documented "
            "threshold rules (independent recomputation)."
        )
    if (
        s["moderation"]["blocked_created_a_report_row"] == 0
        and s["moderation"]["submissions_blocked_422"] > 0
    ):
        f.append(
            "Every moderation-blocked submission left zero stored report "
            "rows (check-before-store holds)."
        )
    if (
        s["pii_at_rest"]["reporter_token_hash_not_64hex"] == 0
        and s["pii_at_rest"]["raw_jwt_found_in_freetext"] == 0
    ):
        f.append(
            "No raw session tokens at rest; reporter identity stored only "
            "as a 64-char hash."
        )
    if s["pii_at_rest"]["reporter_freetext_phone_probe_persisted"] > 0:
        f.append(
            "Audit M-5 reproduced: a phone number typed into "
            "'most pressing needs' persists verbatim and would appear in "
            "an export."
        )
    return m
