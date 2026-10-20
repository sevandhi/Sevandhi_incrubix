"""BaseConnector pipeline end to end: contract HTTP → base → template connector → mock-platforms (real sockets).

Covers Must-have 6 (idempotent publish under timeout), rate-limit short-circuit, breaker, kill switch,
error mapping, schema drift and auth pause. Platform-side request counters prove "no duplicate posts".
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.conftest import SAMPLE_KEY, api_client

CONNECTION = {"connector_key": SAMPLE_KEY, "auth_type": "api_key", "credentials": {"token": "canary-SECRET-7f3a91"}}


def destination(mock_url: str, tenant: str) -> dict[str, Any]:
    return {
        "connector_key": SAMPLE_KEY,
        "surface": "post",
        "instance": {"base_url": f"{mock_url}/{SAMPLE_KEY}/{tenant}"},
        "rules": {"automation_allowed": True},  # caller-confirmed, permissive rules
    }


def publish_body(mock_url: str, tenant: str, key: str, title: str = "Hello world") -> dict[str, Any]:
    return {
        "rendition": {"title": title, "body": "A first post", "content_type": "text", "ai_generated": False},
        "destination": destination(mock_url, tenant),
        "connection": CONNECTION,
        "idempotency_key": key,
    }


def creates(mock_url: str, tenant: str) -> int:
    counts = httpx.get(f"{mock_url}/_counts", params={"platform": SAMPLE_KEY, "tenant": tenant}).json()["counts"]
    return counts.get(SAMPLE_KEY, {}).get(tenant, {}).get("POST /posts", 0)


def platform_posts(mock_url: str, tenant: str) -> list[dict[str, Any]]:
    response = httpx.get(f"{mock_url}/{SAMPLE_KEY}/{tenant}/posts", headers={"Authorization": "Bearer x"})
    return response.json()["posts"]


@pytest.fixture
async def api(app_factory, pg_url, mock_platforms_url, sample_registry, fake_clock):
    app = await app_factory(pg_url=pg_url, registry=sample_registry, clock=fake_clock, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


async def test_publish_returns_exact_keys_and_replays_without_second_post(api, mock_platforms_url):
    body = publish_body(mock_platforms_url, "t-replay", "key-replay-1")
    first = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body)
    assert first.status_code == 200, first.text
    assert set(first.json()) == {"platform_post_id", "live_url", "status"}
    assert first.json()["status"] == "published"
    assert first.headers["Idempotency-Key"] == "key-replay-1"

    second = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert creates(mock_platforms_url, "t-replay") == 1


async def test_same_key_different_body_is_409(api, mock_platforms_url):
    await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, "t-409", "key-409"))
    other = publish_body(mock_platforms_url, "t-409", "key-409", title="A different title")
    response = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=other)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "IDEMPOTENCY_KEY_REUSED" and error["retryable"] is False
    assert creates(mock_platforms_url, "t-409") == 1


async def test_header_and_body_key_mismatch_is_409(api, mock_platforms_url):
    body = publish_body(mock_platforms_url, "t-mismatch", "body-key")
    response = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body, headers={"Idempotency-Key": "other"})
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "header_body_mismatch"
    assert creates(mock_platforms_url, "t-mismatch") == 0


async def test_header_key_used_when_body_has_none(api, mock_platforms_url):
    body = publish_body(mock_platforms_url, "t-header", "unused")
    body.pop("idempotency_key")
    first = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body, headers={"Idempotency-Key": "hdr-1"})
    second = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body, headers={"Idempotency-Key": "hdr-1"})
    assert first.status_code == second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert creates(mock_platforms_url, "t-header") == 1


async def test_timeout_after_create_then_retry_reconciles_one_post(api, mock_platforms_url, fake_clock):
    """HT1: the platform creates the post but never answers; the retry finds it by marker."""
    body = publish_body(mock_platforms_url, "t-timeout", "key-timeout")
    first = await api.post(
        f"/connector/v1/{SAMPLE_KEY}/publish", json=body, headers={"X-Sim-Fault": "timeout_after_create:5"}
    )
    assert first.status_code == 503, first.text
    error = first.json()["error"]
    assert error["code"] == "PLATFORM_UNAVAILABLE" and error["retryable"] is True
    assert error["details"]["intent_state"] == "UNKNOWN"
    assert error["details"]["idempotency_key"] == "key-timeout"
    assert "safe to retry" in error["message"]
    assert len(platform_posts(mock_platforms_url, "t-timeout")) == 1  # the platform did create it

    fake_clock.advance(10)
    retry = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["_meta"]["reconciled"] is True
    assert retry.json()["platform_post_id"] == str(platform_posts(mock_platforms_url, "t-timeout")[0]["id"])
    assert creates(mock_platforms_url, "t-timeout") == 1  # never posted twice


async def test_ten_parallel_identical_publishes_create_one_post(api, mock_platforms_url):
    body = publish_body(mock_platforms_url, "t-parallel", "key-parallel")
    responses = await asyncio.gather(*[api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body) for _ in range(10)])
    assert [r.status_code for r in responses] == [200] * 10
    assert len({r.json()["platform_post_id"] for r in responses}) == 1
    assert creates(mock_platforms_url, "t-parallel") == 1


async def test_429_with_retry_after_blocks_without_calling_platform(api, mock_platforms_url):
    body = publish_body(mock_platforms_url, "t-429", "key-429")
    first = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body, headers={"X-Sim-Fault": "429:120"})
    assert first.status_code == 429
    assert first.headers["Retry-After"] == "120"
    details = first.json()["error"]["details"]
    assert first.json()["error"]["code"] == "PLATFORM_RATE_LIMIT"
    assert details["retry_after_s"] == 120 and details["reset_source"] == "Retry-After"
    hits = creates(mock_platforms_url, "t-429")

    again = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, "t-429", "key-429b"))
    assert again.status_code == 429
    assert again.json()["error"]["details"]["short_circuit"] is True
    assert creates(mock_platforms_url, "t-429") == hits  # no platform call while blocked


async def test_rate_limit_block_expires_with_the_clock(api, mock_platforms_url, fake_clock):
    await api.post(
        f"/connector/v1/{SAMPLE_KEY}/publish",
        json=publish_body(mock_platforms_url, "t-429-exp", "k1"),
        headers={"X-Sim-Fault": "429:30"},
    )
    fake_clock.advance(31)
    ok = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, "t-429-exp", "k1"))
    assert ok.status_code == 200, ok.text  # RETRYABLE intent re-attempted after the reset time


async def test_breaker_opens_after_five_failures_and_stops_calling(api, mock_platforms_url, fake_clock):
    tenant = "t-breaker"
    for i in range(5):
        r = await api.post(
            f"/connector/v1/{SAMPLE_KEY}/publish",
            json=publish_body(mock_platforms_url, tenant, f"kb-{i}"),
            headers={"X-Sim-Fault": "500"},
        )
        assert r.json()["error"]["code"] == "PLATFORM_UNAVAILABLE"
    hits = creates(mock_platforms_url, tenant)
    blocked = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, tenant, "kb-x"))
    assert blocked.status_code == 503
    assert blocked.json()["error"]["details"]["circuit"] == "open"
    assert creates(mock_platforms_url, tenant) == hits

    fake_clock.advance(301)  # half-open: one probe goes through and closes the breaker
    probe = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, tenant, "kb-x"))
    assert probe.status_code == 200, probe.text


async def test_error_map_401_and_422(api, mock_platforms_url):
    auth = await api.post(
        f"/connector/v1/{SAMPLE_KEY}/publish",
        json=publish_body(mock_platforms_url, "t-err", "k-401"),
        headers={"X-Sim-Fault": "401"},
    )
    assert auth.status_code == 424
    assert auth.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    rejected = await api.post(
        f"/connector/v1/{SAMPLE_KEY}/publish",
        json=publish_body(mock_platforms_url, "t-err", "k-422"),
        headers={"X-Sim-Fault": "422"},
    )
    assert rejected.status_code == 422
    error = rejected.json()["error"]
    assert error["code"] == "PLATFORM_CONTENT_REJECTED" and error["retryable"] is False
    assert error["details"]["platform_status"] == 422
    assert error["details"]["platform_message"] == "content rejected by platform"
    replay = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, "t-err", "k-422"))
    assert replay.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
    assert replay.json()["error"]["details"]["replayed"] is True


async def test_body_token_error_map_maps_lemmy_style_400_to_rate_limit(api, mock_platforms_url):
    response = await api.post(
        f"/connector/v1/{SAMPLE_KEY}/publish",
        json=publish_body(mock_platforms_url, "t-400rl", "k-400"),
        headers={"X-Sim-Fault": "400:rate_limit_error"},
    )
    assert response.status_code == 429
    details = response.json()["error"]["details"]
    assert details["error_map_key"] == "400:rate_limit_error"
    assert details["reset_source"] == "default"


async def test_renamed_field_is_schema_mismatch(api, mock_platforms_url):
    response = await api.post(
        f"/connector/v1/{SAMPLE_KEY}/publish",
        json=publish_body(mock_platforms_url, "t-drift", "k-drift"),
        headers={"X-Sim-Fault": "rename_field:id->post_id"},
    )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "PLATFORM_UNAVAILABLE"
    assert error["details"]["reason"] == "response_schema_mismatch"
    assert "id" in error["details"]["fields"]
    assert error["details"]["intent_state"] == "UNKNOWN"


async def test_spec_violation_never_calls_platform(api, mock_platforms_url):
    response = await api.post(
        f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, "t-spec", "k-spec", title="x" * 201)
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "DEST_SPEC_VIOLATION"
    assert any(v["field"] == "title" for v in error["details"]["violations"])
    assert creates(mock_platforms_url, "t-spec") == 0


async def test_kill_switch_at_runtime(api, mock_platforms_url):
    off = await api.put(f"/api/v1/connectors/{SAMPLE_KEY}/kill-switch", json={"disabled": True, "reason": "incident 42"})
    assert off.status_code == 200 and off.json()["disabled"] is True
    response = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, "t-kill", "k-kill"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONNECTOR_DISABLED"
    assert response.json()["error"]["retryable"] is False
    assert creates(mock_platforms_url, "t-kill") == 0
    on = await api.put(f"/api/v1/connectors/{SAMPLE_KEY}/kill-switch", json={"disabled": False})
    assert on.json()["disabled"] is False
    ok = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, "t-kill", "k-kill"))
    assert ok.status_code == 200


async def test_status_delete_metrics_and_mirror_routes(api, mock_platforms_url):
    published = await api.post(f"/api/v1/connector/{SAMPLE_KEY}/publish", json=publish_body(mock_platforms_url, "t-rt", "k-rt"))
    assert published.status_code == 200
    ref = {"platform_post_id": published.json()["platform_post_id"], "connection": CONNECTION,
           "destination": destination(mock_platforms_url, "t-rt")}
    status = await api.post(f"/connector/v1/{SAMPLE_KEY}/status", json=ref)  # alias of get_status
    assert status.json() == {"status": "published", "live_url": published.json()["live_url"]}
    metrics = await api.post(f"/connector/v1/{SAMPLE_KEY}/fetch_metrics", json={**ref, "since": "2026-10-01T00:00:00Z"})
    assert metrics.json() == {"views": 0, "likes": 0, "comments": 0, "shares": None, "saves": None,
                              "watch_time_s": None, "clicks": None}
    deleted = await api.post(f"/connector/v1/{SAMPLE_KEY}/delete", json=ref)
    assert deleted.json() == {"deleted": True}
    gone = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status", json=ref)
    assert gone.json()["status"] == "deleted"


async def test_paused_connection_fails_fast(api, mock_platforms_url):
    """A stored connection that hit CONNECTION_AUTH_EXPIRED is paused; later calls do not reach the platform."""
    from app.db.tables import ConnectionRow
    from app.reliability.secrets import encrypt_credentials

    app = api.app
    async with app.state.session_factory() as session, session.begin():
        session.add(ConnectionRow(id="conn_test_1", connector_key=SAMPLE_KEY, auth_type="api_key",
                                  secret_enc=encrypt_credentials(app.state.settings.fernet_key(), {"token": "stored-token-abc"})))
    body = publish_body(mock_platforms_url, "t-pause", "k-pause")
    body["connection"] = {"connection_id": "conn_test_1"}
    first = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=body, headers={"X-Sim-Fault": "401"})
    assert first.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    assert first.json()["error"]["details"]["connection_state"] == "paused"
    hits = creates(mock_platforms_url, "t-pause")
    second = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json={**body, "idempotency_key": "k-pause-2"})
    assert second.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    assert creates(mock_platforms_url, "t-pause") == hits


async def test_secrets_never_in_responses(api, mock_platforms_url):
    bad = {"rendition": {"title": 5}, "destination": "nope", "connection": CONNECTION, "idempotency_key": "k"}
    response = await api.post(f"/connector/v1/{SAMPLE_KEY}/publish", json=bad)
    assert response.status_code == 422
    assert "canary-SECRET-7f3a91" not in response.text
    error = await api.post(
        f"/connector/v1/{SAMPLE_KEY}/publish",
        json=publish_body(mock_platforms_url, "t-sec", "k-sec"),
        headers={"X-Sim-Fault": "500"},
    )
    assert "canary-SECRET-7f3a91" not in error.text
