"""HMAC partner-webhook connector end to end against the partner mock (real sockets, real Postgres).

HT3 (partner revokes permission): 401 -> CONNECTION_AUTH_EXPIRED, the stored connection is paused and the
next publish fails fast with no partner call. Also: replay window, idempotency dedup, rate limit, 422.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from app.connectors.webhook import signing
from app.db.tables import ConnectionRow
from app.reliability.secrets import encrypt_credentials
from tests.conftest import api_client

KEY = "webhook"
SECRET = "sim-secret"  # the partner mock's default secret
CONNECTION = {"connector_key": KEY, "auth_type": "api_key", "credentials": {"secret": SECRET}}


def dest(mock: str, tenant: str) -> dict[str, Any]:
    return {"connector_key": KEY, "surface": "post", "instance": {"endpoint_url": f"{mock}/partner/{tenant}"},
            "rules": {"automation_allowed": True}}


def publish_body(mock: str, tenant: str, key: str, connection: dict | None = None, title: str = "Partner post") -> dict[str, Any]:
    return {"rendition": {"title": title, "body": "Hello partner", "ai_generated": False}, "destination": dest(mock, tenant),
            "connection": connection or CONNECTION, "idempotency_key": key}


def ref(mock: str, tenant: str, post_id: str) -> dict[str, Any]:
    return {"platform_post_id": post_id, "connection": CONNECTION, "destination": dest(mock, tenant)}


def creates(mock: str, tenant: str) -> int:
    counts = httpx.get(f"{mock}/_counts", params={"platform": "partner", "tenant": tenant}).json()["counts"]
    return counts.get("partner", {}).get(tenant, {}).get("POST /posts", 0)


@pytest.fixture
async def api(app_factory, pg_url, mock_platforms_url):
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


async def test_round_trip_publish_status_delete(api, mock_platforms_url):
    mock = mock_platforms_url
    r = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "p1", "w-rt"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "published" and httpx.get(out["live_url"]).status_code == 200
    status = await api.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "p1", out["platform_post_id"]))
    assert status.json() == {"status": "published", "live_url": out["live_url"]}
    assert (await api.post(f"/connector/v1/{KEY}/delete", json=ref(mock, "p1", out["platform_post_id"]))).json() == {"deleted": True}
    assert httpx.get(out["live_url"]).status_code == 404
    status = await api.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "p1", out["platform_post_id"]))
    assert status.json() == {"status": "deleted", "live_url": None}
    assert (await api.post(f"/connector/v1/{KEY}/delete", json=ref(mock, "p1", out["platform_post_id"]))).json() == {"deleted": True}


async def test_timeout_after_create_then_retry_reconciles(api, mock_platforms_url):
    mock = mock_platforms_url
    body = publish_body(mock, "p-t", "w-timeout")
    first = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-Sim-Fault": "timeout_after_create:5"})
    assert first.status_code == 503 and first.json()["error"]["details"]["intent_state"] == "UNKNOWN"
    retry = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
    assert retry.status_code == 200 and retry.json()["_meta"]["reconciled"] is True
    assert creates(mock, "p-t") == 1


async def test_partner_revokes_permission_pauses_connection(api, mock_platforms_url):
    """HT3: a stored connection is paused after the partner's 401; later calls fail fast without a partner call."""
    mock = mock_platforms_url
    settings = api.app.state.settings
    async with api.app.state.session_factory() as session, session.begin():
        session.add(ConnectionRow(id="conn_42", connector_key=KEY, auth_type="api_key",
                                  secret_enc=encrypt_credentials(settings.fernet_key(), {"secret": SECRET})))
    stored = {"connector_key": KEY, "connection_id": "conn_42"}
    assert (await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "p-rev", "w-rev-1", stored))).status_code == 200
    httpx.post(f"{mock}/partner/p-rev/_partner/revoke").raise_for_status()
    r = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "p-rev", "w-rev-2", stored, title="Second"))
    assert r.status_code == 424
    error = r.json()["error"]
    assert error["code"] == "CONNECTION_AUTH_EXPIRED" and error["retryable"] is False
    assert error["details"]["connection_id"] == "conn_42" and error["details"]["connection_state"] == "paused"
    assert error["details"]["action"] == "reconnect_required" and error["details"]["platform_status"] == 401
    assert SECRET not in r.text
    hits = creates(mock, "p-rev")
    again = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "p-rev", "w-rev-3", stored, title="Third"))
    assert again.status_code == 424 and creates(mock, "p-rev") == hits  # fail fast, no partner call


async def test_rotated_secret_is_auth_expired(api, mock_platforms_url):
    httpx.post(f"{mock_platforms_url}/partner/p-rot/_partner/rotate", json={"secret": "new-secret"}).raise_for_status()
    r = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock_platforms_url, "p-rot", "w-rot"))
    assert r.status_code == 424 and r.json()["error"]["details"]["platform_message"] == "Signature does not match."


async def test_stale_signature_outside_replay_window(api, mock_platforms_url, monkeypatch):
    monkeypatch.setattr(signing, "unix_now", lambda: int(time.time()) - 600)
    r = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock_platforms_url, "p-stale", "w-stale"))
    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "PLATFORM_UNAVAILABLE" and error["details"]["error_map_key"] == "400:timestamp_outside_tolerance"
    assert creates(mock_platforms_url, "p-stale") == 1 and httpx.get(
        f"{mock_platforms_url}/partner/p-stale/p/1").status_code == 404  # nothing was created


async def test_rate_limit_and_rejection(api, mock_platforms_url):
    mock = mock_platforms_url
    httpx.post(f"{mock}/partner/p-rl/_partner/rate_limit", json={"limit": 1, "window_s": 60}).raise_for_status()
    assert (await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "p-rl", "w-rl-1"))).status_code == 200
    limited = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "p-rl", "w-rl-2", title="Other"))
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "PLATFORM_RATE_LIMIT"
    assert 55 <= limited.json()["error"]["details"]["retry_after_s"] <= 61

    body = publish_body(mock, "p-422", "w-422", title="t" * 400)
    body["destination"]["spec_overrides"] = {"title_max_chars": 1000}
    rejected = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"


def _signed(body: bytes, secret: str = SECRET, **headers: str) -> dict[str, str]:
    return {signing.HEADER: signing.sign(secret, body), "Content-Type": "application/json", **headers}


def test_partner_mock_dedups_and_verifies(mock_platforms_url):
    url = f"{mock_platforms_url}/partner/p-dedup/posts"
    body = json.dumps({"rendition": {"title": "x", "body": "y"}}).encode()
    first = httpx.post(url, content=body, headers=_signed(body, **{"Idempotency-Key": "k1"}))
    second = httpx.post(url, content=body, headers=_signed(body, **{"Idempotency-Key": "k1"}))
    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true" and second.json()["id"] == first.json()["id"]
    other = json.dumps({"rendition": {"title": "z"}}).encode()
    assert httpx.post(url, content=other, headers=_signed(other, **{"Idempotency-Key": "k1"})).status_code == 409
    assert httpx.post(url, content=body, headers=_signed(body, "wrong", **{"Idempotency-Key": "k2"})).status_code == 401
    assert httpx.post(url, content=body, headers={"Idempotency-Key": "k3"}).status_code == 401
    old = {signing.HEADER: signing.sign(SECRET, body, timestamp=int(time.time()) - 301), "Idempotency-Key": "k4"}
    assert httpx.post(url, content=body, headers=old).json()["error"] == "timestamp_outside_tolerance"
    assert httpx.post(url, content=body, headers=_signed(body)).status_code == 400  # Idempotency-Key required
