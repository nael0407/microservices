#!/usr/bin/env bash
# Pengujian end-to-end Track C via curl terhadap Lambda Function URL:
#   ADMIN_API_KEY="secret" ./tests/test_api.sh https://<id>.lambda-url.us-east-1.on.aws
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

BASE_URL="${1:-${FUNCTION_URL:-}}"
BASE_URL="${BASE_URL%/}"
if [[ -z "$BASE_URL" ]]; then
  echo "Error: berikan BASE_URL sebagai argumen atau jalankan ./scripts/aws_setup.sh dulu" >&2
  exit 1
fi

ADMIN_KEY="${ADMIN_API_KEY:-dev-admin-key}"
EVENT_ID="event-test-$(date +%s)"
IDEMP_1="idem-$(date +%s)-1"
IDEMP_2="idem-$(date +%s)-2"

echo "==> 1. Health check (GET /health)"
curl -sf "$BASE_URL/health" | python3 -m json.tool

echo "==> 2. Create ticket (POST /events/$EVENT_ID/tickets)"
CREATE_JSON=$(curl -sf -X POST "$BASE_URL/events/$EVENT_ID/tickets" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEMP_1" \
  -d '{"holderName":"Natanael","holderContact":"081234567890","tier":"VIP"}')
echo "$CREATE_JSON" | python3 -m json.tool
SIG=$(echo "$CREATE_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin)['signature'])")

echo "==> 3. Idempotency check (POST ulang dengan Idempotency-Key sama -> 200)"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE_URL/events/$EVENT_ID/tickets" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEMP_1" \
  -d '{"holderName":"Natanael","holderContact":"081234567890","tier":"VIP"}'

echo "==> 4. Get ticket detail & list (GET /events/$EVENT_ID/tickets)"
curl -sf "$BASE_URL/events/$EVENT_ID/tickets/$IDEMP_1" | python3 -m json.tool
curl -sf "$BASE_URL/events/$EVENT_ID/tickets?limit=10" | python3 -m json.tool

echo "==> 5. Update holderName saat reserved (PUT -> 200)"
curl -sf -X PUT "$BASE_URL/events/$EVENT_ID/tickets/$IDEMP_1" \
  -H "Content-Type: application/json" \
  -d '{"holderName":"Natanael S."}' | python3 -m json.tool

echo "==> 6. Admin update status paid tanpa X-API-Key (harus 401)"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X PUT "$BASE_URL/events/$EVENT_ID/tickets/$IDEMP_1" \
  -H "Content-Type: application/json" \
  -d '{"status":"paid"}'

echo "==> 7. Admin update status paid dengan X-API-Key (harus 200)"
curl -sf -X PUT "$BASE_URL/events/$EVENT_ID/tickets/$IDEMP_1" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ADMIN_KEY" \
  -d '{"status":"paid"}' | python3 -m json.tool

echo "==> 8. Check-in dengan signature palsu (harus 403)"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X PUT "$BASE_URL/events/$EVENT_ID/tickets/$IDEMP_1" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ADMIN_KEY" \
  -d '{"status":"checked-in","signature":"palsu"}'

echo "==> 9. Check-in dengan signature valid (harus 200)"
curl -sf -X PUT "$BASE_URL/events/$EVENT_ID/tickets/$IDEMP_1" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ADMIN_KEY" \
  -d "{\"status\":\"checked-in\",\"signature\":\"$SIG\"}" | python3 -m json.tool

echo "==> 10. Cancel tiket ke-2 dua kali (pertama 200, kedua 409)"
curl -sf -X POST "$BASE_URL/events/$EVENT_ID/tickets" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEMP_2" \
  -d '{"holderName":"Budi","holderContact":"081234567891","tier":"REG"}' >/dev/null
curl -s -o /dev/null -w "Cancel #1: HTTP %{http_code}\n" -X DELETE "$BASE_URL/events/$EVENT_ID/tickets/$IDEMP_2"
curl -s -o /dev/null -w "Cancel #2: HTTP %{http_code}\n" -X DELETE "$BASE_URL/events/$EVENT_ID/tickets/$IDEMP_2"

if [[ -n "${BUCKET_NAME:-}" ]]; then
  echo "==> 11. Cek audit log asinkron di S3 (audit/$EVENT_ID/)"
  sleep 3
  aws s3 ls "s3://${BUCKET_NAME}/audit/${EVENT_ID}/" --region "${AWS_REGION:-us-east-1}" || true
fi

echo "==> Semua tes end-to-end Track C selesai!"

