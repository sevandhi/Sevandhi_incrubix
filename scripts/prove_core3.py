"""CORE3 proof: errors from each platform map to the shared codes: expired token, rate limit, rejected content.

    scripts/prove_core3.sh

6 connectors x 3 situations = 18 cells, each produced by an actual call in this run (see scripts/prove_scenarios.py):
the REAL local Lemmy and Ghost where the situation can be produced on them (local mode), mock-platforms (faithful to
the recorded fixtures / platform docs) otherwise. Extra rows (primary=false) repeat Lemmy/Ghost on their mocks.
Assisted holds no token and calls no platform API, so two of its cells are N/A with the reason; its rejected-content
cell is the person's report through the assist-task completion.
Evidence: evidence/levels/core/CORE3_error_matrix.csv, evidence/levels/core/CORE3_error_matrix.log
"""

from __future__ import annotations

import sys
from typing import Any

from prove_lib import Proof
from prove_scenarios import assisted_rejected, auth_expired, rate_limit, rejected

FIELDS = ["connector", "scenario", "primary", "target", "trigger", "http_status", "error_code", "retryable", "platform_status",
          "platform_message", "retry_after_s", "reset_source", "retry_after_header", "next_call", "connection_paused",
          "platform_creates", "message", "expected", "status", "reason"]
NA = {
    ("assisted", "expired_token"): "auth: none. A person posts with their own account; the service holds no token, so no "
                                   "token can expire (the pack's signed links have their own expiry, checked in certification).",
    ("assisted", "rate_limit"): "No platform API is called; a person posts. Platform rate limits are met by the person "
                                "(the destination's frequency cap still applies before a pack is built).",
}


def main() -> int:
    p = Proof("CORE3", "Platform errors mapped to the shared codes (expired token, rate limit, rejected content)",
              "scripts/prove_core3.sh")
    mode = p.mode()
    rows: list[dict[str, Any]] = []

    def run(fn: Any, connector: str, target: str, primary: bool) -> None:
        p.section(f"{connector} / {fn.__name__} / {target}")
        try:
            row = fn(p, connector, target) if fn is not assisted_rejected else fn(p)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed cell, recorded
            row = {"connector": connector, "target": target, "scenario": fn.__name__, "status": "FAIL",
                   "message": f"{type(exc).__name__}: {exc}"[:300]}
        row["primary"] = primary
        row["scenario"] = {"auth_expired": "expired_token", "rate_limit": "rate_limit"}.get(fn.__name__, row.get("scenario"))
        rows.append(row)
        p.check(f"{connector} {row['scenario']} ({target}) -> {row.get('expected')}", row["status"] == "PASS",
                {k: row.get(k) for k in ("http_status", "error_code", "trigger", "next_call", "message")}, critical=primary)

    real = mode == "local"
    for connector in ["discourse", "lemmy", "devto", "ghost", "webhook"]:
        on_real = real and connector in ("lemmy", "ghost")
        run(auth_expired, connector, "real-local" if on_real else "mock", True)
        run(rate_limit, connector, "real-local" if on_real and connector == "lemmy" else "mock", True)
        run(rejected, connector, "real-local" if on_real else "mock", True)
        if on_real:  # the mock variants too (secondary rows)
            run(auth_expired, connector, "mock", False)
            if connector == "lemmy":
                run(rate_limit, connector, "mock", False)
            run(rejected, connector, "mock", False)
    if not real:
        p.add_partial("simulation mode: the Lemmy/Ghost cells came from their mocks, not the real local instances")
    for scenario in ("expired_token", "rate_limit"):
        rows.append({"connector": "assisted", "scenario": scenario, "primary": True, "target": "-", "status": "N/A",
                     "reason": NA[("assisted", scenario)]})
        p.log(f"N/A  assisted {scenario}: {NA[('assisted', scenario)]}")
    run(assisted_rejected, "assisted", "mock", True)
    order = {"discourse": 0, "lemmy": 1, "devto": 2, "ghost": 3, "webhook": 4, "assisted": 5}
    scen = {"expired_token": 0, "rate_limit": 1, "rejected_content": 2}
    rows.sort(key=lambda r: (not r.get("primary"), order.get(r["connector"], 9), scen.get(r.get("scenario"), 9)))
    p.write_csv("evidence/levels/core/CORE3_error_matrix.csv", rows, FIELDS)
    primary = [r for r in rows if r.get("primary")]
    p.log(f"primary cells: {len(primary)}; PASS {sum(r['status'] == 'PASS' for r in primary)}, "
          f"N/A {sum(r['status'] == 'N/A' for r in primary)}, FAIL {sum(r['status'] == 'FAIL' for r in primary)}")
    p.check("18 primary cells (6 connectors x 3 situations)", len(primary) == 18, len(primary))
    return p.finish("evidence/levels/core/CORE3_error_matrix.log")


if __name__ == "__main__":
    sys.exit(main())
