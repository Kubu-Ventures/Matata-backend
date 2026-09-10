#!/usr/bin/env bash
# One-off: ensure .env exists with the 5 required secrets populated.
set -euo pipefail
cd "$(dirname "$0")/.."

gen() { python3 -c 'import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())'; }

[ -f .env ] || cp .env.example .env

ensure() {
  local key="$1" val="$2"
  if grep -qE "^${key}=.+" .env; then
    echo "${key}: already set"
  else
    # portable in-place edit
    python3 - "$key" "$val" <<'PY'
import sys, pathlib
key, val = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
lines = p.read_text().splitlines()
out, seen = [], False
for ln in lines:
    if ln.startswith(key + "="):
        out.append(f"{key}={val}"); seen = True
    else:
        out.append(ln)
if not seen:
    out.append(f"{key}={val}")
p.write_text("\n".join(out) + "\n")
PY
    echo "${key}: generated"
  fi
}

ensure SECRET_KEY "$(gen)"
ensure JWT_SECRET_KEY "$(gen)"
ensure PHONE_HASH_SALT "$(gen)"

echo "--- required keys ---"
grep -nE "^(SECRET_KEY|JWT_SECRET_KEY|PHONE_HASH_SALT|DATABASE_URL|REDIS_URL)=" .env | sed 's/=.*/=<set>/'
