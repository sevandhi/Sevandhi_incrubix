"""Signed download links served by the API itself (IS7): ``GET /api/v1/files/{key}?exp=<unix>&sig=<hmac>``.

Every "download link" the service hands out (job results, certification evidence, assist packs, uploaded
media) uses this route, so it works from the host through the one published API port; the S3 port is
never published. ``presign`` in ``s3.py`` is unchanged and still available.

    url = signed_download_url("jobs/job_1/report.json", settings)      # absolute, PUBLIC_BASE_URL based
    verify_download(key, exp, sig, settings, now)                      # raises IbxError when bad/expired

The signature is HMAC-SHA256 over ``"<key>\\n<exp>"`` with ``settings.files_signing_key()``.
Connectors build pack links with ``signed_download_url(key, ctx.settings)``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime
from urllib.parse import parse_qs, quote, unquote, urlsplit

from app.config import Settings
from app.errors import ErrorCode, IbxError

ROUTE = "/api/v1/files/"


def _sig(key: str, exp: int, settings: Settings) -> str:
    message = f"{key}\n{exp}".encode()
    return hmac.new(settings.files_signing_key(), message, hashlib.sha256).hexdigest()


def signed_download_path(key: str, settings: Settings, expires_s: int | None = None, now: datetime | None = None) -> str:
    """``/api/v1/files/<key>?exp=..&sig=..`` (relative; see ``signed_download_url`` for the absolute link)."""
    key = key.lstrip("/")
    base_ts = int(now.timestamp()) if now is not None else int(time.time())
    exp = base_ts + int(expires_s or settings.FILE_LINK_EXPIRY_S)
    return f"{ROUTE}{quote(key, safe='/._-~')}?exp={exp}&sig={_sig(key, exp, settings)}"


def signed_download_url(key: str, settings: Settings, expires_s: int | None = None, now: datetime | None = None) -> str:
    """Absolute signed link: ``PUBLIC_BASE_URL`` + ``signed_download_path``."""
    return settings.PUBLIC_BASE_URL.rstrip("/") + signed_download_path(key, settings, expires_s, now)


def verify_download(key: str, exp: str | int | None, sig: str | None, settings: Settings, now: datetime | None = None) -> None:
    """Raise UNAUTHORIZED (403) for a bad signature and NOT_FOUND-style 410 for an expired link."""
    try:
        exp_i = int(str(exp))
    except (TypeError, ValueError):
        raise IbxError(ErrorCode.UNAUTHORIZED, "This download link is invalid.", http_status=403) from None
    expected = _sig(key.lstrip("/"), exp_i, settings)
    if not sig or not hmac.compare_digest(expected, str(sig)):
        raise IbxError(ErrorCode.UNAUTHORIZED, "This download link is invalid.", http_status=403)
    current = now.timestamp() if now is not None else time.time()
    if exp_i < current:
        raise IbxError(ErrorCode.NOT_FOUND, "This download link has expired; ask for a new one.", http_status=410,
                       details={"expired_at": exp_i})


def key_from_download_url(url: str) -> tuple[str, str | None, str | None] | None:
    """(key, exp, sig) when ``url`` is one of our signed file links, else None."""
    parts = urlsplit(url)
    if ROUTE not in parts.path:
        return None
    key = unquote(parts.path.split(ROUTE, 1)[1])
    query = parse_qs(parts.query)
    return key, (query.get("exp") or [None])[0], (query.get("sig") or [None])[0]
