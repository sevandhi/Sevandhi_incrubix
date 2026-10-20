"""CORE2 proof: each Core connector passes the full certification run (publish, confirm live URL, status, delete)
with stored evidence. Re-checks the packs that prove_mh2.py exported in this run (runs it first if needed).

    scripts/prove_core2.sh

Per Core connector: a PASSED run at the expected level; the exported pack on disk has run.json, summary.md, the
manifest snapshot, the step files for publish / confirm live / status / delete / confirm deleted (assisted: build pack /
confirm pack / delete / confirm deleted), the deletion (removal) confirmation, and a screenshot or rendered API response.
Evidence: evidence/levels/core/CORE2_certification_runs.csv
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys

from prove_lib import CORE, ROOT, Proof, read_proof

STEP_FILES = {"live": ["PUBLISH", "CONFIRM_LIVE", "STATUS", "DELETE", "CONFIRM_DELETED"],
              "assisted": ["BUILD_PACK", "CONFIRM_PACK", "DELETE", "CONFIRM_DELETED"]}


def main() -> int:
    run_id = os.environ.get("IBX_PROVE_RUN_ID")
    mh2 = read_proof("MH2")
    if mh2 is None or (run_id and mh2.get("run_id") != run_id):
        subprocess.run([sys.executable, "scripts/prove_mh2.py"], cwd=ROOT, check=False)
    p = Proof("CORE2", "Each Core connector passes the full certification run with stored evidence", "scripts/prove_core2.sh")
    summary = ROOT / "evidence/must_haves/MH2_certification_summary.csv"
    rows = list(csv.DictReader(summary.open(encoding="utf-8"))) if summary.exists() else []
    out = []
    no_shot = []
    for connector in CORE:
        mine = [r for r in rows if r["connector"] == connector]
        p.check(f"{connector}: certified in this run", any(r["state"] == "PASSED" and r["level_after"] == r["expected_level"]
                                                         for r in mine), [(r["target"], r["state"], r["level_after"]) for r in mine])
        for r in mine:
            folder = ROOT / r["pack"] if r["pack"] else None
            names = {str(f.relative_to(folder)) for f in folder.rglob("*") if f.is_file()} if folder and folder.exists() else set()
            steps = STEP_FILES["assisted" if connector == "assisted" else "live"]
            alias = {"CONFIRM_DELETED": ("CONFIRM_DELETED", "CONFIRM_REMOVED")}  # Dev.to: removed (unpublished), not deleted
            has_steps = all(any(n.startswith("steps/") and n.endswith(tuple(f"_{a}.json" for a in alias.get(s, (s,))))
                                for n in names) for s in steps)
            core_files = {"run.json", "summary.md", "manifest.snapshot.yaml"} <= names
            confirmation = bool({"deletion_confirmation.json", "removal_confirmation.json"} & names)
            shots = sorted(n for n in names if n.lower().endswith((".png", ".jpg", ".jpeg", ".html")))
            ok = core_files and has_steps and confirmation
            p.check(f"{connector}@{r['target']}: pack on disk has run record, every step, deletion confirmation", ok,
                    {"pack": r["pack"], "files": len(names)})
            if not shots:
                no_shot.append(f"{connector}@{r['target']}")
            out.append({"connector": connector, "target": r["target"], "run_id": r["run_id"], "state": r["state"],
                        "level_after": r["level_after"], "delete_mode": r["delete_mode"], "live_url": r["live_url"],
                        "duration_ms": r["duration_ms"], "pack": r["pack"], "pack_complete": ok, "screenshots": ";".join(shots)})
    if no_shot:
        p.add_partial("no screenshot in the certification pack for: " + ", ".join(no_shot))
    p.write_csv("evidence/levels/core/CORE2_certification_runs.csv", out,
                ["connector", "target", "run_id", "state", "level_after", "delete_mode", "live_url", "duration_ms", "pack",
                 "pack_complete", "screenshots"])
    return p.finish("evidence/levels/core/CORE2_certification_runs.log")


if __name__ == "__main__":
    sys.exit(main())
