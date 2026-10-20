"""Must-have 3: ONE catalogue row, with NO connection and no extra settings, is enough to certify a
destination of EACH Core connector type in simulation, whether the row's URL looks real or already points at
our mock-platforms. No workaround: no stored connection, no credentials, no instance overrides."""

from __future__ import annotations

import pytest

LEVEL = {"discourse": "L4", "lemmy": "L4", "ghost": "L3", "webhook": "L3", "devto": "L2", "assisted": "L1"}
URLS = {
    "discourse": ("https://forum-one.example.org", "{mock}/discourse/one-row"),
    "lemmy": ("https://lemmy-one.example.org/c/ibx_test", "{mock}/lemmy/one-row/c/ibx_test"),
    "ghost": ("https://blog-one.example.org", "{mock}/ghost/one-row"),
    "webhook": ("https://partner-one.example.org/hooks", "{mock}/partner/one-row"),
    "devto": ("https://forem-one.example.org", "{mock}/forem/one-row"),
    "assisted": ("https://news-one.example.org/submit", "{mock}/assist/one-row/submit"),
}
CASES = [(key, form, URLS[key][i]) for key in URLS for i, form in enumerate(("real_url", "mock_url"))]


@pytest.mark.parametrize(("connector", "form", "url"), CASES, ids=[f"{c}-{f}" for c, f, _ in CASES])
async def test_one_row_without_connection_certifies(p2d, connector, form, url):
    row = {"name": f"One row {connector} {form}", "url": url.format(mock=p2d.mock), "connector_key": connector}
    dest = await p2d.add(**row)
    run = await p2d.certify(dest["destination_id"])
    failed = [(s["step"], s.get("detail")) for s in run["steps"] if s["outcome"] != "ok"]
    assert run["state"] == "PASSED", failed
    assert run["level_after"] == LEVEL[connector]
    assert (await p2d.client.get("/api/v1/test-posts")).json()["still_live"] == 0
