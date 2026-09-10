# HDX Disaster Damage Dataset -- Field Mapping & Export Schema

## Overview

This document defines how CrisisMap export fields map to the Humanitarian Data
Exchange (HDX) disaster-damage data model. It is the **canonical external
schema** for the export endpoints:

- `GET /api/v1/export/geojson`
- `GET /api/v1/export/csv`
- `GET /api/v1/export/shapefile`

By aligning exported fields with HDX / HXL conventions, CrisisMap data can be
consumed directly by humanitarian organisations, disaster-response teams, and
GIS workflows (QGIS, ArcGIS, kepler.gl, HDX HXL tooling).

Authoritative source of the field list: `app/services/export_service.py`
(`ExportRecord`, `Anonymiser`, `DBF_FIELD_MAP`). If the code and this table
disagree, the code wins -- but a PR that changes one must change the other.

## Design principles

### Standards alignment
Where a field maps cleanly to an HXL hashtag it is listed below. Consumers can
add a HXL tag row on ingest.

### Privacy by default
Personally identifiable information and internal operational signals are
excluded from **every** export format. The rules are enforced inside
`Anonymiser` and cannot be overridden by any API parameter, JWT role, URL
query, or request header. See "Privacy and export restrictions" below.

### Stable external schema
Internal database columns may evolve; exported field names are meant to stay
stable. `has_photo` replaced the raw `photo_url` object key in the 2026-09
privacy hardening -- external consumers should treat that as the current
baseline.

---

## Geometry

Each report is a GeoJSON `Feature` with `Point` geometry in WGS84
(EPSG:4326). Shapefile export uses the same CRS (`.prj` = EPSG:4326).

| CrisisMap field | GeoJSON | HXL | Notes |
| --- | --- | --- | --- |
| `lng`, `lat` | `geometry` → `Point [lng, lat]` | `#geo+lon`, `#geo+lat` | Decimal degrees. Coarsened when `location_precision` ≠ `exact` (see below). `null` geometry when the report has no GPS fix (landmark text only). |

### `location_precision` query parameter

| Value | Rounding | Approx. ground error | `gps_accuracy_m` in output |
| --- | --- | --- | --- |
| `exact` (default) | none | native device precision | preserved |
| `reduced` | 3 decimal places | ~110 m | dropped (`null`) |
| `coarse` | 2 decimal places | ~1.1 km | dropped (`null`) |

Use `reduced` or `coarse` for any export leaving the responding
organisation, and for sensitive crisis types (e.g. `conflict`) where a
precise coordinate could identify a household. Coarsening is applied
identically across GeoJSON, CSV and Shapefile.

---

## Feature properties

| CrisisMap field | GeoJSON property | HXL | Type | Notes |
| --- | --- | --- | --- | --- |
| `id` | `report_id` | `#meta+id` | string (UUID) | Stable report identifier. |
| `building_id` | `building_id` | `#loc+code` | string (UUID) or `null` | PostGIS footprint the report was matched to, if any. |
| `crisis_type` | `crisis_type` | `#crisis+type` | enum | `flood`, `earthquake`, `conflict`, `wildfire`, `other`. |
| `infrastructure_type` | `infrastructure_type` | `#infra+type` | enum | `residential`, `commercial`, `government`, `utilities`, `transport`, `community`. |
| `damage_severity` | `damage_severity` | `#damage+level` | enum | Reporter's classification: `minimal`, `partial`, `destroyed`. **Never** overwritten by AI. |
| `ai_severity_prediction` | `ai_severity_prediction` | `#damage+level+ai` | enum or `null` | Model's independent estimate; advisory only. |
| `ai_confidence` | `ai_confidence` | `#indicator+confidence` | float 0–1 or `null` | Model confidence for the prediction above. |
| `status` | `status` | `#status` | enum | `pending`, `verified`, `rejected`, `duplicate`, `pending_merge_review`. |
| `reporter_token_hash` | `reporter_token_hash_truncated` | `#meta+id+reporter` | string(12) | First 12 hex chars of a salted SHA-256 of the session token. Non-reversible, non-identifying; lets a consumer group reports from one session without knowing who it was. |
| `reporter_trust_tier` | `reporter_trust_tier` | `#indicator+trust` | int 0–2 | 0 anonymous, 1 verified session, 2 established reporter. |
| `gps_accuracy_m` | `gps_accuracy_m` | `#geo+precision` | float or `null` | Device-reported horizontal accuracy. Dropped when coordinates are coarsened. |
| `landmark_description` | `landmark_description` | `#loc+name` | string or `null` | Free text. **PII-scrubbed** (see below). |
| `electricity_status` | `electricity_status` | `#infra+electricity` | enum or `null` | `functional`, `non_functional`, `unknown`. |
| `health_services_status` | `health_services_status` | `#infra+health` | enum or `null` | `accessible`, `inaccessible`, `unknown`. |
| `most_pressing_needs` | `most_pressing_needs` | `#needs` | string or `null` | Free text. **PII-scrubbed** (see below). |
| `debris_clearing_needed` | `debris_clearing_needed` | `#needs+debris` | bool or `null` | |
| `photo_url` | `has_photo` | `#meta+has_photo` | bool | **Boolean only.** The internal object key is never exported. Imagery is shared out-of-band under a separate data-sharing agreement. |
| `created_at` | `created_at` | `#date+created` | string | ISO 8601 UTC. |
| `updated_at` | `updated_at` | `#date+updated` | string | ISO 8601 UTC. Last pipeline or analyst state change. |

### Free-text PII scrubbing

`landmark_description` and `most_pressing_needs` are reporter-authored and
routinely contain third-party names and contact details ("call John on
+2547…, he has the keys"). Before either value leaves `ExportService` it is
passed through `scrub_free_text()`, which replaces:

| Pattern | Replacement |
| --- | --- |
| Email address | `[redacted-email]` |
| `http(s)://…` / `www.…` URL | `[redacted-url]` |
| Any digit run containing ≥ 7 digits (phone / ID numbers, with spaces, dashes, dots, parentheses, leading `+`) | `[redacted-number]` |

The rule is deliberately conservative: short quantities ("water for 6
families", "12 injured") are preserved so the operational content survives.

---

## Privacy and export restrictions

Enforced by `Anonymiser._BLOCKED_FIELDS` and the transforms above. Not
overridable by any consumer.

| Field / channel | Treatment | Reason |
| --- | --- | --- |
| `reporter_token_hash` (full) | truncated to 12 chars | full hash + leaked salt could enable a dictionary attack on low-entropy identifiers |
| Phone number (plaintext) | never stored, never exported | identifier minimisation |
| `AnalystNote.body` | never exported | internal deliberation, may name individuals |
| `photo_url` (object key) | replaced with `has_photo` bool | avoids distributing a fetchable-looking internal reference |
| `photo_phash` | excluded | internal dedup signal |
| `duplicate_of_id`, `possible_duplicate_of_id`, `duplicate_score` | excluded | internal dedup state |
| `footprint_match_confidence`, `ai_quality_score`, `ai_divergence` | excluded | internal model/GIS signals, easily misread out of context |
| `offline_queued_at` | excluded | device-behaviour metadata |
| Free text | PII-scrubbed | third-party protection (see above) |
| Coordinates | optional coarsening via `location_precision` | household-level de-identification for sensitive contexts |

Every export writes an `audit_log` row (`operation = "export.generate"`)
recording the analyst hash, format, filter set, record count and timestamp.

---

## Shapefile (DBF) column mapping

ESRI DBF attribute names are limited to 10 characters.

| Python attribute | DBF column |
| --- | --- |
| `report_id` | `report_id` |
| `building_id` | `bldg_id` |
| `crisis_type` | `crisis_tp` |
| `infrastructure_type` | `infra_type` |
| `damage_severity` | `dmg_sev` |
| `ai_severity_prediction` | `ai_sev` |
| `ai_confidence` | `ai_conf` |
| `status` | `status` |
| `reporter_token_hash_truncated` | `rptr_hash` |
| `reporter_trust_tier` | `trust_tier` |
| `lat` | `lat` |
| `lng` | `lng` |
| `gps_accuracy_m` | `gps_acc_m` |
| `landmark_description` | `landmark` |
| `electricity_status` | `elec_stat` |
| `health_services_status` | `health_st` |
| `most_pressing_needs` | `needs` |
| `debris_clearing_needed` | `debris` |
| `has_photo` | `has_photo` |
| `created_at` | `created_at` |
| `updated_at` | `updated_at` |

---

## References

- HDX Data Standards -- <https://data.humdata.org/>
- Humanitarian Exchange Language (HXL) -- <https://hxlstandard.org/>
- Microsoft / Google Open Buildings footprints (footprint import source)
