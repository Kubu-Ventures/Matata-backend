# CrisisMap Backend

> Community crisis damage reporting platform built for crisis-response field operations.

[![CI](https://github.com/Kubu-Ventures/Matata-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/Kubu-Ventures/Matata-backend/actions)
[![Coverage](https://img.shields.io/badge/coverage-80%25-brightgreen)](#testing)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

CrisisMap is a FastAPI backend that lets community members report infrastructure damage after a crisis (flood, earthquake, conflict, wildfire) with GPS coordinates and photos. Field analysts and responders triage reports, merge duplicates, and export structured datasets for field operational planning.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [Authentication](#authentication)
- [API Reference](#api-reference)
- [Testing](#testing)
- [Production Deployment](#production-deployment)
- [Admin CLI](#admin-cli)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

---

## Features

- **Zero-friction reporting** -- anonymous JWT in one request, no registration required
- **Phone OTP login** -- reporters and analysts authenticate via SMS one-time password
- **AI triage** -- vision model assesses photo severity; low-confidence predictions are automatically routed to human review (OCHA responsible-AI compliant)
- **pHash deduplication** -- perceptual hashing flags near-duplicate photos before storage
- **PostGIS spatial queries** -- building footprint matching, nearby report search, and heatmap aggregation
- **Pluggable backends** -- swap SMS, storage, moderation, geocoding, translation, and vision providers without touching application code
- **Export pipeline** -- GeoJSON, CSV, and Shapefile exports with analyst PII stripped
- **Real-time SSE stream** -- analysts subscribe to live report events without polling
- **Structured observability** -- JSON logs via structlog, Prometheus metrics, and three-tier health probes
- **Internationalisation** -- Accept-Language negotiation, static JSON catalogues, zero-config language additions

---

## Architecture

```
HTTP Request
    |
    v
RequestIDMiddleware         -- attaches X-Request-ID to every request and error
SanitisationMiddleware      -- HTML-strips all JSON body fields
CORSMiddleware
    |
    v
Route Handler (app/api/)    -- validates shape, calls a service, maps exceptions to HTTP codes
    |
    v
Service Layer (app/services/) -- all business logic; never imports from app/api
    |
    +---> PostgreSQL (asyncpg) via SQLAlchemy 2.0
    +---> Redis (rate limiting, OTP storage, SSE pub/sub, token denylist)
    +---> Celery task queues (gis | ai | notifications | export)
    +---> Object storage (S3 / MinIO / Cloudflare R2)
```

### Layer responsibilities

| Layer | Path | Responsibility |
|-------|------|----------------|
| Routes | `app/api/v1/routes/` | Shape validation, HTTP status mapping. No DB or Redis imports. |
| Services | `app/services/` | Business logic, one module per domain. |
| Workers | `app/workers/` | Celery tasks across four dedicated queues. |
| Models | `app/models/` | SQLAlchemy ORM. All enums in `enums.py` (shared with Pydantic). |
| Schemas | `app/schemas/` | Pydantic request/response shapes. |
| Core | `app/core/` | Config, dependencies, middleware, security, rate limits, i18n, logging. |

### Report submission flow

1. Sanitise text fields (bleach + dangerous-tag regex)
2. Hash reporter token
3. Redis rate limit (10 submissions per hour per token)
4. Stage-2 content moderation (synchronous, before any storage)
5. Perceptual hash computation + upload to object storage
6. Database insert + audit log entry
7. Publish to `gis` queue (building footprint match) and `ai` queue (vision severity)

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| API framework | FastAPI 0.136, Python 3.12 |
| Database | PostgreSQL 15 + PostGIS 3.4 |
| ORM | SQLAlchemy 2.0 async (asyncpg driver) |
| Task queue | Celery 5 + Redis broker |
| Cache / sessions | Redis 7 |
| Object storage | MinIO (self-hosted) or any S3-compatible service |
| SMS | Africa's Talking (+ voice OTP fallback) |
| Vision AI | OpenAI, Anthropic, or Ollama (pluggable) |
| Content moderation | AWS Rekognition (pluggable) |
| Geocoding | Nominatim, Google Maps (pluggable) |
| Translation | LibreTranslate (self-hosted, pluggable) |
| Observability | structlog, Prometheus, Sentry |

---

## Quick Start

### Prerequisites

- Python 3.12
- Docker and Docker Compose v2

### Local development (Docker Compose)

```bash
# 1. Clone the repository
git clone https://github.com/Kubu-Ventures/Matata-backend.git
cd Matata-backend

# 2. Create your environment file
cp .env.example .env
# Edit .env -- at minimum fill in:
#   SECRET_KEY, JWT_SECRET_KEY, PHONE_HASH_SALT
# (see Environment Variables section for generation commands)

# 3. Start the full stack
docker compose up
```

This starts: FastAPI app (port 8000), PostgreSQL + PostGIS, Redis, Celery workers (gis, ai, notifications), and Mailpit (SMTP catcher, web UI at port 8025).

Migrations run automatically on startup. The API is available at `http://localhost:8000` and the interactive docs at `http://localhost:8000/docs`.

### Local development (no Docker)

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run database migrations
alembic upgrade head

# Start the dev server with hot reload
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# In separate terminals, start individual Celery workers:
celery -A app.workers.celery_app worker -Q gis --concurrency 2 --loglevel info
celery -A app.workers.celery_app worker -Q ai --concurrency 2 --loglevel info
celery -A app.workers.celery_app worker -Q notifications --concurrency 2 --loglevel info
celery -A app.workers.celery_app worker -Q export --concurrency 2 --loglevel info
```

---

## Environment Variables

Copy `.env.example` to `.env`. The five variables below are required to start the application. Everything else has a safe default for local development.

```bash
# Generate SECRET_KEY and JWT_SECRET_KEY (run separately for different values)
python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"

# Generate PHONE_HASH_SALT
python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | Yes | 256-bit base64 secret for FastAPI internals |
| `JWT_SECRET_KEY` | Yes | 256-bit base64 secret for token signing |
| `PHONE_HASH_SALT` | Yes | 32+ bytes base64, salts all phone number hashes |
| `DATABASE_URL` | Yes | PostgreSQL+asyncpg connection string |
| `REDIS_URL` | Yes | Redis connection string |

### Pluggable backend defaults

Every external dependency defaults to a safe stub that requires no credentials in development or CI.

| Variable | Options | Default |
|----------|---------|---------|
| `SMS_GATEWAY` | `console`, `africastalking` | `console` |
| `EMAIL_PROVIDER` | `console`, `smtp` | `console` |
| `MODERATION_PROVIDER` | `mock`, `rekognition` | `mock` |
| `STORAGE_BACKEND` | `mock`, `s3` | `mock` |
| `VISION_PROVIDER` | `mock`, `openai`, `anthropic`, `ollama` | `mock` |
| `GEOCODING_PROVIDER` | `nominatim`, `google`, `mock` | `nominatim` |
| `TRANSLATION_PROVIDER` | `libretranslate`, `argostranslate`, `mock` | `libretranslate` |

See `.env.example` for the full reference including all production settings, rotation schedules, and security notes.

---

## Authentication

CrisisMap uses three roles with different authentication flows.

### Anonymous reporter

No registration. A zero-friction JWT is issued immediately.

```http
POST /api/v1/auth/anonymous
```

```json
{ "session_token": "<jwt>" }
```

### Reporter (phone OTP)

```http
POST /api/v1/auth/otp/send    { "phone": "+254700000000" }
POST /api/v1/auth/otp/verify  { "phone": "+254700000000", "otp": "123456" }
```

Returns `token`, `refresh_token`, and `role`. OTP codes expire in 5 minutes. Five failed attempts in 15 minutes triggers a 15-minute lockout (HTTP 429).

### Analyst / Responder / Admin (phone OTP, provisioned account)

Analysts use the same OTP flow, but their phone number must be provisioned first by an admin. At verify time the system looks up the phone hash in `analyst_accounts` and issues an elevated token automatically.

To provision the first admin account (direct DB insert, no existing admin required):

```bash
docker exec -e PYTHONPATH=/app <app-container> python /app/app/cli.py create-admin --phone +254700000000
```

Subsequent accounts can be provisioned via the HTTP API using an admin token:

```http
POST /api/v1/auth/analyst/register
Authorization: Bearer <admin-token>

{ "phone": "+254700000000", "role": "analyst" }
```

### Token usage

All authenticated endpoints accept the token in either header:

```http
Authorization: Bearer <token>
X-Session-Token: <token>
```

### Token refresh

```http
POST /api/v1/auth/refresh
{ "refresh_token": "<refresh-token>" }
```

Refresh tokens are single-use and rotate on every call. Store the new refresh token returned in each response.

### Security properties

- Phone numbers are never stored in plaintext. Only SHA-256 hashes salted with `PHONE_HASH_SALT` are persisted.
- Tokens are added to a Redis denylist on logout.
- Sensitive fields (phone numbers, JWTs, presigned URLs) are automatically scrubbed from structured logs.

---

## API Reference

Interactive Swagger UI: `http://localhost:8000/docs`

ReDoc: `http://localhost:8000/redoc`

OpenAPI schema: `http://localhost:8000/openapi.json`

A comprehensive frontend integration guide covering all endpoints, workflows, request/response examples, and authentication patterns is available at [`docs/frontend-integration-guide.md`](docs/frontend-integration-guide.md).

### Health endpoints

These are intentionally outside the `/api/v1` prefix so orchestrators can reach them without routing configuration.

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness probe. Always fast, no I/O. |
| `GET /health/ready` | Readiness probe. Checks PostgreSQL, Redis, and object storage. |
| `GET /health/worker` | Celery worker ping. |

### Key endpoint groups

| Prefix | Roles | Description |
|--------|-------|-------------|
| `/api/v1/auth/` | All | Token issuance, OTP flow, refresh, logout |
| `/api/v1/reports/` | Anonymous, reporter | Submit reports, check nearby, poll status |
| `/api/v1/stats/` | Public | Summary counts and heatmap GeoJSON |
| `/api/v1/analyst/` | Analyst, responder, admin | Report triage, status transitions, notes, SSE stream |
| `/api/v1/export/` | Analyst, responder, admin | GeoJSON, CSV, Shapefile export |
| `/api/v1/auth/analyst/` | Admin | Account provisioning and management |
| `/metrics` | Bearer token | Prometheus metrics scrape |

---

## Testing

```bash
# Run all unit tests with coverage
pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=80 -v

# Run a single file
pytest tests/test_auth.py -v

# Run a single test by name
pytest tests/test_reports.py::test_submit_report -v
```

The test suite requires no running services. All external dependencies (database, Redis, SMS, storage, AI) are mocked or patched in unit tests. The coverage threshold is 80%.

### Linting and formatting

```bash
flake8 app/ tests/
mypy app/
black app/ tests/
isort app/ tests/
```

---

## Production Deployment

A production Docker Compose configuration is provided in `docker-compose.prod.yml`. It extends the base compose file with production-specific overrides including MinIO for object storage, LibreTranslate for translation, and Ollama for local vision inference.

```bash
# Pull latest and rebuild
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# Run migrations (runs automatically inside the entrypoint, but can be triggered manually)
docker exec <app-container> alembic upgrade head
```

### First-time production setup

1. Copy `.env.example` to `.env` and fill in all required values.
2. Set `ENVIRONMENT=production` and `SMS_GATEWAY=africastalking`.
3. Set `STORAGE_BACKEND=s3` and configure bucket credentials.
4. Create the first admin account with the CLI (see [Admin CLI](#admin-cli)).
5. Import building footprints if GIS matching is required (see below).

### MinIO bucket setup (first boot only)

```bash
docker exec -it <minio-container> mc alias set local http://localhost:9000 \
    "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
docker exec -it <minio-container> mc mb local/crisismap
```

### Building footprint import

Without building footprints the GIS worker still geocodes reports, but `building_id` will be null on every submission.

```bash
docker exec -e PYTHONPATH=/app <app-container> \
    python -m app.cli.import_footprints --source /path/to/footprints.geojson
```

### Rate limits

All rate limits are declared in `app/core/rate_limits.py`. Clients receive `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `Retry-After` headers on every response.

### JWT rotation procedure

1. Generate a new `JWT_SECRET_KEY` value.
2. Update `.env` on the server.
3. Restart the app container.
4. All existing sessions are immediately invalidated. Users must log in again.

---

## Admin CLI

The management CLI runs inside the app container and writes directly to the database. It is the only way to bootstrap the first admin account (the HTTP provisioning endpoint requires an existing admin token).

```bash
# Provision the first admin
docker exec -e PYTHONPATH=/app <app-container> \
    python /app/app/cli.py create-admin --phone +254700000000

# List all provisioned accounts
docker exec -e PYTHONPATH=/app <app-container> \
    python /app/app/cli.py list-accounts

# Deactivate an account by ID
docker exec -e PYTHONPATH=/app <app-container> \
    python /app/app/cli.py deactivate-account --id <uuid>
```

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

Key points:
- Run the full test suite and ensure coverage stays above 80% before submitting.
- All linters (flake8, mypy, black, isort) must pass with zero warnings.
- Open an issue before starting major feature work.
- Follow the existing commit message format: `type(scope): description`.

---

## Security

To report a vulnerability, email **collins.kubu@gmail.com** directly. Do not open a public GitHub issue for security reports. See [SECURITY.md](SECURITY.md) for the full policy.

---

## License

[MIT](LICENSE) -- Copyright (c) 2024 Kubu Ventures
