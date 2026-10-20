"""Signed job callbacks (IS3), the same scheme as the partner webhook (docs/PARTNER_WEBHOOK.md):

    X-IncuBrix-Timestamp: <unix seconds>
    X-IncuBrix-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw body)>

The receiver recomputes the HMAC over ``"<t>."`` + the exact bytes it received, compares in constant time and
rejects a timestamp more than 5 minutes from its clock, so a captured callback can not be replayed later.
The secret is ``CALLBACK_SECRET`` (defaults to ``API_TOKEN``) and is never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from app.config import Settings
from app.errors import IbxError
from app.logging import get_logger
from app.reliability.safe_fetch import check_url

SIGNATURE_HEADER = "X-IncuBrix-Signature"
TIMESTAMP_HEADER = "X-IncuBrix-Timestamp"
TOLERANCE_S = 300
log = get_logger("app.jobs.callbacks")


def sign(secret: bytes, body: bytes, timestamp: int | None = None) -> str:
    """``t=<ts>,v1=<hex>``. The timestamp is wall-clock time (a wire value checked by the receiver's clock)."""
    t = int(time.time()) if timestamp is None else int(timestamp)
    digest = hmac.new(secret, f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def verify(secret: bytes, body: bytes, signature: str | None, *, now: int | None = None, tolerance_s: int = TOLERANCE_S) -> bool:
    """True only for a valid signature whose timestamp is within ``tolerance_s`` of ``now``."""
    if not signature:
        return False
    parts = dict(item.strip().partition("=")[::2] for item in signature.split(","))
    try:
        t = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((int(time.time()) if now is None else now) - t) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, body, t), f"t={t},v1={parts.get('v1', '')}")


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


async def deliver(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    secret: bytes,
    *,
    attempts: int = 3,
    timeout_s: float = 5.0,
    settings: Settings | None = None,
) -> str:
    """POST the signed payload; retry 5xx / network errors with a short back-off. Returns a status string.
    With ``settings``, the URL must pass the SSRF guard first (no callbacks into our internal network)."""
    if not url.startswith(("http://", "https://")):
        return "rejected:bad_url"
    if settings is not None:
        try:
            await check_url(url, settings)
        except IbxError:
            log.warning("callback_refused", reason="url_not_allowed", job_id=payload.get("job_id"))
            return "rejected:url_not_allowed"
    body = encode(payload)
    timestamp = int(time.time())
    headers = {"Content-Type": "application/json", TIMESTAMP_HEADER: str(timestamp),
               SIGNATURE_HEADER: sign(secret, body, timestamp)}
    outcome = "failed"
    for attempt in range(1, attempts + 1):
        try:
            response = await client.post(url, content=body, headers=headers, timeout=timeout_s)
        except httpx.HTTPError as exc:
            outcome = f"failed:{type(exc).__name__}"
        else:
            if response.status_code < 500:
                return "delivered" if response.is_success else f"rejected:{response.status_code}"
            outcome = f"failed:{response.status_code}"
        if attempt < attempts:
            await asyncio.sleep(0.5 * attempt)
    log.warning("callback_failed", outcome=outcome, job_id=payload.get("job_id"))
    return outcome
