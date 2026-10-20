"""Lemmy connector against the REAL local Lemmy 0.19.20 in the compose stack (``-m live_local``).

Run: ``docker compose up -d --wait`` then ``uv run pytest -m live_local tests/integration/test_lemmy_live.py``.
Posts go only to the local community ``ibx_test`` (federation off, internal network) and are purged at once.
The connection carries no credentials: the connector uses the bootstrap's dev-only login for its own instance.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest

from tests.integration._live_stack import PLATFORM_TIMEOUT_S, api_client, in_stack, rendition, require_stack, toxic

pytestmark = pytest.mark.live_local

KEY = "lemmy"
DIRECT = "http://lemmy:8536/c/ibx_test"
PROXIED = "http://toxiproxy:18536/c/ibx_test"  # the same Lemmy behind toxiproxy (latency / timeouts)
CONN = {"connector_key": KEY, "auth_type": "api_key"}  # no credentials: the local bootstrap login is used

LEMMY_PRELUDE = (
    "import json, httpx; cfg = json.load(open('/bootstrap/out/lemmy.json')); B = cfg['base_url'];"
    "H = {'Authorization': 'Bearer ' + cfg['jwt']};"
)


@pytest.fixture(autouse=True)
def _stack() -> None:
    require_stack(KEY)


def dest(url: str = DIRECT, **instance: Any) -> dict[str, Any]:
    return {"connector_key": KEY, "surface": "post", "instance": {"community_url": url, **instance},
            "rules": {"automation_allowed": True}}


def publish_body(title: str, url: str = DIRECT, **instance: Any) -> dict[str, Any]:
    return {"rendition": rendition(title), "destination": dest(url, **instance), "connection": CONN,
            "idempotency_key": f"live-lemmy-{uuid.uuid4().hex[:12]}"}


def posts_with(text: str) -> list[int]:
    """Ids of the admin's newest posts whose body contains ``text`` (counted on the real Lemmy)."""
    code = LEMMY_PRELUDE + (
        "r = httpx.get(B + '/api/v3/user', params={'username': cfg['admin_username'], 'sort': 'New', 'limit': 50}, headers=H);"
        f"print(json.dumps([v['post']['id'] for v in r.json()['posts'] if {text!r} in (v['post'].get('body') or '')]))"
    )
    return in_stack(code)


def community_id() -> int:
    return in_stack("import json; print(json.dumps(json.load(open('/bootstrap/out/lemmy.json'))['communities']['ibx_test']))")


async def delete(client, post_id: str, url: str = DIRECT) -> dict[str, Any]:
    response = await client.post(f"/connector/v1/{KEY}/delete", headers={"X-IncuBrix-Verbose": "1"},
                                 json={"platform_post_id": post_id, "connection": CONN, "destination": dest(url)})
    assert response.status_code == 200, response.text
    return response.json()


async def test_publish_status_metrics_delete_on_real_lemmy():
    async with api_client() as client:
        published = await client.post(f"/connector/v1/{KEY}/publish", json=publish_body("IBX live roundtrip"))
        assert published.status_code == 200, published.text
        post = published.json()
        try:
            assert post["status"] == "published"
            assert post["live_url"] == f"http://lemmy:8536/post/{post['platform_post_id']}"
            # Measured: once Lemmy starts with federation already off (every start after the bootstrap), it serves
            # no ActivityPub routes, so the ap_id answers 404 inside the sandbox; the API is the live check here.
            # On a public instance the same ap_id is the post's page (lemmy-ui).
            post_id = post["platform_post_id"]
            api_status = in_stack("import httpx, json; print(json.dumps(httpx.get('http://lemmy:8536/api/v3/post', "
                                  f"params={{'id': {post_id}}}).status_code))")
            assert api_status == 200

            ref = {"platform_post_id": post["platform_post_id"], "connection": CONN, "destination": dest()}
            status = await client.post(f"/connector/v1/{KEY}/get_status", json=ref)
            assert status.json() == {"status": "published", "live_url": post["live_url"]}
            metrics = (await client.post(f"/connector/v1/{KEY}/fetch_metrics", json=ref)).json()
            assert metrics["likes"] == 1 and metrics["comments"] == 0 and metrics["views"] is None
        finally:
            deleted = await delete(client, post["platform_post_id"])
        assert deleted["deleted"] is True and deleted["_meta"]["delete_mode"] == "purged"
        after = await client.post(f"/connector/v1/{KEY}/get_status", json=ref)
        assert after.json() == {"status": "deleted", "live_url": None}  # purged: couldnt_find_post


async def test_toxiproxy_latency_below_the_timeout_still_publishes_once():
    body = publish_body("IBX live latency", PROXIED, community_id=community_id())
    toxic("lemmy_proxy", 1500)
    try:
        async with api_client() as client:
            started = time.monotonic()
            published = await client.post(f"/connector/v1/{KEY}/publish", json=body)
            elapsed = time.monotonic() - started
            assert published.status_code == 200, published.text
            assert elapsed >= 1.5  # the toxic really delayed the answer
    finally:
        toxic("lemmy_proxy", None)
    async with api_client() as client:
        await delete(client, published.json()["platform_post_id"])


async def test_timeout_after_the_post_was_sent_then_retry_finds_the_same_post():
    """HT1 on the real Lemmy: toxiproxy holds the answer past PLATFORM_TIMEOUT_S; the post exists anyway."""
    body = publish_body("IBX live timeout", PROXIED, community_id=community_id())
    async with api_client() as client:
        toxic("lemmy_proxy", int((PLATFORM_TIMEOUT_S + 3) * 1000))
        try:
            first = await client.post(f"/connector/v1/{KEY}/publish", json=body)
        finally:
            toxic("lemmy_proxy", None)
        assert first.status_code == 503, first.text
        error = first.json()["error"]
        assert error["code"] == "PLATFORM_UNAVAILABLE" and error["details"]["intent_state"] == "UNKNOWN"

        retry = await client.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
        assert retry.status_code == 200, retry.text
        assert retry.json()["_meta"].get("reconciled") is True
        marker = retry.json()["_meta"]["marker"]
        assert posts_with(f"({marker})") == [int(retry.json()["platform_post_id"])]  # exactly one post on Lemmy
        await delete(client, retry.json()["platform_post_id"], PROXIED)
        assert posts_with(f"({marker})") == []


async def test_logged_out_token_is_auth_expired():
    code = LEMMY_PRELUDE + (
        "t = httpx.post(B + '/api/v3/user/login', json={'username_or_email': cfg['admin_username'], 'password': cfg['admin_password']}).json()['jwt'];"
        "httpx.post(B + '/api/v3/user/logout', headers={'Authorization': 'Bearer ' + t}); print(json.dumps(t))"
    )
    revoked = in_stack(code)
    body = publish_body("IBX live revoked token")
    body["connection"] = {**CONN, "credentials": {"jwt": revoked}}
    async with api_client() as client:
        response = await client.post(f"/connector/v1/{KEY}/publish", json=body)
    assert response.status_code == 424, response.text
    error = response.json()["error"]
    assert error["code"] == "CONNECTION_AUTH_EXPIRED" and error["details"]["platform_status"] == 401
    assert revoked not in response.text


async def test_content_rejected_by_real_lemmy():
    body = publish_body("T" * 201)
    async with api_client() as client:
        refused = await client.post(f"/connector/v1/{KEY}/publish", json=body)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
        body["destination"]["spec_overrides"] = {"title_max_chars": 300}
        body["idempotency_key"] += "-override"
        rejected = await client.post(f"/connector/v1/{KEY}/publish", json=body)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
    assert rejected.json()["error"]["details"]["platform_message"] == "invalid_post_title"


def _set_post_limit(count: int, per_second: int) -> None:
    """PUT the site's post rate limit, then wait until GET /api/v3/site shows it (Lemmy caches the site)."""
    code = LEMMY_PRELUDE + (
        "import time;"
        f"r = httpx.put(B + '/api/v3/site', json={{'rate_limit_post': {count}, 'rate_limit_post_per_second': {per_second}}}, headers=H);"
        "ok = False\n"
        "for _ in range(30):\n"
        "    lim = httpx.get(B + '/api/v3/site').json()['site_view']['local_site_rate_limit']\n"
        f"    ok = (lim['post'], lim['post_per_second']) == ({count}, {per_second})\n"
        "    if ok: break\n"
        "    time.sleep(1)\n"
        "print(json.dumps([r.status_code, ok]))"
    )
    assert in_stack(code) == [200, True]


async def test_real_rate_limit_error_gets_its_reset_from_the_site_config():
    """HT2 Lemmy variant on the real instance: 400 rate_limit_error -> PLATFORM_RATE_LIMIT, reset from the site config.

    The post limit is lowered to 2 per 20 s, so one slot frees up after 10 s; afterwards it is restored.
    """
    created: list[str] = []
    _set_post_limit(2, 20)
    try:
        async with api_client() as client:
            limited = None
            for attempt in range(6):
                response = await client.post(f"/connector/v1/{KEY}/publish", json=publish_body(f"IBX live rate {attempt}"))
                if response.status_code == 200:
                    created.append(response.json()["platform_post_id"])
                    continue
                limited = response
                break
            assert limited is not None, "Lemmy never answered rate_limit_error"
            assert limited.status_code == 429, limited.text
            error = limited.json()["error"]
            assert error["code"] == "PLATFORM_RATE_LIMIT" and error["retryable"] is True
            assert error["details"]["error_map_key"] == "400:rate_limit_error"
            assert error["details"]["reset_source"] == "lemmy_site_config" and error["details"]["retry_after_s"] == 10

            blocked = await client.post(f"/connector/v1/{KEY}/publish", json=publish_body("IBX live rate blocked"))
            assert blocked.status_code == 429 and blocked.json()["error"]["details"]["short_circuit"] is True

            time.sleep(error["details"]["retry_after_s"] + 1)  # the computed reset: one slot is free again
            after = await client.post(f"/connector/v1/{KEY}/publish", json=publish_body("IBX live rate after reset"))
            assert after.status_code == 200, after.text
            created.append(after.json()["platform_post_id"])
    finally:
        _set_post_limit(1000, 60)
        async with api_client() as client:
            for post_id in created:
                await delete(client, post_id)


def test_federation_is_off_on_the_local_instance():
    site = in_stack(LEMMY_PRELUDE + "print(json.dumps(httpx.get(B + '/api/v3/site', headers=H).json()['site_view']['local_site']['federation_enabled']))")
    assert site is False
    assert json.dumps(site) == "false"
