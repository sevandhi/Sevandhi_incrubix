#!/usr/bin/env bash
# Integration Standard self-check against a running stack (default: the local compose API).
# Usage: scripts/integration_selfcheck.sh [API_URL]     (token from $API_TOKEN, else .env, else the dev default)
set -euo pipefail
cd "$(dirname "$0")/.."
API_URL="${1:-${IBX_API_URL:-http://127.0.0.1:${IBX_API_PORT:-8088}}}"
TOKEN="${API_TOKEN:-}"
if [[ -z "$TOKEN" && -f .env ]]; then TOKEN="$(grep -E '^API_TOKEN=' .env | tail -1 | cut -d= -f2-)"; fi
TOKEN="${TOKEN:-ibx-dev-token}"
status=0
uv run python scripts/selfcheck.py --api "$API_URL" --token "$TOKEN" "${@:2}" || status=$?
# IS8 evidence: a JSON log sample from the local compose stack (skipped when the API is remote).
if docker compose ps --status running --services 2>/dev/null | grep -qx api; then
  mkdir -p evidence/integration
  docker compose logs --no-log-prefix api worker 2>/dev/null | grep '^{' | tail -60 > evidence/integration/IS8_log_sample.jsonl
  if grep -qF "$TOKEN" evidence/integration/IS8_log_sample.jsonl; then
    echo "FAIL  IS8 log sample contains the API token" >&2; status=1
  else
    echo "PASS  IS8 log sample: $(wc -l < evidence/integration/IS8_log_sample.jsonl) JSON lines, API token absent"
  fi
fi
exit $status
