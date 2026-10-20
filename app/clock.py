"""An injectable clock so time-based behaviour (breaker, rate limits, scheduler, caps) is testable.

Production code asks ``get_clock().now()`` instead of ``datetime.now()``. Tests install a ``FakeClock``
and move time forward with ``advance()``; nothing sleeps for real.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """A clock that only moves when told to. ``sleep`` advances time instead of waiting."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> Clock:
    """Install a clock (tests, the recert demo). Returns the previous one so it can be restored."""
    global _clock
    previous, _clock = _clock, clock
    return previous
