"""MH3 on the stack: ONE row with NO connection certifies a destination of EACH Core connector type
in simulation, with a real-looking URL and with a mock-platforms URL. No stored connection, no credentials."""

from __future__ import annotations

import pytest

LEVEL = {"discourse": "L4", "lemmy": "L4", "ghost": "L3", "webhook": "L3", "devto": "L2", "assisted": "L1"}
URLS = {
    "discourse": ("https://forum-{tag}.example.org", "http://mock-platforms:8080/discourse/one-{tag}"),
    "lemmy": ("https://lemmy-{tag}.example.org/c/ibx_test", "http://mock-platforms:8080/lemmy/one-{tag}/c/ibx_test"),
    "ghost": ("https://blog-{tag}.example.org", "http://mock-platforms:8080/ghost/one-{tag}"),
    "webhook": ("https://partner-{tag}.example.org/hooks", "http://mock-platforms:8080/partner/one-{tag}"),
    "devto": ("https://forem-{tag}.example.org", "http://mock-platforms:8080/forem/one-{tag}"),
    "assisted": ("https://news-{tag}.example.org/submit", "http://mock-platforms:8080/assist/one-{tag}/submit"),
}


@pytest.mark.parametrize("connector", list(URLS))
def test_one_row_without_connection_certifies(stack, connector):
    for form, url in zip(("real_url", "mock_url"), URLS[connector], strict=True):
        tag = f"{connector}-{form.split('_')[0]}-{stack.run_tag}"
        created = stack.call("POST", "/api/v1/destinations",
                             json={"name": f"One row {tag}", "url": url.format(tag=tag), "connector_key": connector})
        assert created.status_code == 201, created.text
        run = stack.certify(created.json()["destination_id"])
        assert run["state"] == "PASSED", [s for s in run["steps"] if s["outcome"] != "ok"]
        assert run["level_after"] == LEVEL[connector]
    stack.write_evidence(f"MH3_add_{connector}.log")
