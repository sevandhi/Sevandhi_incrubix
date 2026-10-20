"""Bearer-token auth and a per-IP rate limit for /api/v1 and /connector/v1 (health, ready and docs stay open).

Pure ASGI middleware so the checks run before the idempotency layer: an unauthenticated caller can
never create or read idempotency rows.
"""

from __future__ import annotations

import hmac
import ipaddress
import time
from typing import Any

from app.api.errors import error_response
from app.errors import ErrorCode, IbxError
from app.reliability.ratelimit import BucketRegistry

PROTECTED_PREFIXES = ("/api/v1", "/connector/v1")
# GETs that need no bearer token: signed download links carry their own HMAC (exp + sig, checked by the route);
# the MRSS feed and its click redirect are read by feed readers, which send no token.
SIGNED_LINK_PREFIXES = ("/api/v1/files/",)
PUBLIC_GET_PREFIXES = (*SIGNED_LINK_PREFIXES, "/api/v1/feeds/", "/api/v1/r/")


def _bearer(scope: dict[str, Any]) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == b"authorization":
            text = value.decode("latin-1")
            if text[:7].lower() == "bearer ":
                return text[7:].strip()
            return None
    return None


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def client_ip(scope: dict[str, Any], settings: Any) -> str:
    """The caller's IP for the per-client rate limit.

    ``X-Forwarded-For`` is only believed when the direct peer is one of ``TRUSTED_PROXIES`` (e.g. Caddy or a
    tunnel). Then the right-most address that is not a trusted proxy is the client; anything left of it could
    have been written by the client itself. With no trusted proxies (the default) the header is ignored."""
    client = scope.get("client")
    peer = client[0] if client else "unknown"
    networks = [ipaddress.ip_network(n, strict=False) for n in settings.TRUSTED_PROXIES]

    def trusted(address: Any) -> bool:
        return address is not None and any(address in net for net in networks)

    if not networks or not trusted(_ip(peer)):
        return peer
    forwarded = ",".join(v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == b"x-forwarded-for")
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        address = _ip(hop)
        if address is None:
            return peer  # malformed header: fall back to the proxy itself
        if not trusted(address):
            return str(address)
    return peer


class GateMiddleware:
    """401 UNAUTHORIZED without the right token; 429 API_RATE_LIMITED above RATE_LIMIT_PER_MIN per IP."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.buckets = BucketRegistry()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return
        settings = scope["app"].state.settings
        per_min = settings.RATE_LIMIT_PER_MIN
        if per_min > 0:
            wait = self.buckets.take(client_ip(scope, settings), per_min, 60.0, time.monotonic())
            if wait > 0:
                err = IbxError(ErrorCode.API_RATE_LIMITED, "Too many requests from this client.", details={"retry_after_s": int(wait) + 1})
                await error_response(err)(scope, receive, send)
                return
        if scope.get("method") in ("GET", "HEAD") and scope.get("path", "").startswith(PUBLIC_GET_PREFIXES):
            await self.app(scope, receive, send)
            return
        expected = settings.API_TOKEN.get_secret_value()
        token = _bearer(scope)
        if not token or not hmac.compare_digest(token.encode(), expected.encode()):
            err = IbxError(ErrorCode.UNAUTHORIZED, "Missing or invalid bearer token (Authorization: Bearer <API_TOKEN>).")
            await error_response(err)(scope, receive, send)
            return
        await self.app(scope, receive, send)
