# CrisisMap: impact and innovation brief

For Engineering for Change (E4C) and ASME, toward partnership, adoption, and
a Solutions Library assessment.

Subject: CrisisMap, the backend for the Matata community crisis-damage
reporting platform.

Companion documents: `01-backend-audit.md` (engineering audit),
`02-technical-validation.md` (methodology and measured results),
`04-architecture-diagrams.md` (five diagrams).

Date: 2026-09-10.

---

## 1. The problem, stated plainly

When a flood, earthquake, fire or conflict event damages a populated area,
the humanitarian response depends on one thing arriving quickly and being
trustworthy: a map of what is damaged, how badly, and what people there need
now.

Today that map is assembled slowly. It comes from windshield surveys,
enumerator teams walking blocks with paper or KoBo forms, satellite-imagery
analysis that takes days and misses building interiors, and a scatter of
WhatsApp groups and spreadsheets that no one can audit. During the window
when the information would actually change how resources are allocated, the
first 24 to 72 hours, responders are working from fragments.

The affected population is the one sensor that is already everywhere, on the
ground, at the moment of impact. The blocker has never been their
willingness to report. It is that the tools ask for an account, a login, a
stable connection and a language, and then produce data that a coordinating
body cannot verify, cannot deduplicate, and cannot safely share.

CrisisMap is a backend that removes those blockers.

---

## 2. What it is

A FastAPI service (Python 3.12, async SQLAlchemy, Celery workers, Redis,
PostGIS) that accepts a damage report from anyone in seconds and turns it
into a verified, deduplicated, geospatially anchored record fit for
humanitarian coordination.

The lifecycle of one report:

1. Report. A person opens the frontend, gets an anonymous session token with
   no registration (`POST /auth/anonymous`), and submits: crisis type, what
   kind of structure, how badly damaged, a GPS point or a landmark
   description, a photo, electricity and health-service status, and their
   most pressing needs. Four languages are supported. Adding one is a JSON
   file.
2. Safety. The text is HTML-stripped. The image is checked by a moderation
   provider before anything is written to storage.
3. Anchor. A GIS worker matches the GPS point to a building footprint
   (point-in-polygon, then nearest-neighbour) so multiple reports about the
   same structure can be linked.
4. Second opinion. A vision model estimates damage severity and image
   quality. It writes only advisory `ai_*` fields and a review priority. It
   never overwrites the reporter's own classification.
5. Deduplicate. The report is scored against nearby reports on a weighted
   composite of building match, GPS proximity, image similarity and category
   agreement. High-confidence duplicates are queued for a human analyst to
   confirm a merge. They are never merged silently.
6. Triage. Analysts work a priority-ordered queue, with the cases where the
   AI and the reporter disagree surfaced first.
7. Share. The verified dataset exports as GeoJSON, CSV or Shapefile in a
   schema aligned to the Humanitarian Data Exchange (HDX) and HXL
   conventions, with reporter free-text PII scrubbed and an option to
   coarsen coordinates for sensitive contexts.

Every external dependency (SMS, email, image moderation, object storage, the
vision model, geocoding, translation) has a working mock as its default. The
entire system runs, and was validated, with no third-party account and no
internet egress.

---

## 3. Why it is worth a look

The novelty is not any single feature. It is a set of engineering decisions
that are uncommon in tools built for this sector, each verifiable in the
code and the validation run.

### 3.1 AI is a structurally constrained second opinion, not an authority

The reporter's `damage_severity` is immutable once submitted. The vision
model writes to a disjoint set of columns (`ai_severity_prediction`,
`ai_confidence`, `ai_quality_score`, `ai_divergence`, `review_priority`).
There is no code path, no admin flag, no reprocessing job, that lets the
model change the human's classification. When the model disagrees with the
reporter at high confidence, that is treated as a signal to route to a
human, not a correction to apply. The validation run recomputes the routing
decision for every report independently from the stored columns and checks
it against what the pipeline did. Agreement was 100 percent.

This is the property most humanitarian-AI guidance now asks for (the OECD AI
Principles, and OCHA's guidance on the responsible use of AI in humanitarian
action). Here it is enforced at the schema level, not in a policy document.

### 3.2 Zero-friction reporting with a graded trust model

A crisis-affected person reports with no account and no PII. Abuse
resistance does not depend on knowing who they are. It comes from
synchronous content moderation, a per-session rate limit, reporter trust
tiers that rise and fall with analyst verification, and human triage. This
is a deliberate inversion of the usual "verify identity first" posture,
which excludes exactly the people closest to the damage.

### 3.3 Exactly-once deduplication across independent async workers

Duplicate scoring has to run once per report, after both the GIS and AI
stages finish, in whatever order they finish. A Redis counter is seeded
before the jobs are dispatched and decremented by each worker on every
terminal path. Only the decrement that reaches zero dispatches the scorer.
The scorer itself is idempotent (it acts only while the report is still
`pending`) and writes a durable audit row so a lost counter can be
reconstructed. A scheduled reconciliation sweep re-drives any report whose
pipeline stalled. This is ordinary distributed-systems discipline, and it is
usually missing from NGO-built tools, where "the worker sometimes does not
run" is a known and tolerated failure.

### 3.4 Privacy by construction, disclosed honestly

Login identifiers (phone, email, Privy DID) are salted-SHA-256 hashed in the
request that receives them and never stored in plaintext. Photos have EXIF
and GPS metadata stripped before upload. Exports truncate the reporter hash
to 12 characters, drop analyst notes entirely, replace the internal photo
key with a boolean, redact emails, phone numbers and URLs from reporter free
text, and can round coordinates to about 110 m or about 1.1 km for conflict
settings.

The brief is equally clear about the limit. A single fast hash over a
low-entropy space (phone numbers, roughly 10^9 possibilities) is
pseudonymisation, not strong anonymisation. If both the database and the
`.env` salt leak, numbers are recoverable. A KMS-held HMAC key is the
documented upgrade. Naming this here, rather than claiming more than the
design delivers, is itself part of the do-no-harm posture.

### 3.5 Adoptable by a mid-capacity organisation

The whole stack is one `docker compose up`. It ran end-to-end (API, Postgres
with PostGIS, Redis, four Celery workers, object storage) inside a 3.4 GB
development VM during validation. Every provider is swappable by environment
variable, so a deploying body points storage at its own S3, moderation at
its own service or a self-hosted model, and translation at a self-hosted
LibreTranslate, without touching code.

---

## 4. Alignment with humanitarian frameworks and the SDGs

| Framework | How CrisisMap maps to it |
|---|---|
| Sendai Framework, Priority 1, understanding disaster risk | Produces a structured, geolocated, building-level post-event damage dataset from community input during the response window. |
| Sendai, Priority 4, build back better | The verified dataset and its per-building damage timeline feed reconstruction planning; the audit trail supports accountability in fund allocation. |
| SDG 11.5, reduce deaths and economic losses from disasters, focus on the poor and vulnerable | Lowers the latency between impact and an actionable damage picture; the zero-friction design reaches populations excluded by account-based tools. |
| SDG 11.b, integrated disaster risk-reduction management | HDX and HXL aligned exports make the data usable by existing coordination platforms rather than a silo. |
| SDG 13.1, strengthen resilience and adaptive capacity | Reusable across hazard types and reusable between events on the same building stock. |
| SDG 16.6 and 16.7, accountable institutions, responsive and inclusive decision-making | Every state change writes an `audit_log` row; the community is the primary data source. |
| SDG 9.1 and 9.a, resilient infrastructure | The building-footprint-anchored dataset is an infrastructure-condition record. |
| OCHA and HDX data standards | Export schema documented field by field in `docs/hdx_schema.md` against HDX and HXL. |
| OECD AI Principles; OCHA guidance on AI in humanitarian action | AI output is advisory and schema-isolated; human in the loop on merges and on AI/reporter disagreement; the divergence threshold self-calibrates from analyst feedback. |

---

## 5. Maturity (TRL) and evidence

Assessed at TRL 6: a complete system validated in a relevant (simulated)
environment, with a deployed instance running at `matata.pipelinegpt.xyz`.
Not yet exercised in a live crisis event, which is the TRL 7 threshold and a
natural goal for a piloting partnership.

Evidence base, in increasing order of strength:

* Static quality. `flake8` and `mypy` clean across the codebase;
  `black` and `isort` enforced; enforced architectural layering (routes
  never touch the database directly; services never import the web layer).
* Automated tests. 731 tests at about 81 percent line coverage, runnable
  with no external services (SQLite plus an in-memory Celery broker).
* Independent engineering audit. `01-backend-audit.md`, a full read of the
  service, worker, route and model layers. Two high-severity issues and a
  set of medium issues were found and remediated the same day (regional
  access scoping, deduplication hash consistency, export PII handling,
  refresh-token atomicity, pipeline reconciliation, dedup idempotency).
  Section 5 of that document tracks each to a fix and a test.
* End-to-end crisis simulation. `02-technical-validation.md`. Synthetic
  reports with ground-truth labels driven through the live stack and graded
  against that ground truth. The simulation surfaced two real bugs the unit
  tests could not reach (a transaction-abort in candidate loading, and lost
  moderation-rejection audit rows). Both were fixed and the run repeated.

Headline measured results are in section 2 of the technical validation
report. The one-page summary carries the top-line figures.

---

## 6. What a partnership would unlock

* A live pilot with a municipal disaster-management office or a national Red
  Cross or Red Crescent society, taking the system from TRL 6 to a
  field-demonstrated TRL 7. That is the evidence step that most changes an
  adoption decision.
* A Solutions Library entry and an E4C sector-report contribution on
  community-sourced damage assessment, with CrisisMap as a documented
  reference implementation.
* Standards review. An ASME and E4C read of the export schema and the
  responsible-AI constraints against emerging norms, feeding back into both
  this system and the guidance.
* Sustainability model. CrisisMap is open by construction. A partnership
  would help define the hosting, support and governance model that lets a
  coordinating body actually run it.

---

## 7. Honest limitations

Stated here so a reviewer does not have to extract them.

* Not a live-event deployment yet. All results are from simulation and
  automated tests.
* Throughput is not benchmarked on representative hardware. Validation ran
  on a memory-constrained dev VM with a single API worker. The numbers in
  the technical report are correctness and accuracy figures, which are
  hardware-independent, not a capacity claim. The API is stateless and the
  workers are independent, so capacity is a horizontal-scale question, but
  that claim is untested at scale.
* Pseudonymisation, not anonymisation. See section 3.4.
* Building-footprint dependency. The strongest deduplication and GIS signals
  require a footprint dataset (Microsoft or Google Open Buildings, for
  example) loaded for the area of operation. Without it the system still
  works but deduplication degrades to flag-only. The deployment guide states
  this. The validation run loads footprints, as any real deployment must.
* Moderation and vision quality depend on the chosen provider. The system
  validates the decision pipeline around the model (routing, divergence,
  human in the loop). It does not certify any particular model's accuracy.

---

Prepared alongside the technical validation report. Every claim traces to
code on branch `feat/privy-email-auth` or to a simulation output under
`simulation/out/`.
