"""FastAPI application factory: ``uvicorn app.main:app``.

Start-up (lifespan): configure JSON logging with redaction → connect Postgres and apply migrations
under an advisory lock → ensure the S3 bucket → discover connectors and job handlers.
Middleware, outermost first: correlation id → auth + per-IP rate limit → HTTP idempotency.
Routers: /health, /ready, the connector contract (+ /api/v1 mirror) and every module in app/api/v1/.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from starlette.middleware import Middleware

from app.api.errors import install_error_handlers
from app.api.openapi import install_openapi
from app.api.security import GateMiddleware
from app.api.v1 import discover_routers
from app.clock import Clock, get_clock
from app.config import Settings, ensure_safe_secrets, get_settings
from app.contract.registry import Registry, get_registry
from app.contract.router import SIM_FAULT
from app.contract.router import router as contract_router
from app.db.migrate import upgrade_head
from app.db.session import make_engine, make_session_factory
from app.jobs.registry import load_handlers
from app.logging import CorrelationIdMiddleware, configure_logging, get_logger
from app.reliability.http_idempotency import IdempotencyMiddleware
from app.reliability.redact import redact
from app.storage import S3Storage

log = get_logger("app.main")


def make_platform_client(settings: Settings) -> httpx.AsyncClient:
    """HTTP client for platform calls. In sim/dev it forwards the caller's X-Sim-Fault to mock-platforms only."""
    mock_base = settings.MOCK_PLATFORM_URL.rstrip("/")

    async def add_sim_fault(request: httpx.Request) -> None:
        fault = SIM_FAULT.get()
        if fault and str(request.url).startswith(mock_base):
            request.headers["X-Sim-Fault"] = fault

    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.PLATFORM_TIMEOUT_S, connect=min(5.0, settings.PLATFORM_TIMEOUT_S)),
        follow_redirects=False,
        event_hooks={"request": [add_sim_fault]},
        headers={"User-Agent": f"ibx-destination-network/{settings.SERVICE_VERSION}"},
    )


def create_app(
    settings: Settings | None = None,
    *,
    storage: Any = None,
    registry: Registry | None = None,
    clock: Clock | None = None,
    http: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    ensure_safe_secrets(settings)  # demo/prod never run on the repo's dev placeholders

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.LOG_LEVEL, settings.LOG_JSON, settings.secret_values())
        engine = make_engine(settings.DATABASE_URL, settings.DB_POOL_SIZE)
        if settings.DB_AUTO_MIGRATE:
            try:
                await upgrade_head(engine)
            except Exception as exc:  # noqa: BLE001 - /ready reports the database as down
                log.error("migration_failed", error=redact(exc))
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.storage = storage if storage is not None else S3Storage(settings)
        try:
            await app.state.storage.ensure_bucket()
        except Exception as exc:  # noqa: BLE001 - /ready reports storage as down
            log.warning("storage_not_ready", error=redact(exc))
        own_http = http is None
        app.state.http = http or make_platform_client(settings)
        handler_modules = load_handlers()
        log.info("api_started", connectors=app.state.registry.keys(), handlers=handler_modules)
        try:
            yield
        finally:
            if own_http:
                await app.state.http.aclose()
            await engine.dispose()

    app = FastAPI(
        title="IncuBrix Long-Tail Destination Network",
        version=settings.SERVICE_VERSION,
        description="Connector contract (Appendix A), destination catalogue, posting rules and certification.",
        openapi_version="3.1.0",
        lifespan=lifespan,
        middleware=[
            Middleware(CorrelationIdMiddleware),
            Middleware(GateMiddleware),
            Middleware(IdempotencyMiddleware),
        ],
    )
    # State that exists even without the lifespan (OpenAPI export, unit tests without a database).
    app.state.settings = settings
    app.state.clock = clock or get_clock()
    app.state.registry = registry or get_registry()
    app.state.connectors = {}
    app.state.engine = None
    app.state.session_factory = None
    app.state.storage = storage
    app.state.http = http
    install_error_handlers(app)
    app.include_router(contract_router)
    routers, errors = discover_routers()
    for _name, router in routers:
        app.include_router(router)
    app.state.router_errors = errors
    install_openapi(app)  # error envelope, bearer auth, idempotency headers in openapi.yaml
    return app


app = create_app()
