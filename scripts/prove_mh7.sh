#!/usr/bin/env bash
# MH7 proof: tokens never appear in logs or the repository; simulation mode runs everything with no real accounts.
# Uses the stack named by IBX_API_URL (+ COMPOSE_PROJECT_NAME for docker exec), or brings one up (scripts/prove_common.sh).
# Writes the evidence named in scripts/prove_mh7.py and evidence/proof_runs/; exit 0 = Yes, 2 = Partial, 1 = No.
source "$(dirname "$0")/prove_common.sh"
ensure_stack
uv run python scripts/prove_mh7.py "$@"
