# CrisisMap: executive summary

One page for a first conversation with Engineering for Change and ASME.

---

## The problem

In the first 24 to 72 hours after a flood, earthquake, fire or conflict
event, humanitarian responders allocate resources from a damage picture that
is still fragmentary. It is assembled slowly, from enumerator teams,
satellite analysis, and scattered WhatsApp groups and spreadsheets, and what
results cannot be verified, deduplicated, or safely shared between
organisations. The affected population is already on the ground at the
moment of impact, but the tools ask them for an account, a login, and a
stable connection.

## The solution

CrisisMap is an open backend that turns a damage report from anyone, with no
account, in seconds, in four languages, into a verified, deduplicated,
building-anchored record in a format humanitarian coordination platforms
already consume (HDX and HXL).

* A GIS worker anchors each report to a building footprint so reports about
  the same structure link together.
* A vision model gives an advisory second opinion on damage severity, and by
  design cannot overwrite the reporter's own classification.
* Deduplication scores each report against its neighbours. Likely duplicates
  are queued for a human analyst to confirm, never merged silently.
* Analysts work a priority queue, with reporter and AI disagreements first.
* Exports are PII-scrubbed and can be geographically coarsened for conflict
  settings.

Every external dependency (storage, moderation, vision, SMS, email,
geocoding, translation) has a working mock default. The whole stack runs
with `docker compose up` and was validated inside a 3.4 GB VM.

## Evidence it works

From a clean simulation run of 120 synthetic reports with ground-truth
labels, driven through the live stack and graded, plus the automated suite
and an independent audit.

| | |
|---|---|
| Automated tests | 731 passing, about 81 percent line coverage, no external services required |
| Static analysis | `flake8`, `mypy`, `black`, `isort` all clean; architectural layering enforced |
| Independent engineering audit | 2 high-severity plus several medium issues found, all remediated the same day with regression tests (`01-backend-audit.md` section 5) |
| GIS building match | 100 percent of GPS reports matched to the correct footprint, mean confidence 0.996 |
| AI routing consistency | 100 percent agreement between the pipeline's routing and an independent recomputation from stored fields |
| Deduplication | precision 1.0, recall 1.0, F1 1.0 against 8 planted duplicate clusters; exactly one dedup outcome per report |
| Safety guarantee | 4 moderation-blocked submissions left zero stored report rows |
| Data at rest | zero raw tokens, zero raw JWTs in free text; reporter identity stored only as a salted 64-character hash |
| Pipeline completeness | GIS and AI stages completed for 100 percent of submitted reports |

The simulation itself caught two real bugs that the unit tests could not
reach: a transaction abort in the deduplication candidate query, and lost
moderation-rejection audit rows. Both were fixed and the run repeated.

## Maturity

TRL 6: complete system, validated in a simulated crisis environment, with a
deployed instance at `matata.pipelinegpt.xyz`. Not yet exercised in a live
event, which is the TRL 7 threshold.

## The ask

A piloting partnership with a municipal disaster-management office or a Red
Cross or Red Crescent society to take CrisisMap from simulated to
field-demonstrated, plus an E4C Solutions Library entry and a standards
review of the export schema and responsible-AI constraints.

## Honest limitations

Simulation and tests only, not a live deployment. Throughput not benchmarked
on production hardware. Pseudonymisation (salted hash), not strong
anonymisation. Strongest GIS and dedup signals need a building-footprint
dataset loaded for the area.

---

Full detail: `02-technical-validation.md` (methodology and results),
`03-impact-and-innovation.md` (framing, SDG and Sendai alignment), and
`04-architecture-diagrams.md` (five diagrams).
