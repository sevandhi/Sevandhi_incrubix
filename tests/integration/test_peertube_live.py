"""live_local: the PeerTube connector against the REAL local PeerTube (compose profile `video`).

    docker compose -f compose.yaml -f deploy/compose/peertube.yaml --profile video up -d --build --wait
    uv run pytest -m live_local tests/integration/test_peertube_live.py

Everything goes through the published API port (the contract), with the instance's dev-only root login
(IBX_PEERTUBE_ROOT_PASSWORD, default = the compose placeholder). The instance is on the internal-only network.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.integration._live_stack import api_client, in_stack, require_stack

pytestmark = pytest.mark.live_local

BASE = "http://peertube.ibx.test:9000"
CONN = {"connector_key": "peertube", "auth_type": "oauth2",
        "credentials": {"username": "root", "password": os.environ.get("IBX_PEERTUBE_ROOT_PASSWORD", "ibx-local-dev-only-peertube")}}
DEST = {"connector_key": "peertube", "surface": "video", "rules": {"automation_allowed": True}, "instance": {"base_url": BASE}}


def _require_peertube() -> None:
    require_stack("peertube")
    code = (f"import httpx,json\ntry: print(json.dumps(httpx.get('{BASE}/api/v1/ping', timeout=5).status_code))\n"
            "except Exception: print(json.dumps(0))")
    if in_stack(code) != 200:
        pytest.skip("the local PeerTube (profile video) is not running")


def _page(url: str) -> dict:
    code = f"import httpx,json; r=httpx.get('{url}', timeout=10); print(json.dumps({{'status': r.status_code, 'text': r.text[:20000]}}))"
    return in_stack(code)


async def test_real_peertube_roundtrip():
    _require_peertube()
    token = f"ibxlive{uuid.uuid4().hex[:8]}"
    body = {"rendition": {"content_type": "text", "title": f"IncuBrix live test {token}", "body": "Neutral test upload, deleted at once.",
                          "tags": ["test"], "ai_generated": False},
            "destination": DEST, "connection": CONN, "idempotency_key": f"live-{token}"}
    async with api_client() as client:
        published = await client.post("/connector/v1/peertube/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
        assert published.status_code == 200, published.text
        result = published.json()
        assert result["status"] == "published" and result["live_url"].startswith(f"{BASE}/videos/watch/")
        page = _page(result["live_url"])
        assert page["status"] == 200 and token in page["text"]  # og:title on the real watch page
        replay = await client.post("/connector/v1/peertube/publish", json=body)
        assert replay.json()["platform_post_id"] == result["platform_post_id"] and replay.headers.get("Idempotent-Replayed") == "true"

        ref = {"platform_post_id": result["platform_post_id"], "connection": CONN, "destination": DEST}
        assert (await client.post("/connector/v1/peertube/get_status", json=ref)).json()["status"] == "published"
        metrics = (await client.post("/connector/v1/peertube/fetch_metrics", json=ref)).json()
        assert metrics["views"] == 0 and metrics["likes"] == 0 and metrics["comments"] == 0 and metrics["watch_time_s"] == 0
        assert metrics["shares"] is None and metrics["clicks"] is None
        refreshed = await client.post("/connector/v1/peertube/refresh_auth", json={"connection": CONN, "destination": DEST})
        assert refreshed.status_code == 200 and refreshed.json()["expires_at"]

        deleted = await client.post("/connector/v1/peertube/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
        assert deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "deleted"
        assert (await client.post("/connector/v1/peertube/get_status", json=ref)).json() == {"status": "deleted", "live_url": None}
        api_after = _page(f"{BASE}/api/v1/videos/{result['platform_post_id']}")
        assert api_after["status"] == 404
        # Finding (why certification confirms deletion through the API): the web page still answers 200.
        assert _page(result["live_url"])["status"] == 200


async def test_real_peertube_wrong_password_is_auth_expired():
    _require_peertube()
    conn = {**CONN, "credentials": {"username": "root", "password": "definitely-wrong"}}
    body = {"rendition": {"content_type": "text", "title": "never uploaded", "body": "x", "ai_generated": False},
            "destination": DEST, "connection": conn, "idempotency_key": f"live-bad-{uuid.uuid4().hex[:8]}"}
    async with api_client() as client:
        response = await client.post("/connector/v1/peertube/publish", json=body)
    assert response.status_code == 424, response.text
    assert response.json()["error"]["details"]["error_map_key"] == "400:invalid_grant"


async def test_real_peertube_refuses_a_long_name():
    _require_peertube()
    dest = {**DEST, "spec_overrides": {"title_max_chars": 500}}  # let it through our spec check to reach PeerTube
    body = {"rendition": {"content_type": "text", "title": "x" * 130, "body": "x", "ai_generated": False},
            "destination": dest, "connection": CONN, "idempotency_key": f"live-long-{uuid.uuid4().hex[:8]}"}
    async with api_client() as client:
        response = await client.post("/connector/v1/peertube/publish", json=body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "PLATFORM_CONTENT_REJECTED" and error["details"]["platform_status"] == 400


async def test_real_peertube_certification_by_one_row_and_a_connection():
    """Local mode: a stored connection (dev-only root login) + one catalogue row -> certification on the real PeerTube.
    Needs the harness to confirm deletion through the API (``live_check_by_api``, proposed harness change): the real watch
    page answers 200 after deletion. Without that diff the run stops at CONFIRM_DELETED and this test is an xfail."""
    import asyncio
    import inspect

    from app.certification import harness

    _require_peertube()
    tag = uuid.uuid4().hex[:8]
    async with api_client() as client:
        conn = await client.post("/api/v1/connections", json={"connector_key": "peertube", "auth_type": "oauth2",
                                                               "credentials": CONN["credentials"], "ref": f"pt-root-{tag}"})
        assert conn.status_code in (200, 201), conn.text
        row = {"name": f"Local PeerTube {tag}", "url": BASE, "connector_key": "peertube", "surface": "video",
               "connection_ref": f"pt-root-{tag}", "instance_channel_id": None}
        row = {k: v for k, v in row.items() if v is not None}
        dest = await client.post("/api/v1/destinations", json=row)
        if dest.status_code == 200 and "DUPLICATE" in str(dest.json()):
            pytest.skip("a destination for this instance already exists on this stack")
        assert dest.status_code == 201, dest.text
        started = await client.post("/api/v1/certifications", json={"destination_id": dest.json()["destination_id"]})
        assert started.status_code == 202, started.text
        for _ in range(240):
            job = (await client.get(f"/api/v1/jobs/{started.json()['job_id']}")).json()
            if job["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.5)
        run = (await client.get(started.json()["run_url"])).json()
    print("certification run:", {k: run.get(k) for k in ("run_id", "state", "level_after")})
    print("steps:", [(s["step"], s["outcome"]) for s in run["steps"]])
    failed = [s for s in run["steps"] if s["outcome"] != "ok"]
    if run["state"] != "PASSED" and "live_check_by_api" not in inspect.getsource(harness.Harness):
        assert failed and failed[0]["step"] == "CONFIRM_DELETED", failed
        pytest.xfail("the harness does not read live_check_by_api yet (proposed harness change)")
    assert run["state"] == "PASSED", failed
    assert run["level_after"] == "L4"
