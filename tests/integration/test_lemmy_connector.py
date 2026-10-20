"""Lemmy connector end to end: contract HTTP -> BaseConnector -> Lemmy connector -> mock Lemmy (real sockets, Postgres).

The mock reproduces the recorded Lemmy 0.19.20 behaviour (400 tokens, rate-limit bucket, soft delete +
purge). Platform-side request counters prove that a retry never posts twice.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.conftest import api_client

KEY = "lemmy"
CONN = {"connector_key": KEY, "auth_type": "api_key", "credentials": {"jwt": "canary-LEMMY-7f3a91"}}


def dest(mock: str, tenant: str, **extra: Any) -> dict[str, Any]:
    return {"connector_key": KEY, "surface": "post", "instance": {"community_url": f"{mock}/lemmy/{tenant}/c/ibx_test"},
            "rules": {"automation_allowed": True}, **extra}


def publish_body(mock: str, tenant: str, key: str, title: str = "Hello from IncuBrix", **dest_extra: Any) -> dict[str, Any]:
    return {"rendition": {"title": title, "body": "A first post.", "content_type": "text", "ai_generated": False},
            "destination": dest(mock, tenant, **dest_extra), "connection": CONN, "idempotency_key": key}


def count(mock: str, tenant: str, route: str = "POST /api/v3/post") -> int:
    data = httpx.get(f"{mock}/_counts", params={"platform": KEY, "tenant": tenant}).json()["counts"]
    return data.get(KEY, {}).get(tenant, {}).get(route, 0)


def posts_with_marker(mock: str, tenant: str) -> list[dict[str, Any]]:
    data = httpx.get(f"{mock}/lemmy/{tenant}/api/v3/user", params={"username": "ibx_admin", "limit": 50}).json()
    return [v["post"] for v in data["posts"] if "[//]: # (ibx-" in (v["post"].get("body") or "")]


@pytest.fixture
async def api(app_factory, pg_url, mock_platforms_url):
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        yield client


async def test_publish_status_metrics_delete_roundtrip(api, mock_platforms_url):
    mock = mock_platforms_url
    published = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "rt", "lemmy-rt-1"))
    assert published.status_code == 200, published.text
    body = published.json()
    assert set(body) == {"platform_post_id", "live_url", "status"} and body["status"] == "published"
    assert body["live_url"] == f"{mock}/lemmy/rt/post/{body['platform_post_id']}"
    assert httpx.get(body["live_url"]).status_code == 200  # the live URL answers

    ref = {"platform_post_id": body["platform_post_id"], "connection": CONN, "destination": dest(mock, "rt")}
    status = await api.post(f"/connector/v1/{KEY}/get_status", json=ref)
    assert status.json() == {"status": "published", "live_url": body["live_url"]}
    metrics = await api.post(f"/connector/v1/{KEY}/fetch_metrics", json=ref)
    assert metrics.json()["likes"] == 1 and metrics.json()["comments"] == 0 and metrics.json()["views"] is None

    deleted = await api.post(f"/connector/v1/{KEY}/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "purged"
    after = await api.post(f"/connector/v1/{KEY}/get_status", json=ref)
    assert after.json() == {"status": "deleted", "live_url": None}
    assert httpx.get(body["live_url"]).status_code == 404


async def test_timeout_after_create_then_retry_returns_the_same_post(api, mock_platforms_url):
    """HT1: the post is created but the answer never arrives; the retry reconciles by the body marker."""
    mock = mock_platforms_url
    body = publish_body(mock, "timeout", "lemmy-timeout-1")
    first = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-Sim-Fault": "timeout_after_create:5"})
    assert first.status_code == 503, first.text
    error = first.json()["error"]
    assert error["code"] == "PLATFORM_UNAVAILABLE" and error["retryable"] is True
    assert error["details"]["intent_state"] == "UNKNOWN" and "safe to retry" in error["message"]
    created = posts_with_marker(mock, "timeout")
    assert len(created) == 1  # the platform did create it

    retry = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["platform_post_id"] == str(created[0]["id"])
    assert retry.json()["_meta"]["reconciled"] is True and retry.headers["Idempotent-Replayed"] == "true"
    again = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert again.json()["platform_post_id"] == str(created[0]["id"])
    assert count(mock, "timeout") == 1 and len(posts_with_marker(mock, "timeout")) == 1


async def test_queued_timeout_fault_on_the_create_only(api, mock_platforms_url):
    mock = mock_platforms_url
    httpx.post(f"{mock}/_faults", json={"platform": KEY, "tenant": "queued", "method": "POST", "path": "/api/v3/post",
                                        "fault": "timeout_after_create:5", "count": 1}).raise_for_status()
    body = publish_body(mock, "queued", "lemmy-queued-1")
    assert (await api.post(f"/connector/v1/{KEY}/publish", json=body)).status_code == 503
    retry = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert retry.status_code == 200 and count(mock, "queued") == 1


async def test_ten_parallel_identical_publishes_create_one_post(api, mock_platforms_url):
    mock = mock_platforms_url
    body = publish_body(mock, "parallel", "lemmy-parallel-1")
    responses = await asyncio.gather(*[api.post(f"/connector/v1/{KEY}/publish", json=body) for _ in range(10)])
    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["platform_post_id"] for r in responses}) == 1
    assert count(mock, "parallel") == 1


async def test_lemmy_rate_limit_400_gets_reset_from_site_config_then_short_circuits(api, mock_platforms_url):
    """HT2 Lemmy variant: 400 rate_limit_error (no header) -> 429 PLATFORM_RATE_LIMIT, reset from the site config."""
    mock = mock_platforms_url
    httpx.put(f"{mock}/lemmy/rl/api/v3/site", json={"rate_limit_post": 1, "rate_limit_post_per_second": 90},
              headers={"Authorization": "Bearer admin-setup"}).raise_for_status()
    assert (await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "rl", "lemmy-rl-1"))).status_code == 200

    limited = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "rl", "lemmy-rl-2", title="Second post"))
    assert limited.status_code == 429, limited.text
    error = limited.json()["error"]
    assert error["code"] == "PLATFORM_RATE_LIMIT" and error["retryable"] is True
    assert error["details"]["reset_source"] == "lemmy_site_config"
    assert error["details"]["retry_after_s"] == 90 and error["details"]["error_map_key"] == "400:rate_limit_error"
    assert int(limited.headers["Retry-After"]) >= 89
    posts_before = count(mock, "rl")

    third = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "rl", "lemmy-rl-3", title="Third post"))
    assert third.status_code == 429 and third.json()["error"]["details"]["short_circuit"] is True
    assert count(mock, "rl") == posts_before  # no platform call while blocked


@pytest.mark.parametrize("token", ["revoked-token-abc", "invalid-jwt-xyz"])
async def test_revoked_token_is_auth_expired(api, mock_platforms_url, token):
    body = publish_body(mock_platforms_url, "auth", f"lemmy-auth-{token}")
    body["connection"] = {**CONN, "credentials": {"jwt": token}}
    response = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == "CONNECTION_AUTH_EXPIRED" and error["retryable"] is False
    assert error["details"]["platform_status"] == 401 and token not in response.text


async def test_logged_out_token_is_auth_expired(api, mock_platforms_url):
    mock = mock_platforms_url
    jwt = httpx.post(f"{mock}/lemmy/logout/api/v3/user/login", json={"username_or_email": "ibx_admin", "password": "pw-12345"}).json()["jwt"]
    httpx.post(f"{mock}/lemmy/logout/api/v3/user/logout", headers={"Authorization": f"Bearer {jwt}"}).raise_for_status()
    body = publish_body(mock, "logout", "lemmy-logout-1")
    body["destination"] = dest(mock, "logout")
    body["connection"] = {**CONN, "credentials": {"jwt": jwt}}
    response = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED"


async def test_over_long_title_is_refused_before_any_call(api, mock_platforms_url):
    """HT4: 201 characters -> 422 DEST_SPEC_VIOLATION, the platform is not called."""
    response = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock_platforms_url, "long", "lemmy-long-1", title="T" * 201))
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "DEST_SPEC_VIOLATION"
    assert error["details"]["violations"][0] | {"message": None} == {
        "field": "title", "rule": "title_max_chars", "limit": 200, "actual": 201, "message": None}
    assert count(mock_platforms_url, "long") == 0


async def test_platform_content_rejection_is_422_and_replayed(api, mock_platforms_url):
    """A looser spec override lets the post reach Lemmy, which refuses it: 400 invalid_post_title."""
    mock = mock_platforms_url
    body = publish_body(mock, "reject", "lemmy-reject-1", title="T" * 250, spec_overrides={"title_max_chars": 300})
    first = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert first.status_code == 422, first.text
    error = first.json()["error"]
    assert error["code"] == "PLATFORM_CONTENT_REJECTED" and error["retryable"] is False
    assert error["details"]["platform_message"] == "invalid_post_title"
    again = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert again.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED" and again.json()["error"]["details"]["replayed"] is True
    assert count(mock, "reject") == 1


async def test_renamed_field_fails_loudly_and_the_retry_still_finds_the_post(api, mock_platforms_url):
    """HT7 at connector level: a renamed response field is a schema mismatch, never a silent success."""
    mock = mock_platforms_url
    body = publish_body(mock, "drift", "lemmy-drift-1")
    first = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-Sim-Fault": "rename_field:ap_id->url_of_post"})
    assert first.status_code == 503
    details = first.json()["error"]["details"]
    assert details["reason"] == "response_schema_mismatch" and "post_view.post.ap_id" in details["fields"]
    retry = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert retry.status_code == 200 and count(mock, "drift") == 1


async def test_password_login_connection(api, mock_platforms_url):
    body = publish_body(mock_platforms_url, "login", "lemmy-login-1")
    body["connection"] = {**CONN, "credentials": {"username": "ibx_admin", "password": "pw-12345"}}
    response = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.status_code == 200, response.text
    assert count(mock_platforms_url, "login", "POST /api/v3/user/login") == 1


async def test_non_admin_delete_is_a_soft_delete(api, mock_platforms_url):
    mock = mock_platforms_url
    conn = {**CONN, "credentials": {"jwt": "nonadmin-user-token"}}
    body = publish_body(mock, "soft", "lemmy-soft-1")
    body["connection"] = conn
    post_id = (await api.post(f"/connector/v1/{KEY}/publish", json=body)).json()["platform_post_id"]
    ref = {"platform_post_id": post_id, "connection": conn, "destination": dest(mock, "soft")}
    deleted = await api.post(f"/connector/v1/{KEY}/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
    assert deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "soft"
    assert (await api.post(f"/connector/v1/{KEY}/get_status", json=ref)).json()["status"] == "deleted"


async def test_simulation_mode_routes_a_real_url_to_the_mock(app_factory, pg_url, mock_platforms_url):
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url, SIMULATION_MODE=True)
    async with api_client(app) as client:
        body = {"rendition": {"title": "Sim post", "body": "x", "content_type": "text", "ai_generated": False},
                "destination": {"connector_key": KEY, "instance": {"community_url": "http://lemmy:8536/c/ibx_test"},
                                "rules": {"automation_allowed": True}},
                "connection": {"connector_key": KEY, "auth_type": "api_key"}, "idempotency_key": "lemmy-sim-1"}
        response = await client.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["live_url"].startswith(f"{mock_platforms_url}/lemmy/lemmy-8536-c-ibx-test/post/")
