"""Async engine and session helpers.

* ``make_engine(url)`` builds an asyncpg engine.
* ``get_session`` is the FastAPI dependency. When the HTTP idempotency middleware has opened a
  transaction for this request, the session joins it (``create_savepoint`` mode), so the job row and the
  idempotency row commit together. Otherwise the session commits when the handler returns.
* ``advisory_key(text)`` turns any string into a signed 64-bit key for ``pg_advisory_*lock``.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.errors import ErrorCode, IbxError

# libpq ``sslmode`` values asyncpg understands as its ``ssl`` argument (Supabase gives ``?sslmode=require``).
_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


def engine_url_and_args(url: str) -> tuple[str, dict[str, Any]]:
    """An SQLAlchemy asyncpg URL + ``connect_args`` from any Postgres URL a host hands out.

    ``postgres://`` / ``postgresql://`` (as Supabase and Render show them) become ``postgresql+asyncpg://``, and a
    libpq ``sslmode=require`` becomes asyncpg's ``ssl="require"`` (asyncpg rejects an ``sslmode`` keyword)."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if parsed.drivername in ("postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"):
        parsed = parsed.set(drivername="postgresql+asyncpg")
    query = dict(parsed.query)
    connect_args: dict[str, Any] = {}
    mode = query.pop("sslmode", None) or query.pop("ssl", None)
    if isinstance(mode, str) and mode in _SSL_MODES:
        connect_args["ssl"] = mode
    elif mode is not None:
        raise ValueError(f"unsupported sslmode {mode!r} in DATABASE_URL (use one of {', '.join(_SSL_MODES)})")
    return parsed.set(query=query).render_as_string(hide_password=False), connect_args


def make_engine(url: str, pool_size: int = 10) -> AsyncEngine:
    url, connect_args = engine_url_and_args(url)
    return create_async_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True,
                               connect_args=connect_args)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def advisory_key(text: str) -> int:
    """Stable signed 64-bit integer for an advisory lock (Python's hash() is salted per process)."""
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big", signed=True)


def sync_dsn(url: str) -> str:
    """A plain ``postgresql://`` DSN (for asyncpg LISTEN connections) from an SQLAlchemy URL."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url).set(drivername="postgresql")
    query = dict(parsed.query)
    if "ssl" in query and "sslmode" not in query:  # asyncpg's DSN parser knows libpq's sslmode, not ssl
        query["sslmode"] = query.pop("ssl")
    return parsed.set(query=query).render_as_string(hide_password=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one unit of work per request."""
    shared_conn: Any = getattr(request.state, "idempotency_conn", None)
    if shared_conn is not None:
        # Joined to the middleware's open transaction; commit() only releases a savepoint.
        session = AsyncSession(bind=shared_conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        try:
            yield session
            await session.flush()
        finally:
            await session.close()
        return
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise IbxError(ErrorCode.DEPENDENCY_UNAVAILABLE, "The database is not configured.")
    async with factory() as session:
        yield session
        await session.commit()


# Use in endpoints: ``async def handler(session: SessionDep): ...``
SessionDep = Annotated[AsyncSession, Depends(get_session)]
