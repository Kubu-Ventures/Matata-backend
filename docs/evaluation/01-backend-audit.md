# CrisisMap Backend -- Engineering Audit

**Scope:** full read of the service, worker, route, middleware, model and config
layers on branch `feat/privy-email-auth` (commit `90719fe`), plus static
analysis and the automated test suite. This is the internal "fix-before-you-pitch"
document; the outward-facing technical and impact reports are separate.

**Date:** 2026-09-10

---

## 1. Headline assessment

The codebase is **well above prototype quality**. Static analysis is clean,
the layering discipline (routes → services → workers, no cross-imports) is
real and consistently held, error shaping and audit logging are systematic,
and the async/Celery split is handled carefully (the commit-before-dispatch
race fix and the duplicate-scoring coordination gate are both non-trivial and
correctly reasoned).

Nothing found is catastrophic. The issues below are, in order: **one
unenforced access control**, **one correctness bug in the deduplication
signal**, and a cluster of medium-severity gaps around responsible-data
handling, idempotency/reconciliation, and the self-calibration loop. All are
fixable without architectural change.

### Toolchain results

| Check | Result |
|---|---|
| `flake8 app/ tests/` | **0 issues** |
| `mypy app/` | **0 issues** (73 files) |
| `bandit -r app/ -ll` | 1 medium, 9 low. The medium is `urlopen()` in the admin-only footprint-import CLI, already scheme-guarded -- low real risk. |
| `pytest` (SQLite + eager Celery) | **708 passed, 1 failed, 1 skipped -- 81.4% coverage** |

The single test failure -- `tests/test_gis.py::TestGISBuildingMatchEndpoint::test_returns_200_with_building_id`
-- is **not a product defect**. That test does not stub Redis and tries to
reach a real `localhost:6379`; every other test in the suite mocks it. It is a
test-hermeticity bug that turns CI red whenever Redis is absent. Fix: give it
the `mock_redis` fixture like its siblings.

---

## 2. Findings (severity-ranked)

### HIGH

#### H-1 · Responder regional access scoping is not enforced
`region_geojson` is a column on `analyst_account`, is accepted by
`POST /auth/analyst/register`, and is consumed by both `analyst_service`
(PostGIS `ST_Within` filter) and `analyst.py` via
`current_user.get("region_geojson")`. **But no token-issuance path ever puts
it into the JWT** -- `_build_access_token()` only carries `sub/role/tier/jti`,
and neither `verify_privy_and_issue_tokens`, `verify_otp`,
`issue_analyst_token`, nor `rotate_refresh_token` reads
`account.region_geojson`.

Net effect: `_region_geojson()` always returns `None`, the `ST_Within` filter
is never applied, and **every provisioned responder can read every report in
the system** -- feed, detail, SSE stream, and stats. The database column and
the provisioning API make the control look implemented.

*Files:* `app/services/auth_service.py:219-248,533-540,607-618,927-934`,
`app/api/v1/routes/analyst.py:104-110,585`.

*Fix:* add `region_geojson` to the access-token payload (and the refresh
payload so it survives rotation); populate it from the resolved
`AnalystAccount` in the two verify paths and the CLI issuer.

---

#### H-2 · Deduplication image signal compares two different hash algorithms
Two independent perceptual-hash implementations write the **same column**
`report.photo_phash`:

- `submission_service._compute_phash()` -- a hand-rolled 8×8 block-average
  hash (an *average hash*, not a DCT pHash), written at submission time.
- `ai_tasks._compute_phash()` -- `imagehash.phash()`, a true DCT pHash,
  written when the AI worker finishes and **overwrites** the submission value.

`duplicate_service._image_signal()` computes a Hamming distance between
whatever two strings are stored. During any processing backlog -- the normal
state during a live event -- candidate reports still hold the submission-time
*average hash* while the incoming report holds the AI-time *DCT hash*. The
Hamming distance between an aHash and a pHash of the **same image** is large
and noise-like, so the 20 %-weighted image signal produces both false
matches and missed matches. Additionally, if `imagehash` is not installed the
AI task writes `NULL`, silently destroying the signal for that report.

*Files:* `app/services/submission_service.py:172-236`,
`app/workers/ai_tasks.py:363-388,582-628`.

*Fix:* one algorithm. Either compute `imagehash.phash()` at submission time
too (it needs Pillow, already a dependency), or drop the submission-time
computation entirely and let the AI worker be the sole writer -- accepting
that photo-less-then-patched reports get their hash slightly later.

---

### MEDIUM

#### M-1 · Merge-review is unreachable without building footprints loaded
Composite score = `0.4·building + 0.3·gps + 0.2·image + 0.1·category`.
With no `building_id` match the ceiling is `0.3 + 0.2 + 0.1 = 0.6`, exactly
the `ANALYST_FLAG` threshold -- so the `≥ 0.9` `pending_merge_review` path
**cannot fire**. The README notes footprint import is optional; if it is
skipped, `building_id` is always `NULL` and deduplication silently degrades
to flag-only. This is an operational dependency that must be stated in the
deployment guide and satisfied in any evaluation run.
*Files:* `app/services/duplicate_service.py:51-65,308-354`.

#### M-2 · `feedback_type` mismatch zeroes the per-type accuracy breakdown
`transition_report_status()` writes `feedback_type = new_status.value`
(`"verified"` / `"rejected"`); `get_ai_accuracy()` buckets by
`("verify", "reject", "severity_override")`. The `verify` and `reject`
buckets are therefore permanently `count = 0` on the AI-accuracy dashboard.
The type-agnostic `agreement_rate` / `high_confidence_agreement_rate` are
unaffected, so the calibration threshold itself is still driven by real data.
*Files:* `app/services/analyst_service.py:604-608,1083-1090`.

#### M-3 · `transition_report_status()` has no valid-transition matrix
Any report -- including one already `verified` / `rejected` /
`pending_merge_review` -- can be transitioned to any of
`verified/rejected/duplicate`. Consequences: repeated `verify`/`reject`
walks `reporter_trust_tier` up and down within `[0, 2]`; a
`pending_merge_review` report can be `verified` directly, bypassing the
confirm-/reject-merge workflow and orphaning `possible_duplicate_of_id`.
*File:* `app/services/analyst_service.py:471-560`.

#### M-4 · `/reports/nearby` exposes the incident map with no auth
`GET /reports/nearby` requires no token and no app-level rate limit, and
returns exact `lat`/`lng`, `status`, `damage_severity` and timestamps for
every report near a point. Coordinate scanning reconstructs the full
incident map. For conflict settings this is a protection risk. The 30-second
per-cell cache is the only throttle.
*Files:* `app/api/v1/routes/reports.py:423-454`,
`app/services/submission_service.py:739-844`.

#### M-5 · Exports carry un-scrubbed reporter free text and exact coordinates
`landmark_description`, `most_pressing_needs` and `photo_url` are exported
verbatim in GeoJSON/CSV/Shapefile whose docstring guarantees the output is
"safe for external distribution". Reporters routinely put names, phone
numbers and third-party details into "most pressing needs". Exact `lat`/`lng`
are exported with no generalisation/jitter option for sensitive crisis types.
The identifier-level anonymisation (hash truncation, no notes, no phone) is
solid -- the free-text and geometry channels are the gap.
*File:* `app/services/export_service.py:1-18,80-113,121+`.

#### M-6 · Refresh-token rotation is not atomic; no reuse detection
`rotate_refresh_token()` is `GET` then `DELETE`, not `GETDEL` / Lua /
`WATCH`-`MULTI`. Two concurrent refreshes of the same token both succeed. A
replayed (already-rotated) token returns a generic "invalid" with no
token-family revocation and no security signal. The inline comment claims
"Atomically invalidate".
*File:* `app/services/auth_service.py:794-831`.

#### M-7 · Self-calibration runs only as a GET side effect
The divergence-threshold "active learning loop" recalibrates **only** when a
human opens `GET /analyst/ai-accuracy`. There is no scheduled job, so the
loop is closed opportunistically and the threshold goes stale after
`AI_DIVERGENCE_STALENESS_DAYS`. Calibration also uses **all-time** feedback
with no recency weighting (`SELECT * FROM ai_feedback`, unbounded), so once
the table is large the threshold responds very slowly to genuine model
drift -- the opposite of the stated intent. The two-key threshold write is
non-atomic.
*File:* `app/services/analyst_service.py:1004-1180`.

#### M-8 · Post-commit dispatch failures are never reconciled
In `submit_report`, if `redis.set(gate)` or `queue.publish_*` raises **after**
`await db.commit()`, the report is durably stored but never receives GIS / AI
/ dedup processing; the client gets a 500. The gate's 1-hour TTL cleans
itself up and the report sits `pending` forever. There is no sweep that
re-dispatches orphaned `pending` reports.
*File:* `app/api/v1/routes/reports.py:237-284`.

#### M-9 · Duplicate-scoring gate is a Redis single point of failure
`crisismap:report:{id}:pending_dup_steps` exists only in Redis, and
`mark_step_done_and_maybe_dispatch()` swallows every Redis error. If Redis is
unavailable when a worker reaches a terminal state, or is flushed, the gate
is lost and `score_report` never runs for that report -- with only a
debug/warning log. No reconciliation.
*File:* `app/workers/duplicate_dispatch.py:52-96`.

#### M-10 · `_score_report_impl` is not idempotent
No "act only if the report is still in an eligible state" guard. A second run
(gate double-decrement, retry after a partial commit, or a manual
re-dispatch) can move an analyst-actioned report back to
`pending_merge_review` or re-apply a flag. The audit `before_state` is
hard-coded `{"status": "pending"}` regardless of the real prior state.
*File:* `app/workers/duplicate_tasks.py:322-396,441-541`.

---

### LOW / NOTES

| # | Note |
|---|---|
| L-1 | `duplicate_tasks._load_candidates`: `id != ALL(:existing_ids)` with an empty list can raise Postgres *"cannot determine type of empty array"*, which the broad `except` swallows -- silently falling back to a bounding-box scan instead of `ST_DWithin` whenever there is no building match. Needs runtime confirmation (watch `celery-duplicate` logs for the fallback warning). `app/workers/duplicate_tasks.py:208-258`. |
| L-2 | Three rate limiters do `INCR` then conditional `EXPIRE`; a crash between them leaves a TTL-less key → permanent lockout for that identifier. `submission_service.py:258-261`, `auth_service.py:374-376`, `submission_service` nearby path. Use `SET k 0 EX n NX` + `INCR`. |
| L-3 | `hash_identifier()` is a single fast `SHA256(salt‖value)` over low-entropy inputs (phone numbers ≈ 10⁹ space). If the DB **and** the `.env` salt both leak, phone numbers are recoverable in seconds. Deterministic lookup rules out a slow KDF, but an HMAC keyed by a secret held in a KMS (not `.env`) is a clear upgrade. Disclose this honestly in the impact report rather than claiming strong anonymisation. `auth_service.py:152-169`. |
| L-4 | The 10-submissions/hour limit is keyed on JWT `sub`; a fresh `sub` is one unauthenticated `POST /auth/anonymous` away. It is per-session, not per-device. Abuse mitigation genuinely rests on moderation + trust tiers + analyst triage -- say so. `reports.py:206`. |
| L-5 | No application-level request-body size cap; the system relies entirely on `nginx client_max_body_size 20m`. A deployment that exposes uvicorn directly is unprotected against a multi-GB JSON body (the sanitisation middleware reads the whole body into memory). `middleware.py:172-214`. |
| L-6 | `GISService._nearest_neighbour` confidence = `1 − dist/radius`, and `radius` grows with reported GPS accuracy → the same building at the same distance yields a different `footprint_match_confidence` per device. The stored confidence is not comparable across reports. `gis_service.py:210-263,301-318`. |
| L-7 | `GISService.update_building_severity` takes MAX severity across **all** linked reports regardless of `status` (includes `rejected` / `duplicate`) and never downgrades. One rejected "destroyed" report pins a building to "destroyed" on the public heatmap indefinitely. `gis_service.py:137-176`. |
| L-8 | `get_nearby_reports` applies `LIMIT 50` on the bounding-box query **before** the precise haversine filter. In a dense hotspot, genuinely-near reports can be dropped from the pre-submission duplicate check. `submission_service.py:787-820`. |
| L-9 | Docstring drift: `duplicate_tasks.py` header still describes the old silent AUTO_MERGE; `ReviewPriority` docstring says `low` needs quality ≥ 0.60 but the code only checks `< 0.30`. |
| L-10 | `ai_tasks.py` calls `asyncio.run()` twice per task (a fresh event loop for the download and again for the vision call). Fine on the prefork pool; raises on a gevent/eventlet pool. Consider one `asyncio.run()` wrapping both. |
| L-11 | GIS task **retries the whole task** if `_invalidate_gis_caches` hits a transient Redis error *after* the DB commit, instead of swallowing it the way the AI task swallows SSE-publish failures. `gis_tasks.py:213-221,248-254`. |
| L-12 | `reports.py::_get_raw_token` is dead code. |
| L-13 | `is_agreement` on a `reject` compares the AI severity guess to the reporter's claimed severity; a rejected spam report where the two happen to match is recorded as "AI agreed", mildly polluting the calibration signal. `analyst_service.py` `_log_ai_feedback`. |

---

## 3. What is genuinely solid (for the technical report)

- **Layer discipline** -- verified: no `sqlalchemy`/`redis` imports in routes, no
  `app.api` imports in services, enums single-sourced.
- **Moderation before storage** -- `submission_service` calls `moderate()` and
  evaluates the result before any `upload_image()`; rejection writes an audit
  row and raises, and no storage object is created. Correct per spec §8.2.
- **AI never overwrites `damage_severity`** -- the AI worker writes only
  `ai_*` columns and `review_priority`; the reporter's classification is
  immutable. This is the core responsible-AI property and it holds.
- **Commit-before-dispatch** -- background jobs are published by the route
  *after* `await db.commit()`, eliminating the worker "row not found" race.
  The reasoning is documented in both the service and the route.
- **Duplicate-scoring coordination gate** -- the ±terminal-path accounting in
  `duplicate_dispatch` is careful and, retry semantics aside, correct: the
  gate is seeded before jobs publish, decremented only at genuine terminal
  states, and never inside a retry path.
- **Human-in-the-loop merge** -- high-confidence duplicates are queued as
  `pending_merge_review` for explicit analyst confirmation, not merged
  silently. Good OCHA-alignment story (subject to M-1).
- **Structured logging with sensitive-field scrubbing**, `X-Request-ID`
  propagation through exception handlers, strict security headers, and a
  denylist-backed logout.
- **Static analysis is clean and coverage is 81 %** with a fully mocked test
  suite that needs no running services.

---

## 4. Recommended fix order before an external submission

1. **H-1** -- wire `region_geojson` into the JWT (access-control hole).
2. **H-2** -- unify the perceptual hash (dedup correctness).
3. **M-5** -- free-text / coordinate handling in exports (do-no-harm; E4C will
   look here first).
4. **M-4** -- authenticate or coarsen `/reports/nearby`.
5. **M-1** -- load footprints in every evaluation run and document the
   dependency.
6. **M-2**, **M-3**, **M-6**, **M-8/9/10** -- correctness and robustness.
7. The rest as normal backlog. Fix the non-hermetic GIS test so CI is green.

---

## 5. Remediation status (2026-09-10, same day)

Applied on branch `feat/privy-email-auth`, full toolchain re-run after each:
`flake8` 0, `mypy` 0 (75 files), `black`/`isort` clean, **731 passed,
1 skipped, 80.9 % coverage** (up from 708 with 1 failed; that failure was the
non-hermetic GIS test, now fixed).

Two of the fixes below (L-1 and the moderation-audit persistence bug) were
found by the end-to-end simulation, not the audit. They are recorded here so
this table stays the single list of what changed. See
`02-technical-validation.md` sections 4.6 and 4.7.

| # | Status | What changed |
|---|---|---|
| **H-1** | ✅ Fixed | `region_geojson` is now a JWT claim, set from the resolved `AnalystAccount` in `verify_otp` and `verify_privy_and_issue_tokens` (and `issue_analyst_token`), and carried through refresh-token rotation. Omitted entirely for reporters/analysts/admins. New tests assert the claim lands and survives rotation. A **nullable** region still means "national responder, sees everything" -- that is now a documented, deliberate config, not a silent hole. |
| **H-2** | ✅ Fixed | One perceptual-hash implementation only -- `image_service.compute_phash` (`imagehash.phash`, DCT). The AI worker is the **sole writer** of `report.photo_phash`, hashing the stored (compressed) image. Submission-time average-hash removed. The duplicate-scoring gate already guarantees the AI step finishes before `score_report`, so the hash is always present when compared. |
| **M-2** | ✅ Fixed | `transition_report_status` writes the canonical `verify` / `reject` feedback token (was `verified` / `rejected`), so the AI-accuracy dashboard's per-type buckets populate. Tokens are now module constants shared with `get_ai_accuracy`. |
| **M-3** | ✅ Fixed | Added `_VALID_STATUS_TRANSITIONS` matrix. No-op transitions and any transition out of `pending_merge_review` (which must use confirm/reject-merge) are rejected with `ValueError`. |
| **M-5** | ✅ Fixed | Reporter free text (`landmark_description`, `most_pressing_needs`) is PII-scrubbed (email / phone / long digit runs / URLs → `[redacted-*]`) unconditionally in `Anonymiser`. `photo_url` object key replaced with a `has_photo` boolean. New `location_precision` export option (`exact` / `reduced` ~110 m / `coarse` ~1.1 km) coarsens coordinates and drops `gps_accuracy_m`. `docs/hdx_schema.md` filled in as the real external contract. |
| **M-6** | ✅ Fixed | Refresh-token rotation is one atomic `GETDEL` -- concurrent double-refresh can no longer both succeed. A rejected (missing) refresh token now logs a replay/security signal. Full token-family revocation remains a follow-up. |
| **M-8 / M-9** | ✅ Fixed | New `app/workers/reconciliation_tasks.py` sweep (`reconcile_stuck_reports`) re-drives `pending` reports older than a 15-min grace period whose GIS / AI / dedup stage is demonstrably incomplete. Runs on Celery beat every 5 min (new `celery-beat` compose service) or on demand via `python -m app.cli reconcile`. A durable `report.duplicate_scored` audit row is now written on every terminal dedup outcome so the sweep can tell "scored, independent" from "scoring job lost". |
| **M-10** | ✅ Fixed | `_score_report_impl` now no-ops unless the report is still `pending` -- a re-dispatch or gate double-decrement can't move an analyst-actioned report back to `pending_merge_review` or re-flag it. |
| **M-1** | ⚙️ Handled in eval | Not a code bug -- an operational dependency. The simulation harness seeds a full footprint grid before every run so the `building` (40 %) signal and the `≥ 0.9` merge-review path are actually exercised; the deployment guide states the dependency. |
| **M-4** | 📋 Roadmap | `/reports/nearby` auth/coarsening -- deliberately deferred; documented with rationale in the technical report. Needs a product decision on the public-map contract. |
| **M-7** | 📋 Roadmap | Scheduled + recency-weighted self-calibration -- deferred. The beat infrastructure added for M-8/9 is the hook it will use. |
| **L-1** | ✅ Fixed (found by sim) | `duplicate_tasks._load_candidates`: the PostGIS candidate query raised on an untyped/mistyped `existing_ids` array, aborting the transaction; the `except` fallback then died with `InFailedSqlTransaction`, so `score_report` failed for whole batches. Worse than the audit's "silently falls back" description. Fix: `CAST(:existing_ids AS uuid[])` plus `db.rollback()` at the top of the `except`. Regression test added. Deduplication recall went from near-zero to 1.0 on the same scenario. |
| **Mod-audit** | ✅ Fixed (found by sim) | Moderation-rejection audit rows (spec section 8.2.1) were flushed by the service then rolled back, because the 422 is raised before the route commits. The simulation reported 0 rejection audit rows despite 4 blocked images. Fix: the two route handlers on this path commit the flushed audit row before raising the 422. Regression test added. |
| **L-2…L-13** | 📋 Backlog | Tracked; none blocking. `sim_providers.py` mypy error fixed in passing. |
