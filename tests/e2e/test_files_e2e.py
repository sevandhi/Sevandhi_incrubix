"""IS7 on the stack: upload a media file, then open the returned signed link exactly as given (host, one API port)."""

from __future__ import annotations

import httpx

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


def test_upload_and_download_through_the_api_port(stack):
    media = stack.call("POST", "/api/v1/media", files={"file": ("e2e.png", PNG, "image/png")})
    assert media.status_code == 201, media.text
    url = media.json()["url"]
    assert url.startswith(str(stack.client.base_url).rstrip("/") + "/api/v1/files/media/"), url
    download = httpx.get(url, timeout=30)  # no bearer token: the signature is the credential
    assert download.status_code == 200 and download.content == PNG
    assert httpx.get(url.replace("sig=", "sig=00"), timeout=30).status_code == 403
    stack.log.append(f"GET {url.split('?')[0]}?exp=...&sig=... (no token) -> {download.status_code}, {len(download.content)} bytes\n")
    stack.write_evidence("IS7_files_in_out.log")
