# CrisisMap backend: architecture diagrams

Five diagrams, drawn from the code on branch `feat/privy-email-auth` after
the 2026-09-10 remediation round. They render natively on GitHub and in most
Markdown viewers (Mermaid). The technical validation report refers to them by
number.

Reading conventions:

* Every box is a module that exists under `app/`.
* Every labelled arrow is a call or a queue publish you can find in the
  source.
* A node tagged `signal` is the mechanism the diagram exists to explain.
* A node tagged `human` is a point where control passes to an analyst.

---

## Figure 1: request lifecycle and layer discipline

Every request goes through the same middleware chain and then a strict
three-layer stack. Routes hold no database or Redis imports. Services never
import from `app.api`. Errors are reshaped to a single `{"error": ...}`
envelope on the way out, with the request id preserved.

```mermaid
flowchart TD
    HTTP[HTTP request] --> RID[RequestIDMiddleware]
    RID --> SAN[SanitisationMiddleware<br/>HTML-strips JSON body]
    SAN --> CORS[CORSMiddleware]
    CORS --> ROUTE["app/api/v1/routes/<br/>thin handler: validate shape,<br/>call one service, map errors"]
    ROUTE -->|no DB or Redis here| SVC["app/services/<br/>all business logic<br/>imports models + core only"]
    SVC --> PG[(Postgres / PostGIS)]
    SVC --> RD[(Redis)]
    SVC --> Q[[Celery queues:<br/>gis, ai, duplicate, notifications]]
    ROUTE -. any raise .-> EXC["exception handlers in main.py<br/>shape to { error: ... }<br/>keep X-Request-ID<br/>LocalisedHTTPException translates at raise time"]

    class ROUTE,SVC signal
    classDef signal stroke-width:3px
```

Static analysis confirms the enforced edge: no `sqlalchemy` or `redis`
import appears in `app/api/`, and no `app.api` import appears in
`app/services/`. Enums live in one place, `app/models/enums.py`.

---

## Figure 2: report submission, the single orchestrator

`submission_service.create_report()` runs one ordered sequence. Two
properties matter here. First, content is moderated before any storage
object is created, so a rejected image leaves nothing in object storage and
one audit row. Second, background jobs are published by the route after the
database commit, which closes a worker "row not found" race. If that publish
still fails, a scheduled reconciliation sweep re-drives the report.

```mermaid
flowchart TD
    S1[1. sanitise text<br/>bleach + dangerous-tag regex] --> S2[2. hash reporter token<br/>salted SHA-256]
    S2 --> S3[3. Redis rate limit<br/>10 per hour per token]
    S3 --> S4{4. Stage-2 moderation<br/>SYNCHRONOUS, before any storage write}
    S4 -->|rejected| REJ["write audit row, raise 422<br/>no storage object, no report row<br/>route commits the audit row"]
    S4 -->|passed| S5[5. compress, upload to S3 / MinIO]
    S5 --> S6[6. DB insert + report.create audit row]
    S6 --> S7[7. db.commit in the route handler]
    S7 -->|after commit only| GATE[seed duplicate-scoring gate<br/>1 if no photo, 2 if photo]
    GATE --> QG[[publish queue: gis]]
    GATE --> QA[[publish queue: ai, if photo]]
    S7 -. if dispatch fails after commit .-> RECON[reconcile_stuck_reports<br/>beat, every 5 min<br/>re-drives any pending report<br/>with an incomplete stage]

    class S4 signal
    class REJ human
    classDef signal stroke-width:3px
    classDef human stroke-width:3px,stroke-dasharray: 4 3
```

The perceptual hash used for deduplication is written later, by the AI
worker, on the stored compressed image. There is one hash implementation in
the codebase (`image_service.compute_phash`, DCT p-hash). Before the
remediation round there were two, on two different algorithms, writing the
same column: see audit finding H-2.

---

## Figure 3: AI triage, advisory and never authoritative

The vision model writes only `ai_*` columns and a `review_priority`. It
never touches `damage_severity`. The reporter's classification is immutable
once submitted. Routing is a deterministic function of quality and
confidence. When the model's severity guess differs from the reporter's at
high confidence, that is a signal to route to a human, not a correction to
apply.

```mermaid
flowchart TD
    AI[ai_tasks worker<br/>vision provider gives<br/>quality and confidence] --> Q1{quality < 0.30 ?}
    Q1 -->|yes| CRIT[review_priority = critical]
    Q1 -->|no| Q2{confidence < 0.60 ?}
    Q2 -->|yes| CRIT
    Q2 -->|no| Q3{ai_divergence<br/>or confidence < 0.80 ?}
    Q3 -->|yes| HIGH[review_priority = high]
    Q3 -->|no| LOW[review_priority = low]

    DIV["ai_divergence set when:<br/>ai_severity != reporter_severity<br/>AND ai_confidence > 0.70.<br/>threshold self-calibrates from<br/>analyst agree / disagree feedback"] -.-> Q3

    W["AI worker writes:<br/>ai_severity_prediction, ai_confidence,<br/>ai_quality_score, ai_divergence, review_priority.<br/>never writes: damage_severity"]

    class W signal
    class Q3,HIGH human
    classDef signal stroke-width:3px
    classDef human stroke-width:3px,stroke-dasharray: 4 3
```

In the simulation the routing decision is recomputed for every report,
independently, from the stored `ai_*` columns and compared with what the
pipeline actually assigned. Agreement was 100 percent across 90 reports with
AI output (see the technical validation report, section 2).

---

## Figure 4: duplicate detection, one score, one run per report

An incoming report is scored against nearby candidates on a weighted
composite of four signals. The score maps to one of three actions. A Redis
counter coordinates the asynchronous fan-in so that `score_report` runs
exactly once per report, after both feeding stages have finished, in
whatever order they finish. The scorer no-ops unless the report is still
`pending` (idempotent), and writes a durable audit row so a lost counter can
be reconstructed.

```mermaid
flowchart LR
    subgraph SCORE [composite score]
        B[building match x 0.4]
        G[GPS proximity x 0.3]
        I[image p-hash x 0.2]
        C[category agreement x 0.1]
        B --> SUM((sum))
        G --> SUM
        I --> SUM
        C --> SUM
    end
    SUM --> T1{score >= 0.90}
    SUM --> T2{0.60 to 0.90}
    SUM --> T3{score < 0.60}
    T1 -->|yes| MR[status = pending_merge_review<br/>analyst confirms the merge]
    T2 -->|yes| FL[set possible_duplicate_of_id<br/>analyst flag]
    T3 -->|yes| IND[independent, no report change]

    subgraph GATE [coordination gate: duplicate_dispatch.py]
        CTR["Redis counter<br/>report:{id}:pending_dup_steps<br/>seeded to 1 or 2"]
        GIS[gis_tasks done] -->|minus 1| CTR
        AIW[ai_tasks done] -->|minus 1| CTR
        CTR -->|reaches 0, dispatch once| SR[score_report]
        SR --> AUD[audit row: report.duplicate_scored]
    end

    class SUM,CTR signal
    class MR,FL human
    classDef signal stroke-width:3px
    classDef human stroke-width:3px,stroke-dasharray: 4 3
```

In the clean simulation run every one of 115 reports produced exactly one
terminal dedup outcome (94 `report.duplicate_scored` plus 21
`report.pending_merge_review`). Precision, recall and F1 against the planted
duplicate clusters were all 1.0. That result depends on the L-1 fix in this
remediation round: the candidate-loading query used to abort the
transaction on an edge case, killing `score_report` for part of the batch.

---

## Figure 5: responsible data handling at each boundary

The system is built so a login identifier never reaches disk, and reporter
free text never leaves in an export without scrubbing. This diagram traces
one report's personal data across four boundaries.

```mermaid
flowchart TD
    subgraph EDGE [request edge]
        ID[phone / email / Privy DID] -->|salted SHA-256,<br/>plaintext discarded in-call| H[stored: 64-char hash only]
    end
    subgraph STORE [object storage]
        PH[uploaded photo] -->|EXIF and GPS stripped,<br/>moderated before store| KEY[opaque object key]
    end
    subgraph VIEW [analyst view]
        FEED[feed, detail, SSE] --> TIER[reporter_trust_tier only<br/>no hash, no token, no notes]
        TIER -->|regional responder| SW[PostGIS ST_Within on region_geojson claim]
    end
    subgraph EXPORT [external export: GeoJSON / CSV / Shapefile]
        E1[hash truncated to first 12 chars]
        E2["free text: emails, phone numbers, URLs<br/>replaced with [redacted-*]"]
        E3[photo key replaced with has_photo boolean]
        E4[coordinates: optional coarsen<br/>exact, ~110 m, or ~1.1 km]
    end

    H --> FEED
    KEY --> FEED
    TIER --> E1

    XC["cross-cutting: structured logs scrub phone numbers, JWTs and presigned URLs.<br/>every mutation writes an audit_log row. tokens denylisted on logout."]

    class H,KEY,E1,E2,E3,E4 signal
    class SW human
    classDef signal stroke-width:3px
    classDef human stroke-width:3px,stroke-dasharray: 4 3
```

The export column here reflects the M-5 fix: free-text scrubbing and the
`has_photo` boolean are new in this round; hash truncation and identifier
exclusion were already in place. The honest caveat is stated plainly in the
impact brief: a single fast SHA-256 over a low-entropy phone-number space is
pseudonymisation, not strong anonymisation, and an HMAC key held in a KMS
rather than in `.env` is the documented upgrade.
