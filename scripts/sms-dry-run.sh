#!/usr/bin/env bash
# Phase 3 gate: end-to-end SMS dry run on the LOCAL Supabase stack with the
# mock SMS provider. Never sends real messages (SMS_PROVIDER is forced to mock).
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=supabase/functions/.env.local
LOG=$(mktemp -t vigil-functions)
SECRET_FILE=$(mktemp -t vigil-secret)

npx supabase status >/dev/null 2>&1 || npx supabase start

# Throwaway secret for this run; the env file is gitignored.
openssl rand -hex 24 > "$SECRET_FILE"
{
  echo "SMS_PROVIDER=mock"
  echo "VIGIL_DISPATCH_SECRET=$(cat "$SECRET_FILE")"
  echo "VIGIL_APP_URL=http://localhost:5173"
} > "$ENV_FILE"

npx supabase functions serve --env-file "$ENV_FILE" > "$LOG" 2>&1 &
SERVE_PID=$!
cleanup() {
  kill "$SERVE_PID" 2>/dev/null || true
  wait "$SERVE_PID" 2>/dev/null || true
  rm -f "$SECRET_FILE"
}
trap cleanup EXIT

echo "waiting for edge functions..."
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:54321/functions/v1/dispatch || true)
  if [ "$code" = "401" ]; then break; fi
  sleep 1
done

VIGIL_DISPATCH_SECRET=$(cat "$SECRET_FILE") npx deno test \
  --config supabase/functions/deno.json --allow-net --allow-env --allow-read --allow-write --allow-run=uv \
  supabase/tests/e2e/

# Privacy check: logs may contain masked numbers (+1555***0901), never full ones.
if grep -Eq '\+1555555[0-9]{4}|\+15555550[0-9]{3}' "$LOG"; then
  echo "FAIL: full phone number found in function logs" >&2
  exit 1
fi
echo "OK: no full phone numbers in function logs ($(grep -c 'mock sms' "$LOG" || true) mock sends logged)"
