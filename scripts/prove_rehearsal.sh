#!/usr/bin/env bash
# Hidden-test rehearsal HC1-HC7 through the batch runner, graded 4/2/0 -> evidence/hard_cases/rehearsal_summary.csv.
# Uses the stack named by IBX_API_URL / IBX_MOCK_URL, or brings one up (scripts/prove_common.sh).
source "$(dirname "$0")/prove_common.sh"
ensure_stack
uv run python scripts/rehearse.py --api "$IBX_API_URL" --mock "$IBX_MOCK_URL" "$@"
