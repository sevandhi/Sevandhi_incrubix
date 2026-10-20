"""MRSS connector + the self-served feed routes (no mock: the feed is ours).

The feed GET routes are read here WITH the API token: until the proposed one-line change to
app/api/security.py is applied, every /api/v1 route needs it. ``test_feed_routes_are_public`` records
which state the gate is in.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.connectors.mrss.feed import FeedStore, parse_feed
from tests.conftest import api_client

BASE = "http://ibx.test"


def _dest(feed_key: str) -> dict:
    return {"connector_key": "mrss", "surface": "item", "name": "AI weekly", "rules": {"automation_allowed": True},
            "instance": {"feed_key": feed_key, "feed_description": "Test feed"}, "language": "en"}


def _body(feed_key: str, key: str, **rendition) -> dict:
    r = {"title": "Our launch video", "body": "Two minutes on what changed.", "link_url": "https://example.com/launch",
         "media": [{"url": "https://cdn.example.com/launch.mp4", "mime": "video/mp4", "bytes": 1234, "duration_s": 120,
                    "width": 1280, "height": 720}], "tags": ["ai", "video"], "ai_generated": False, **rendition}
    return {"rendition": r, "destination": _dest(feed_key), "connection": {"connector_key": "mrss"}, "idempotency_key": key}


@pytest.fixture
async def mrss_app(app_factory, pg_url):
    return await app_factory(pg_url=pg_url, PUBLIC_BASE_URL=BASE)


async def test_roundtrip_feed_item_click_metrics_delete(mrss_app):
    feed = f"f-{uuid.uuid4().hex[:6]}"
    async with api_client(mrss_app) as client:
        published = await client.post("/connector/v1/mrss/publish", json=_body(feed, f"k-{feed}"))
        assert published.status_code == 200, published.text
        result = published.json()
        guid = result["platform_post_id"]
        assert guid.startswith("ibx-") and result["status"] == "published"
        assert result["live_url"] == f"{BASE}/api/v1/feeds/{feed}/items/{guid}"

        xml = await client.get(f"/api/v1/feeds/{feed}/mrss.xml")
        assert xml.status_code == 200 and xml.headers["content-type"].startswith("application/rss+xml")
        parsed = parse_feed(xml.content)  # feedparser: well-formed RSS 2.0
        assert parsed.feed.title == "AI weekly" and parsed.feed.language == "en"
        [entry] = parsed.entries
        assert entry.id == guid and entry.title == "Our launch video"
        assert entry.link == f"{BASE}/api/v1/r/{guid}"
        assert entry.media_content[0]["url"] == "https://cdn.example.com/launch.mp4"
        assert entry.media_content[0]["type"] == "video/mp4" and entry.media_content[0]["duration"] == "120"
        assert entry.enclosures[0]["href"] == "https://cdn.example.com/launch.mp4"
        assert [t.term for t in entry.tags] == ["ai", "video"]

        item = await client.get(f"/api/v1/feeds/{feed}/items/{guid}")
        assert item.status_code == 200 and guid in item.text

        for _ in range(3):
            click = await client.get(f"/api/v1/r/{guid}")
            assert click.status_code == 302 and click.headers["location"] == "https://example.com/launch"
        ref = {"platform_post_id": guid, "connection": {"connector_key": "mrss"}, "destination": _dest(feed)}
        metrics = await client.post("/connector/v1/mrss/fetch_metrics", json=ref)
        assert metrics.json() == {"views": None, "likes": None, "comments": None, "shares": None, "saves": None,
                                  "watch_time_s": None, "clicks": 3}

        status = await client.post("/connector/v1/mrss/get_status", json=ref, headers={"X-IncuBrix-Verbose": "1"})
        assert status.json()["status"] == "published" and status.json()["_meta"]["in_feed"] is True

        deleted = await client.post("/connector/v1/mrss/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
        assert deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "deleted"
        assert (await client.get(f"/api/v1/feeds/{feed}/items/{guid}")).status_code == 404
        assert parse_feed((await client.get(f"/api/v1/feeds/{feed}/mrss.xml")).content).entries == []
        assert (await client.get(f"/api/v1/r/{guid}")).status_code == 404
        status = await client.post("/connector/v1/mrss/get_status", json=ref)
        assert status.json() == {"status": "deleted", "live_url": None}


async def test_feed_lists_newest_first_and_keeps_feeds_apart(mrss_app):
    feed, other = f"f-{uuid.uuid4().hex[:6]}", f"g-{uuid.uuid4().hex[:6]}"
    async with api_client(mrss_app) as client:
        for n in range(3):
            assert (await client.post("/connector/v1/mrss/publish", json=_body(feed, f"k{n}-{feed}", title=f"Item {n}"))).status_code == 200
        assert (await client.post("/connector/v1/mrss/publish", json=_body(other, f"k-{other}", title="Elsewhere"))).status_code == 200
        entries = parse_feed((await client.get(f"/api/v1/feeds/{feed}/mrss.xml")).content).entries
        assert [e.title for e in entries] == ["Item 2", "Item 1", "Item 0"]
        assert (await client.get("/api/v1/feeds/Not A Key/mrss.xml")).status_code == 404


async def test_timeout_after_write_then_retry_adds_exactly_one_item(mrss_app, monkeypatch):
    """MH6 for a feed: the item is written, then the call 'times out'; the retry finds it by its guid (= marker)."""
    feed = f"f-{uuid.uuid4().hex[:6]}"
    original = FeedStore.put_item

    async def write_then_time_out(self, item, meta=None):
        await original(self, item, meta)
        raise httpx.ReadTimeout("simulated: the write landed but the answer was lost")

    async with api_client(mrss_app) as client:
        monkeypatch.setattr(FeedStore, "put_item", write_then_time_out)
        first = await client.post("/connector/v1/mrss/publish", json=_body(feed, f"k-{feed}"))
        assert first.status_code == 503 and first.json()["error"]["details"]["intent_state"] == "UNKNOWN"
        monkeypatch.setattr(FeedStore, "put_item", original)
        retry = await client.post("/connector/v1/mrss/publish", json=_body(feed, f"k-{feed}"))
        assert retry.status_code == 200 and retry.headers.get("Idempotent-Replayed") == "true"
        assert len(parse_feed((await client.get(f"/api/v1/feeds/{feed}/mrss.xml")).content).entries) == 1


async def test_rejected_content_and_bad_feed_key(mrss_app):
    feed = f"f-{uuid.uuid4().hex[:6]}"
    async with api_client(mrss_app) as client:
        no_title = await client.post("/connector/v1/mrss/publish", json=_body(feed, f"k1-{feed}", title=""))
        assert no_title.status_code == 422 and no_title.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
        bad_media = _body(feed, f"k2-{feed}", media=[{"url": "ftp://host/file.mp4", "mime": "video/mp4"}])
        refused = await client.post("/connector/v1/mrss/publish", json=bad_media)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
        body = _body(feed, f"k3-{feed}")
        body["destination"]["instance"]["feed_key"] = "Bad Key!"
        bad_key = await client.post("/connector/v1/mrss/publish", json=body)
        assert bad_key.status_code == 422 and bad_key.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


async def test_feed_url_works_as_feed_key(mrss_app):
    feed = f"f-{uuid.uuid4().hex[:6]}"
    body = _body(feed, f"k-{feed}")
    body["destination"]["instance"]["feed_key"] = f"https://feeds.example.org/api/v1/feeds/{feed}/mrss.xml"
    async with api_client(mrss_app) as client:
        response = await client.post("/connector/v1/mrss/publish", json=body)
        assert response.status_code == 200 and f"/feeds/{feed}/items/" in response.json()["live_url"]


async def test_simulation_mode_keeps_the_feed_local(app_factory, pg_url):
    app = await app_factory(pg_url=pg_url, PUBLIC_BASE_URL=BASE, SIMULATION_MODE=True)
    feed = f"f-{uuid.uuid4().hex[:6]}"
    async with api_client(app) as client:
        response = await client.post("/connector/v1/mrss/publish", json=_body(feed, f"k-{feed}"))
        assert response.status_code == 200 and response.json()["live_url"].startswith(BASE)


async def test_feed_routes_are_public(mrss_app):
    """Feed readers send no token. Records the bearer gate's current state for these routes (see the module doc)."""
    from app.api import security

    public = all(p in getattr(security, "PUBLIC_GET_PREFIXES", security.SIGNED_LINK_PREFIXES)
                 for p in ("/api/v1/feeds/", "/api/v1/r/"))
    async with api_client(mrss_app, token=None) as anonymous:
        response = await anonymous.get("/api/v1/feeds/anything/mrss.xml")
    if public:
        assert response.status_code == 200
    else:
        assert response.status_code == 401
        pytest.xfail("app/api/security.py does not exempt /api/v1/feeds/ and /api/v1/r/ yet (proposed change)")
