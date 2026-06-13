# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run dev server (hot-reload)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run all tests with coverage
pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=80 -v

# Run a single test file
pytest tests/test_auth.py -v

# Run a single test by name
pytest tests/test_reports.py::test_submit_report -v

# Lint
flake8 app/ tests/
mypy app/

# Format
black app/ tests/
isort app/ tests/

# Database migrations
alembic upgrade head
alembic revision --autogenerate -m "description"

# Full stack (app + Postgres + Redis + Celery workers + Mailpit)
docker-compose up

# Celery worker (manually, outside Docker)
celery -A app.workers.celery_app worker -Q gis --concurrency 2 --loglevel info
```

## Architecture

**CrisisMap** is a FastAPI backend for the Matata community crisis damage reporting platform. Python 3.12, async SQLAlchemy 2.0 (asyncpg), Celery workers, Redis, PostGIS.

### Request lifecycle

```
HTTP → RequestIDMiddleware → SanitisationMiddleware (HTML-strips JSON bodies)
     → CORSMiddleware → route handler → service layer → DB / Redis / queue
```

All errors are shaped to `{"error": "<detail>"}` by exception handlers in `main.py`. The `X-Request-ID` header is propagated through exceptions.

### Layer responsibilities

- **`app/api/v1/routes/`** — thin route handlers: validate shape, call a service, map service exceptions to HTTP codes. No DB/Redis imports here.
- **`app/services/`** — all business logic. Each service module owns one domain (auth, submission, GIS, etc.). Services import from `app/models` and `app/core`, never from `app/api`.
- **`app/workers/`** — Celery tasks. Four dedicated queues: `gis`, `ai`, `notifications`, `default`. Workers share the same service layer as the API process.
- **`app/models/`** — SQLAlchemy ORM models. All enums live in `app/models/enums.py` (single source of truth for both ORM and Pydantic).
- **`app/schemas/`** — Pydantic request/response schemas.
- **`app/core/`** — config, dependencies (DB sessions, Redis), middleware, security, rate limits, logging, i18n.

### Database sessions

Two session flavours exist (see `app/core/dependencies.py`):
- `get_db` — async `AsyncSession` via asyncpg for all FastAPI route handlers.
- `get_sync_db` — sync `Session` via psycopg2 for `GISService`, which is shared with Celery workers. The sync engine is created lazily to avoid importing psycopg2 in the async process at startup.

### Authentication

Three roles: `anonymous_reporter` (zero-friction JWT from `POST /api/v1/auth/anonymous`), `reporter` (OTP-verified via SMS), `analyst` (email + password with JWT + refresh tokens).

Token auth accepts either `Authorization: Bearer <token>` or `X-Session-Token: <token>`. Tokens are added to a Redis denylist on logout. Phone numbers are never stored — only SHA-256 hashes salted with `PHONE_HASH_SALT`.

Use `require_role(Role.analyst)` from `app/api/v1/routes/auth.py` as a FastAPI dependency to gate analyst-only endpoints.

### Report submission flow

`submission_service.create_report()` is the single orchestrator (spec-compliant order):
1. Sanitise text (bleach + dangerous-tag regex)
2. Hash reporter token
3. Redis rate limit (10 submissions/hour per token)
4. Stage-2 moderation (synchronous, before storage)
5. pHash computation + S3/MinIO upload
6. DB insert + audit log
7. Publish to `gis` and `ai` Celery queues

### Pluggable backends (all configured via env vars)

Every external dependency has a mock/stub default safe for dev and CI:

| Env var | Options | Default |
|---|---|---|
| `SMS_GATEWAY` | `console`, `africastalking` | `console` |
| `EMAIL_PROVIDER` | `console`, `smtp` | `console` |
| `MODERATION_PROVIDER` | `mock`, `rekognition` | `mock` |
| `STORAGE_BACKEND` | `mock`, `s3` | `mock` |
| `VISION_PROVIDER` | `mock`, `openai`, `anthropic`, `ollama` | `mock` |
| `GEOCODING_PROVIDER` | `nominatim`, `google`, `mock` | `nominatim` |
| `TRANSLATION_PROVIDER` | `libretranslate`, `argostranslate`, `mock` | `libretranslate` |

### i18n

`app/core/i18n.py` is the authority. Language resolution order: `?lang=` query param → `Accept-Language` header → `"en"`. Static JSON catalogues live in `app/i18n/<code>.json`. Adding a language requires only adding its code to `SUPPORTED_LANGUAGES` in `i18n.py` and dropping a JSON file — no other code changes.

`LocalisedHTTPException` translates its detail string at raise-time using the static catalogue.

### Observability

- Structured logging via structlog. JSON in production, coloured console in development. Sensitive fields (phone numbers, JWTs, presigned URLs) are automatically scrubbed by `_scrub_sensitive` in `app/core/logging.py`.
- Prometheus metrics via `prometheus-fastapi-instrumentator`. The `/metrics` endpoint requires a static bearer token (`METRICS_TOKEN`).
- Health endpoints at `/health` (liveness), `/health/ready` (readiness), `/health/worker` (Celery worker ping) — no `/api/v1` prefix so orchestrators can reach them directly.

### Rate limiting

All rate limits are declared as constants in `app/core/rate_limits.py` and applied via slowapi decorators. The `RateLimitHeaderMiddleware` injects `X-RateLimit-*` and `Retry-After` headers onto 429 responses.

### Key env vars required to start

```
SECRET_KEY=<256-bit base64>
JWT_SECRET_KEY=<256-bit base64>
PHONE_HASH_SALT=<32+ random bytes base64>
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
REDIS_URL=redis://localhost:6379
```

Copy `.env.example` to `.env` and fill in these five values at minimum.
