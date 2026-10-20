"""Fault matrix for the Slack and PeerTube connectors: every mock-platforms fault, injected on publish, comes
back as the right IncuBrix code; timeout_after_create + retry leaves exactly ONE post; a renamed response field is
a schema mismatch whose retry reconciles by the marker. (MRSS has no remote platform: its timeout case is in
test_mrss_connector.py.)"""

from __future__ import annotations

import uuid

import httpx
import pytest

CASES = {
    "slack": ({"bot_token": "xoxb-fm-0001"}, lambda mock, t: {"channel_id": "C0FAULTS1", "api_base": f"{mock}/slack/{t}/api"},
              "message", "POST /api/chat.postMessage"),
    "peertube": ({"username": "sim-fm", "password": "sim-pass"}, lambda mock, t: {"base_url": f"{mock}/peertube/{t}"},
                 "video", "POST /api/v1/videos/upload"),
}
EXPECTED = {
    "429:30": (429, "PLATFORM_RATE_LIMIT"),
    "401": (424, "CONNECTION_AUTH_EXPIRED"),
    "422": (422, "PLATFORM_CONTENT_REJECTED"),
    "500": (503, "PLATFORM_UNAVAILABLE"),
    "503": (503, "PLATFORM_UNAVAILABLE"),
    "timeout_after_create:4": (503, "PLATFORM_UNAVAILABLE"),
    "create_then_502": (503, "PLATFORM_UNAVAILABLE"),
}


def _body(key: str, mock: str, tenant: str) -> dict:
    credentials, instance, surface, _ = CASES[key]
    return {
        "rendition": {"content_type": "text", "title": "Fault matrix post", "body": "A neutral body.", "ai_generated": False},
        "destination": {"connector_key": key, "surface": surface, "rules": {"automation_allowed": True}, "instance": instance(mock, tenant)},
        "connection": {"connector_key": key, "credentials": credentials},
        "idempotency_key": f"fm-{key}-{tenant}",
    }


def _creates(mock: str, key: str, tenant: str) -> int:
    counts = httpx.get(f"{mock}/_counts", params={"platform": key, "tenant": tenant}).json()["counts"]
    return counts.get(key, {}).get(tenant, {}).get(CASES[key][3], 0)


@pytest.mark.parametrize("key", list(CASES))
@pytest.mark.parametrize("fault", list(EXPECTED))
async def test_each_fault_maps_and_never_duplicates(contract_client, mock_platforms_url, key, fault):
    tenant = f"fm-{uuid.uuid4().hex[:6]}"
    body = _body(key, mock_platforms_url, tenant)
    response = await contract_client.post(f"/connector/v1/{key}/publish", json=body, headers={"X-Sim-Fault": fault})
    status, code = EXPECTED[fault]
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    if fault.startswith("429"):
        assert error["details"]["retry_after_s"] == 30
    if fault.startswith(("timeout", "create_then")):
        assert error["details"]["intent_state"] == "UNKNOWN"
        retry = await contract_client.post(f"/connector/v1/{key}/publish", json=body)
        assert retry.status_code == 200, retry.text
        assert retry.headers.get("Idempotent-Replayed") == "true"
        assert _creates(mock_platforms_url, key, tenant) == 1  # exactly one post on the platform


@pytest.mark.parametrize(("key", "field"), [("slack", "ts"), ("peertube", "uuid")])
async def test_renamed_field_is_schema_mismatch_and_retry_reconciles(contract_client, mock_platforms_url, key, field):
    tenant = f"fm-{uuid.uuid4().hex[:6]}"
    body = _body(key, mock_platforms_url, tenant)
    response = await contract_client.post(f"/connector/v1/{key}/publish", json=body, headers={"X-Sim-Fault": f"rename_field:{field}->renamed"})
    assert response.status_code == 503, response.text
    details = response.json()["error"]["details"]
    assert details["reason"] == "response_schema_mismatch" and details["intent_state"] == "UNKNOWN"
    retry = await contract_client.post(f"/connector/v1/{key}/publish", json=body)
    assert retry.status_code == 200 and _creates(mock_platforms_url, key, tenant) == 1


async def test_slack_200_error_fault_is_mapped_by_body(contract_client, mock_platforms_url):
    """The generic fault ``200:<token>`` gives HTTP 200 with an error body: mapped by the body, never 'success'."""
    tenant = f"fm-{uuid.uuid4().hex[:6]}"
    for fault, status, code in (("200:token_revoked", 424, "CONNECTION_AUTH_EXPIRED"), ("200:ratelimited", 429, "PLATFORM_RATE_LIMIT"),
                                ("200:msg_too_long", 422, "PLATFORM_CONTENT_REJECTED"), ("200:internal_error", 503, "PLATFORM_UNAVAILABLE")):
        body = _body("slack", mock_platforms_url, f"{tenant}-{fault.split(':')[1]}")
        body["connection"]["credentials"] = {"bot_token": f"xoxb-fm-{fault.split(':')[1]}"}  # its own rate-limit scope
        response = await contract_client.post("/connector/v1/slack/publish", json=body, headers={"X-Sim-Fault": fault})
        assert (response.status_code, response.json()["error"]["code"]) == (status, code), response.text
        if fault == "200:internal_error":  # Slack: "part of the operation may have succeeded" -> unknown, reconcile
            assert response.json()["error"]["details"]["intent_state"] == "UNKNOWN"
