#!/usr/bin/env bash
# Every proof on one stack, then evidence/LEDGER.md. A form row is "Yes" only if its proof passed in THIS run.
#   scripts/prove_all.sh                      bring a local-mode stack up (scripts/prove_common.sh), run all, down -v
#   IBX_API_URL=... COMPOSE_PROJECT_NAME=... scripts/prove_all.sh     use a running stack
# Optional: IBX_PROVE_SKIP_MH8=1 (no clean-copy run), IBX_PROVE_SKIP_ADV=1 (no Advanced-level scripts),
#           IBX_LIVE_URL=<demo URL> (MH9 checks the live demo).
# Order matters: MH7 switches api/worker to simulation and back; MH9 reads MH8; CORE1/CORE2 read the others.
source "$(dirname "$0")/prove_common.sh"
set +e
export IBX_PROVE_RUN_ID="${IBX_PROVE_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
ensure_stack
declare -a results=()
run() { local id="$1"; shift; echo; echo "########## $id"; "$@"; local rc=$?; results+=("$id=$rc"); }
py() { uv run python "$@"; }

run MH1 py scripts/prove_mh1.py
run MH2 py scripts/prove_mh2.py
run MH3 py scripts/prove_mh3.py
run MH4 py scripts/prove_mh4.py
run MH6 py scripts/prove_mh6.py
run CORE3 py scripts/prove_core3.py
run REHEARSAL py scripts/rehearse.py --api "$IBX_API_URL" --mock "$IBX_MOCK_URL"
run MH5 py scripts/prove_mh5.py
if [[ "${IBX_PROVE_SKIP_ADV:-0}" != "1" ]]; then
  run ADV1 py scripts/prove_external.py ADV1 -- uv run python scripts/prove_adv1.py --api "$IBX_API_URL" --token "$API_TOKEN"
  run ADV2 py scripts/prove_external.py ADV2 --cap Partial --reason "eval_rules_capture.py measures the AI proposal against hand-written rules; the human confirm step is shown by ADV2_proposal_confirm.log, not re-run by this proof; this run re-measures the deterministic mock only (hosted free-tier rows in evidence/metrics/rules_capture_eval_summary.csv come from a separate keyed run and are not overwritten)" -- uv run python scripts/eval_rules_capture.py --only mock --out evidence/metrics/rules_capture_mock_rerun
  run ADV3 py scripts/prove_external.py ADV3 -- uv run python scripts/prove_adv3.py
  run ADV4 py scripts/prove_external.py ADV4 --cap Partial --reason "unit/property tests plus rehearsal HC3.8 (auto-fit with TITLE_TRUNCATED); no per-destination tailoring diff evidence pack yet" -- uv run pytest -q -p no:cacheprovider tests/unit/test_tailoring_profiles.py tests/unit/test_tailoring_truncate.py
fi
run MH7 py scripts/prove_mh7.py
if [[ "${IBX_PROVE_SKIP_MH8:-0}" != "1" ]]; then run MH8 py scripts/prove_mh8.py; fi
run MH9 py scripts/prove_mh9.py
run CORE2 py scripts/prove_core2.py
run CORE1 py scripts/prove_core1.py
py scripts/prove_ledger.py --run-id "$IBX_PROVE_RUN_ID"

echo; echo "########## summary (exit 0 = Yes, 2 = Partial, 1 = No), run $IBX_PROVE_RUN_ID"
worst=0
for r in "${results[@]}"; do echo "  $r"; rc="${r#*=}"; [[ "$rc" != 0 && "$worst" != 1 ]] && worst="$rc"; done
exit "$worst"
