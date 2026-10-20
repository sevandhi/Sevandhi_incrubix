"""OUT2 in process (simulation mode, mock-platforms + our own MRSS feed): fetch_metrics on five connectors, normalised,
stored with raw fields and a consistency check; unsupported metrics are null; the scheduled poll; network yield."""

from __future__ import annotations

import httpx
import pytest

from app.metrics import normalize as nm

RULES = {"frequency_cap": "5/P1D", "self_promo_allowed": "true", "link_policy": "allow", "disclosure_required": "false",
         "automation_allowed": "true", "language": "en", "category": "ai", "audience_tags": "ai"}
ROWS = {
    "discourse": {"url": "{mock}/discourse/p5a-disc"},
    "lemmy": {"url": "{mock}/lemmy/p5a-lemmy/c/ibx_test"},
    "slack": {"url": "https://ibx-test.slack.com/archives/C0P5AMETR", "instance_channel_id": "C0P5AMETR",
              "instance_api_base": "{mock}/slack/p5a-slack/api"},
    "peertube": {"url": "{mock}/peertube/p5a-pt"},
    "mrss": {"url": "https://feeds-p5a.example.org/api/v1/feeds/p5a-feed/mrss.xml", "instance_feed_key": "p5a-feed"},
}


async def publish(env, connector: str) -> dict:
    row = {k: v.format(mock=env.mock) for k, v in ROWS[connector].items()}
    dest = await env.add(name=f"Metrics {connector}", connector_key=connector, **RULES, **row)
    run = await env.certify(dest["destination_id"])
    assert run["state"] == "PASSED", [s for s in run["steps"] if s["outcome"] != "ok"]
    created = await env.client.post("/api/v1/publications", json={"destination_id": dest["destination_id"], "content": {
        "title": "What do you measure on long-tail communities?", "body": "We compare engagement across small communities.",
        "language": "en", "ai_generated": False, "is_promotional": False,
        **({"canonical_url": "https://example.com/p5a-article", "link_url": "https://example.com/p5a-article"}
           if connector == "mrss" else {})}})
    assert created.status_code == 202, created.text
    await env.drain()
    pub = (await env.client.get(f"/api/v1/publications/{created.json()['publication']['publication_id']}")).json()
    assert pub["state"] == "published", pub
    return pub


async def fetch(env, pub: dict) -> dict:
    response = await env.client.post("/api/v1/metrics/fetch", json={"publication_id": pub["publication_id"]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("connector", sorted(ROWS))
async def test_fetch_metrics_is_normalised_traceable_and_null_when_unsupported(p5a_factory, connector):
    # PUBLIC_BASE_URL like compose's (127.0.0.1:<port>): an own host, so the MRSS item URL is certifiable (as for the MRSS connector)
    p5a = await p5a_factory(PUBLIC_BASE_URL="http://127.0.0.1:9")
    pub = await publish(p5a, connector)
    post_id = pub["platform_post_id"]
    if connector == "slack":
        channel, ts = post_id.split(":", 1)
        for path, body in (("_mock/react", {"channel": channel, "ts": ts, "name": "tada", "count": 2}),
                           ("_mock/reply", {"channel": channel, "ts": ts})):
            httpx.post(f"{p5a.mock}/slack/p5a-slack/{path}", json=body).raise_for_status()
    elif connector == "peertube":
        httpx.post(f"{p5a.mock}/peertube/p5a-pt/_mock/engage", json={"id": post_id, "views": 3, "likes": 1,
                                                                     "comments": 2, "watch_time": 40}).raise_for_status()
    elif connector == "mrss":
        for _ in range(2):
            click = await p5a.client.get(f"/api/v1/r/{post_id}", headers={"Authorization": ""})
            assert click.status_code == 302
    snap = await fetch(p5a, pub)
    metrics = snap["metrics"]
    assert set(metrics) == set(nm.KEYS)
    assert snap["consistent"] is True, snap["raw"]  # the stored numbers equal the ones recomputed from raw fields
    assert snap["raw"]["fields"] and snap["source"] == ("own_service" if connector == "mrss" else "mock")
    for key in nm.KEYS:  # unsupported → null, never 0
        if key not in nm.supported(connector):
            assert metrics[key] is None, (connector, key)
        else:
            assert isinstance(metrics[key], int | float) or (connector == "discourse" and key == "clicks"), (connector, key)
    expected = {"slack": {"likes": 2, "comments": 1}, "peertube": {"views": 3, "likes": 1, "comments": 2, "watch_time_s": 40.0},
                "mrss": {"clicks": 2}}.get(connector, {})
    assert {k: metrics[k] for k in expected} == expected
    assert snap["raw"]["calls"] or connector == "mrss"
    listed = (await p5a.client.get("/api/v1/metrics", params={"publication_id": pub["publication_id"]})).json()
    assert listed["items"][0]["snapshot_id"] == snap["snapshot_id"]


async def test_scheduled_poll_and_network_yield(p5a):
    pub = await publish(p5a, "peertube")
    httpx.post(f"{p5a.mock}/peertube/p5a-pt/_mock/engage", json={"id": pub["platform_post_id"], "likes": 4}).raise_for_status()
    started = await p5a.client.post("/api/v1/metrics/poll", json={})
    assert started.status_code == 202
    await p5a.drain()
    job = await p5a.job(started.json()["job_id"])
    assert job["status"] == "succeeded" and job["result"]["polled"] == 1 and job["result"]["ok"] == 1
    health = (await p5a.client.get("/api/v1/network/health")).json()
    [row] = [d for d in health["destinations"] if d["connector_key"] == "peertube"]
    assert row["yield"]["posts_measured"] == 1 and row["yield"]["totals"]["likes"] == 4
    assert row["yield"]["totals"]["shares"] is None and row["metrics_supported"] == nm.supported("peertube")


async def test_fetch_errors_are_enveloped(p5a):
    bad = await p5a.client.post("/api/v1/metrics/fetch", json={})
    assert bad.status_code == 422
    missing = await p5a.client.post("/api/v1/metrics/fetch", json={"publication_id": "pub_nope"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"
    mapping = (await p5a.client.get("/api/v1/metrics/mapping")).json()
    assert mapping["connectors"]["lemmy"]["views"]["field"] is None and "ghost" in mapping["no_metrics_capability"]
