"""MH3 proof: adding another forum, community or site of a supported type is ONE catalogue row (no code, no connection).

    scripts/prove_mh3.sh

For each of the six Core connector types: one new JSON row with only name + url + connector_key (no connection, no
credentials) for a brand-new instance, then certification → PASSED. Also: "forum 41" as one CSV line (text/csv), and
(local mode) a second community on the REAL local Lemmy (``ibx_test_2``) as one row. The connector registry is the same
before and after (nothing was added but rows). In simulation mode the real-looking URLs of
tests/e2e/test_one_row_each_connector_e2e.py are also used (prove_mh7.sh runs that part).
Evidence: evidence/must_haves/MH3_add_instance.log, evidence/must_haves/MH3_add_instance.csv
"""

from __future__ import annotations

import sys
from typing import Any

from prove_lib import CORE, Proof, dest_url, tag

LEVEL = {"discourse": "L4", "lemmy": "L4", "ghost": "L3", "webhook": "L3", "devto": "L2", "assisted": "L1"}


def main() -> int:
    p = Proof("MH3", "Configuration, not code: a new instance is one catalogue row", "scripts/prove_mh3.sh")
    mode = p.mode()
    before = p.http("GET", "/api/v1/connectors", quiet=True).json()
    rows: list[dict[str, Any]] = []

    def certify_row(connector: str, how: str, created: dict[str, Any], row_text: str) -> None:
        run = p.certify(created["destination_id"])
        ok = run.get("state") == "PASSED" and run.get("level_after") == LEVEL[connector]
        bad = [{"step": s["step"], "detail": s.get("detail")} for s in run.get("steps", []) if s.get("outcome") != "ok"]
        p.check(f"{connector} via {how}: one row -> certified {LEVEL[connector]}", ok,
                {"state": run.get("state"), "level": run.get("level_after"), "failed": bad[:1]})
        rows.append({"connector": connector, "how": how, "row": row_text, "destination_id": created["destination_id"],
                     "connection_used": created.get("connection_id") or "", "run_id": run.get("run_id"),
                     "state": run.get("state"), "level_after": run.get("level_after"), "duration_ms": run.get("duration_ms")})

    p.section("one JSON row per connector type, no connection")
    for connector in CORE:
        row = {"name": f"New {connector} instance {tag()}", "url": dest_url(connector, tag(f"mh3-{connector}-")),
               "connector_key": connector}
        created = p.create_dest(row)
        p.check(f"{connector}: the row created a destination at L0 with no connection", created.get("created") is True
                and created.get("level") == "L0", {"level": created.get("level"), "connection": created.get("connection_id")})
        certify_row(connector, "json-row", created, str(row))

    p.section("forum 41 as one CSV line (text/csv)")
    tenant = tag("forum-41-")
    csv_text = ("name,url,connector_key,surface,language,frequency_cap,self_promo_allowed,automation_allowed\n"
                f"Forum 41 {tenant},http://mock-platforms:8080/discourse/{tenant},discourse,topic,en,1/P7D,false,true\n")
    response = p.http("POST", "/api/v1/destinations", content=csv_text.encode(), headers={"Content-Type": "text/csv"})
    ok = response.status_code == 201 and response.json()["summary"]["created"] == 1
    p.check("CSV with one data line -> exactly one destination created", ok, response.json().get("summary"))
    if ok:
        certify_row("discourse", "csv-line", response.json()["results"][0]["destination"] | {"destination_id": response.json()["results"][0]["destination_id"]}, csv_text.strip().splitlines()[1])

    if mode == "local":
        p.section("a second community on the REAL local Lemmy as one row")
        row = {"name": f"Local Lemmy second community {tag()}", "url": "http://lemmy:8536/c/ibx_test_2", "connector_key": "lemmy"}
        certify_row("lemmy", "json-row real-local ibx_test_2", p.create_dest(row), str(row))
    else:
        p.add_partial("simulation mode: no row for the real local Lemmy in this run")

    after = p.http("GET", "/api/v1/connectors", quiet=True).json()
    p.check("the connector registry is unchanged (rows only, no code)", before == after,
            {"before": [c["key"] for c in before["connectors"]], "after": [c["key"] for c in after["connectors"]]})
    p.write_csv("evidence/must_haves/MH3_add_instance.csv", rows,
                ["connector", "how", "row", "destination_id", "connection_used", "run_id", "state", "level_after", "duration_ms"])
    return p.finish("evidence/must_haves/MH3_add_instance.log")


if __name__ == "__main__":
    sys.exit(main())
