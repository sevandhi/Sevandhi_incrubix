"""Regression tests for minor fixes that need Postgres."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import make_session_factory
from app.reliability import breaker


async def test_two_first_failures_at_once_do_not_crash(pg_url):
    """Two concurrent first failures on a new scope used to INSERT the same row twice (unique violation → 500)."""
    engine = create_async_engine(pg_url)
    factory = make_session_factory(engine)
    now = datetime(2026, 10, 17, tzinfo=UTC)

    async def fail_once() -> bool:
        async with factory() as session, session.begin():
            return await breaker.record(session, "sample|race.example", "sample", unavailable=True, now=now,
                                        threshold=5, open_s=300, error="boom")

    try:
        for _ in range(5):  # several rounds, each on a fresh scope, to make the race likely
            await asyncio.gather(*[fail_once() for _ in range(8)])
            async with factory() as session, session.begin():
                row = await breaker._locked_row(session, "sample|race.example")
                assert row is not None
                await session.delete(row)
    finally:
        await engine.dispose()
