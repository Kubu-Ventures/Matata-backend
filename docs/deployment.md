# Deployment Guide

## Contents

1. [Development Quick-Start](#development-quick-start)
2. [Self-Hosted Production](#self-hosted-production)
3. [Cloud Prototype Deployments](#cloud-prototype-deployments)
4. [Minimum Server Requirements](#minimum-server-requirements)
5. [Database Backup Procedure](#database-backup-procedure)
6. [Environment Variable Reference](#environment-variable-reference)

---

## Development Quick-Start

```bash
# 1. Clone and configure environment
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY and any external API keys

# 2. Start all services (migrations run automatically on app startup)
docker compose up --build

# API will be available at:  http://localhost:8000
# Interactive docs at:       http://localhost:8000/docs
```

**Manual migration commands** (if needed outside Docker):

```bash
# Apply all pending migrations
make migrate
# or: alembic upgrade head

# Create a new migration after model changes
alembic revision --autogenerate -m "describe your change"
```

---

## Self-Hosted Production

### Prerequisites

- Docker ≥ 24 and Docker Compose plugin
- A domain name with DNS pointing to your server
- TLS certificates (Let's Encrypt recommended — see below)

### Step 1 — Obtain TLS Certificates

```bash
# Using Certbot (standalone mode — stop nginx first if running)
certbot certonly --standalone -d your-domain.com

# Copy certificates to the location nginx expects
mkdir -p infra/certs
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem infra/certs/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem   infra/certs/
chmod 600 infra/certs/privkey.pem
```

### Step 2 — Configure Environment

```bash
cp .env.example .env
# Set all required variables — see Environment Variable Reference below
# Critical: SECRET_KEY, DATABASE_URL, REDIS_URL, ALLOWED_ORIGINS
```

### Step 3 — Build and Start

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.prod.yml \
  up -d --build
```

### Step 4 — Verify

```bash
# Check all containers are healthy
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps

# Tail logs
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f app

# Health endpoint
curl https://your-domain.com/health
```

### Renewing TLS Certificates

```bash
# Add to crontab (runs at 2am on the 1st of each month)
0 2 1 * * certbot renew --quiet && \
  cp /etc/letsencrypt/live/your-domain.com/fullchain.pem /path/to/project/infra/certs/ && \
  cp /etc/letsencrypt/live/your-domain.com/privkey.pem   /path/to/project/infra/certs/ && \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec nginx nginx -s reload
```

---

## Cloud Prototype Deployments

### Render

1. Create a new **Web Service** → connect your GitHub repository.
2. Set **Environment** to `Docker`, **Dockerfile path** to `Dockerfile`, **Target** to `production`.
3. Add a **PostgreSQL** add-on (Render provides PostGIS support — select the PostGIS plan).
4. Add a **Redis** add-on.
5. Set all environment variables in the Render dashboard (see reference below).
6. Set the **Start Command** to leave blank (the `entrypoint.sh` handles migrations + server start).
7. Deploy. Render exposes port 8000 automatically behind their TLS proxy.

### Railway

1. Create a new project → **Deploy from GitHub Repo**.
2. Add a **PostgreSQL** plugin and a **Redis** plugin to the project.
3. In the service settings set **Dockerfile path** to `Dockerfile` and **Target** to `production`.
4. Under **Variables**, add all environment variables. Railway injects `DATABASE_URL` and `REDIS_URL` automatically from plugins — verify the variable names match your `.env.example`.
5. Deploy. Railway handles TLS automatically.

### Fly.io

```bash
# Install flyctl and authenticate
brew install flyctl && fly auth login

# Launch (follow prompts — choose Dockerfile, skip auto-deploy)
fly launch --dockerfile Dockerfile

# Set secrets
fly secrets set SECRET_KEY="your-secret" \
               DATABASE_URL="postgresql+asyncpg://..." \
               REDIS_URL="redis://..."

# Attach a managed Postgres cluster (includes PostGIS)
fly postgres create --name matata-db
fly postgres attach --app matata-backend matata-db

# Deploy
fly deploy --build-target production
```

---

## Minimum Server Requirements

Per tech spec §17.2, for up to **1,000 concurrent users**:

| Resource | Minimum        | Recommended     |
|----------|----------------|-----------------|
| vCPU     | 4              | 8               |
| RAM      | 8 GB           | 16 GB           |
| Disk     | 50 GB SSD      | 100 GB SSD      |
| Network  | 100 Mbps       | 1 Gbps          |
| OS       | Ubuntu 22.04+  | Ubuntu 24.04+   |

**Gunicorn workers**: set `WEB_CONCURRENCY` to `(2 × vCPU) + 1`. On a 4-vCPU server that is `9`.

---

## Database Backup Procedure

The `postgres_data` Docker volume is labelled `backup: "true"` in `docker-compose.prod.yml`.

### Manual Backup

```bash
# Dump the database to a compressed file
docker compose exec postgres \
  pg_dump -U matata -d matata_db -Fc \
  > backups/matata_db_$(date +%Y%m%d_%H%M%S).dump
```

### Restore from Backup

```bash
# Stop the app to prevent writes during restore
docker compose stop app

# Restore
docker compose exec -T postgres \
  pg_restore -U matata -d matata_db --clean < backups/matata_db_YYYYMMDD_HHMMSS.dump

# Restart
docker compose start app
```

### Automated Daily Backups (crontab)

```bash
# Run every day at 3am, keep 30 days of backups
0 3 * * * cd /path/to/project && \
  docker compose exec postgres \
    pg_dump -U matata -d matata_db -Fc \
  > backups/matata_db_$(date +\%Y\%m\%d).dump && \
  find backups/ -name "*.dump" -mtime +30 -delete
```

---

## Environment Variable Reference

All variables must be present in `.env` for production. Defaults shown where safe.

| Variable | Required | Description | Example |
|---|---|---|---|
| `SECRET_KEY` | ✅ | JWT signing secret — minimum 32 random chars | `openssl rand -hex 32` |
| `DATABASE_URL` | ✅ | Async PostgreSQL connection string | `postgresql+asyncpg://matata:pass@postgres:5432/matata_db` |
| `REDIS_URL` | ✅ | Redis connection string | `redis://redis:6379` |
| `ALLOWED_ORIGINS` | ✅ | Comma-separated CORS origins | `https://app.example.com` |
| `ENVIRONMENT` | ✅ | Runtime environment name | `production` |
| `LOG_LEVEL` | — | Logging verbosity | `info` |
| `WEB_CONCURRENCY` | — | Gunicorn worker count | `9` (4-vCPU server) |
| `POSTGRES_USER` | ✅ | Database username | `matata` |
| `POSTGRES_PASSWORD` | ✅ | Database password | — |
| `POSTGRES_DB` | ✅ | Database name | `matata_db` |
| `POSTGRES_HOST` | — | Postgres hostname (entrypoint wait loop) | `postgres` |
| `POSTGRES_PORT` | — | Postgres port (entrypoint wait loop) | `5432` |
| `ANTHROPIC_API_KEY` | ✅ | Claude AI API key for damage assessment | — |
| `AWS_ACCESS_KEY_ID` | ✅* | S3-compatible storage key | — |
| `AWS_SECRET_ACCESS_KEY` | ✅* | S3-compatible storage secret | — |
| `AWS_S3_BUCKET` | ✅* | Bucket name for photo uploads | `matata-reports` |
| `AWS_S3_ENDPOINT_URL` | — | Override for non-AWS S3 endpoints | — |
| `SENDGRID_API_KEY` | — | Email notification provider | — |
| `SENTRY_DSN` | — | Error tracking DSN | — |
| `CELERY_BROKER_URL` | — | Celery broker (defaults to `REDIS_URL`) | `redis://redis:6379/1` |
| `CELERY_RESULT_BACKEND` | — | Celery result backend | `redis://redis:6379/2` |

> ✅* = Required when the feature is enabled.