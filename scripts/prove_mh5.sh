#!/usr/bin/env bash
# MH5 proof: never spam, never cheat. Test posts go only to our own instances and are removed within 10 minutes;
# Uses the stack named by IBX_API_URL (+ COMPOSE_PROJECT_NAME for docker exec), or brings one up (scripts/prove_common.sh).
# Writes the evidence named in scripts/prove_mh5.py and evidence/proof_runs/; exit 0 = Yes, 2 = Partial, 1 = No.
source "$(dirname "$0")/prove_common.sh"
ensure_stack
uv run python scripts/prove_mh5.py "$@"
