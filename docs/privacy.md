# Privacy and data handling (operator notes)

The public privacy, consent and safeguarding policy lives in the frontend
repository: [`PRIVACY.md`](https://github.com/Kubu-Ventures/Matata/blob/main/PRIVACY.md),
shown in the app at `/privacy`. This page tells operators which backend
settings that policy depends on, so a deployment does what the policy says.

## What the backend stores

| Data | Where | Notes |
| --- | --- | --- |
| Report fields (location, damage, needs, landmark text) | `report` table | Free text is HTML-stripped on the way in |
| Photo | Object storage, key in `report.photo_url` | EXIF/GPS removed before upload (`image_service.py`) |
| Photo fingerprint | `report.photo_phash` | Used for duplicate detection; not an image |
| Reporter identity | `report.reporter_token_hash` | Salted SHA-256 of the session token, never the token |
| Login identifiers (email, Privy DID, phone) | `analyst_accounts`, JWT `sub` | Salted SHA-256 only; plaintext never stored or logged |
| Analyst notes | `analyst_note` | Never exported |
| Audit trail | `audit_log` | Hashed actor ids only |

Exports (`export_service.py`) drop analyst notes and photo keys, truncate the
reporter hash, redact emails, phone numbers and URLs from free text, and can
round coordinates (`location_precision=reduced|coarse`).

## Settings the policy relies on

| Setting | Policy promise | What to set |
| --- | --- | --- |
| `PHOTO_RETENTION_DAYS` | Photos are deleted after a fixed number of days | The number in section 7 of the policy (for example `90`). `0` keeps photos indefinitely and must not be used with the published policy |
| `MODERATION_PROVIDER` | Photos are checked for harmful content | `rekognition` in production; `mock` checks nothing |
| `VISION_PROVIDER`, `GEOCODING_PROVIDER`, `TRANSLATION_PROVIDER`, `SMS_GATEWAY`, `STORAGE_BACKEND` | Section 5 lists every outside service that sees data | Remove the providers you do not use from the policy's table, and add your hosting provider |

## Photo retention

`app/workers/retention_tasks.py` runs daily on Celery beat. For every photo
whose newest referencing report is older than `PHOTO_RETENTION_DAYS`, it
deletes the object from storage and clears `photo_url` on every report that
points at it. A merge can copy one photo key onto several reports, which is
why the age is taken from the newest report using the key. Report rows are
kept; only the photo goes. A failed storage delete leaves the row untouched
so the next run retries it.

Run it by hand, or preview first:

```bash
python -m app.cli purge-photos --dry-run           # count only
python -m app.cli purge-photos                     # uses PHOTO_RETENTION_DAYS
python -m app.cli purge-photos --days 90 --limit 1000
```

The beat schedule only runs if a `celery beat` process is running. Without
one, schedule the CLI command (for example a daily cron job).

## Not yet handled in code

- **Deleting a single report on request.** The policy promises deletion on
  request; today an operator does it in the database. A self-service delete
  for reporters is a follow-up.
- **Report record retention.** Report rows are kept until an operator
  removes or anonymises them at the end of a response.
- **Key management.** `PHONE_HASH_SALT` sits in the environment. A KMS-held
  HMAC key is the documented upgrade (see `docs/evaluation/03-impact-and-innovation.md`).
