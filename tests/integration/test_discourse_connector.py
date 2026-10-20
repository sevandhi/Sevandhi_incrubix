"""Discourse connector end to end: contract HTTP -> BaseConnector -> mock Discourse (real sockets, real Postgres).

Covers the round trip with a soft delete, many forums on one connector, timeout_after_create + retry
(no duplicate topic), 403 invalid_access, 422, native 429, moderation queue, metrics and schema drift.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

KEY = "discourse"
CONNECTION = {"connector_key": KEY, "auth_type": "api_key", "credentials": {"api_key": "dev-key-1", "api_username": "ibx_admin"}}
BODY = "A useful question for the community about release planning."


def dest(mock: str, tenant: str, **instance: Any) -> dict[str, Any]:
    return {"connector_key": KEY, "surface": "topic", "instance": {"base_url": f"{mock}/{KEY}/{tenant}", **instance},
            "rules": {"automation_allowed": True}}


def publish_body(mock: str, tenant: str, key: str, title: str | None = None, connection: dict | None = None) -> dict[str, Any]:
    return {
        "rendition": {"title": title or f"How do you plan releases {uuid.uuid4().hex[:6]}?", "body": BODY, "ai_generated": False},
        "destination": dest(mock, tenant),
        "connection": connection or CONNECTION,
        "idempotency_key": key,
    }


def ref(mock: str, tenant: str, post_id: str) -> dict[str, Any]:
    return {"platform_post_id": post_id, "connection": CONNECTION, "destination": dest(mock, tenant)}


def creates(mock: str, tenant: str) -> int:
    counts = httpx.get(f"{mock}/_counts", params={"platform": KEY, "tenant": tenant}).json()["counts"]
    return counts.get(KEY, {}).get(tenant, {}).get("POST /posts.json", 0)


ADMIN = {"Api-Key": "dev-key-1", "Api-Username": "ibx_admin"}


async def test_round_trip_publish_status_metrics_soft_delete(contract_client, mock_platforms_url):
    c, mock = contract_client, mock_platforms_url
    published = await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forum-1", "d-rt-1"))
    assert published.status_code == 200, published.text
    out = published.json()
    assert set(out) == {"platform_post_id", "live_url", "status"} and out["status"] == "published"
    page = httpx.get(out["live_url"])
    assert page.status_code == 200 and "How do you plan releases" in page.text

    status = await c.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "forum-1", out["platform_post_id"]))
    assert status.json() == {"status": "published", "live_url": out["live_url"]}

    deleted = await c.post(f"/connector/v1/{KEY}/delete", json=ref(mock, "forum-1", out["platform_post_id"]),
                           headers={"X-IncuBrix-Verbose": "1"})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "soft"
    assert httpx.get(out["live_url"]).status_code == 404  # public: gone
    admin = httpx.get(f"{mock}/{KEY}/forum-1/t/{out['platform_post_id']}.json", headers=ADMIN)
    assert admin.status_code == 200 and admin.json()["deleted_at"]  # staff: soft-deleted, still there
    status = await c.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "forum-1", out["platform_post_id"]))
    assert status.json() == {"status": "deleted", "live_url": None}
    again = await c.post(f"/connector/v1/{KEY}/delete", json=ref(mock, "forum-1", out["platform_post_id"]))
    assert again.status_code == 200 and again.json() == {"deleted": True}  # idempotent delete


async def test_metrics_views_likes_replies_clicks(contract_client, mock_platforms_url):
    c, mock = contract_client, mock_platforms_url
    body = publish_body(mock, "forum-m", "d-metrics")
    body["rendition"]["body"] = BODY + " See https://example.com/notes for details."
    out = (await c.post(f"/connector/v1/{KEY}/publish", json=body)).json()
    topic_id = int(out["platform_post_id"])
    base = f"{mock}/{KEY}/forum-m"
    topic = httpx.get(f"{base}/t/{topic_id}.json", headers=ADMIN).json()
    first_post = topic["post_stream"]["posts"][0]["id"]
    httpx.get(out["live_url"])
    httpx.get(out["live_url"])
    httpx.post(f"{base}/post_actions.json", json={"id": first_post, "post_action_type_id": 2}, headers=ADMIN)
    httpx.post(f"{base}/posts.json", json={"topic_id": topic_id, "raw": "A reply with enough characters in it."}, headers=ADMIN)
    httpx.get(f"{base}/clicks/track", params={"url": "https://example.com/notes", "post_id": first_post, "topic_id": topic_id})
    metrics = await c.post(f"/connector/v1/{KEY}/fetch_metrics", json=ref(mock, "forum-m", out["platform_post_id"]))
    assert metrics.json() == {"views": 2, "likes": 1, "comments": 1, "clicks": 1, "shares": None, "saves": None, "watch_time_s": None}


async def test_one_connector_many_forums(contract_client, mock_platforms_url):
    c, mock = contract_client, mock_platforms_url
    ids = []
    for tenant in ("forum-a", "forum-41"):
        r = await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, tenant, f"d-many-{tenant}"))
        assert r.status_code == 200, r.text
        assert f"/{KEY}/{tenant}/t/" in r.json()["live_url"]
        ids.append(r.json()["platform_post_id"])
    # separate forums, separate id spaces: both platforms answered topic 1. The second id we return is
    # tagged with its instance, so Appendix A's get_status(platform_post_id, connection) can tell them apart (MH1).
    from app.connectors.base import platform_id

    assert [platform_id(i) for i in ids] == ["1", "1"] and ids[0] == "1" and ids[1] != ids[0]


async def test_timeout_after_create_then_retry_reconciles(contract_client, mock_platforms_url):
    c, mock = contract_client, mock_platforms_url
    body = publish_body(mock, "forum-t", "d-timeout")
    first = await c.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-Sim-Fault": "timeout_after_create:5"})
    assert first.status_code == 503, first.text
    assert first.json()["error"]["details"]["intent_state"] == "UNKNOWN"
    retry = await c.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["_meta"]["reconciled"] is True and retry.json()["platform_post_id"] == "1"
    assert creates(mock, "forum-t") == 1  # one topic on the platform, never two


async def test_invalid_key_is_auth_expired(contract_client, mock_platforms_url):
    bad = {**CONNECTION, "credentials": {"api_key": "revoked-key", "api_username": "ibx_admin"}}
    r = await contract_client.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock_platforms_url, "forum-auth", "d-auth", connection=bad))
    assert r.status_code == 424
    error = r.json()["error"]
    assert error["code"] == "CONNECTION_AUTH_EXPIRED" and error["retryable"] is False
    assert error["details"]["platform_status"] == 403 and error["details"]["error_map_key"] == "403:invalid_access"
    assert "revoked-key" not in r.text


async def test_spec_and_platform_rejections(contract_client, mock_platforms_url):
    c, mock = contract_client, mock_platforms_url
    short = publish_body(mock, "forum-422", "d-short", title="Too short")
    r = await c.post(f"/connector/v1/{KEY}/publish", json=short)
    assert r.status_code == 422 and r.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
    assert creates(mock, "forum-422") == 0  # caught before any platform call
    title = "A duplicate title for this forum here"
    assert (await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forum-422", "d-dup-1", title=title))).status_code == 200
    dup = await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forum-422", "d-dup-2", title=title))
    assert dup.status_code == 422
    error = dup.json()["error"]
    assert error["code"] == "PLATFORM_CONTENT_REJECTED" and "already been used" in error["details"]["platform_message"]
    replay = await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forum-422", "d-dup-2", title=title))
    assert replay.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"  # FAILED intent: same error replayed
    assert creates(mock, "forum-422") == 2


async def test_native_rate_limit_429_with_wait_seconds(contract_client, mock_platforms_url):
    c, mock = contract_client, mock_platforms_url
    setting = httpx.put(f"{mock}/{KEY}/forum-rl/admin/site_settings/rate_limit_create_topic.json",
                        json={"rate_limit_create_topic": 60}, headers=ADMIN)
    assert setting.status_code == 200
    assert (await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forum-rl", "d-rl-1"))).status_code == 200
    limited = await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forum-rl", "d-rl-2"))
    assert limited.status_code == 429
    details = limited.json()["error"]["details"]
    assert limited.json()["error"]["code"] == "PLATFORM_RATE_LIMIT"
    assert 55 <= details["retry_after_s"] <= 61 and details["reset_source"] == "Retry-After"
    hits = creates(mock, "forum-rl")
    blocked = await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forum-rl", "d-rl-3"))
    assert blocked.status_code == 429 and blocked.json()["error"]["details"]["short_circuit"] is True
    assert creates(mock, "forum-rl") == hits


async def test_moderation_queue_pending_then_approved(contract_client, mock_platforms_url):
    c, mock = contract_client, mock_platforms_url
    base = f"{mock}/{KEY}/forum-q"
    httpx.put(f"{base}/admin/site_settings/review_every_post.json", json={"review_every_post": True}, headers=ADMIN)
    r = await c.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forum-q", "d-queue"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending" and r.json()["live_url"] is None
    post_id = r.json()["platform_post_id"]
    assert post_id.startswith("pending:ibx-")
    status = await c.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "forum-q", post_id))
    assert status.json() == {"status": "pending", "live_url": None}
    reviewable = httpx.get(f"{base}/review.json", headers=ADMIN).json()["reviewables"][0]
    httpx.put(f"{base}/review/{reviewable['id']}/perform/approve_post.json", headers=ADMIN).raise_for_status()
    status = await c.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "forum-q", post_id))
    assert status.json()["status"] == "published" and "/t/" in status.json()["live_url"]
    deleted = await c.post(f"/connector/v1/{KEY}/delete", json=ref(mock, "forum-q", post_id))
    assert deleted.json() == {"deleted": True}


@pytest.mark.parametrize("fault", ["rename_field:topic_id->tid", "500"])
async def test_schema_drift_and_5xx_are_unavailable(contract_client, mock_platforms_url, fault):
    tenant = "forum-drift" if fault.startswith("rename") else "forum-500"
    r = await contract_client.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock_platforms_url, tenant, f"d-{tenant}"),
                                   headers={"X-Sim-Fault": fault})
    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "PLATFORM_UNAVAILABLE" and error["retryable"] is True
    if fault.startswith("rename"):
        assert error["details"]["reason"] == "response_schema_mismatch" and error["details"]["intent_state"] == "UNKNOWN"
