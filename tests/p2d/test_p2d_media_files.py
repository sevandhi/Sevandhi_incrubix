"""IS7 files in/out: media upload (multipart or URL) with limits, signed download through the API, signed job links."""

from __future__ import annotations

import httpx

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def anon(app) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ibx.test")


async def test_multipart_upload_then_signed_download_without_token(p2d):
    response = await p2d.client.post("/api/v1/media", files={"file": ("pic.png", PNG, "image/png")}, data={"alt": "a pic"})
    assert response.status_code == 201, response.text
    media = response.json()
    assert media["mime"] == "image/png" and media["bytes"] == len(PNG) and media["key"] == f"media/{media['sha256']}"
    assert media["url"].startswith("http://ibx.test/api/v1/files/media/")
    async with anon(p2d.app) as client:
        download = await client.get(media["url"])
        assert download.status_code == 200 and download.content == PNG
        assert download.headers["content-type"] == "image/png"
        tampered = await client.get(media["url"].replace("sig=", "sig=0"))
        assert tampered.status_code == 403
        p2d.clock.advance(4000)
        expired = await client.get(media["url"])
        assert expired.status_code == 410
        unsigned = await client.get(f"/api/v1/files/{media['key']}")
        assert unsigned.status_code in (401, 422)
    again = await p2d.client.post("/api/v1/media", files={"file": ("other-name.png", PNG, "image/png")})
    assert again.json()["media_id"] == media["media_id"]  # content-addressed: stored once


async def test_type_is_sniffed_and_limits_enforced(p2d):
    fake = await p2d.client.post("/api/v1/media", files={"file": ("x.png", b"MZ\x90\x00 not an image", "image/png")})
    assert fake.status_code == 415 and fake.json()["error"]["details"]["detected"] is None
    p2d.app.state.settings.MEDIA_MAX_BYTES = 32
    big = await p2d.client.post("/api/v1/media", files={"file": ("pic.png", PNG, "image/png")})
    assert big.status_code == 413
    empty_form = await p2d.client.post("/api/v1/media", files={"other": ("a", b"x")})
    assert empty_form.status_code == 422


async def test_media_by_url_and_use_in_content(p2d, monkeypatch):
    import app.api.v1.media as media_mod

    async def fake_download(request, url, limit):  # noqa: ANN001, ANN202
        assert url == "https://cdn.example.org/clip.png"
        return PNG, "image/png"

    monkeypatch.setattr(media_mod, "_download", fake_download)
    media = (await p2d.client.post("/api/v1/media", json={"url": "https://cdn.example.org/clip.png"})).json()
    assert media["source"] == "url" and media["filename"] == "clip.png"
    content = await p2d.client.post("/api/v1/content", json={"title": "With media", "body": "Body text here",
                                                             "content_type": "image", "media": [{"media_id": media["media_id"]}]})
    assert content.status_code == 201, content.text
    assert content.json()["media"][0]["key"] == media["key"]
    missing = await p2d.client.post("/api/v1/content", json={"body": "x", "media": [{"media_id": "f" * 64}]})
    assert missing.status_code == 422


async def test_job_result_links_are_signed_api_links(p2d):
    job = (await p2d.client.post("/api/v1/jobs", json={"kind": "selfcheck"})).json()
    await p2d.drain()
    links = (await p2d.job(job["job_id"]))["result"]["links"]
    async with anon(p2d.app) as client:
        for link in links.values():
            assert "/api/v1/files/jobs/" in link
            assert (await client.get(link)).status_code == 200


async def test_signed_link_helpers_round_trip(make_settings):
    from app.storage.links import key_from_download_url, signed_download_url, verify_download

    settings = make_settings(PUBLIC_BASE_URL="http://127.0.0.1:18088")
    url = signed_download_url("certifications/d/r/run.json", settings, expires_s=60)
    key, exp, sig = key_from_download_url(url)
    assert url.startswith("http://127.0.0.1:18088/api/v1/files/") and key == "certifications/d/r/run.json"
    verify_download(key, exp, sig, settings)
    other = make_settings(API_TOKEN="another-token-123456")
    try:
        verify_download(key, exp, sig, other)
        raise AssertionError("a link signed with another key must not verify")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "http_status", None) == 403
