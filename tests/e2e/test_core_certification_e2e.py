"""MH2 / Core level on the stack (simulation mode): each Core connector passes the full certification run."""

from __future__ import annotations

import pytest

CORE = ["discourse", "lemmy", "ghost", "webhook", "devto", "assisted"]
EXPECTED = {"discourse": {"L4"}, "lemmy": {"L4"}, "ghost": {"L3"}, "webhook": {"L3"}, "devto": {"L2"}, "assisted": {"L1"}}


@pytest.mark.parametrize("connector", CORE)
def test_core_connector_certifies_on_the_stack(stack, connector):
    tag = f"core-{connector}-{stack.run_tag}"
    row = {"name": f"Core {connector} {stack.run_tag}", "connector_key": connector, "frequency_cap": "1/P7D",
           "self_promo_allowed": "false", "automation_allowed": "false" if connector == "assisted" else "true",
           "url": {"lemmy": f"https://{tag}.example.org/c/ibx_test",
                   "assisted": f"http://mock-platforms:8080/assist/{tag}/submit"}.get(connector, f"https://{tag}.example.org")}
    if connector == "webhook":
        conn = stack.call("POST", "/api/v1/connections", json={"connector_key": "webhook", "credentials": {"secret": "sim-secret"}})
        row["connection_id"] = conn.json()["connection_id"]
    dest = stack.call("POST", "/api/v1/destinations", json=row)
    assert dest.status_code == 201, dest.text
    run = stack.certify(dest.json()["destination_id"])
    assert run["state"] == "PASSED", [s for s in run["steps"] if s["outcome"] != "ok"]
    assert run["level_after"] in EXPECTED[connector]
    if connector == "devto":  # the author path (no admin rights), never reported as deleted
        delete = next(s for s in run["steps"] if s["step"] == "DELETE")["detail"]
        assert delete["response"]["deleted"] is False and run["result"]["delete_mode"] == "unpublish_to_draft"
        assert run["steps"][-1]["step"] == "CONFIRM_REMOVED"
    stack.write_evidence(f"MH2_certify_{connector}.log")
