"""OUT1: POST /api/v1/recommendations against Postgres — hard filters with reasons (excluded, promo, language,
cap with retry_at, format), past results from metrics_snapshots, content_id vs inline content, validation, the
optional LLM phrasing (mock) never changing the order."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.db.tables import ContentItemRow, DestinationRow, MetricsSnapshotRow, PublicationRow, RuleSetRow
from tests.conftest import api_client

VIDEO = {"title": "Turning one long video into ten Shorts", "body": "Our video editing workflow for YouTube creators.",
         "tags": ["video", "editing", "youtube"], "language": "en", "is_promotional": False, "ai_generated": False}


def row(name: str, **kw) -> DestinationRow:
    slug = f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
    base = dict(destination_key=f"lemmy|https://l.example/c/{slug}|post", connector_key="lemmy", name=name,
                url=f"https://l.example/c/{slug}", normalized_url=f"https://l.example/c/{slug}", platform_key="lemmy",
                surface="post", category="video", audience_tags=["video", "creators"], language="en", level="L3",
                instance={}, extra={})
    return DestinationRow(**{**base, **kw})


async def test_recommendations_filter_rank_and_explain(app_factory, pg_url):
    app = await app_factory(pg_url=pg_url)
    now = app.state.clock.now()
    good, busy, capped = row("Video Editing"), row("YouTube Creators"), row("Weekly Video Club")
    demoted = row("Old Videos", status="excluded", excluded_reason="certification failed: field renamed")
    nopromo = row("No Ads Videos", self_promo_allowed=False)
    german = row("Videos auf Deutsch", language="de")
    peertube = row("Tube Instance", connector_key="peertube", platform_key="peertube", surface="channel")
    baking = row("Bread Bakers", category="food", audience_tags=["baking"])
    async with app.state.session_factory() as s, s.begin():
        s.add_all([good, busy, capped, demoted, nopromo, german, peertube, baking])
        await s.flush()
        s.add(RuleSetRow(destination_id=capped.id, version=1, rules={"frequency_cap": "1/P7D"}, status="confirmed"))
        s.add(PublicationRow(destination_id=capped.id, idempotency_key=f"k-{uuid.uuid4().hex}", state="published",
                             published_at=now - timedelta(days=2)))
        for n, likes in enumerate((40, 35, 50)):
            s.add(MetricsSnapshotRow(connector_key="lemmy", destination_id=busy.id, platform_post_id=f"p{n}",
                                     metrics={"views": None, "likes": likes, "comments": 5, "shares": None, "clicks": None},
                                     captured_at=now))
        s.add(MetricsSnapshotRow(connector_key="lemmy", destination_id=good.id, platform_post_id="q1",
                                 metrics={"likes": 0, "comments": 0}, captured_at=now))
        item = ContentItemRow(**{k: v for k, v in VIDEO.items()}, extra={"content_type": "text"})
        s.add(item)
    async with api_client(app) as client:
        r = await client.post("/api/v1/recommendations", json={"content": VIDEO, "k": 5})
        assert r.status_code == 200, r.text
        body = r.json()
        names = [x["name"] for x in body["results"]]
        excluded = {x["name"]: x for x in body["excluded"]}
        assert excluded["Old Videos"]["code"] == "DESTINATION_EXCLUDED" and "field renamed" in excluded["Old Videos"]["reason"]
        assert excluded["Videos auf Deutsch"]["code"] == "DEST_RULE_LANGUAGE"
        assert excluded["Tube Instance"]["code"] == "FORMAT_NEEDS_VIDEO"
        cap = excluded["Weekly Video Club"]
        assert cap["code"] == "DEST_RULE_FREQUENCY_CAP"
        assert datetime.fromisoformat(cap["retry_at"].replace("Z", "+00:00")) > now
        assert "No Ads Videos" in names  # not promotional: allowed
        # past results: the busy destination outranks the zero-engagement one with the same text fit
        busy_r = next(x for x in body["results"] if x["name"] == "YouTube Creators")
        good_r = next(x for x in body["results"] if x["name"] == "Video Editing")
        assert busy_r["factors"]["past_results"] > 0.5 > good_r["factors"]["past_results"]
        assert busy_r["past_results"] == {"posts": 3, "engagement_sum": 140.0}
        assert names[-1] == "Bread Bakers" or "Bread Bakers" not in names
        for x in body["results"]:
            assert x["reason"] and x["factors"].keys() == x["contributions"].keys()
        assert body["reason_phrasing"]["used"] is False and body["candidates_considered"] == 8

        promo = await client.post("/api/v1/recommendations", json={"content": {**VIDEO, "is_promotional": True}, "k": 5})
        assert {x["name"]: x["code"] for x in promo.json()["excluded"]}["No Ads Videos"] == "DEST_RULE_SELF_PROMO"

        by_id = await client.post("/api/v1/recommendations", json={"content_id": item.id, "k": 5})
        assert by_id.status_code == 200 and by_id.json()["content_id"] == item.id
        assert [x["name"] for x in by_id.json()["results"]] == names

        phrased = await client.post("/api/v1/recommendations", json={"content": VIDEO, "k": 5, "phrase_with_llm": True})
        assert [x["name"] for x in phrased.json()["results"]] == names  # an LLM never re-ranks
        assert phrased.json()["reason_phrasing"].get("provider") == "mock"

        auto = await client.post("/api/v1/recommendations", json={"content": VIDEO, "automated_only": True})
        assert auto.status_code == 200

        bad = await client.post("/api/v1/recommendations", json={"content": VIDEO, "content_id": item.id})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
        bad_w = await client.post("/api/v1/recommendations", json={"content": VIDEO, "weights": {"popularity": 1}})
        assert bad_w.status_code == 422
        missing = await client.post("/api/v1/recommendations", json={"content_id": "content_nope"})
        assert missing.status_code == 404


async def test_recommendations_with_an_empty_catalogue(app_factory, pg_url):
    app = await app_factory(pg_url=pg_url)
    async with app.state.session_factory() as s, s.begin():
        from sqlalchemy import text

        await s.execute(text("DELETE FROM metrics_snapshots"))
    async with api_client(app) as client:
        r = await client.post("/api/v1/recommendations", json={"content": VIDEO})
        assert r.status_code == 200
        assert r.json()["excluded"] is not None and isinstance(r.json()["results"], list)
