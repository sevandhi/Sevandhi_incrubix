"""IncuBrix partner-webhook signatures (docs/PARTNER_WEBHOOK.md).

    X-IncuBrix-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>

The partner recomputes the HMAC over the exact raw body bytes, compares in constant time, and rejects a
timestamp more than ``TOLERANCE_S`` (5 minutes) away from its own clock (replay window).
"""

from __future__ import annotations

import hashlib
import hmac
import time

HEADER = "X-IncuBrix-Signature"
TOLERANCE_S = 300


def unix_now() -> int:
    """Wall-clock seconds. The signature is a wire-protocol value, so it uses real time, not the app clock."""
    return int(time.time())


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    t = unix_now() if timestamp is None else int(timestamp)
    digest = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def verify(secret: str, header: str | None, body: bytes, now: int | None = None, tolerance_s: int = TOLERANCE_S) -> str | None:
    """None when valid, else the reason: missing_signature | malformed_signature | timestamp_outside_tolerance | invalid_signature."""
    if not header:
        return "missing_signature"
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        name, _, value = item.strip().partition("=")
        parts.setdefault(name, []).append(value)
    try:
        t = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        return "malformed_signature"
    current = unix_now() if now is None else now
    if abs(current - t) > tolerance_s:
        return "timestamp_outside_tolerance"
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
        return "invalid_signature"
    return None
