"""CORE1 proof: "All must-haves pass" = every MH1..MH9 proof reported Yes in this run.

    scripts/prove_core1.sh          (reads evidence/proof_runs/MH*.json; run scripts/prove_all.sh first)

With IBX_PROVE_RUN_ID set (prove_all.sh sets it), a proof from another run counts as not run.
Evidence: evidence/levels/core/CORE1_must_haves.csv
"""

from __future__ import annotations

import os
import sys

from prove_lib import Proof, read_proof


def main() -> int:
    p = Proof("CORE1", "All must-haves pass (MH1-MH9 proofs all Yes in this run)", "scripts/prove_core1.sh")
    run_id = os.environ.get("IBX_PROVE_RUN_ID")
    rows = []
    for n in range(1, 10):
        proof = read_proof(f"MH{n}")
        fresh = proof is not None and (run_id is None or proof.get("run_id") == run_id)
        status = proof["status"] if fresh else "not run"
        rows.append({"must_have": f"MH{n}", "status": status, "finished_at": (proof or {}).get("finished_at"),
                     "run_id": (proof or {}).get("run_id"), "reasons": " | ".join((proof or {}).get("reasons", []))[:600]})
        p.check(f"MH{n} proof status Yes", status == "Yes", f"{status}: {rows[-1]['reasons'][:200]}")
    p.write_csv("evidence/levels/core/CORE1_must_haves.csv", rows, ["must_have", "status", "finished_at", "run_id", "reasons"])
    return p.finish("evidence/levels/core/CORE1_must_haves.log")


if __name__ == "__main__":
    sys.exit(main())
