"""MH2 proof: the six Core connectors pass the full certification run, with the evidence packs exported to the repo.

    scripts/prove_mh2.sh

One new catalogue row per Core connector (mock-platforms instance), plus the REAL local Lemmy and Ghost when the
stack runs in local mode, then ``POST /api/v1/certifications`` for each. Each run must be PASSED at the expected level
(discourse/lemmy L4, ghost/webhook L3, devto L2 = unpublish only, assisted L1). Dev.to uses the normal-author path
(no admin unpublish), and its removal must never be reported as ``deleted: true``.
The packs are exported with scripts/export_cert.py into evidence/certification/<connector>/<destination>/<run_id>/.
Evidence: evidence/must_haves/MH2_certify.log, evidence/must_haves/MH2_certification_summary.csv,
          evidence/metrics/certification_runs.csv
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from prove_lib import CORE, REAL_DEST, ROOT, Proof, dest_url, tag

EXPECTED = {"discourse": "L4", "lemmy": "L4", "ghost": "L3", "webhook": "L3", "devto": "L2", "assisted": "L1"}
LIVE_STEPS = ["PUBLISH", "CONFIRM_LIVE", "STATUS", "DELETE", "CONFIRM_DELETED"]
ASSIST_STEPS = ["BUILD_PACK", "CONFIRM_PACK", "DELETE", "CONFIRM_DELETED"]


def row_for(connector: str, url: str, name: str) -> dict[str, Any]:
    return {"name": name, "url": url, "connector_key": connector, "language": "en", "frequency_cap": "3/P7D",
            "self_promo_allowed": "false", "link_policy": "allow", "disclosure_required": "true",
            "automation_allowed": "false" if connector == "assisted" else "true", "category": "core-certification"}


def export(run_id: str) -> Path | None:
    out = subprocess.run([sys.executable, "scripts/export_cert.py", "--run", run_id], cwd=ROOT, capture_output=True,
                         text=True, timeout=300)
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    if out.returncode != 0 or "->" not in line:
        return None
    return ROOT / line.split("->", 1)[1].strip()


def pack_check(p: Proof, connector: str, label: str, folder: Path | None, run: dict[str, Any]) -> dict[str, Any]:
    files = sorted(str(f.relative_to(folder)) for f in folder.rglob("*") if f.is_file()) if folder else []
    steps = [s["step"] for s in run.get("steps", [])]
    needed = ASSIST_STEPS if connector == "assisted" else LIVE_STEPS
    # Dev.to never deletes: its last step is CONFIRM_REMOVED (unpublished), not CONFIRM_DELETED
    has_steps = all(s in steps or (s == "CONFIRM_DELETED" and "CONFIRM_REMOVED" in steps) for s in needed)
    confirmation = any(f.endswith(("deletion_confirmation.json", "removal_confirmation.json")) for f in files)
    screenshots = [f for f in files if f.lower().endswith((".png", ".jpg", ".jpeg"))]
    p.check(f"{label}: pack exported with run.json, every step and the deletion/removal confirmation",
            bool(folder) and "run.json" in files and has_steps and confirmation,
            {"steps": steps, "files": len(files)})
    if connector == "devto":
        text = "\n".join((folder / f).read_text(encoding="utf-8", errors="replace") for f in files if f.endswith(".json")) if folder else ""
        delete = next((s for s in run.get("steps", []) if s["step"] == "DELETE"), {})
        response = (delete.get("detail") or {}).get("response") or {}
        p.check(f"{label}: Dev.to removal is unpublish, never deleted:true", response.get("deleted") is False
                and '"deleted": true' not in text, {"removal_mode": response.get("removal_mode")})
    return {"files": len(files), "screenshots": ";".join(screenshots), "steps": "|".join(steps)}


def main() -> int:
    p = Proof("MH2", "Six Core connectors certified (publish, confirm live URL, status, delete) with stored evidence",
              "scripts/prove_mh2.sh")
    mode = p.mode()
    targets: list[tuple[str, str, str]] = [(c, "mock", dest_url(c, tag(f"mh2-{c}-"))) for c in CORE]
    if mode == "local":
        targets += [(c, "real-local", url) for c, url in REAL_DEST.items()]
    else:
        p.add_partial("simulation mode: the real local Lemmy and Ghost were not certified in this run")
    rows: list[dict[str, Any]] = []
    no_screenshot: list[str] = []
    for connector, target, url in targets:
        label = f"{connector}@{target}"
        p.section(f"certify {label}: {url}")
        dest = p.create_dest(row_for(connector, url, f"MH2 {connector} {target} {tag()}" if target == "mock"
                                     else f"Local {connector} (real instance)"))
        run = p.certify(dest["destination_id"])
        p.log("steps: " + json.dumps([(s["step"], s["outcome"], s.get("duration_ms")) for s in run.get("steps", [])]))
        passed = run.get("state") == "PASSED" and run.get("level_after") == EXPECTED[connector]
        failed_steps = [{"step": s["step"], "detail": s.get("detail")} for s in run.get("steps", []) if s.get("outcome") != "ok"]
        p.check(f"{label}: certification PASSED at {EXPECTED[connector]}", passed,
                {"state": run.get("state"), "level": run.get("level_after"), "failure": run.get("failure_code"),
                 "failed_steps": failed_steps[:2]})
        folder = export(run["run_id"]) if run.get("state") in ("PASSED", "DEMOTED") else None
        p.check(f"{label}: evidence pack exported", folder is not None, str(folder.relative_to(ROOT)) if folder else "")
        pack = pack_check(p, connector, label, folder, run)
        if not pack["screenshots"]:
            no_screenshot.append(label)
        result = run.get("result") or {}
        rows.append({"connector": connector, "target": target, "destination_id": dest["destination_id"], "url": url,
                     "run_id": run["run_id"], "state": run.get("state"), "level_after": run.get("level_after"),
                     "expected_level": EXPECTED[connector], "delete_mode": result.get("delete_mode"),
                     "platform_delete_mode": result.get("platform_delete_mode"), "live_url": result.get("live_url"),
                     "duration_ms": run.get("duration_ms"), "steps": pack["steps"], "screenshots": pack["screenshots"],
                     "pack": str(folder.relative_to(ROOT)) if folder else "", "simulation": mode == "simulation"})
    if no_screenshot:
        p.add_partial("certification packs have no screenshot (brief: 'run record, live URL or API response, screenshot, "
                      f"deletion confirmation'); missing for: {', '.join(no_screenshot)}")
    fields = ["connector", "target", "destination_id", "url", "run_id", "state", "level_after", "expected_level", "delete_mode",
              "platform_delete_mode", "live_url", "duration_ms", "steps", "screenshots", "pack", "simulation"]
    p.write_csv("evidence/must_haves/MH2_certification_summary.csv", rows, fields)
    p.write_csv("evidence/metrics/certification_runs.csv",
                [{**r, "hardware": "see MH2_certify.log header", "runs": 1} for r in rows],
                ["connector", "target", "run_id", "state", "level_after", "duration_ms", "simulation", "hardware", "runs", "pack"])
    return p.finish("evidence/must_haves/MH2_certify.log")


if __name__ == "__main__":
    sys.exit(main())
