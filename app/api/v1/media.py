"""Files in (IS7): ``POST /api/v1/media`` stores one media file in S3.

* ``multipart/form-data`` with a ``file`` field, or JSON ``{"url": "https://..."}`` (downloaded by the service);
* limits: ``MEDIA_MAX_BYTES`` and ``MEDIA_ALLOWED_MIME``; the type is taken from the file's magic bytes, never
  from the client's claim alone;
* stored content-addressed at ``media/<sha256>`` (the same file twice is stored once);
* the answer has ``media_id`` (use it in ``POST /api/v1/content`` media) and a signed download ``url``
  (``/api/v1/files/...``) that works through the one published API port.
URL downloads go through ``app.reliability.safe_fetch`` in every mode (no SSRF into the internal network).
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.catalogue.service import media_key
from app.errors import ErrorCode, IbxError
from app.reliability.safe_fetch import read_media_url
from app.storage.links import signed_download_url

router = APIRouter(prefix="/api/v1/media", tags=["media"])

_MAGIC: list[tuple[bytes, int, str]] = [
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
    (b"%PDF-", 0, "application/pdf"),
    (b"\x1a\x45\xdf\xa3", 0, "video/webm"),
    (b"ftyp", 4, "video/mp4"),
]


class MediaUrlIn(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=2000)
    alt: str | None = Field(default=None, max_length=1000)


def sniff_mime(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for magic, offset, mime in _MAGIC:
        if data[offset: offset + len(magic)] == magic:
            return mime
    return None


def _too_big(limit: int) -> IbxError:
    return IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"The file is larger than the {limit} byte limit.", http_status=413,
                    details={"limit_bytes": limit})


async def _download(request: Request, url: str, limit: int) -> tuple[bytes, str | None]:
    """Fetch a caller's URL through the one SSRF guard (every mode, every redirect hop; own links from storage)."""
    state = request.app.state
    fetched = await read_media_url(url, state.settings, state.storage, max_bytes=limit, now=state.clock.now())
    return fetched.data, fetched.mime


@router.post("", status_code=201, summary="Upload a media file (multipart 'file') or fetch one from {url}",
             openapi_extra={"requestBody": {"required": True, "content": {
                 "multipart/form-data": {"schema": {"type": "object", "properties": {
                     "file": {"type": "string", "format": "binary"}, "alt": {"type": "string"}}, "required": ["file"]}},
                 "application/json": {"schema": MediaUrlIn.model_json_schema()}}}})
async def upload_media(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    limit = settings.MEDIA_MAX_BYTES
    ctype = request.headers.get("content-type", "").lower()
    filename, alt, source = None, None, "upload"
    if ctype.startswith("multipart/form-data"):
        form = await request.form(max_files=1)
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "multipart upload needs a 'file' field.",
                           details={"errors": [{"loc": ["body", "file"], "type": "missing", "msg": "Field required"}]})
        data = await upload.read(limit + 1)
        if len(data) > limit:
            raise _too_big(limit)
        filename, declared = upload.filename, upload.content_type
        alt = form.get("alt") if isinstance(form.get("alt"), str) else None
    else:
        try:
            body = MediaUrlIn.model_validate_json(await request.body())
        except ValueError:
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "Send multipart 'file' or JSON {\"url\": \"https://...\"}.",
                           details={"errors": [{"loc": ["body", "url"], "type": "value_error", "msg": "http(s) URL required"}]}) from None
        data, declared = await _download(request, body.url, limit)
        filename, alt, source = urlsplit(body.url).path.rsplit("/", 1)[-1] or None, body.alt, "url"
    if not data:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "The file is empty.")
    mime = sniff_mime(data)
    if mime is None or mime not in settings.MEDIA_ALLOWED_MIME:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"Unsupported media type ({mime or 'unknown'}).", http_status=415,
                       details={"allowed": settings.MEDIA_ALLOWED_MIME, "detected": mime, "declared": declared})
    sha = hashlib.sha256(data).hexdigest()
    storage = request.app.state.storage
    key = media_key(sha)
    if await storage.stat(key) is None:
        await storage.put_bytes(key, data, mime)
    out: dict[str, Any] = {
        "media_id": sha, "key": key, "mime": mime, "bytes": len(data), "sha256": sha, "source": source,
        "filename": filename, "alt": alt, "declared_mime": declared,
        "url": signed_download_url(key, settings, now=request.app.state.clock.now()),
        "url_expires_in_s": settings.FILE_LINK_EXPIRY_S,
    }
    return JSONResponse(out, status_code=201)
