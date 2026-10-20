#!/usr/bin/env bash
# CORE1 "All must-haves pass": reads the MH1-MH9 proof results of this run (run scripts/prove_all.sh first).
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python scripts/prove_core1.py "$@"
