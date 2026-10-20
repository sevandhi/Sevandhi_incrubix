"""Connector instances for background jobs (the API builds its own in ``app.contract.router``).

A job has settings, a session factory, storage and a clock but no app; ``connector_for`` builds a
``ConnectorContext`` from them (one per connector key and settings object) with a shared platform HTTP client.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import Clock
from app.config import Settings
from app.connectors.base import BaseConnector, ConnectorContext
from app.contract.registry import Registry, get_registry
from app.errors import ErrorCode, IbxError
from app.logging import get_logger

_cache: dict[tuple[str, int, int, int], BaseConnector] = {}
_clients: dict[int, httpx.AsyncClient] = {}


def platform_client(settings: Settings) -> httpx.AsyncClient:
    client = _clients.get(id(settings))
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.PLATFORM_TIMEOUT_S, connect=min(5.0, settings.PLATFORM_TIMEOUT_S)),
            follow_redirects=False,
            headers={"User-Agent": f"ibx-destination-network/{settings.SERVICE_VERSION}"},
        )
        _clients[id(settings)] = client
    return client


def connector_for(
    key: str,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    storage: Any,
    clock: Clock,
    registry: Registry | None = None,
    http: httpx.AsyncClient | None = None,
) -> BaseConnector:
    registry = registry or get_registry()
    manifest = registry.manifest(key)
    if manifest is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, "Unknown connector key.", details={"registry_errors": registry.errors})
    cache_key = (key, id(settings), id(session_factory), id(registry))
    connector = _cache.get(cache_key)
    if connector is None or connector.ctx.clock is not clock or connector.ctx.storage is not storage:
        ctx = ConnectorContext(
            manifest=manifest,
            settings=settings,
            http=http or platform_client(settings),
            clock=clock,
            log=get_logger(f"connector.{key}", connector=key),
            db_session_factory=session_factory,
            simulation=settings.SIMULATION_MODE,
            storage=storage,
        )
        connector = registry.build(key, ctx)
        _cache[cache_key] = connector
    return connector
