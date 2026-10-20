#!/usr/bin/env bash
# Sourced by scripts/prove_*.sh. ensure_stack: use the stack named by the environment, or bring one up.
#   * IBX_API_URL set and /health answers   -> use that stack (nothing is started or stopped)
#   * otherwise                              -> docker compose up -d --build --wait (local mode: real Lemmy + Ghost,
#                                               mocks for the rest; APP_ENV=dev so the simulation tools are on) and
#                                               docker compose down -v on exit (unless IBX_KEEP_STACK=1)
# Ports: IBX_API_PORT / IBX_MOCK_PORT (default: free ports picked by the OS, so no other stack answers for ours).
# Project: COMPOSE_PROJECT_NAME (default ibx_prove).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

_health() { uv run python -c "import httpx,sys; sys.exit(0 if httpx.get('$1/health', timeout=5).status_code == 200 else 1)" >/dev/null 2>&1; }

ensure_stack() {
  export IBX_BOOTSTRAP_OUT_UID="${IBX_BOOTSTRAP_OUT_UID:-10001}" IBX_BOOTSTRAP_OUT_GID="${IBX_BOOTSTRAP_OUT_GID:-10001}"
  export API_TOKEN="${API_TOKEN:-ibx-dev-token}"
  if [[ -n "${IBX_API_URL:-}" ]] && _health "$IBX_API_URL"; then
    export IBX_MOCK_URL="${IBX_MOCK_URL:-http://127.0.0.1:${IBX_MOCK_PORT:-8089}}"
    echo "using the running stack at $IBX_API_URL (project ${COMPOSE_PROJECT_NAME:-?})"
    return 0
  fi
  export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ibx_prove}"
  local free='import socket\ns=[socket.socket() for _ in range(2)]\n[x.bind(("127.0.0.1",0)) for x in s]\nprint(" ".join(str(x.getsockname()[1]) for x in s))'
  if [[ -z "${IBX_API_PORT:-}" || -z "${IBX_MOCK_PORT:-}" ]]; then
    read -r IBX_API_PORT IBX_MOCK_PORT < <(python3 -c "$(printf "$free")")
  fi
  export IBX_API_PORT IBX_MOCK_PORT
  export SIMULATION_MODE="${SIMULATION_MODE:-false}" APP_ENV="${APP_ENV:-dev}"
  export PUBLIC_BASE_URL="http://127.0.0.1:${IBX_API_PORT}"
  if [[ "${IBX_KEEP_STACK:-0}" != "1" ]]; then
    trap 'docker compose down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
  fi
  echo "bringing up compose project $COMPOSE_PROJECT_NAME on ports $IBX_API_PORT/$IBX_MOCK_PORT (SIMULATION_MODE=$SIMULATION_MODE)"
  docker compose up -d --build --wait
  export IBX_API_URL="http://127.0.0.1:${IBX_API_PORT}" IBX_MOCK_URL="http://127.0.0.1:${IBX_MOCK_PORT}"
}
