"""One guard for every server-side fetch of a URL that a caller gave us (SSRF).

Used by ``POST /api/v1/media {url}``, the assisted connector's media, and job callbacks. Other server-side
fetches use it too (catalogue ``csv_url``, rules pages).

* Only http(s) URLs without ``user:pass@``.
* The host must resolve to public addresses only: no private, loopback, link-local (cloud metadata),
  reserved or multicast address. Redirects are followed by hand (at most 5) and every hop is checked again.
* Our own compose hosts (``FETCH_LOCAL_HOSTS``: the mock platforms and local test instances) are allowed only
  in simulation or a dev/test run. Loopback names (``localhost``, ``127.0.0.1``, ``::1``) are allowed only in a
  dev/test run (APP_ENV dev|test): inside the api container they are the api itself, so a demo or prod run,
  even in simulation, never fetches them. Internal services (toxiproxy's admin API, Postgres, S3) are never allowed.
* Limits: ``max_bytes`` (413), ``allowed_mime`` (415) and a timeout.
* Our own signed file links (``/api/v1/files/...``) are never fetched over HTTP: the signature is verified and
  the object is read from storage, and only ``media/`` objects (uploads) can be used this way.

Known limit: the host is resolved here and again by httpx, so a DNS-rebinding race is still possible.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import Settings
from app.errors import ErrorCode, IbxError
from app.storage.links import key_from_download_url, verify_download

MAX_REDIRECTS = 5
NEVER_FETCH_HOSTS = frozenset({"toxiproxy", "postgres", "s3", "bootstrap", "metadata", "metadata.google.internal"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
OWN_FILE_PREFIXES = ("media/",)  # stored objects a caller may reference by our signed link


@dataclass
class Fetched:
    data: bytes
    mime: str | None
    url: str


def _refused() -> IbxError:
    return IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "The URL must be a public http(s) address.", retryable=False,
                    details={"errors": [{"loc": ["url"], "type": "url_not_allowed", "msg": "not a public http(s) URL"}]})


def local_mode(settings: Settings) -> bool:
    return settings.SIMULATION_MODE or settings.APP_ENV in ("dev", "test")


def local_host_allowed(host: str, settings: Settings) -> bool:
    """One of our own compose hosts that this run may fetch (see the module docstring)."""
    if host not in settings.FETCH_LOCAL_HOSTS:
        return False
    if host in LOOPBACK_HOSTS:
        return settings.APP_ENV in ("dev", "test")
    return local_mode(settings)


async def check_url(url: str, settings: Settings) -> None:
    """Raise 422 unless ``url`` may be fetched by this service right now."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise _refused()
    if host in NEVER_FETCH_HOSTS:
        raise _refused()
    if local_host_allowed(host, settings):
        return
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise IbxError(ErrorCode.INPUT_FETCH_FAILED, "The URL's host could not be resolved.", retryable=False) from None
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            raise _refused()


async def fetch_url(
    url: str,
    settings: Settings,
    *,
    max_bytes: int,
    allowed_mime: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Fetched:
    """GET a caller's URL with every check above. Errors are IbxErrors (never the response body)."""
    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            await check_url(url, settings)
            async with client.stream("GET", url, follow_redirects=False, timeout=settings.MEDIA_FETCH_TIMEOUT_S) as response:
                if response.is_redirect and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise IbxError(ErrorCode.INPUT_FETCH_FAILED, f"The URL answered HTTP {response.status_code}.",
                                   details={"http_status": response.status_code})
                mime = response.headers.get("content-type", "").split(";")[0].strip().lower() or None
                if allowed_mime is not None and mime not in allowed_mime:
                    raise _wrong_type(mime, allowed_mime)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise _too_big(max_bytes)
                    chunks.append(chunk)
                return Fetched(b"".join(chunks), mime, url)
        raise IbxError(ErrorCode.INPUT_FETCH_FAILED, "The URL redirects too many times.", retryable=False)
    except httpx.HTTPError as exc:
        raise IbxError(ErrorCode.INPUT_FETCH_FAILED, f"The URL could not be downloaded ({type(exc).__name__}).") from exc
    finally:
        if own_client:
            await client.aclose()


async def read_media_url(
    url: str,
    settings: Settings,
    storage: Any,
    *,
    max_bytes: int,
    allowed_mime: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> Fetched:
    """A media URL from a caller: one of our signed upload links (verified, read from storage) or a public URL."""
    own = key_from_download_url(url)
    if own is None:
        return await fetch_url(url, settings, max_bytes=max_bytes, allowed_mime=allowed_mime, client=client)
    key, exp, sig = own
    verify_download(key, exp, sig, settings, now)  # 403 / 410: only links we signed, and only while valid
    if not key.startswith(OWN_FILE_PREFIXES):
        raise _refused()
    info = await storage.stat(key)
    if info is None:
        raise IbxError(ErrorCode.NOT_FOUND, "The linked file no longer exists.")
    if info.size > max_bytes:
        raise _too_big(max_bytes)
    mime = (info.content_type or "").split(";")[0].strip().lower() or None
    if allowed_mime is not None and mime not in allowed_mime:
        raise _wrong_type(mime, allowed_mime)
    return Fetched(await storage.get_bytes(key), mime, url)


def _too_big(limit: int) -> IbxError:
    return IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"The file is larger than the {limit} byte limit.", http_status=413,
                    details={"limit_bytes": limit})


def _wrong_type(mime: str | None, allowed: list[str]) -> IbxError:
    return IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"Unsupported media type ({mime or 'unknown'}).", http_status=415,
                    details={"allowed": allowed, "detected": mime})
