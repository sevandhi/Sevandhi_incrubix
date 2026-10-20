"""Ghost connector against the REAL local Ghost 6.70 in the compose stack (``-m live_local``).

Run: ``docker compose up -d --wait`` then ``uv run pytest -m live_local tests/integration/test_ghost_live.py``.
Posts go only to the local Ghost (internal network, no newsletter) and are deleted at once. The connection
carries no credentials: the connector uses the bootstrap's dev-only Admin key for its own instance.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from tests.integration._live_stack import PLATFORM_TIMEOUT_S, api_client, in_stack, rendition, require_stack, toxic

pytestmark = pytest.mark.live_local

KEY = "ghost"
DIRECT = "http://ghost:2368"
PROXIED = "http://toxiproxy:12368"  # the same Ghost behind toxiproxy
CONN = {"connector_key": KEY, "auth_type": "api_key"}

# Counts posts on the real Ghost whose slug ends with a marker, signing the token like the connector does.
GHOST_COUNT = (
    "import json, time, httpx\n"
    "from app.connectors.ghost.connector import admin_token, split_admin_key\n"
    "cfg = json.load(open('/bootstrap/out/ghost.json'))\n"
    "token, _ = admin_token(*split_admin_key(cfg['admin_api_key']), time.time())\n"
    "r = httpx.get(cfg['base_url'] + '/ghost/api/admin/posts/', params={'filter': \"slug:~$'%s'\", 'limit': 'all'},"
    " headers={'Authorization': 'Ghost ' + token})\n"
    "print(json.dumps([p['id'] for p in r.json()['posts']]))"
)


@pytest.fixture(autouse=True)
def _stack() -> None:
    require_stack(KEY)


def dest(url: str = DIRECT, **extra: Any) -> dict[str, Any]:
    return {"connector_key": KEY, "surface": "article", "instance": {"base_url": url}, "rules": {"automation_allowed": True},
            **extra}


def publish_body(title: str, url: str = DIRECT, **extra: Any) -> dict[str, Any]:
    return {"rendition": rendition(title, "Live **markdown** body."), "destination": dest(url, **extra),
            "connection": CONN, "idempotency_key": f"live-ghost-{uuid.uuid4().hex[:12]}"}


def http_status(url: str) -> int:
    return in_stack(f"import httpx, json; print(json.dumps(httpx.get({url!r}, timeout=60).status_code))")


async def delete(client, post_id: str, url: str = DIRECT) -> dict[str, Any]:
    response = await client.post(f"/connector/v1/{KEY}/delete", headers={"X-IncuBrix-Verbose": "1"},
                                 json={"platform_post_id": post_id, "connection": CONN, "destination": dest(url)})
    assert response.status_code == 200, response.text
    return response.json()


async def test_publish_status_delete_on_real_ghost():
    async with api_client() as client:
        published = await client.post(f"/connector/v1/{KEY}/publish", json=publish_body("IBX live Ghost roundtrip"))
        assert published.status_code == 200, published.text
        post = published.json()
        try:
            assert post["status"] == "published" and post["live_url"].startswith(f"{DIRECT}/ibx-live-ghost-roundtrip-ibx-")
            assert http_status(post["live_url"]) == 200  # the public page is up
            ref = {"platform_post_id": post["platform_post_id"], "connection": CONN, "destination": dest()}
            status = await client.post(f"/connector/v1/{KEY}/get_status", json=ref)
            assert status.json() == {"status": "published", "live_url": post["live_url"]}
        finally:
            deleted = await delete(client, post["platform_post_id"])
        assert deleted["deleted"] is True and deleted["_meta"]["delete_mode"] == "permanent"
        assert (await client.post(f"/connector/v1/{KEY}/get_status", json=ref)).json() == {"status": "deleted", "live_url": None}
        assert http_status(post["live_url"]) == 404


async def test_refresh_auth_on_real_ghost():
    async with api_client() as client:
        response = await client.post(f"/connector/v1/{KEY}/refresh_auth", json={"connection": CONN, "destination": dest()})
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"expires_at"} and response.json()["expires_at"] is not None


async def test_timeout_after_the_post_was_sent_then_retry_finds_the_same_post():
    """HT1 on the real Ghost: toxiproxy holds the answer past PLATFORM_TIMEOUT_S; the post exists anyway."""
    body = publish_body("IBX live Ghost timeout", PROXIED)
    async with api_client() as client:
        toxic("ghost_proxy", int((PLATFORM_TIMEOUT_S + 3) * 1000))
        try:
            first = await client.post(f"/connector/v1/{KEY}/publish", json=body)
        finally:
            toxic("ghost_proxy", None)
        assert first.status_code == 503, first.text
        assert first.json()["error"]["details"]["intent_state"] == "UNKNOWN"

        retry = await client.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
        assert retry.status_code == 200, retry.text
        marker = retry.json()["_meta"]["marker"]
        assert retry.json()["_meta"].get("reconciled") is True
        assert in_stack(GHOST_COUNT % marker) == [retry.json()["platform_post_id"]]  # exactly one post on Ghost
        await delete(client, retry.json()["platform_post_id"], PROXIED)
        assert in_stack(GHOST_COUNT % marker) == []


async def test_toxiproxy_latency_below_the_timeout_still_publishes():
    toxic("ghost_proxy", 1500)
    try:
        async with api_client() as client:
            started = time.monotonic()
            published = await client.post(f"/connector/v1/{KEY}/publish", json=publish_body("IBX live Ghost latency", PROXIED))
            assert published.status_code == 200, published.text
            assert time.monotonic() - started >= 1.5
    finally:
        toxic("ghost_proxy", None)
    async with api_client() as client:
        await delete(client, published.json()["platform_post_id"])


async def test_wrong_secret_is_auth_expired():
    key_id = in_stack("import json; print(json.dumps(json.load(open('/bootstrap/out/ghost.json'))['admin_api_key'].split(':')[0]))")
    body = publish_body("IBX live Ghost bad key")
    body["connection"] = {**CONN, "credentials": {"admin_api_key": f"{key_id}:{'00' * 32}"}}
    async with api_client() as client:
        response = await client.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == "CONNECTION_AUTH_EXPIRED" and error["details"]["platform_code"] == "INVALID_JWT"
    assert "clock" in error["message"]


async def test_over_long_title_rejected_by_real_ghost():
    body = publish_body("T" * 256)
    async with api_client() as client:
        refused = await client.post(f"/connector/v1/{KEY}/publish", json=body)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
        body["destination"]["spec_overrides"] = {"title_max_chars": 300}
        body["idempotency_key"] += "-override"
        rejected = await client.post(f"/connector/v1/{KEY}/publish", json=body)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
    assert "255" in rejected.json()["error"]["message"] or "Validation" in rejected.json()["error"]["message"]
