"""Discourse connector against the REAL local Discourse (compose profile ``full``). Never a public forum.

Discourse has no host port (it sits on the internal ``ibx_sandbox`` network), so these tests drive the
running API container over its published port, and the API reaches ``http://discourse`` itself.

    IBX_LIVE_API_URL=http://127.0.0.1:<IBX_API_PORT>  IBX_LIVE_API_TOKEN=<API_TOKEN>
    IBX_LIVE_DISCOURSE_JSON=<a copy of /bootstrap/out/discourse.json>   (dev-only generated credentials)
    uv run pytest -m live_local_full tests/integration/test_discourse_live_full.py
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.live_local,
    pytest.mark.live_local_full,
    pytest.mark.skipif(
        not (os.environ.get("IBX_LIVE_API_URL") and os.environ.get("IBX_LIVE_DISCOURSE_JSON")),
        reason="needs the compose stack with --profile full (IBX_LIVE_API_URL, IBX_LIVE_DISCOURSE_JSON)",
    ),
]
KEY = "discourse"


class _Live(dict):
    """Holds the live settings; its repr never shows the dev-only API key (pytest prints fixtures on failure)."""

    def __repr__(self) -> str:
        return "<live discourse settings (redacted)>"


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    info = json.loads(Path(os.environ["IBX_LIVE_DISCOURSE_JSON"]).read_text())
    connection = {"connector_key": KEY, "auth_type": "api_key",
                  "credentials": {"api_key": info["api_key"], "api_username": info["api_username"]}}
    destination = {"connector_key": KEY, "surface": "topic", "rules": {"automation_allowed": True},
                   "instance": {"base_url": info.get("base_url", "http://discourse"), "category_id": info["category_id"]}}
    client = httpx.Client(base_url=os.environ["IBX_LIVE_API_URL"], timeout=60,
                          headers={"Authorization": f"Bearer {os.environ.get('IBX_LIVE_API_TOKEN', '')}"})
    return _Live(connection=connection, destination=destination, client=client)


def _publish(live: dict[str, Any], key: str, title: str) -> httpx.Response:
    body = {"rendition": {"title": title, "body": f"Certification-style test topic for '{title}'. It is deleted at once.",
                          "ai_generated": False},
            "destination": live["destination"], "connection": live["connection"], "idempotency_key": key}
    return live["client"].post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})


def test_real_discourse_round_trip(live):
    nonce = uuid.uuid4().hex[:10]
    key = f"live-discourse-{nonce}"
    title = f"IncuBrix live test topic {nonce} please ignore"
    first = _publish(live, key, title)
    assert first.status_code == 200, first.text
    out = first.json()
    try:
        assert out["status"] == "published" and out["live_url"].startswith("http://discourse/t/")
        replay = _publish(live, key, title)
        assert replay.status_code == 200 and replay.json()["platform_post_id"] == out["platform_post_id"]
        assert replay.headers.get("Idempotent-Replayed") == "true"
        ref = {"platform_post_id": out["platform_post_id"], "connection": live["connection"], "destination": live["destination"]}
        status = live["client"].post(f"/connector/v1/{KEY}/get_status", json=ref)
        assert status.json() == {"status": "published", "live_url": out["live_url"]}
        metrics = live["client"].post(f"/connector/v1/{KEY}/fetch_metrics", json=ref).json()
        assert metrics["comments"] == 0 and metrics["likes"] == 0 and metrics["shares"] is None
    finally:
        ref = {"platform_post_id": out["platform_post_id"], "connection": live["connection"], "destination": live["destination"]}
        deleted = live["client"].post(f"/connector/v1/{KEY}/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "soft"
    status = live["client"].post(f"/connector/v1/{KEY}/get_status", json=ref)
    assert status.json() == {"status": "deleted", "live_url": None}
    again = live["client"].post(f"/connector/v1/{KEY}/delete", json=ref)
    assert again.status_code == 200 and again.json() == {"deleted": True}  # second delete of a soft-deleted topic


def test_real_discourse_errors(live):
    nonce = uuid.uuid4().hex[:10]
    bad = {**live["connection"], "credentials": {"api_key": "0" * 64, "api_username": live["connection"]["credentials"]["api_username"]}}
    body = {"rendition": {"title": f"IncuBrix live auth test {nonce}", "body": "This must be refused before anything is created."},
            "destination": live["destination"], "connection": bad, "idempotency_key": f"live-auth-{nonce}"}
    refused = live["client"].post(f"/connector/v1/{KEY}/publish", json=body)
    assert refused.status_code == 424
    assert refused.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED" and refused.json()["error"]["details"]["platform_status"] == 403

    title = f"IncuBrix live duplicate {nonce} test"
    first = _publish(live, f"live-dup-a-{nonce}", title)
    assert first.status_code == 200, first.text
    try:
        dup = _publish(live, f"live-dup-b-{nonce}", title)
        assert dup.status_code == 422 and dup.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
        assert "already been used" in dup.json()["error"]["details"]["platform_message"]
    finally:
        ref = {"platform_post_id": first.json()["platform_post_id"], "connection": live["connection"], "destination": live["destination"]}
        assert live["client"].post(f"/connector/v1/{KEY}/delete", json=ref).json() == {"deleted": True}
