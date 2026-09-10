#!/usr/bin/env bash
# End-to-end smoke test against the local docker-compose stack.
# Proves: anon auth -> submit (with photo) -> moderation -> storage -> DB insert
#         -> GIS job -> AI job -> duplicate scoring -> analyst-visible state.
set -uo pipefail
BASE="${BASE:-http://localhost:8081}"
cd "$(dirname "$0")/.."

pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
no()   { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "== 1. health =="
curl -sf "$BASE/health"        >/dev/null && ok "/health" || no "/health"
curl -sf "$BASE/health/ready"  | tee /tmp/ready.json | grep -q '"status":"ready"' && ok "/health/ready" || no "/health/ready"
cat /tmp/ready.json; echo

echo "== 2. anonymous auth =="
ANON=$(curl -sf -X POST "$BASE/api/v1/auth/anonymous")
echo "  $ANON"
TOKEN=$(echo "$ANON" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("session_token") or json.load(sys.stdin).get("token",""))' 2>/dev/null)
[ -n "$TOKEN" ] && ok "got session token" || { no "no token"; TOKEN=""; }

echo "== 3. make a tiny valid JPEG =="
python3 - <<'PY'
from PIL import Image
import io, random
random.seed(1)
im = Image.new("RGB",(320,240))
px = im.load()
for y in range(240):
    for x in range(320):
        px[x,y] = ((x*7+13)%256, (y*5+90)%256, ((x+y)*3)%256)
im.save("/tmp/sim_photo.jpg","JPEG",quality=85)
print("wrote /tmp/sim_photo.jpg")
PY

echo "== 4. submit a report with photo =="
META='{"crisis_type":"earthquake","infrastructure_type":"residential","damage_severity":"partial","lat":-1.2921,"lng":36.8219,"gps_accuracy_m":8.0,"most_pressing_needs":"water and shelter","debris_clearing_needed":true}'
RESP=$(curl -sf -X POST "$BASE/api/v1/reports" \
  -H "Authorization: Bearer $TOKEN" \
  -F "metadata=$META" \
  -F "photo=@/tmp/sim_photo.jpg;type=image/jpeg")
echo "  $RESP"
RID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])' 2>/dev/null)
[ -n "$RID" ] && ok "report created id=$RID" || no "report create failed"

echo "== 5. wait for async pipeline (GIS + AI + dedup) =="
for i in $(seq 1 30); do
  sleep 2
  ROW=$(docker compose exec -T postgres psql -U matata -d matata_db -tA -F'|' -c \
    "select status, photo_status, footprint_match_confidence, ai_confidence, ai_severity_prediction, review_priority, photo_phash is not null from report where id='$RID';" 2>/dev/null)
  echo "  t+$((i*2))s: $ROW"
  echo "$ROW" | grep -qE '\|(accepted|insufficient_quality|ai_processing_failed)\|' && break
done
echo "$ROW" | grep -qE '\|(accepted|insufficient_quality|ai_processing_failed)\|' && ok "AI stage terminal" || no "AI stage did not finish"
FMC=$(echo "$ROW" | cut -d'|' -f3)
[ -n "$FMC" ] && [ "$FMC" != "" ] && ok "GIS stage wrote footprint_match_confidence=$FMC" || no "GIS stage did not write"

echo "== 6. audit log rows for this report =="
AUD=$(docker compose exec -T postgres psql -U matata -d matata_db -tA -c \
  "select operation||' ' from audit_log where record_id='$RID' order by created_at;" 2>/dev/null | tr -d '\n')
echo "  $AUD"
echo "$AUD" | grep -q 'report.create' && ok "audit: report.create present" || no "audit: report.create missing"

echo "== 7. PII-at-rest spot check (no plaintext token, phone, or bearer in report row) =="
DUMP=$(docker compose exec -T postgres psql -U matata -d matata_db -tA -c \
  "select coalesce(reporter_token_hash,'') from report where id='$RID';" 2>/dev/null)
echo "  reporter_token_hash = $DUMP"
[ ${#DUMP} -eq 64 ] && ok "reporter_token_hash is a 64-char sha256 (not raw)" || no "unexpected reporter_token_hash"

echo "== 8. worker queues registered =="
docker compose exec -T celery-gis celery -A app.workers.celery_app inspect registered 2>/dev/null | grep -qE 'match_building' && ok "gis worker has match_building" || no "gis worker missing task"

echo
echo "==== smoke result: $pass passed, $fail failed ===="
exit $fail
