#!/usr/bin/env bash
# MH8 clean-copy test: copy the working tree (no .venv/.git/.env/caches), docker compose up --build --wait there,
# certify the real local Lemmy and Ghost, run scripts/test.sh, docker compose down -v. Needs no running stack.
# Project IBX_CLEAN_PROJECT (default ibx_clean), ports IBX_CLEAN_API_PORT/IBX_CLEAN_MOCK_PORT (default: free ports picked at start).
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python scripts/prove_mh8.py "$@"
