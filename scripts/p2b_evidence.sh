#!/usr/bin/env bash
# Evidence for the Core connectors (discourse, devto, webhook, assisted) and their mocks.
# Writes evidence/connectors/P2B_connectors.txt from real test runs (never typed by hand).
#
#   scripts/p2b_evidence.sh                       offline suites only (mocks + Postgres via testcontainers)
#   IBX_LIVE_API_URL=http://127.0.0.1:<port> IBX_LIVE_API_TOKEN=<token> IBX_LIVE_DISCOURSE_JSON=<path> \
#     scripts/p2b_evidence.sh                     also the real-Discourse tests (compose profile `full`)
set -euo pipefail
cd "$(dirname "$0")/.."
out=evidence/connectors/P2B_connectors.txt
mkdir -p "$(dirname "$out")"
offline=(
  tests/unit/test_p2b_connectors_unit.py
  tests/unit/test_p2b_mock_fidelity.py
  tests/integration/test_discourse_connector.py
  tests/integration/test_devto_connector.py
  tests/integration/test_webhook_connector.py
  tests/integration/test_assisted_connector.py
  tests/integration/test_p2b_fault_matrix.py
)
{
  echo "# Core connector evidence (discourse, devto, webhook, assisted)"
  echo "generated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "command: uv run pytest -m 'not live_local' -rA ${offline[*]}"
  echo
  uv run pytest -q -p no:cacheprovider -m "not live_local" -rA "${offline[@]}" 2>&1 | grep -E "^(PASSED|FAILED|ERROR|SKIPPED)|passed|failed" || true
  if [[ -n "${IBX_LIVE_API_URL:-}" && -n "${IBX_LIVE_DISCOURSE_JSON:-}" ]]; then
    echo
    echo "## Real local Discourse (compose profile full), via the API at ${IBX_LIVE_API_URL}"
    uv run pytest -q -p no:cacheprovider -m live_local_full -rA tests/integration/test_discourse_live_full.py 2>&1 \
      | grep -E "^(PASSED|FAILED|ERROR|SKIPPED)|passed|failed" || true
  else
    echo
    echo "## Real local Discourse: not run (IBX_LIVE_API_URL / IBX_LIVE_DISCOURSE_JSON not set)"
  fi
} > "$out"
echo "wrote $out"
grep -E "passed|failed" "$out"
