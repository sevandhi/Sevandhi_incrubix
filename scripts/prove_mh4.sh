#!/usr/bin/env bash
# MH4 proof: frequency cap, self-promotion, link policy and AI disclosure are checked before every post; a post that
# Uses the stack named by IBX_API_URL (+ COMPOSE_PROJECT_NAME for docker exec), or brings one up (scripts/prove_common.sh).
# Writes the evidence named in scripts/prove_mh4.py and evidence/proof_runs/; exit 0 = Yes, 2 = Partial, 1 = No.
source "$(dirname "$0")/prove_common.sh"
ensure_stack
uv run python scripts/prove_mh4.py "$@"
