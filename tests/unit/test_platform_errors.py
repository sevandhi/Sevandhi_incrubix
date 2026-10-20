"""IS5 error envelope on every path (no database needed), auth, correlation ids, and the contract router's checks."""

from __future__ import annotations

import httpx
import pytest

from app.errors import CATALOGUE, ErrorCode, IbxError
from app.main import create_app
from app.storage import MemoryStorage
from tests.conftest import SAMPLE_KEY, TEST_TOKEN


def assert_envelope(response: httpx.Response, code: str, status: int) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["retryable"], bool) and isinstance(body["error"]["message"], str)
    return body["error"]


@pytest.fixture
def app(make_settings, sample_registry):
    application = create_app(make_settings(DISABLED_CONNECTORS="other"), storage=MemoryStorage(), registry=sample_registry)

    @application.get("/api/v1/_boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter22-secret")

    @application.get("/api/v1/_limited")
    async def limited() -> None:
        raise IbxError(ErrorCode.PLATFORM_RATE_LIMIT, details={"retry_after_s": 42})

    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c


async def test_unknown_route_404(client):
    assert_envelope(await client.get("/api/v1/nope"), "NOT_FOUND", 404)


async def test_wrong_method_405(client):
    assert_envelope(await client.delete("/api/v1/errors"), "HTTP_METHOD_NOT_ALLOWED", 405)


async def test_unhandled_exception_500_without_leaking(client):
    response = await client.get("/api/v1/_boom", headers={"X-Correlation-Id": "corr-500"})
    error = assert_envelope(response, "INTERNAL_ERROR", 500)
    assert "hunter22" not in response.text
    assert error["details"]["correlation_id"] == "corr-500"
    assert response.headers["X-Correlation-Id"] == "corr-500"


async def test_rate_limit_error_sets_retry_after(client):
    response = await client.get("/api/v1/_limited")
    assert_envelope(response, "PLATFORM_RATE_LIMIT", 429)
    assert response.headers["Retry-After"] == "42"


async def test_auth_required_and_open_health(client, app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as anon:
        response = await anon.get("/api/v1/errors")
        assert_envelope(response, "UNAUTHORIZED", 401)
        assert_envelope(await anon.post(f"/connector/v1/{SAMPLE_KEY}/validate", json={}), "UNAUTHORIZED", 401)
        assert (await anon.get("/health")).status_code == 200
        bad = await anon.get("/api/v1/errors", headers={"Authorization": "Bearer wrong"})
        assert_envelope(bad, "UNAUTHORIZED", 401)


async def test_correlation_id_echoed_or_created(client):
    echoed = await client.get("/health", headers={"X-Correlation-Id": "abc-123"})
    assert echoed.headers["X-Correlation-Id"] == "abc-123"
    created = await client.get("/health")
    assert len(created.headers["X-Correlation-Id"]) == 32
    injected = await client.get("/health", headers={"X-Correlation-Id": "bad id\twith spaces"})
    assert injected.headers["X-Correlation-Id"] != "bad id\twith spaces"


async def test_error_catalogue_lists_every_code(client):
    body = (await client.get("/api/v1/errors")).json()
    assert {c["code"] for c in body["codes"]} == {c.value for c in ErrorCode}
    assert all(c["code"] in ErrorCode.__members__ for c in body["codes"])
    assert set(CATALOGUE) == set(ErrorCode)


async def test_validation_error_never_echoes_input(client):
    body = {"rendition": {"title": ["not", "a", "string"]}, "destination": {},
            "connection": {"credentials": {"token": {"nested": "SUPER-SECRET-TOKEN-1"}}}}
    response = await client.post(f"/connector/v1/{SAMPLE_KEY}/validate", json=body)
    error = assert_envelope(response, "REQUEST_VALIDATION_FAILED", 422)
    assert "SUPER-SECRET-TOKEN-1" not in response.text
    assert all(set(e) == {"loc", "type", "msg"} for e in error["details"]["errors"])
    assert any(e["loc"][:3] == ["body", "rendition", "title"] for e in error["details"]["errors"])


async def test_malformed_json_is_validation_error(client):
    response = await client.post(
        f"/connector/v1/{SAMPLE_KEY}/validate", content=b'{"token": "LEAK-ME-PLEASE", ', headers={"Content-Type": "application/json"}
    )
    assert_envelope(response, "REQUEST_VALIDATION_FAILED", 422)
    assert "LEAK-ME-PLEASE" not in response.text


async def test_unknown_connector_and_method(client):
    assert_envelope(await client.post("/connector/v1/nosuch/validate", json={}), "CONNECTOR_NOT_FOUND", 404)
    assert_envelope(await client.post(f"/connector/v1/{SAMPLE_KEY}/frobnicate", json={}), "METHOD_NOT_SUPPORTED", 404)
    assert_envelope(await client.get("/connector/v1/nosuch/manifest"), "CONNECTOR_NOT_FOUND", 404)


async def test_capability_not_listed_is_404_before_body_validation(client):
    # the template lists publish/status/delete/metrics, not refresh_auth or assist
    error = assert_envelope(await client.post(f"/connector/v1/{SAMPLE_KEY}/refresh_auth", json={}), "METHOD_NOT_SUPPORTED", 404)
    assert error["details"]["capabilities"] == ["publish", "status", "delete", "metrics"]
    assert_envelope(await client.post(f"/api/v1/connector/{SAMPLE_KEY}/build_assist_pack", json={}), "METHOD_NOT_SUPPORTED", 404)
    assert_envelope(await client.post(f"/connector/v1/{SAMPLE_KEY}/assist", json={}), "METHOD_NOT_SUPPORTED", 404)


async def test_manifest_served_with_appendix_keys_only(client):
    manifest = (await client.get(f"/connector/v1/{SAMPLE_KEY}/manifest")).json()
    assert set(manifest) == {"key", "version", "tier", "auth", "surfaces", "rate_limits", "capabilities", "error_map", "instance_param"}
    assert (await client.get(f"/api/v1/connector/{SAMPLE_KEY}/manifest")).json() == manifest


async def test_validate_needs_no_database_and_lists_all_violations(client):
    body = {
        "rendition": {"title": "t" * 250, "body": "b" * 10001, "content_type": "video"},
        "destination": {"surface": "post", "instance": {"base_url": "http://x"}},
    }
    response = await client.post(f"/connector/v1/{SAMPLE_KEY}/validate", json=body, headers={"X-IncuBrix-Verbose": "1"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert {v["field"] for v in data["violations"]} >= {"title", "body"}
    assert data["_meta"]["connector"] == SAMPLE_KEY
    plain = await client.post(f"/connector/v1/{SAMPLE_KEY}/validate", json=body)
    assert set(plain.json()) == {"ok", "violations"}


async def test_unknown_surface_is_spec_violation(client):
    body = {"rendition": {"body": "x"}, "destination": {"surface": "story", "instance": {"base_url": "http://x"}}}
    error = assert_envelope(await client.post(f"/connector/v1/{SAMPLE_KEY}/validate", json=body), "DEST_SPEC_VIOLATION", 422)
    assert error["details"]["violations"][0]["field"] == "surface"


async def test_env_kill_switch(make_settings, sample_registry):
    app = create_app(make_settings(DISABLED_CONNECTORS=f"{SAMPLE_KEY}, other"), storage=MemoryStorage(), registry=sample_registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        body = {"rendition": {"body": "x"}, "destination": {"instance": {"base_url": "http://x"}}}
        error = assert_envelope(await c.post(f"/connector/v1/{SAMPLE_KEY}/validate", json=body), "CONNECTOR_DISABLED", 503)
        assert error["retryable"] is False


async def test_api_rate_limit_per_ip(make_settings, sample_registry):
    app = create_app(make_settings(RATE_LIMIT_PER_MIN=3), storage=MemoryStorage(), registry=sample_registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        statuses = [(await c.get("/api/v1/errors")).status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        response = await c.get("/api/v1/errors")
        assert_envelope(response, "API_RATE_LIMITED", 429)
        assert int(response.headers["Retry-After"]) >= 1
        assert (await c.get("/health")).status_code == 200  # health is never limited
