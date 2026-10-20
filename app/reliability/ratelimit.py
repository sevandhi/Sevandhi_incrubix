"""Rate limiting: a pure token bucket, plus the DB-backed "blocked until" state set by platform 429s.

* ``TokenBucket`` is proactive: built from the manifest's ``rate_limits.per_connection`` it spaces our
  own calls so we rarely hit the platform's limit. Time is passed in, so tests need no sleeping.
* ``rate_limit_state`` is reactive: when a platform says "rate limited until T", every later call on the
  same scope short-circuits with ``PLATFORM_RATE_LIMIT`` until T, without touching the platform.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import RateLimitStateRow


@dataclass
class TokenBucket:
    """``capacity`` tokens, refilled continuously at ``capacity / per_seconds`` tokens per second."""

    capacity: float
    per_seconds: float
    tokens: float = field(default=-1.0)
    updated: float | None = None  # monotonic-ish seconds of the last refill

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.per_seconds <= 0:
            raise ValueError("capacity and per_seconds must be positive")
        if self.tokens < 0:
            self.tokens = self.capacity

    @property
    def rate(self) -> float:
        return self.capacity / self.per_seconds

    def _refill(self, now: float) -> None:
        if self.updated is not None and now > self.updated:
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        if self.updated is None or now > self.updated:
            self.updated = now

    def try_take(self, now: float, n: float = 1.0) -> float:
        """Take ``n`` tokens if available and return 0.0; otherwise take nothing and return the wait in seconds."""
        self._refill(now)
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        return (n - self.tokens) / self.rate


class BucketRegistry:
    """One bucket per scope (e.g. connector + connection), created on first use. Thread-safe."""

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def take(self, scope: str, capacity: float, per_seconds: float, now: float) -> float:
        with self._lock:
            bucket = self._buckets.get(scope)
            if bucket is None or bucket.capacity != capacity or bucket.per_seconds != per_seconds:
                bucket = self._buckets[scope] = TokenBucket(capacity, per_seconds)
            return bucket.try_take(now)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


# ------------------------------------------------------------------ DB state ("blocked until")


async def blocked_until(session: AsyncSession, scope: str, now: datetime) -> RateLimitStateRow | None:
    """The state row if the scope is still blocked at ``now``; otherwise ``None``."""
    row = await session.get(RateLimitStateRow, scope)
    if row is not None and row.blocked_until is not None and row.blocked_until > now:
        return row
    return None


async def block(
    session: AsyncSession, scope: str, connector_key: str, until: datetime, source: str, code: str
) -> None:
    """Record (or extend) a block. A later reset time always wins over an earlier one."""
    stmt = insert(RateLimitStateRow).values(
        scope=scope, connector_key=connector_key, blocked_until=until, source=source, code=code
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[RateLimitStateRow.scope],
        set_={"blocked_until": until, "source": source, "code": code},
        where=(RateLimitStateRow.blocked_until.is_(None)) | (RateLimitStateRow.blocked_until < until),
    )
    await session.execute(stmt)
