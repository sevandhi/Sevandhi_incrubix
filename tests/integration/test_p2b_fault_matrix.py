"""Fault matrix for the mocks (discourse, forem, partner, assist) and their connectors.

1. Mock level: every fault string of mocks/platforms/faults.py works on every one of these mock routes, and
   ``timeout_after_create`` really creates the item before hanging.
2. Contract level: each fault, injected on publish, comes back as the right IncuBrix error code and HTTP status.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from app.connectors.webhook import signing

DISCOURSE_HEADERS = {"Api-Key": "k", "Api-Username": "ibx_admin"}


def _requests(mock: str, tenant: str) -> dict[str, tuple[str, str, dict[str, Any]]]:
    """A creating request per mock: (method, url, httpx kwargs)."""
    partner_body = json.dumps({"rendition": {"title": "t", "body": "b"}}).encode()
    return {
        "discourse": ("POST", f"{mock}/discourse/{tenant}/posts.json",
                      {"json": {"title": "A long enough topic title", "raw": "A long enough first post body."}, "headers": DISCOURSE_HEADERS}),
        "forem": ("POST", f"{mock}/forem/{tenant}/api/articles",
                  {"json": {"article": {"title": "Hello", "body_markdown": "x", "published": True}}, "headers": {"api-key": "k"}}),
        "partner": ("POST", f"{mock}/partner/{tenant}/posts",
                    {"content": partner_body, "headers": {signing.HEADER: signing.sign("sim-secret", partner_body), "Idempotency-Key": tenant}}),
        "assist": ("POST", f"{mock}/assist/{tenant}/submit", {"data": {"title": "t", "text": "b"}}),
    }


def _exists(mock: str, platform: str, tenant: str) -> bool:
    if platform == "discourse":
        return httpx.get(f"{mock}/discourse/{tenant}/t/1.json", headers=DISCOURSE_HEADERS).status_code == 200
    if platform == "forem":
        return httpx.get(f"{mock}/forem/{tenant}/api/articles/1").status_code == 200
    if platform == "partner":
        return httpx.get(f"{mock}/partner/{tenant}/p/1").status_code == 200
    return httpx.get(f"{mock}/assist/{tenant}/p/1").status_code == 200


PLATFORMS = ["discourse", "forem", "partner", "assist"]


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize(
    "fault, status, error",
    [("429:7", 429, "rate_limited"), ("401", 401, "http_401"), ("403:invalid_access", 403, "invalid_access"),
     ("422", 422, "http_422"), ("500", 500, "http_500"), ("503", 503, "http_503")],
)
def test_status_faults_on_every_mock(mock_platforms_url, platform, fault, status, error):
    tenant = f"fm-{uuid.uuid4().hex[:6]}"
    method, url, kwargs = _requests(mock_platforms_url, tenant)[platform]
    response = httpx.request(method, url, headers={**kwargs.pop("headers", {}), "X-Sim-Fault": fault}, **kwargs)
    assert response.status_code == status and response.json()["error"] == error
    if status == 429:
        assert response.headers["Retry-After"] == "7"
    assert not _exists(mock_platforms_url, platform, tenant)  # a status fault never creates anything


@pytest.mark.parametrize("platform", PLATFORMS)
def test_timeout_after_create_creates_then_hangs(mock_platforms_url, platform):
    tenant = f"fm-{uuid.uuid4().hex[:6]}"
    method, url, kwargs = _requests(mock_platforms_url, tenant)[platform]
    with pytest.raises(httpx.ReadTimeout):
        httpx.request(method, url, headers={**kwargs.pop("headers", {}), "X-Sim-Fault": "timeout_after_create:3"}, timeout=1, **kwargs)
    assert _exists(mock_platforms_url, platform, tenant)


@pytest.mark.parametrize("platform, field", [("discourse", "topic_id"), ("forem", "id"), ("partner", "id")])
def test_rename_field_on_json_mocks(mock_platforms_url, platform, field):
    tenant = f"fm-{uuid.uuid4().hex[:6]}"
    method, url, kwargs = _requests(mock_platforms_url, tenant)[platform]
    response = httpx.request(method, url, headers={**kwargs.pop("headers", {}), "X-Sim-Fault": f"rename_field:{field}->renamed"}, **kwargs)
    assert response.status_code < 300 and field not in response.json() and "renamed" in response.json()


# ------------------------------------------------------------------ through the contract

CASES = {
    "discourse": ({"api_key": "k", "api_username": "ibx_admin"}, "discourse", "base_url", "topic"),
    "devto": ({"api_key": "k"}, "forem", "base_url", "article"),
    "webhook": ({"secret": "sim-secret"}, "partner", "endpoint_url", "post"),
}
EXPECTED = {
    "429:30": (429, "PLATFORM_RATE_LIMIT"),
    "401": (424, "CONNECTION_AUTH_EXPIRED"),
    "422": (422, "PLATFORM_CONTENT_REJECTED"),
    "500": (503, "PLATFORM_UNAVAILABLE"),
    "503": (503, "PLATFORM_UNAVAILABLE"),
    "timeout_after_create:4": (503, "PLATFORM_UNAVAILABLE"),
}


@pytest.mark.parametrize("key", list(CASES))
@pytest.mark.parametrize("fault", list(EXPECTED))
async def test_contract_maps_each_fault(contract_client, mock_platforms_url, key, fault):
    credentials, route, param, surface = CASES[key]
    tenant = f"cm-{uuid.uuid4().hex[:6]}"
    body = {
        "rendition": {"title": "A long enough title for every platform", "body": "A body that is long enough to pass.",
                      "ai_generated": False},
        "destination": {"connector_key": key, "surface": surface, "rules": {"automation_allowed": True},
                        "instance": {param: f"{mock_platforms_url}/{route}/{tenant}"}},
        "connection": {"connector_key": key, "auth_type": "api_key", "credentials": credentials},
        "idempotency_key": f"fm-{key}-{tenant}",
    }
    response = await contract_client.post(f"/connector/v1/{key}/publish", json=body, headers={"X-Sim-Fault": fault})
    status, code = EXPECTED[fault]
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    if fault.startswith("429"):
        assert error["details"]["retry_after_s"] == 30
    if fault.startswith("timeout"):
        assert error["details"]["intent_state"] == "UNKNOWN"
        retry = await contract_client.post(f"/connector/v1/{key}/publish", json=body)
        assert retry.status_code == 200, retry.text  # reconciled, not re-posted
