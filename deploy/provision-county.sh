#!/usr/bin/env bash
# Provision (or update) one county's Matata site on this server.
#
#   deploy/provision-county.sh --slug tana-river --name "Tana River County" \
#       --admin coordinator@example.go.ke \
#       --analyst drm1@example.go.ke --analyst gis@example.go.ke \
#       --responder ops@example.go.ke \
#       --hotspot "Hola=39.98,-1.53,40.06,-1.46" \
#       --hotspot "Garsen=40.08,-2.30,40.15,-2.24"
#
# Result: https://<slug>.<BASE_DOMAIN> serving the reporting app and the
# analyst portal, backed by a stack whose data belongs to that county alone.
#
# Safe to re-run: secrets are generated once and kept, accounts that already
# exist are left alone, footprints are upserted, and containers are rebuilt
# only when the code changed. Re-run it to add people or hotspots.
#
# Requires: docker with compose v2, curl, openssl. Settings shared by every
# county live in deploy/operator.env (copy deploy/operator.env.example).

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERPASS_URL="${OVERPASS_URL:-https://overpass-api.de/api/interpreter}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-600}"

usage() {
  cat <<'EOF'
Usage: deploy/provision-county.sh --slug SLUG --name NAME --admin EMAIL [options]

Required:
  --slug SLUG            Lowercase id used in the web address, e.g. tana-river
  --name NAME            Display name, e.g. "Tana River County"
  --admin EMAIL          The county's pilot coordinator (admin role)

People (repeat as needed):
  --analyst EMAIL        Reviews and verifies reports
  --responder EMAIL      Acts on verified reports

Building footprints (repeat as needed):
  --hotspot NAME=BBOX    Fetch OpenStreetMap buildings for a box given as
                         min_lng,min_lat,max_lng,max_lat (e.g. from bboxfinder.com)
  --footprints FILE      Import an OSM GeoJSON / Overpass JSON file you already have

Options:
  --country CODE         ISO country code to bias geocoding (default: ke)
  --refresh-footprints   Re-download hotspots even if a cached copy exists
  --no-photo-screening   Deploy without AWS Rekognition (demos only)
  -h, --help             Show this help
EOF
}

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    WARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ── Arguments ────────────────────────────────────────────────────────────────
SLUG="" NAME="" ADMIN="" COUNTRY="ke"
ANALYSTS=() RESPONDERS=() HOTSPOTS=() FOOTPRINT_FILES=()
REFRESH_FOOTPRINTS=0 PHOTO_SCREENING=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug) SLUG="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --admin) ADMIN="${2:-}"; shift 2 ;;
    --analyst) ANALYSTS+=("${2:-}"); shift 2 ;;
    --responder) RESPONDERS+=("${2:-}"); shift 2 ;;
    --hotspot) HOTSPOTS+=("${2:-}"); shift 2 ;;
    --footprints) FOOTPRINT_FILES+=("${2:-}"); shift 2 ;;
    --country) COUNTRY="${2:-}"; shift 2 ;;
    --refresh-footprints) REFRESH_FOOTPRINTS=1; shift ;;
    --no-photo-screening) PHOTO_SCREENING=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown argument: $1" ;;
  esac
done

EMAIL_RE='^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
BBOX_RE='^-?[0-9]+(\.[0-9]+)?,-?[0-9]+(\.[0-9]+)?,-?[0-9]+(\.[0-9]+)?,-?[0-9]+(\.[0-9]+)?$'

[[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$ ]] \
  || die "--slug must be 3-32 lowercase letters, digits or hyphens (got '$SLUG')."
[[ -n "$NAME" ]] || die "--name is required."
[[ "$NAME" != *'"'* && "$NAME" != *'$'* ]] || die "--name cannot contain \" or \$."
[[ "$ADMIN" =~ $EMAIL_RE ]] || die "--admin must be an email address."
for e in "${ANALYSTS[@]}" "${RESPONDERS[@]}"; do
  [[ "$e" =~ $EMAIL_RE ]] || die "'$e' is not an email address."
done
for h in "${HOTSPOTS[@]}"; do
  [[ "$h" == *=* ]] || die "--hotspot must be NAME=min_lng,min_lat,max_lng,max_lat (got '$h')."
  [[ "${h#*=}" =~ $BBOX_RE ]] || die "Bad bounding box in --hotspot '$h'."
  [[ "${h%%=*}" =~ ^[A-Za-z0-9._-]+$ ]] || die "Hotspot name '${h%%=*}' may use only letters, digits, . _ -"
done
for f in "${FOOTPRINT_FILES[@]}"; do
  [[ -f "$f" ]] || die "Footprints file not found: $f"
done
[[ "$COUNTRY" =~ ^[a-z]{2}$ ]] || die "--country must be a two-letter code like ke."

# ── Operator settings ────────────────────────────────────────────────────────
OPERATOR_ENV="$DEPLOY_DIR/operator.env"
[[ -f "$OPERATOR_ENV" ]] || die "Missing $OPERATOR_ENV — copy operator.env.example and fill it in."
set -a
# shellcheck source=/dev/null
source "$OPERATOR_ENV"
set +a

: "${BASE_DOMAIN:?set BASE_DOMAIN in operator.env}"
: "${ACME_EMAIL:?set ACME_EMAIL in operator.env}"
: "${FRONTEND_DIR:?set FRONTEND_DIR in operator.env}"
: "${PRIVY_APP_ID:?set PRIVY_APP_ID in operator.env — residents can report without it, but no analyst can log in}"
: "${PRIVY_VERIFICATION_KEY:?set PRIVY_VERIFICATION_KEY in operator.env}"
VISION_PROVIDER="${VISION_PROVIDER:-ollama}"
TRANSLATION_PROVIDER="${TRANSLATION_PROVIDER:-libretranslate}"
[[ -f "$FRONTEND_DIR/package.json" ]] || die "FRONTEND_DIR ($FRONTEND_DIR) is not the frontend's matata-app/ directory."

if [[ $PHOTO_SCREENING -eq 1 ]]; then
  [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]] \
    || die "Photo screening needs AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in operator.env (or pass --no-photo-screening for a demo)."
  MODERATION_PROVIDER=rekognition
else
  warn "Photos will NOT be screened. Do not use this for a real county pilot."
  MODERATION_PROVIDER=mock
fi
if [[ "$VISION_PROVIDER" == "anthropic" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
  die "VISION_PROVIDER=anthropic needs ANTHROPIC_API_KEY in operator.env."
fi

HOST="$SLUG.$BASE_DOMAIN"
PUBLIC_URL="https://$HOST"
COUNTY_DIR="$DEPLOY_DIR/counties/$SLUG"
PROJECT="matata-$SLUG"
# .localhost names get Caddy's internal CA instead of Let's Encrypt (local testing).
LOCAL_TEST=0; [[ "$BASE_DOMAIN" == "localhost" ]] && LOCAL_TEST=1

# ── Preflight ────────────────────────────────────────────────────────────────
log "Checking this server"
for cmd in docker curl openssl; do
  command -v "$cmd" >/dev/null || die "'$cmd' is not installed."
done
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required ('docker compose')."
docker info >/dev/null 2>&1 || die "Cannot talk to Docker. Is the daemon running and are you in the docker group?"
info "docker $(docker version --format '{{.Server.Version}}'), $(docker compose version --short)"

if [[ $LOCAL_TEST -eq 0 ]]; then
  if getent hosts "$HOST" >/dev/null; then
    info "DNS: $HOST -> $(getent hosts "$HOST" | awk '{print $1}' | head -1)"
  else
    warn "$HOST does not resolve yet. Point it (or *.$BASE_DOMAIN) at this server; HTTPS starts working once it does."
  fi
fi

# ── Shared services ──────────────────────────────────────────────────────────
log "Starting shared services (HTTPS proxy$( [[ $VISION_PROVIDER == ollama ]] && echo ", vision model")$( [[ $TRANSLATION_PROVIDER == libretranslate ]] && echo ", translation"))"
SHARED_PROFILES=()
[[ "$VISION_PROVIDER" == "ollama" ]] && SHARED_PROFILES+=(ollama)
[[ "$TRANSLATION_PROVIDER" == "libretranslate" ]] && SHARED_PROFILES+=(translate)
shared() {
  COMPOSE_PROFILES="$(IFS=,; echo "${SHARED_PROFILES[*]:-}")" \
    docker compose -p matata-shared -f "$DEPLOY_DIR/shared/docker-compose.yml" \
      --env-file "$OPERATOR_ENV" "$@"
}
mkdir -p "$DEPLOY_DIR/shared/sites"
shared up -d
[[ "$VISION_PROVIDER" == "ollama" ]] \
  && info "First boot downloads the LLaVA model (~4.7 GB); AI checks start once it finishes."

# ── County settings ──────────────────────────────────────────────────────────
log "Writing settings for $NAME"
mkdir -p "$COUNTY_DIR/footprints"
chmod 700 "$COUNTY_DIR"

SECRETS_FILE="$COUNTY_DIR/secrets.env"
if [[ ! -f "$SECRETS_FILE" ]]; then
  umask 077
  cat > "$SECRETS_FILE" <<EOF
# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ). Never regenerate: rotating
# PHONE_HASH_SALT orphans every account; losing POSTGRES_PASSWORD locks the DB.
SECRET_KEY=$(openssl rand -base64 32)
JWT_SECRET_KEY=$(openssl rand -base64 32)
PHONE_HASH_SALT=$(openssl rand -base64 32)
POSTGRES_PASSWORD=$(openssl rand -hex 24)
MINIO_ROOT_USER=matata-$SLUG
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
EOF
  umask 022
  info "Generated new secrets in counties/$SLUG/secrets.env — back this file up."
else
  info "Keeping existing secrets in counties/$SLUG/secrets.env"
fi
# shellcheck source=/dev/null
source "$SECRETS_FILE"

COUNTY_ENV="$COUNTY_DIR/county.env"
umask 077
cat > "$COUNTY_ENV" <<EOF
# Rendered by provision-county.sh on every run — edit operator.env or the
# script instead; changes made here are overwritten.

# Deployment wiring (read by deploy/county/docker-compose.yml)
COUNTY_SLUG=$SLUG
COUNTY_NAME="$NAME"
COUNTY_ENV_FILE=$COUNTY_ENV
PUBLIC_URL=$PUBLIC_URL
FRONTEND_DIR=$FRONTEND_DIR
DEPLOY_DIR=$DEPLOY_DIR

# Core
ENVIRONMENT=production
LOG_LEVEL=info
SECRET_KEY=$SECRET_KEY
JWT_SECRET_KEY=$JWT_SECRET_KEY
PHONE_HASH_SALT=$PHONE_HASH_SALT
ALLOWED_ORIGINS=$PUBLIC_URL
APP_PUBLIC_URL=$PUBLIC_URL
DASHBOARD_BASE_URL=$PUBLIC_URL
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
DATABASE_URL=postgresql+asyncpg://matata:$POSTGRES_PASSWORD@postgres:5432/matata_db
REDIS_URL=redis://redis:6379/0

# Login (Privy email OTP)
PRIVY_APP_ID=$PRIVY_APP_ID
PRIVY_VERIFICATION_KEY="$PRIVY_VERIFICATION_KEY"

# Photos: stored in this county's own MinIO; screened by Rekognition
STORAGE_BACKEND=s3
S3_BUCKET_NAME=crisismap
S3_ENDPOINT_URL=http://minio:9000
MINIO_ROOT_USER=$MINIO_ROOT_USER
MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASSWORD
S3_ACCESS_KEY_ID=$MINIO_ROOT_USER
S3_SECRET_ACCESS_KEY=$MINIO_ROOT_PASSWORD
MODERATION_PROVIDER=$MODERATION_PROVIDER
AWS_REGION=${AWS_REGION:-eu-west-1}
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}

# AI second opinion
VISION_PROVIDER=$VISION_PROVIDER
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_VISION_MODEL=llava
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}

# Translation fallback
TRANSLATION_PROVIDER=$TRANSLATION_PROVIDER
LIBRETRANSLATE_URL=http://libretranslate:5000

# Geocoding
GEOCODING_PROVIDER=nominatim
GEOCODING_COUNTRY_CODE=$COUNTRY
GEOCODING_CONTACT=${GEOCODING_CONTACT:-$ACME_EMAIL}

# Login emails come from Privy; SMS OTP stays dormant
EMAIL_PROVIDER=console
SMS_GATEWAY=console
EOF
umask 022

county() {
  docker compose -p "$PROJECT" -f "$DEPLOY_DIR/county/docker-compose.yml" \
    --env-file "$COUNTY_ENV" "$@"
}

# ── HTTPS route ──────────────────────────────────────────────────────────────
SITE_FILE="$DEPLOY_DIR/shared/sites/$SLUG.caddy"
cat > "$SITE_FILE" <<EOF
# $NAME — written by provision-county.sh
$HOST {
	encode zstd gzip
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		Referrer-Policy strict-origin-when-cross-origin
	}
	@api path /api/* /health /health/*
	handle @api {
		request_body {
			max_size 20MB
		}
		reverse_proxy $SLUG-api:8000
	}
	handle {
		reverse_proxy $SLUG-web:3000
	}
}
EOF

# ── Build and start ──────────────────────────────────────────────────────────
log "Building and starting $PROJECT (first build takes 10-20 minutes)"
county up -d --build --remove-orphans

log "Waiting for the API to become healthy"
app_id="$(county ps -q app)"
deadline=$(( SECONDS + HEALTH_TIMEOUT_S ))
until [[ "$(docker inspect -f '{{.State.Health.Status}}' "$app_id" 2>/dev/null)" == "healthy" ]]; do
  (( SECONDS < deadline )) || { county logs --tail 50 app >&2; die "API not healthy after ${HEALTH_TIMEOUT_S}s (logs above)."; }
  sleep 5
done
info "API healthy (migrations applied)."

# ── Building footprints ──────────────────────────────────────────────────────
import_footprints() {  # $1 = host path
  local dest="/tmp/footprints-$(basename "$1")"
  docker cp "$1" "$app_id:$dest"
  county exec -T app python -m app.cli.import_footprints --source-type osm --source "$dest"
  county exec -T app rm -f "$dest"
}

if [[ ${#HOTSPOTS[@]} -gt 0 || ${#FOOTPRINT_FILES[@]} -gt 0 ]]; then
  log "Loading building footprints"
fi
for h in "${HOTSPOTS[@]}"; do
  hname="${h%%=*}"
  IFS=, read -r min_lng min_lat max_lng max_lat <<< "${h#*=}"
  out="$COUNTY_DIR/footprints/$hname.json"
  if [[ ! -s "$out" || $REFRESH_FOOTPRINTS -eq 1 ]]; then
    info "$hname: downloading OpenStreetMap buildings"
    # Overpass takes south,west,north,east. Relations are skipped: the importer
    # does not assemble multipolygons from Overpass JSON.
    query="[out:json][timeout:180];way[\"building\"]($min_lat,$min_lng,$max_lat,$max_lng);out geom;"
    curl -fsS --retry 3 --retry-delay 20 --max-time 300 \
      -A "Matata provisioning (+${GEOCODING_CONTACT:-$ACME_EMAIL})" \
      --data-urlencode "data=$query" "$OVERPASS_URL" -o "$out.part" \
      || die "Overpass download failed for $hname. Try again later, or pass --footprints with a HOT Export Tool file."
    mv "$out.part" "$out"
    sleep 5  # be gentle with the public Overpass server
  else
    info "$hname: using cached download (--refresh-footprints to re-fetch)"
  fi
  import_footprints "$out"
done
for f in "${FOOTPRINT_FILES[@]}"; do
  info "$(basename "$f"): importing"
  import_footprints "$f"
done

# ── Accounts ─────────────────────────────────────────────────────────────────
log "Creating review team accounts"
create_account() {  # $1 = email, $2 = role, $3 = label
  county exec -T app python -m app.cli create-account --email "$1" --role "$2" --label "$3" \
    | grep -E '^(SUCCESS|INFO|ERROR)' | sed 's/^/    /'
}
create_account "$ADMIN" admin "$SLUG-coordinator"
n=0; for e in "${ANALYSTS[@]}"; do n=$((n + 1)); create_account "$e" analyst "$SLUG-analyst-$n"; done
n=0; for e in "${RESPONDERS[@]}"; do n=$((n + 1)); create_account "$e" responder "$SLUG-responder-$n"; done

# ── Go live ──────────────────────────────────────────────────────────────────
log "Publishing https://$HOST"
shared exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

CURL_OPTS=(-fsS --max-time 15)
[[ $LOCAL_TEST -eq 1 ]] && CURL_OPTS+=(-k --resolve "$HOST:443:127.0.0.1")
ok=0
for _ in $(seq 1 24); do  # certificate issuance can take a minute
  if curl "${CURL_OPTS[@]}" "$PUBLIC_URL/health" >/dev/null 2>&1 \
     && curl "${CURL_OPTS[@]}" -o /dev/null "$PUBLIC_URL/"; then
    ok=1; break
  fi
  sleep 5
done
if [[ $ok -eq 1 ]]; then
  info "Live: $PUBLIC_URL (reporting app) and $PUBLIC_URL/health (API)"
else
  warn "$PUBLIC_URL is not answering yet. Check DNS, ports 80/443, and: docker compose -p matata-shared logs caddy"
fi

log "Done: $NAME"
cat <<EOF
    Site            $PUBLIC_URL
    Analyst portal  $PUBLIC_URL/analyst
    Compose project $PROJECT  (logs: docker compose -p $PROJECT logs -f app)
    Secrets         $SECRETS_FILE  <- back this up somewhere safe

    Still to do by hand:
    1. Privy dashboard > Configuration > App settings > Allowed origins:
       add $PUBLIC_URL (until you do, nobody on the review team can log in).
    2. Log in at $PUBLIC_URL/analyst/login with each review team email.
    3. Send a test report from a phone inside a hotspot and verify it.
EOF
