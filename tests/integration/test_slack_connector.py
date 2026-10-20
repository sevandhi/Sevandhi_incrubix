"""Slack connector through the HTTP contract against the Slack mock (mocks/platforms/routes/slack.py).

Covers the round trip, metadata-marker reconciliation after a timeout (exactly one message), and the error
classes that Slack returns as HTTP 200 ``ok:false`` (red_team #5) next to the real HTTP 429.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

TOKEN = "xoxb-test-0001-ibx"
CHANNEL = "C0IBXTEST1"


def _conn(token: str = TOKEN) -> dict:
    return {"connector_key": "slack", "auth_type": "bot_token", "credentials": {"bot_token": token}}


def _dest(mock: str, tenant: str, **spec_overrides) -> dict:
    dest = {"connector_key": "slack", "surface": "message", "rules": {"automation_allowed": True},
            "instance": {"channel_id": CHANNEL, "api_base": f"{mock}/slack/{tenant}/api"}}
    if spec_overrides:
        dest["spec_overrides"] = spec_overrides
    return dest


def _publish_body(mock: str, tenant: str, key: str, **overrides) -> dict:
    body = {"rendition": {"title": "Launch week", "body": "We shipped *v2* today.", "link_url": "https://example.com/v2",
                          "ai_generated": False},
            "destination": _dest(mock, tenant), "connection": _conn(), "idempotency_key": key}
    body.update(overrides)
    return body


def _messages(mock: str, tenant: str) -> list[dict]:
    response = httpx.get(f"{mock}/slack/{tenant}/api/conversations.history", params={"channel": CHANNEL, "include_all_metadata": "true"},
                         headers={"Authorization": f"Bearer {TOKEN}"})
    return response.json()["messages"]


async def test_roundtrip_publish_status_metrics_delete(contract_client, mock_platforms_url):
    tenant = f"s-{uuid.uuid4().hex[:6]}"
    published = await contract_client.post("/connector/v1/slack/publish", json=_publish_body(mock_platforms_url, tenant, f"k-{tenant}"),
                                           headers={"X-IncuBrix-Verbose": "1"})
    assert published.status_code == 200, published.text
    result = published.json()
    post_id = result["platform_post_id"]
    assert post_id.startswith(f"{CHANNEL}:") and result["status"] == "published"
    assert result["live_url"].startswith(f"{mock_platforms_url}/slack/{tenant}/archives/{CHANNEL}/p")
    [message] = _messages(mock_platforms_url, tenant)
    assert message["text"] == "*Launch week*\nWe shipped *v2* today.\nhttps://example.com/v2"
    assert message["metadata"] == {"event_type": "ibx_publication", "event_payload": {"marker": result["_meta"]["marker"]}}
    assert httpx.get(result["live_url"]).status_code == 200

    ref = {"platform_post_id": post_id, "connection": _conn(), "destination": _dest(mock_platforms_url, tenant)}
    status = await contract_client.post("/connector/v1/slack/get_status", json=ref)
    assert status.json() == {"status": "published", "live_url": result["live_url"]}

    ts = post_id.split(":", 1)[1]
    httpx.post(f"{mock_platforms_url}/slack/{tenant}/_mock/react", json={"channel": CHANNEL, "ts": ts, "name": "tada", "count": 3})
    httpx.post(f"{mock_platforms_url}/slack/{tenant}/_mock/react", json={"channel": CHANNEL, "ts": ts, "name": "eyes", "count": 1})
    httpx.post(f"{mock_platforms_url}/slack/{tenant}/_mock/reply", json={"channel": CHANNEL, "ts": ts})
    metrics = await contract_client.post("/connector/v1/slack/fetch_metrics", json=ref)
    assert metrics.json() == {"views": None, "likes": 4, "comments": 1, "shares": None, "saves": None,
                              "watch_time_s": None, "clicks": None}

    deleted = await contract_client.post("/connector/v1/slack/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
    assert deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "deleted"
    assert (await contract_client.post("/connector/v1/slack/get_status", json=ref)).json() == {"status": "deleted", "live_url": None}
    assert httpx.get(result["live_url"]).status_code == 404
    again = await contract_client.post("/connector/v1/slack/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
    assert again.json()["_meta"]["delete_mode"] == "already_gone"


async def test_timeout_after_create_then_retry_posts_exactly_once(contract_client, mock_platforms_url):
    tenant = f"s-{uuid.uuid4().hex[:6]}"
    body = _publish_body(mock_platforms_url, tenant, f"k-{tenant}")
    first = await contract_client.post("/connector/v1/slack/publish", json=body, headers={"X-Sim-Fault": "timeout_after_create:4"})
    assert first.status_code == 503 and first.json()["error"]["details"]["intent_state"] == "UNKNOWN"
    retry = await contract_client.post("/connector/v1/slack/publish", json=body)
    assert retry.status_code == 200, retry.text
    assert retry.headers.get("Idempotent-Replayed") == "true"  # found by the metadata marker, not posted again
    assert len(_messages(mock_platforms_url, tenant)) == 1
    counts = httpx.get(f"{mock_platforms_url}/_counts", params={"platform": "slack", "tenant": tenant}).json()["counts"]
    assert counts["slack"][tenant]["POST /api/chat.postMessage"] == 1


async def test_http_200_ok_false_token_revoked_is_auth_expired(contract_client, mock_platforms_url):
    tenant = f"s-{uuid.uuid4().hex[:6]}"
    httpx.post(f"{mock_platforms_url}/slack/{tenant}/_mock/revoke", json={"token": TOKEN})
    response = await contract_client.post("/connector/v1/slack/publish", json=_publish_body(mock_platforms_url, tenant, f"k-{tenant}"))
    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == "CONNECTION_AUTH_EXPIRED"
    assert error["details"]["platform_status"] == 200 and error["details"]["error_map_key"] == "200:token_revoked"


@pytest.mark.parametrize(("token", "key"), [("xoxb-expired-1", "200:token_expired"), ("not-a-slack-token", "200:invalid_auth")])
async def test_other_auth_errors(contract_client, mock_platforms_url, token, key):
    tenant = f"s-{uuid.uuid4().hex[:6]}"
    body = _publish_body(mock_platforms_url, tenant, f"k-{tenant}", connection=_conn(token))
    response = await contract_client.post("/connector/v1/slack/publish", json=body)
    assert response.status_code == 424 and response.json()["error"]["details"]["error_map_key"] == key


@pytest.mark.parametrize(("mode", "key"), [("http_429", "429"), ("rate_limited", "200:rate_limited")])
async def test_rate_limit_backs_off_and_short_circuits(contract_client, mock_platforms_url, mode, key):
    tenant = f"s-{uuid.uuid4().hex[:6]}"
    httpx.post(f"{mock_platforms_url}/slack/{tenant}/_mock/rate_limit", json={"count": 1, "retry_after": 17, "mode": mode})
    body = _publish_body(mock_platforms_url, tenant, f"k-{tenant}")
    response = await contract_client.post("/connector/v1/slack/publish", json=body)
    assert response.status_code == 429, response.text
    details = response.json()["error"]["details"]
    assert response.json()["error"]["code"] == "PLATFORM_RATE_LIMIT" and details["error_map_key"] == key
    if mode == "http_429":
        assert details["retry_after_s"] == 17  # from Slack's Retry-After header
    again = await contract_client.post("/connector/v1/slack/publish", json={**body, "idempotency_key": f"k2-{tenant}"})
    assert again.status_code == 429 and again.json()["error"]["details"]["short_circuit"] is True  # no platform call


async def test_rejected_content(contract_client, mock_platforms_url):
    tenant = f"s-{uuid.uuid4().hex[:6]}"
    # 1) our spec check refuses a body over Slack's 40,000 characters before any call
    long_body = _publish_body(mock_platforms_url, tenant, f"k1-{tenant}",
                              rendition={"title": "t", "body": "x" * 40_001, "ai_generated": False})
    response = await contract_client.post("/connector/v1/slack/publish", json=long_body)
    assert response.status_code == 422 and response.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
    # 2) with a looser override, Slack's own refusal (HTTP 200 ok:false msg_too_long) is PLATFORM_CONTENT_REJECTED
    long_body["destination"] = _dest(mock_platforms_url, tenant, body_max_chars=50_000)
    response = await contract_client.post("/connector/v1/slack/publish", json=long_body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
    assert response.json()["error"]["details"]["error_map_key"] == "200:msg_too_long"
    # 3) an archived channel
    httpx.post(f"{mock_platforms_url}/slack/{tenant}/_mock/archive", json={"channel": CHANNEL})
    archived = await contract_client.post("/connector/v1/slack/publish", json=_publish_body(mock_platforms_url, tenant, f"k3-{tenant}"))
    assert archived.status_code == 422 and archived.json()["error"]["details"]["error_map_key"] == "200:is_archived"


async def test_sim_mode_rewrites_only_the_api_base(app_factory, pg_url, mock_platforms_url):
    """SIMULATION_MODE: a real-looking destination (real Slack API, no token) goes to the mock with a placeholder."""
    from tests.conftest import api_client

    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url, SIMULATION_MODE=True)
    body = {"rendition": {"title": "Sim", "body": "Hello", "ai_generated": False},
            "destination": {"connector_key": "slack", "rules": {"automation_allowed": True}, "instance": {"channel_id": "C0SIMROW1"}},
            "connection": {"connector_key": "slack"}, "idempotency_key": f"sim-{uuid.uuid4().hex[:6]}"}
    async with api_client(app) as client:
        response = await client.post("/connector/v1/slack/publish", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["platform_post_id"].startswith("C0SIMROW1:")
    assert response.json()["live_url"].startswith(f"{mock_platforms_url}/slack/c0simrow1/archives/C0SIMROW1/")
