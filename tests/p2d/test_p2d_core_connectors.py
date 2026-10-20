"""Gate: every Core connector passes the full certification run in simulation mode (MH2 / Core level).

Expected levels follow app/certification/levels.py: Discourse and Lemmy have metrics (L4), Ghost and the webhook
delete but have no metrics (L3), Dev.to can only unpublish (L2, never "deleted"), assisted builds a pack (L1).
"""

from __future__ import annotations

import pytest

CORE = [
    ("discourse", "https://forum-core.example.org", "L4", "soft_deleted"),
    ("lemmy", "https://lemmy-core.example.org/c/ibx_test", "L4", None),
    ("ghost", "https://blog-core.example.org", "L3", "deleted"),
    ("webhook", "https://partner-core.example.org/hooks/ibx", "L3", "deleted"),
    ("devto", "https://forem-core.example.org", "L2", None),
    ("assisted", "{mock}/assist/core/submit", "L1", "pack_revoked"),  # the simulation rewrite drops /submit
]


@pytest.mark.parametrize(("connector", "url", "level", "mode"), CORE, ids=[c[0] for c in CORE])
async def test_core_connector_certifies(p2d, connector, url, level, mode):
    if p2d.app.state.registry.manifest(connector) is None:
        pytest.skip(f"connector {connector} is not registered")
    extra = {}
    if connector == "webhook":  # simulation keeps credential names and swaps the values for sim-<name>
        conn = await p2d.client.post("/api/v1/connections", json={"connector_key": "webhook", "credentials": {"secret": "x" * 16}})
        extra["connection_id"] = conn.json()["connection_id"]
    dest = await p2d.add(name=f"Core {connector}", url=url.format(mock=p2d.mock), connector_key=connector, frequency_cap="1/P7D",
                         self_promo_allowed="false", automation_allowed="false" if connector == "assisted" else "true", **extra)
    run = await p2d.certify(dest["destination_id"])
    failed = [s for s in run["steps"] if s["outcome"] != "ok"]
    assert run["state"] == "PASSED", failed
    assert run["level_after"] == level, run["result"]
    if connector == "devto":  # the connector's own removal mode, and never reported as deleted
        assert run["result"]["delete_mode"] == "unpublish_to_draft"  # the author path a normal dev.to account has
        delete = next(s for s in run["steps"] if s["step"] == "DELETE")
        assert delete["detail"]["response"]["deleted"] is False
    elif connector == "lemmy":
        assert run["result"]["delete_mode"] in ("purged", "soft_deleted", "deleted")
    else:
        assert run["result"]["delete_mode"] == mode
    audit = (await p2d.client.get("/api/v1/test-posts")).json()
    assert audit["still_live"] == 0


async def test_lemmy_live_page_404_is_confirmed_through_the_api(p2d):
    """Measured: local Lemmy with federation off answers 404 at ap_id although the post exists."""
    import httpx

    if p2d.app.state.registry.manifest("lemmy") is None:
        pytest.skip("lemmy is not registered")
    httpx.post(f"{p2d.mock}/_faults", json={"platform": "lemmy", "tenant": "*", "method": "GET", "fault": "404",
                                             "path": "/post/", "count": -1}).raise_for_status()
    dest = await p2d.add(name="Lemmy no AP", url="https://lemmy-noap.example.org/c/ibx_test", connector_key="lemmy")
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED", [s for s in run["steps"] if s["outcome"] != "ok"]
    live = next(s for s in run["steps"] if s["step"] == "CONFIRM_LIVE")["detail"]
    assert live["method"] == "platform_api_get_status" and live["live_url_attempts"][0]["status"] == 404
    deleted = next(s for s in run["steps"] if s["step"] == "CONFIRM_DELETED")["detail"]
    assert deleted["platform_delete_mode"] in ("purged", "soft", "already_gone") and run["result"]["platform_delete_mode"]
