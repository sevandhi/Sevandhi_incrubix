"""PeerTube connector through the HTTP contract against the PeerTube mock (shaped on a real v8.3.0, see manifest)."""

from __future__ import annotations

import uuid

import httpx
import pytest

CONN = {"connector_key": "peertube", "auth_type": "oauth2", "credentials": {"username": "sim-uploader", "password": "sim-pass"}}


def _dest(mock: str, tenant: str) -> dict:
    return {"connector_key": "peertube", "surface": "video", "rules": {"automation_allowed": True},
            "instance": {"base_url": f"{mock}/peertube/{tenant}"}}


def _body(mock: str, tenant: str, key: str, **overrides) -> dict:
    body = {"rendition": {"content_type": "text", "title": "IncuBrix test card", "body": "A short neutral description.",
                          "tags": ["test", "incubrix"], "language": "en", "ai_generated": False},
            "destination": _dest(mock, tenant), "connection": CONN, "idempotency_key": key}
    body.update(overrides)
    return body


async def _mock_token(mock: str, tenant: str) -> str:
    client = httpx.get(f"{mock}/peertube/{tenant}/api/v1/oauth-clients/local").json()
    token = httpx.post(f"{mock}/peertube/{tenant}/api/v1/users/token",
                       data={**client, "grant_type": "password", "username": "sim-uploader", "password": "sim-pass"}).json()
    return token["access_token"]


async def _my_videos(mock: str, tenant: str) -> list[dict]:
    token = await _mock_token(mock, tenant)
    return httpx.get(f"{mock}/peertube/{tenant}/api/v1/users/me/videos", headers={"Authorization": f"Bearer {token}"}).json()["data"]


async def test_roundtrip_with_metrics_and_refresh(contract_client, mock_platforms_url):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    published = await contract_client.post("/connector/v1/peertube/publish", json=_body(mock_platforms_url, tenant, f"k-{tenant}"),
                                           headers={"X-IncuBrix-Verbose": "1"})
    assert published.status_code == 200, published.text
    result = published.json()
    assert result["status"] == "published" and "/videos/watch/" in result["live_url"]
    assert result["_meta"]["media_source"] == "bundled_test_card"
    [video] = await _my_videos(mock_platforms_url, tenant)
    assert video["description"].endswith(f"[//]: # ({result['_meta']['marker']})")
    assert video["tags"] == ["test", "incubrix"]

    ref = {"platform_post_id": result["platform_post_id"], "connection": CONN, "destination": _dest(mock_platforms_url, tenant)}
    status = await contract_client.post("/connector/v1/peertube/get_status", json=ref)
    assert status.json() == {"status": "published", "live_url": result["live_url"]}

    httpx.post(f"{mock_platforms_url}/peertube/{tenant}/_mock/engage",
               json={"id": result["platform_post_id"], "views": 7, "likes": 2, "comments": 1, "watch_time": 9})
    metrics = await contract_client.post("/connector/v1/peertube/fetch_metrics", json=ref)
    assert metrics.json() == {"views": 7, "likes": 2, "comments": 1, "shares": None, "saves": None, "watch_time_s": 9.0, "clicks": None}

    refreshed = await contract_client.post("/connector/v1/peertube/refresh_auth", json={"connection": CONN, "destination": ref["destination"]})
    assert refreshed.status_code == 200 and refreshed.json()["expires_at"]

    deleted = await contract_client.post("/connector/v1/peertube/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
    assert deleted.json()["deleted"] is True and deleted.json()["_meta"]["delete_mode"] == "deleted"
    assert (await contract_client.post("/connector/v1/peertube/get_status", json=ref)).json() == {"status": "deleted", "live_url": None}
    again = await contract_client.post("/connector/v1/peertube/delete", json=ref, headers={"X-IncuBrix-Verbose": "1"})
    assert again.json()["_meta"]["delete_mode"] == "already_gone"


async def test_video_media_is_fetched_and_uploaded(contract_client, mock_platforms_url):
    """A video rendition uploads its own file (fetched through safe_fetch; the mock host is a local host in tests)."""
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    rendition = {"content_type": "video", "title": "Own clip", "body": "desc", "ai_generated": False,
                 "media": [{"url": f"{mock_platforms_url}/peertube/{tenant}/_static/clip.mp4", "mime": "video/mp4"}]}
    response = await contract_client.post("/connector/v1/peertube/publish",
                                          json=_body(mock_platforms_url, tenant, f"k-{tenant}", rendition=rendition))
    assert response.status_code == 200, response.text
    [video] = await _my_videos(mock_platforms_url, tenant)
    assert video["name"] == "Own clip"
    missing = {**rendition, "media": [{"url": f"{mock_platforms_url}/peertube/{tenant}/_static/none.mp4", "mime": "video/mp4"}]}
    response = await contract_client.post("/connector/v1/peertube/publish",
                                          json=_body(mock_platforms_url, tenant, f"k2-{tenant}", rendition=missing))
    assert response.json()["error"]["code"] == "INPUT_FETCH_FAILED"  # nothing is uploaded when the file can not be read
    assert len(await _my_videos(mock_platforms_url, tenant)) == 1


async def test_video_rendition_without_a_file_is_refused(contract_client, mock_platforms_url):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    rendition = {"content_type": "video", "title": "No file", "body": "desc", "ai_generated": False}
    response = await contract_client.post("/connector/v1/peertube/publish", json=_body(mock_platforms_url, tenant, f"k-{tenant}", rendition=rendition))
    assert response.status_code == 422 and response.json()["error"]["code"] == "DEST_SPEC_VIOLATION"


async def test_timeout_after_create_then_retry_uploads_exactly_once(contract_client, mock_platforms_url):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    body = _body(mock_platforms_url, tenant, f"k-{tenant}")
    first = await contract_client.post("/connector/v1/peertube/publish", json=body, headers={"X-Sim-Fault": "timeout_after_create:4"})
    assert first.status_code == 503 and first.json()["error"]["details"]["intent_state"] == "UNKNOWN"
    retry = await contract_client.post("/connector/v1/peertube/publish", json=body)
    assert retry.status_code == 200 and retry.headers.get("Idempotent-Replayed") == "true"
    assert len(await _my_videos(mock_platforms_url, tenant)) == 1
    counts = httpx.get(f"{mock_platforms_url}/_counts", params={"platform": "peertube", "tenant": tenant}).json()["counts"]
    assert counts["peertube"][tenant]["POST /api/v1/videos/upload"] == 1


async def test_expired_token_is_renewed_transparently(contract_client, mock_platforms_url):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    first = await contract_client.post("/connector/v1/peertube/publish", json=_body(mock_platforms_url, tenant, f"k1-{tenant}"))
    assert first.status_code == 200
    httpx.post(f"{mock_platforms_url}/peertube/{tenant}/_mock/expire_tokens")
    second = await contract_client.post("/connector/v1/peertube/publish", json=_body(mock_platforms_url, tenant, f"k2-{tenant}"))
    assert second.status_code == 200, second.text  # 401 invalid_token -> one re-login -> the upload goes through


async def test_wrong_password_is_auth_expired(contract_client, mock_platforms_url):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    httpx.post(f"{mock_platforms_url}/peertube/{tenant}/_mock/users", json={"username": "alice", "password": "right-one"})
    right = {"connector_key": "peertube", "credentials": {"username": "alice", "password": "right-one"}}
    ok = await contract_client.post("/connector/v1/peertube/publish", json=_body(mock_platforms_url, tenant, f"k0-{tenant}", connection=right))
    assert ok.status_code == 200, ok.text
    # regression (found on the real PeerTube): a token cached for alice must NOT serve a caller with a wrong password
    conn = {"connector_key": "peertube", "credentials": {"username": "alice", "password": "wrong"}}
    response = await contract_client.post("/connector/v1/peertube/publish", json=_body(mock_platforms_url, tenant, f"k-{tenant}", connection=conn))
    assert response.status_code == 424, response.text
    assert response.json()["error"]["details"]["error_map_key"] == "400:invalid_grant"


async def test_revoked_access_token_is_auth_expired(contract_client, mock_platforms_url):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    conn = {"connector_key": "peertube", "credentials": {"access_token": "0123456789abcdef0123456789abcdef01234567"}}
    response = await contract_client.post("/connector/v1/peertube/publish", json=_body(mock_platforms_url, tenant, f"k-{tenant}", connection=conn))
    assert response.status_code == 424 and response.json()["error"]["details"]["error_map_key"] == "401"


async def test_real_shaped_429_is_backed_off(contract_client, mock_platforms_url):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    await _mock_token(mock_platforms_url, tenant)
    httpx.post(f"{mock_platforms_url}/peertube/{tenant}/_mock/rate_limit", json={"count": 50, "retry_after": 10})
    response = await contract_client.post("/connector/v1/peertube/publish", json=_body(mock_platforms_url, tenant, f"k-{tenant}"))
    assert response.status_code == 429, response.text
    details = response.json()["error"]["details"]
    assert details["retry_after_s"] == 10 and response.json()["error"]["code"] == "PLATFORM_RATE_LIMIT"


@pytest.mark.parametrize(("title", "expected"), [("ab", "DEST_SPEC_VIOLATION"), ("x" * 121, "DEST_SPEC_VIOLATION")])
async def test_title_limits_measured_on_real_peertube(contract_client, mock_platforms_url, title, expected):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    body = _body(mock_platforms_url, tenant, f"k-{tenant}")
    body["rendition"]["title"] = title
    response = await contract_client.post("/connector/v1/peertube/publish", json=body)
    assert response.status_code == 422 and response.json()["error"]["code"] == expected


async def test_platform_refusal_is_content_rejected(contract_client, mock_platforms_url):
    tenant = f"p-{uuid.uuid4().hex[:6]}"
    body = _body(mock_platforms_url, tenant, f"k-{tenant}")
    body["rendition"]["title"] = "x" * 130
    body["destination"]["spec_overrides"] = {"title_max_chars": 500}
    response = await contract_client.post("/connector/v1/peertube/publish", json=body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "PLATFORM_CONTENT_REJECTED" and "name" in error["message"]
