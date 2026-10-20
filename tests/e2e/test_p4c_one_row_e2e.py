"""ADV5 on the stack: ONE row with NO connection certifies a Slack, PeerTube and MRSS destination in
simulation. Writes ``P4C_add_<type>.log`` to IBX_E2E_EVIDENCE_DIR.

MRSS and PeerTube need the harness to confirm live/deleted through the platform API (``live_check_by_api``). While the harness does not read that attribute, their runs are expected to stop at CONFIRM_LIVE /
CONFIRM_DELETED; the evidence is still written and the test is reported as xfail with the reason.
"""

from __future__ import annotations

import inspect

import pytest

from app.certification import harness

ROWS = {
    "slack": {"url": "https://ibx-{tag}.slack.com/archives/C0E2E{up}", "instance_channel_id": "C0E2E{up}"},
    "peertube": {"url": "https://videos-{tag}.example.org"},
    "mrss": {"url": "https://feeds.example.org/api/v1/feeds/e2e-{tag}/mrss.xml", "instance_feed_key": "e2e-{tag}"},
}
LEVEL = {"slack": "L4", "peertube": "L4", "mrss": "L2"}


@pytest.mark.parametrize("connector", list(ROWS))
def test_one_row_certifies_new_connector(stack, connector):
    tag = f"{connector}-{stack.run_tag}"
    row = {k: v.format(tag=tag, up=stack.run_tag.upper()) for k, v in ROWS[connector].items()}
    created = stack.call("POST", "/api/v1/destinations", json={"name": f"One row {tag}", "connector_key": connector, **row})
    assert created.status_code == 201, created.text
    run = stack.certify(created.json()["destination_id"])
    stack.write_evidence(f"P4C_add_{connector}.log")
    failed = [s for s in run["steps"] if s["outcome"] != "ok"]
    needs_api_check = connector in ("mrss", "peertube") and "live_check_by_api" not in inspect.getsource(harness.Harness)
    if needs_api_check and run["state"] != "PASSED":
        assert failed and failed[0]["step"] in ("CONFIRM_LIVE", "CONFIRM_DELETED"), failed
        pytest.xfail(f"{connector}: the harness does not read live_check_by_api yet (proposed harness change); stopped at {failed[0]['step']}")
    assert run["state"] == "PASSED", failed
    assert run["level_after"] == LEVEL[connector]
