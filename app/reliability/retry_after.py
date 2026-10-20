"""Work out when a rate limit resets, from whatever the platform tells us (plan §4.2).

Sources, in order:
1. ``Retry-After``: delta seconds (``120``) or an HTTP-date (``Wed, 21 Oct 2026 07:28:00 GMT``);
2. ``X-RateLimit-Reset`` / ``RateLimit-Reset`` / ``X-Rate-Limit-Reset``: a number that is an epoch
   (seconds, or milliseconds) when it is larger than 1e9, otherwise a delta in seconds;
3. the IETF ``RateLimit: limit=.., remaining=.., reset=N`` header (delta seconds);
4. body hints: Discourse ``extras.wait_seconds``, ``retry_after`` / ``wait_seconds`` / ``reset_at`` fields.
Returns ``None`` when nothing usable is present; the caller then uses its own fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

EPOCH_THRESHOLD = 1e9  # a reset value above this is an absolute epoch, not a delta
EPOCH_MS_THRESHOLD = 1e12
MAX_WAIT_S = 7 * 24 * 3600  # ignore absurd values (clock bugs) beyond a week


@dataclass(frozen=True)
class ResetInfo:
    retry_at: datetime
    retry_after_s: int
    source: str

    def as_details(self) -> dict[str, Any]:
        return {
            "retry_at": self.retry_at.isoformat().replace("+00:00", "Z"),
            "retry_after_s": self.retry_after_s,
            "reset_source": self.source,
        }


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value is None and isinstance(headers, dict):  # plain dicts are case-sensitive
        lowered = {str(k).lower(): v for k, v in headers.items()}
        value = lowered.get(name.lower())
    return str(value).strip() if value is not None else None


def _to_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def _from_delta(seconds: float, now: datetime, source: str) -> ResetInfo:
    seconds = min(max(0.0, seconds), MAX_WAIT_S)
    wait = int(seconds) if seconds == int(seconds) else int(seconds) + 1
    return ResetInfo(now + timedelta(seconds=wait), wait, source)


def _from_epoch_or_delta(number: float, now: datetime, source: str) -> ResetInfo:
    if number > EPOCH_MS_THRESHOLD:
        number = number / 1000.0
    if number > EPOCH_THRESHOLD:
        return _from_delta(number - now.timestamp(), now, source)
    return _from_delta(number, now, source)


def parse_retry_after(value: str, now: datetime) -> ResetInfo | None:
    """``Retry-After`` as delta seconds or HTTP-date."""
    number = _to_number(value)
    if number is not None:
        return _from_delta(number, now, "Retry-After")
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return _from_delta((when - now).total_seconds(), now, "Retry-After")


def _body_hint(body: Any, now: datetime) -> ResetInfo | None:
    if not isinstance(body, dict):
        return None
    extras = body.get("extras")
    if isinstance(extras, dict) and _to_number(extras.get("wait_seconds")) is not None:
        return _from_delta(float(extras["wait_seconds"]), now, "body:extras.wait_seconds")
    candidates = [body]
    if isinstance(body.get("error"), dict):
        candidates.append(body["error"])
    for container in candidates:
        for key in ("retry_after", "wait_seconds", "retry_after_s"):
            number = _to_number(container.get(key))
            if number is not None:
                return _from_epoch_or_delta(number, now, f"body:{key}")
        reset_at = container.get("reset_at")
        if isinstance(reset_at, str):
            try:
                when = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return _from_delta((when - now).total_seconds(), now, "body:reset_at")
    return None


_IETF_RESET = re.compile(r"(?:^|[,;\s])reset=(\d+(?:\.\d+)?)")


def parse_reset(headers: Any, body: Any, now: datetime) -> ResetInfo | None:
    """Best reset time from response headers and (parsed JSON) body; ``None`` if none is given."""
    value = _header(headers, "Retry-After")
    if value:
        info = parse_retry_after(value, now)
        if info is not None:
            return info
    for name in ("X-RateLimit-Reset", "RateLimit-Reset", "X-Rate-Limit-Reset"):
        value = _header(headers, name)
        number = _to_number(value) if value else None
        if number is not None:
            return _from_epoch_or_delta(number, now, name)
    value = _header(headers, "RateLimit")
    if value:
        match = _IETF_RESET.search(value)
        if match:
            return _from_delta(float(match.group(1)), now, "RateLimit")
    return _body_hint(body, now)


def fallback_reset(seconds: float, now: datetime, source: str = "fallback") -> ResetInfo:
    return _from_delta(seconds, now, source)
