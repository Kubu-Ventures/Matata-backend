#!/bin/sh
set -e

# ── Wait for PostgreSQL ───────────────────────────────────────
# Uses pg_isready from the postgresql-client package.
# Retries up to 30 times (60 seconds total) before giving up.

MAX_RETRIES=30
RETRY_INTERVAL=2
RETRIES=0

echo "⏳ Waiting for PostgreSQL to be ready..."

until pg_isready -h "${POSTGRES_HOST:-postgres}" \
                 -p "${POSTGRES_PORT:-5432}" \
                 -U "${POSTGRES_USER:-matata}" \
                 -d "${POSTGRES_DB:-matata_db}" \
                 -q; do
  RETRIES=$((RETRIES + 1))
  if [ "$RETRIES" -ge "$MAX_RETRIES" ]; then
    echo "❌ PostgreSQL did not become ready after $((MAX_RETRIES * RETRY_INTERVAL)) seconds. Aborting."
    exit 1
  fi
  echo "   attempt $RETRIES/$MAX_RETRIES — retrying in ${RETRY_INTERVAL}s..."
  sleep "$RETRY_INTERVAL"
done

echo "✅ PostgreSQL is ready."

# ── Run Migrations ────────────────────────────────────────────
# A non-zero exit from alembic will propagate and halt container startup,
# which is the correct behaviour — never start with a broken schema.

echo "🔄 Running database migrations..."
alembic upgrade head
echo "✅ Migrations complete."

# ── Start Application ─────────────────────────────────────────
# Replace this shell process with the application server so that
# signals (SIGTERM, SIGINT) are forwarded correctly.

echo "🚀 Starting application server..."
exec "$@"