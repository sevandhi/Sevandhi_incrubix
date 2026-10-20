#!/usr/bin/env bash
# One command for every test suite (IS1). No API keys are needed.
#   scripts/test.sh            unit + contract + integration (testcontainers Postgres, mocks), THEN the compose stack
#                              in simulation (project ibx_test) with the end-to-end suite (tests/e2e) and the
#                              Integration Standard self-check; the stack is removed (-v)
#   scripts/test.sh --fast     unit + contract + integration only (no compose stack)
#   scripts/test.sh --unit     unit tests only (no Docker)
#   scripts/test.sh --e2e      only the compose-stack part
#   scripts/test.sh --live     the stack in local mode (real Lemmy + Ghost containers) and the live_local tests
# Ports: IBX_API_PORT / IBX_MOCK_PORT (defaults 18088 / 18089 here, so a running dev stack is not disturbed).
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:-all}"

stack_up() {
  export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ibx_test}"
  export IBX_API_PORT="${IBX_API_PORT:-18088}" IBX_MOCK_PORT="${IBX_MOCK_PORT:-18089}"
  export IBX_BOOTSTRAP_OUT_UID="${IBX_BOOTSTRAP_OUT_UID:-10001}" IBX_BOOTSTRAP_OUT_GID="${IBX_BOOTSTRAP_OUT_GID:-10001}"
  export SIMULATION_MODE="$1" APP_ENV="${APP_ENV:-dev}" PUBLIC_BASE_URL="http://127.0.0.1:${IBX_API_PORT}"
  trap 'docker compose down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
  docker compose up -d --build --wait
  export IBX_API_URL="http://127.0.0.1:${IBX_API_PORT}" IBX_MOCK_URL="http://127.0.0.1:${IBX_MOCK_PORT}"
  export IBX_E2E_API_URL="$IBX_API_URL" IBX_E2E_COMPOSE_PROJECT="$COMPOSE_PROJECT_NAME" IBX_LIVE_API_URL="$IBX_API_URL"
}

stack_tests() {
  stack_up true
  scripts/integration_selfcheck.sh "$IBX_API_URL"
  uv run pytest -q tests/e2e -m "not slow"
}

case "$mode" in
  --unit) exec uv run pytest -q tests/unit ;;
  --fast) exec uv run pytest -q -m "not live_local" ;;
  all|--all) uv run pytest -q -m "not live_local"; stack_tests ;;
  --e2e) stack_tests ;;
  --live) stack_up false; uv run pytest -q -m live_local ;;
  *) echo "usage: $0 [--fast|--unit|--e2e|--live]" >&2; exit 2 ;;
esac
