"""Ghost connector end to end: contract HTTP -> BaseConnector -> Ghost connector -> mock Ghost (real sockets, Postgres).

The mock reproduces the recorded Ghost 6.70 behaviour (Admin JWT checks, 422 ValidationError, slug
"-2" on conflict, 204 delete). Platform-side counters prove that a retry never posts twice.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from app.connectors.ghost import connector as ghost_module
from tests.conftest import api_client

KEY = "ghost"
ADMIN_KEY = "6acfcd9b8944510001d12d99:" + "1f" * 32
CONN = {"connector_key": KEY, "auth_type": "api_key", "credentials": {"admin_api_key": ADMIN_KEY}}


def dest(mock: str, tenant: str, **extra: Any) -> dict[str, Any]:
    return {"connector_key": KEY, "surface": "article", "instance": {"base_url": f"{mock}/ghost/{tenant}"},
            "rules": {"automation_allowed": True}, **extra}


def publish_body(mock: str, tenant: str, key: str, title: str = "Hello from IncuBrix", **dest_extra: Any) -> dict[str, Any]:
    return {"rendition": {"title": title, "body": "A **first** post.", "content_type": "text", "ai_generated": False},
            "destination": dest(mock, tenant, **dest_extra), "connection": CONN, "idempotency_key": key}


def creates(mock: str, tenant: str) -> int:
    data = httpx.get(f"{mock}/_counts", params={"platform": KEY, "tenant": tenant}).json()["counts"]
    return data.get(KEY, {}).get(tenant, {}).get("POST /ghost/api/admin/posts", 0)


def admin_get(mock: str, tenant: str, path: str) -> httpx.Response:
    key_id, _, secret = ADMIN_KEY.partition(":")
    token, _ = ghost_module.admin_token(key_id, bytes.fromhex(secret), time.time())
    return httpx.get(f"{mock}/ghost/{tenant}/ghost/api/admin{path}", headers={"Authorization": f"Ghost {token}"})


@pytest.fixture
async def api(app_factory, pg_url, mock_platforms_url):
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        yield client


async def test_publish_status_delete_roundtrip(api, mock_platforms_url):
    mock = mock_platforms_url
    published = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "rt", "ghost-rt-1"))
    assert published.status_code == 200, published.text
    body = published.json()
    assert set(body) == {"platform_post_id", "live_url", "status"} and body["status"] == "published"
    assert body["live_url"].startswith(f"{mock}/ghost/rt/hello-from-incubrix-ibx-")
    assert httpx.get(body["live_url"]).status_code == 200

    ref = {"platform_post_id": body["platform_post_id"], "connection": CONN, "destination": dest(mock, "rt")}
    assert (await api.post(f"/connector/v1/{KEY}/get_status", json=ref)).json() == {"status": "published", "live_url": body["live_url"]}
    deleted = await api.post(f"/connector/v1/{KEY}/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
    assert deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "permanent"
    assert (await api.post(f"/connector/v1/{KEY}/get_status", json=ref)).json() == {"status": "deleted", "live_url": None}
    assert httpx.get(body["live_url"]).status_code == 404
    again = await api.post(f"/connector/v1/{KEY}/delete", json=ref)
    assert again.json() == {"deleted": True}  # deleting twice is fine


async def test_metrics_is_not_supported(api, mock_platforms_url):
    ref = {"platform_post_id": "6acf00000000000000000001", "connection": CONN, "destination": dest(mock_platforms_url, "m")}
    response = await api.post(f"/connector/v1/{KEY}/fetch_metrics", json=ref)
    assert response.status_code == 404 and response.json()["error"]["code"] == "METHOD_NOT_SUPPORTED"


async def test_timeout_after_create_then_retry_returns_the_same_post(api, mock_platforms_url):
    """HT1: the post is created but the answer never arrives; the retry finds it by the slug marker."""
    mock = mock_platforms_url
    body = publish_body(mock, "timeout", "ghost-timeout-1")
    first = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-Sim-Fault": "timeout_after_create:5"})
    assert first.status_code == 503, first.text
    assert first.json()["error"]["details"]["intent_state"] == "UNKNOWN"
    on_platform = admin_get(mock, "timeout", "/posts/").json()["posts"]
    assert len(on_platform) == 1

    retry = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["platform_post_id"] == on_platform[0]["id"] and retry.json()["_meta"]["reconciled"] is True
    assert creates(mock, "timeout") == 1 and len(admin_get(mock, "timeout", "/posts/").json()["posts"]) == 1


async def test_ten_parallel_identical_publishes_create_one_post(api, mock_platforms_url):
    mock = mock_platforms_url
    body = publish_body(mock, "parallel", "ghost-parallel-1")
    responses = await asyncio.gather(*[api.post(f"/connector/v1/{KEY}/publish", json=body) for _ in range(10)])
    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["platform_post_id"] for r in responses}) == 1 and creates(mock, "parallel") == 1


async def test_expired_token_is_auth_expired_with_a_clock_hint(api, mock_platforms_url, monkeypatch):
    """HT3 clock-skew variant: our clock 10 minutes behind -> the token is already expired for Ghost."""
    monkeypatch.setattr(ghost_module, "wall_clock", lambda: time.time() - 600)
    response = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock_platforms_url, "skew", "ghost-skew-1"))
    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == "CONNECTION_AUTH_EXPIRED" and error["retryable"] is False
    assert error["details"]["platform_code"] == "INVALID_JWT" and "jwt expired" in error["message"]
    assert "clock" in error["message"] and "1f1f1f" not in response.text


async def test_deleted_integration_is_auth_expired_and_refresh_auth_says_so(api, mock_platforms_url):
    """HT3: the owner deletes the integration in Ghost Admin -> 401 UNKNOWN_ADMIN_API_KEY."""
    mock = mock_platforms_url
    httpx.post(f"{mock}/ghost/revoked/_mock/revoke", json={"kid": ADMIN_KEY.split(":")[0]}).raise_for_status()
    response = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "revoked", "ghost-revoked-1"))
    assert response.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    assert response.json()["error"]["details"]["platform_code"] == "UNKNOWN_ADMIN_API_KEY"
    refreshed = await api.post(f"/connector/v1/{KEY}/refresh_auth", json={"connection": CONN, "destination": dest(mock, "revoked")})
    assert refreshed.status_code == 424


async def test_refresh_auth_returns_the_new_token_expiry(api, mock_platforms_url):
    before = time.time()
    response = await api.post(f"/connector/v1/{KEY}/refresh_auth", json={"connection": CONN, "destination": dest(mock_platforms_url, "r")})
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"expires_at"}
    from datetime import datetime

    expires = datetime.fromisoformat(response.json()["expires_at"].replace("Z", "+00:00")).timestamp()
    assert before + 299 <= expires <= time.time() + 301


async def test_malformed_key_is_auth_expired_without_any_call(api, mock_platforms_url):
    body = publish_body(mock_platforms_url, "badkey", "ghost-badkey-1")
    body["connection"] = {**CONN, "credentials": {"admin_api_key": "not-a-ghost-key"}}
    response = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    assert response.json()["error"]["details"]["reason"] == "malformed_admin_key"
    assert creates(mock_platforms_url, "badkey") == 0


async def test_over_long_title_is_refused_before_any_call(api, mock_platforms_url):
    response = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock_platforms_url, "long", "ghost-long-1", title="T" * 256))
    assert response.status_code == 422 and response.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
    assert response.json()["error"]["details"]["violations"][0]["limit"] == 255
    assert creates(mock_platforms_url, "long") == 0


async def test_platform_422_is_content_rejected(api, mock_platforms_url):
    body = publish_body(mock_platforms_url, "reject", "ghost-reject-1", title="T" * 256, spec_overrides={"title_max_chars": 300})
    response = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "PLATFORM_CONTENT_REJECTED" and error["details"]["platform_status"] == 422


async def test_429_with_retry_after_then_short_circuit(api, mock_platforms_url):
    mock = mock_platforms_url
    limited = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "rl", "ghost-rl-1"), headers={"X-Sim-Fault": "429:120"})
    assert limited.status_code == 429
    details = limited.json()["error"]["details"]
    assert details["retry_after_s"] == 120 and details["reset_source"] == "Retry-After"
    before = creates(mock, "rl")
    second = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "rl", "ghost-rl-2", title="Another"))
    assert second.status_code == 429 and second.json()["error"]["details"]["short_circuit"] is True
    assert creates(mock, "rl") == before


async def test_renamed_url_fails_loudly_and_the_retry_still_finds_the_post(api, mock_platforms_url):
    mock = mock_platforms_url
    body = publish_body(mock, "drift", "ghost-drift-1")
    first = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-Sim-Fault": "rename_field:url->permalink"})
    assert first.status_code == 503
    assert first.json()["error"]["details"]["reason"] == "response_schema_mismatch"
    assert "posts.0.url" in first.json()["error"]["details"]["fields"]
    retry = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert retry.status_code == 200 and creates(mock, "drift") == 1


async def test_simulation_mode_routes_a_real_url_to_the_mock(app_factory, pg_url, mock_platforms_url):
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url, SIMULATION_MODE=True)
    async with api_client(app) as client:
        body = {"rendition": {"title": "Sim article", "body": "x", "content_type": "text", "ai_generated": False},
                "destination": {"connector_key": KEY, "instance": {"base_url": "http://ghost:2368"},
                                "rules": {"automation_allowed": True}},
                "connection": {"connector_key": KEY, "auth_type": "api_key"}, "idempotency_key": "ghost-sim-1"}
        response = await client.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["live_url"].startswith(f"{mock_platforms_url}/ghost/ghost-2368/sim-article-ibx-")
