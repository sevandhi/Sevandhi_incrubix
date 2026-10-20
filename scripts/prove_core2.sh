#!/usr/bin/env bash
# CORE2 proof: each Core connector passes the full certification run (publish, confirm live URL, status, delete)
# Uses the stack named by IBX_API_URL (+ COMPOSE_PROJECT_NAME for docker exec), or brings one up (scripts/prove_common.sh).
# Writes the evidence named in scripts/prove_core2.py and evidence/proof_runs/; exit 0 = Yes, 2 = Partial, 1 = No.
source "$(dirname "$0")/prove_common.sh"
ensure_stack
uv run python scripts/prove_core2.py "$@"
