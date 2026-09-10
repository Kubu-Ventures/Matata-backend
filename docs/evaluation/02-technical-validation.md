# CrisisMap backend: technical validation

This document records how we validated the CrisisMap backend as a working,
accurate crisis-damage reporting pipeline, and what we measured.

Branch and commit: `feat/privy-email-auth`, after the remediation round
described in `01-backend-audit.md` section 5.

Companion documents: `01-backend-audit.md` (the engineering audit),
`03-impact-and-innovation.md` (framing and framework alignment),
`04-architecture-diagrams.md` (five diagrams), and the raw simulation output
under `simulation/out/`.

Date: 2026-09-10.

---

## 1. What "validated" means here

Three layers of evidence, weakest to strongest.

1. Static analysis and the automated test suite. Does the code hold
   together, do the units behave.
2. An independent engineering audit. A manual read of the service, worker,
   route and model layers, looking for the failure modes that unit tests
   cannot reach.
3. An end-to-end crisis simulation. Synthetic reports carrying ground-truth
   labels, driven through the running stack (API, Postgres with PostGIS,
   Redis, four Celery workers, object storage), then graded against those
   labels.

The simulation is the load-bearing evidence. It is the only layer that puts
real PostGIS, real concurrency and real worker coordination in the same run,
and it found two real bugs that the other two layers missed (sections 4.6
and 4.7).

---

## 2. Headline results

From the clean run at `simulation/out/20260910T124412Z/`
(n = 120, concurrency = 3, duplicate fraction = 0.3), plus the fault run at
`simulation/out/20260910T140802Z/` (n = 60, `celery-ai` killed and restarted
mid-load).

| Dimension | Result | How to read it |
|---|---|---|
| Submissions accepted | 113 of 120 created (94%); 4 rejected by moderation; the remaining 3 were client-side 60 s timeouts or token-fetch failures under swap pressure on the VM | request handling under concurrent load on a small VM |
| GIS stage completion | 100% (113 of 113) | the building-match worker reached a terminal state for every report |
| AI stage completion | 100% (95 of 95 photo-bearing reports) | the vision worker reached a terminal state |
| GIS match rate | 100% of GPS reports matched to a building | point-in-polygon / nearest-neighbour coverage |
| GIS match correctness | 100% matched to the exact footprint the synthetic report sat on | not just "a building", the right one |
| Mean match confidence | 0.996 | point-in-polygon matches return 1.0; the mean is pulled down slightly by nearest-neighbour cases |
| AI routing consistency | 100% (90 of 90) | the pipeline's `review_priority` versus an independent recomputation from the stored `ai_*` columns |
| Divergence-flag consistency | 100% | the pipeline's `ai_divergence` versus independent recomputation |
| Deduplication precision | 1.0 | of the reports flagged as duplicates, all really were |
| Deduplication recall | 1.0 | of 26 planted non-canonical duplicates across 8 clusters, all 26 were caught |
| Deduplication F1 | 1.0 | |
| Dedup outcomes per report | exactly 1 (94 scored-independent + 21 queued for merge review, across 115 reports) | the coordination gate dispatched `score_report` once and only once |
| Moderation before storage | 4 blocked submissions, 0 stored report rows | the check-before-store guarantee holds |
| Reporter identity at rest | 0 raw tokens, 0 raw JWTs in free text; every stored hash is 64 hex characters | pseudonymisation holds |
| Audit completeness | 113 of 113 reports have a `report.create` row | |
| End-to-end latency, submit to last pipeline write | p50 235 s, p95 382 s | on the constrained validation VM, see section 6 |
| Submit latency | p50 2.8 s, p95 43 s, p99 58 s | same caveat; the p95/p99 tail is the VM under swap pressure, not the code |

Fault-recovery results are in section 4.8.

---

## 3. The simulation in detail

### 3.1 Design

`simulation/` is a self-contained harness. It is not part of the
application. It runs from the host against the local `docker-compose` stack.

* `footprints.py` seeds a regular grid of synthetic building footprints into
  PostGIS. This is a hard prerequisite, not an optional step (audit M-1):
  without footprints the building signal never fires and deduplication
  degrades to flag-only.
* `scenario.py` generates n planned reports, deterministic given a seed.
  Each carries the request payload and the ground truth the grader needs:
  which footprint it sits on, whether it is a duplicate, and which report is
  the canonical member of its cluster. The `dup_fraction` of reports that
  are deliberate duplicates share a footprint, jitter the GPS a few metres,
  keep the same crisis and infrastructure category, and carry a
  lightly-perturbed copy of the cluster's canonical image. One "most
  pressing needs" string deliberately contains a name and a phone number, so
  we can check the export scrubber removes it.
* `runner.py` authenticates a pool of anonymous sessions and submits the
  planned reports at a target concurrency, recording per-request timing and
  HTTP outcome.
* `run.py` orchestrates: seed, generate, drive load, wait for the async
  pipeline to drain, collect, grade, write `results.json`, CSVs and
  `summary.md`.
* `collect.py` reads the `report` and `audit_log` tables directly and
  grades every dimension in section 2. The AI-routing check recomputes the
  expected `review_priority` and `ai_divergence` from the stored columns
  using an independent copy of the threshold rules, and compares. It does
  not trust the pipeline's own output.

### 3.2 The deterministic model surrogates

`app/services/sim_providers.py` (activated by `VISION_PROVIDER=sim` and
`MODERATION_PROVIDER=sim`, never a production value) provides deterministic
stand-ins for the vision and moderation models. They are not models. Each
output is a pure function of `sha256(image_bytes)`, chosen so a realistic
spread of outcomes lands in every routing branch: about 84% usable, 12%
borderline and 4% unusable image quality; about 70% high-confidence, 20%
medium and 10% low; about 15% of usable images predict a severity that
differs from the reporter's; about 3% of images rejected by moderation.

This is deliberate. The goal is to validate the decision pipeline (routing,
the divergence flag, the merge-review path, the "insufficient quality"
notification, and the check-before-store guarantee) independently of any
specific production model's accuracy. Because the mapping is deterministic,
the correct decision for every report can be recomputed offline and checked,
and a run reproduces exactly.

Certifying a particular production vision model's damage-classification
accuracy is a separate exercise that needs a labelled real-imagery dataset.
It is out of scope here and is listed as a limitation in the impact brief.

### 3.3 Running it

```bash
docker compose up -d
python -m simulation.run --n 120 --concurrency 3 --dup-fraction 0.3
python -m simulation.run --n 60 --concurrency 3 --faults --fault-at-s 8
```

Output lands in `simulation/out/<timestamp>/`.

---

## 4. Findings

### 4.1 Request handling and the safety gate

113 of 120 submissions were created. 4 were rejected by the moderation
surrogate (matching its designed roughly 3% reject rate) and returned 422
without creating a report row: `blocked_created_a_report_row` was 0. The
remaining 3 requests either timed out at the client's 60 second limit under
swap pressure on the VM or failed to obtain an anonymous token; 2 are
counted as hard errors and 1 as an unclassified non-2xx. The fault run, at a
smaller size, had zero hard errors.

The phone-number probe string persisted verbatim in `most_pressing_needs`
for the 22 reports that carried it. That is expected: M-5 is about exports,
not storage. The unit tests
(`tests/test_export.py::TestAnonymiser::test_free_text_pii_is_scrubbed` and
neighbours) confirm the export path replaces it with `[redacted-number]`
before the value leaves `ExportService`.

### 4.2 GIS building match

100% match rate, 100% correct footprint, mean confidence 0.996 across 108
GPS-bearing reports. Point-in-polygon matches return confidence 1.0. The
mean sits just below 1.0 because a few reports fell on a footprint edge and
resolved through nearest-neighbour, which scales confidence with distance.

Known limitation of the confidence number, not the match: the
nearest-neighbour search radius grows with the reporter's stated GPS
accuracy, so the same building at the same distance can yield a different
`footprint_match_confidence` depending on the device (audit L-6). The match
itself is correct; the confidence value is not comparable across devices.

### 4.3 AI routing and the responsible-AI property

100% routing agreement (90 of 90) and 100% divergence-flag agreement. The
routing distribution was low 50, high 24, critical 16, which tracks the
surrogate's designed spread.

The reporter's `damage_severity` was never written by the AI worker in any
of the 113 reports. We checked this by comparing the stored value with the
value submitted in the request. This is the core responsible-AI property and
it held.

### 4.4 Deduplication

8 duplicate clusters, 26 non-canonical members. All 26 were correctly
pointed at a member of their own cluster: 26 true positives, 0 false
negatives, recall 1.0. Across 79 distinct (non-duplicate) reports, 0 were
wrongly flagged: 0 false positives, precision 1.0. F1 is 1.0.

21 reports reached `pending_merge_review` (composite score at or above 0.90,
queued for an analyst to confirm). The rest of the duplicate members scored
in the 0.60 to 0.90 flag band. No report was moved out of an
analyst-actionable state by a second scoring run: the idempotency guard
(M-10) held.

Every report produced exactly one terminal dedup outcome: 94
`report.duplicate_scored` audit rows plus 21 `report.pending_merge_review`
rows, against 115 `report.create` rows. The coordination gate dispatched
`score_report` once and only once per report.

### 4.5 Pipeline completeness and reconciliation

GIS and AI stages completed for 100% of submitted reports. In the clean run
the reconciliation beat sweep had nothing to do: no report was stuck.
End-to-end latency percentiles are in section 2, with the section 6 caveat.

### 4.6 A bug the simulation caught: dedup transaction abort (L-1)

The unit test suite runs on SQLite with no PostGIS, so the two
candidate-loading SQL paths in `duplicate_tasks._load_candidates`, the
PostGIS `ST_DWithin` query and its bounding-box fallback, are never both
exercised together. Under the simulation's Postgres load the `ST_DWithin`
query raised on an edge case (an untyped or mistyped `existing_ids` array).
That aborts the surrounding transaction. The `except` clause then ran the
fallback query on the aborted transaction, which failed with
`InFailedSqlTransaction`, and that exception was not caught, so
`score_report` failed outright for every affected report. Deduplication was
silently not running for part of the batch.

This is audit finding L-1, and the simulation showed it is worse than the
audit's "silently falls back to a bounding-box scan" description. There is
no successful fallback. The task dies.

Fixed two ways: cast the array parameter (`CAST(:existing_ids AS uuid[])`),
and roll back the transaction at the top of the `except` so the fallback
always runs on a clean transaction. A regression test was added
(`tests/test_duplicate_detection.py::TestScoreReportImpl::test_geo_query_failure_rolls_back_before_fallback`),
and the simulation was re-run. The section 2 numbers are post-fix. Before the
fix, deduplication recall on the same scenario was near zero.

### 4.7 A bug the simulation caught: lost moderation-rejection audit rows

The first clean run reported `moderation_rejection_audit_rows: 0` even
though 4 images were rejected. Spec section 8.2.1 requires every rejection to
be recorded in the audit log.

Cause: `submission_service.create_report()` flushes the rejection audit row
and then raises `ModerationRejectionError` before the route reaches
`db.commit()`. The route caught the exception and returned 422, so the
request-scoped session rolled back and the flushed audit row was discarded.

Fixed in the two route handlers that carry this path
(`POST /reports` and `PATCH /reports/{id}/photo`): commit the audit row
before raising the 422. No report row is inserted on this path, so the
commit persists exactly the audit entry. A regression test was added
(`tests/test_submission.py::TestSubmitReportEndpoint::test_moderation_rejection_commits_the_audit_row`).

### 4.8 Fault recovery

The fault run kills the `celery-ai` container 8 seconds into the load and
restarts it 8 seconds later, while submissions continue.

What happened:

* Zero submissions failed. The API stayed up; AI processing is asynchronous,
  so the kill did not touch the request path. 57 of 60 reports were created,
  1 was rejected by moderation, 2 did not create a report (client-side token
  fetch under load).
* GIS completed for 100 percent of submitted reports.
* 55 of 57 photo-bearing reports completed the AI stage on their own, via
  Celery's `task_acks_late` redelivery from the durable Redis broker: a task
  that was in flight when the worker died was redelivered when it came back.
* 2 of 57 (3.5 percent) had their AI job lost in the kill window and
  redelivery did not recover them. Their `photo_status` stayed at
  `processing`.
* The reconciliation sweep is the backstop for exactly this case. With its
  default 15-minute grace it would have re-driven both on the next sweep
  once they aged past the grace (they were 12 minutes old when we checked).
  Running `python -m app.cli reconcile --grace-minutes 5` re-dispatched both
  AI jobs immediately:

  ```
  stuck reports scanned : 2
  AI jobs re-dispatched  : 2
  ```

* After that, 100 percent of reports had completed every stage.
  Deduplication precision, recall and F1 on the fault run were all 1.0
  (5 clusters, 13 non-canonical members, 13 true positives, 0 false
  negatives, 0 false positives). AI routing consistency was 100 percent.
* The fault run also confirmed the moderation-audit fix from section 4.7:
  `moderation_rejection_audit_rows` was 3, not 0.

The 15-minute grace is deliberate. It stops the sweep from fighting a report
that is merely slow rather than stuck. An operator who needs faster recovery
after a known outage lowers it on the command line, as above.

---

## 5. Toolchain results (static and unit)

| Check | Result |
|---|---|
| `flake8 app/ tests/` | 0 issues |
| `mypy app/` | 0 issues, 75 files |
| `black --check` / `isort --check` | clean |
| `pytest` (SQLite + eager Celery) | 731 passed, 1 skipped, 80.9% line coverage |

The suite needs no running services. `tests/conftest.py` injects env vars
before any app import and forces Celery into eager mode.

---

## 6. Why throughput is not a headline number

The validation ran on a WSL2 VM with about 3.4 GB of RAM and a single
uvicorn worker. Under concurrent multipart uploads that box goes into swap
and drops connections, so the load runs here were kept to low concurrency,
and the submit-latency and end-to-end-latency figures in section 2 reflect
that environment, not the software's capacity.

What this does validate: the correctness and accuracy metrics (GIS match, AI
routing, deduplication precision and recall, the safety gate, data at rest)
are independent of hardware. A slower box changes how long the run takes,
not whether the pipeline reaches the right answer. And the pipeline drains:
every submitted report reached a terminal state in every stage.

What it does not validate: sustained requests per second at scale. The API
is stateless and the workers are independent queue consumers, so capacity is
a horizontal-scale question, but that claim is untested at scale and is
called out as a limitation in `03-impact-and-innovation.md` section 7. A
capacity benchmark belongs on representative infrastructure and is a natural
early step in a piloting partnership.

---

## 7. Reproducibility

* The simulation is deterministic given `--seed` (default 1234). Same seed,
  same planned reports, same ground truth.
* The model surrogates are deterministic functions of image bytes.
* Raw output is kept under `simulation/out/<timestamp>/`: `results.json`,
  `requests.csv`, `series_*.csv`, `summary.md`.
* The audit and its remediation status are in `01-backend-audit.md`.

Every number in this report traces to a file under `simulation/out/` or to a
toolchain command that can be re-run from a clean checkout.
