"""All-in-one live demo: ONE process serves the API, runs the job worker + scheduler, and hosts the mock platforms.

    uvicorn --factory app.demo.allinone:create_demo_app --host 0.0.0.0 --port $PORT --loop asyncio

(``scripts/demo_start.sh`` is the container command of ``Dockerfile.demo``; ``render.yaml`` deploys it.)

Why one process: a free host (Render free: 512 MB RAM, 0.1 CPU, one container) cannot run the compose stack's
api + worker + mock-platforms containers. Three Python processes would each load the whole app (about 3 × the RAM);
one asyncio process shares it. Everything below is the same code the compose stack runs, only wired together:

* **API**: ``app.main.create_app`` unchanged (auth, per-IP rate limit, idempotency, migrations under the advisory
  lock in its lifespan, S3 bucket check, routers).
* **Worker + scheduler**: ``app.jobs.worker.Worker`` consumer loops + the ``LISTEN`` waker and
  ``app.jobs.scheduler.Scheduler`` (leader advisory lock), started as background tasks after the API's lifespan
  has connected the database. They need a *session* connection (LISTEN/NOTIFY, session advisory locks), so with
  Supabase use the session pooler (port 5432), never the transaction pooler (6543).
* **Mock platforms**: ``mocks.platforms.app`` on a second uvicorn server inside the same event loop, bound to
  127.0.0.1 only, at the port of ``MOCK_PLATFORM_URL`` (default ``http://mock-platforms:8080``). The host name of
  ``MOCK_PLATFORM_URL`` is mapped to 127.0.0.1 inside this process (``install_host_aliases``), because a PaaS lets
  nobody edit ``/etc/hosts``. So every seed row, rehearsal case and SSRF allow-list entry that says
  ``mock-platforms`` works unchanged, and loopback stays refused by the SSRF guard, so a caller's URL can
  never reach the API itself.
* **/sim proxy**: ``/sim/<path>`` on the public port forwards to the internal mock platforms, so reviewers (and
  ``scripts/rehearse.py --mock https://<host>/sim``) can read request counters and mock pages. GET/HEAD are open;
  any other method needs the bearer token. Both share the per-IP rate limit. It is a simulation harness (like the
  compose stack's published mock port), not part of the API contract, so it is not in openapi.yaml.
* **Demo destinations**: at start a ``demo.seed`` job creates and certifies the demo destinations if they do not
  exist yet (``app.demo.seed``); ``POST /api/v1/sim/reset`` resets them.

Refuses to start unless ``SIMULATION_MODE=true`` and ``APP_ENV`` is not ``prod``: every platform call goes to the
in-process mocks, never to a real site.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import socket
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI

from app.api.errors import error_response
from app.api.security import _bearer, client_ip
from app.config import Settings, get_settings
from app.errors import ErrorCode, IbxError
from app.logging import get_logger
from app.reliability.ratelimit import BucketRegistry
from app.reliability.redact import redact

log = get_logger("app.demo")

SIM_PREFIX = "/sim"
DEMO_TOKEN_HEADER = "x-incubrix-demo-token"


def _header(scope: dict[str, Any], name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key.decode("latin-1").lower() == name:
            return value.decode("latin-1").strip() or None
    return None
LOOPBACK = "127.0.0.1"
_HOP_HEADERS = frozenset({"host", "authorization", "connection", "keep-alive", "transfer-encoding", "te", "trailer",
                          "upgrade", "proxy-authorization", "proxy-authenticate", "content-length", "content-encoding"})

# ------------------------------------------------------------------ host aliases (in-process /etc/hosts)

_ALIASES: dict[str, str] = {}
_original_getaddrinfo = socket.getaddrinfo


def _aliased_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if isinstance(host, (str, bytes)):
        name = (host.decode() if isinstance(host, bytes) else host).lower().rstrip(".")
        host = _ALIASES.get(name, host)
    return _original_getaddrinfo(host, *args, **kwargs)


def install_host_aliases(aliases: dict[str, str]) -> None:
    """Resolve these host names to fixed addresses in this process (asyncio + httpx + asyncpg all use
    ``socket.getaddrinfo``; uvloop does not, hence ``--loop asyncio``)."""
    _ALIASES.update({k.lower().rstrip("."): v for k, v in aliases.items()})
    socket.getaddrinfo = _aliased_getaddrinfo


def uninstall_host_aliases() -> None:
    """Undo ``install_host_aliases`` (tests)."""
    _ALIASES.clear()
    socket.getaddrinfo = _original_getaddrinfo


def mock_endpoint(settings: Settings) -> tuple[str, int]:
    parts = urlsplit(settings.MOCK_PLATFORM_URL)
    if parts.scheme != "http" or not parts.hostname:
        raise RuntimeError("The all-in-one demo needs MOCK_PLATFORM_URL=http://<name>:<port> (e.g. http://mock-platforms:8080).")
    return parts.hostname.lower(), parts.port or 80


def check_demo_settings(settings: Settings) -> None:
    if not settings.SIMULATION_MODE:
        raise RuntimeError("The all-in-one demo only runs with SIMULATION_MODE=true (every platform call goes to the "
                           "in-process mock platforms).")
    if settings.APP_ENV == "prod":
        raise RuntimeError("The all-in-one demo refuses APP_ENV=prod; use APP_ENV=demo.")
    mock_endpoint(settings)


# ------------------------------------------------------------------ the mock platforms, in the same event loop


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the main server (both run in one loop)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class MockPlatforms:
    def __init__(self, port: int) -> None:
        from mocks.platforms.app import create_app as create_mock_app

        config = uvicorn.Config(create_mock_app(), host=LOOPBACK, port=port, lifespan="off", access_log=False,
                                log_config=None, log_level="warning")
        self.port = port
        self.server = _EmbeddedServer(config)
        self.task: asyncio.Task[None] | None = None

    async def start(self, timeout_s: float = 20.0) -> None:
        self.task = asyncio.create_task(self.server.serve())
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not self.server.started:
            if self.task.done():
                self.task.result()  # re-raise the startup error (e.g. port in use)
                raise RuntimeError(f"mock platforms stopped during start-up (port {self.port})")
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError(f"mock platforms did not start on {LOOPBACK}:{self.port} within {timeout_s} s")
            await asyncio.sleep(0.05)
        log.info("demo_mock_platforms_started", port=self.port)

    async def stop(self) -> None:
        self.server.should_exit = True
        if self.task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.task, timeout=10)


# ------------------------------------------------------------------ /sim → internal mock platforms


class SimProxyMiddleware:
    """``/sim/<path>`` → ``http://127.0.0.1:<mock port>/<path>``. GET/HEAD open, other methods need the demo token (Authorization: Bearer, or X-IncuBrix-Demo-Token);
    every request counts against the per-IP rate limit (``RATE_LIMIT_PER_MIN``)."""

    def __init__(self, app: Any, *, settings: Settings, target: str) -> None:
        self.app = app
        self.settings = settings
        self.target = target.rstrip("/")
        self.buckets = BucketRegistry()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == SIM_PREFIX or path.startswith(SIM_PREFIX + "/")):
            await self.app(scope, receive, send)
            return
        settings = self.settings
        if settings.RATE_LIMIT_PER_MIN > 0:
            wait = self.buckets.take(client_ip(scope, settings), settings.RATE_LIMIT_PER_MIN, 60.0, time.monotonic())
            if wait > 0:
                err = IbxError(ErrorCode.API_RATE_LIMITED, "Too many requests from this client.", details={"retry_after_s": int(wait) + 1})
                await error_response(err)(scope, receive, send)
                return
        method = scope.get("method", "GET")
        if method not in ("GET", "HEAD"):
            # The demo token may come in X-IncuBrix-Demo-Token, so that Authorization can carry the simulated
            # platform's own credentials (e.g. a mock Lemmy admin changing its rate limit).
            token = _header(scope, DEMO_TOKEN_HEADER) or _bearer(scope)
            expected = settings.API_TOKEN.get_secret_value()
            if not token or not hmac.compare_digest(token.encode(), expected.encode()):
                err = IbxError(ErrorCode.UNAUTHORIZED, "Missing or invalid bearer token (Authorization: Bearer <API_TOKEN>).")
                await error_response(err)(scope, receive, send)
                return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        query = scope.get("query_string", b"").decode("latin-1")
        url = self.target + (path[len(SIM_PREFIX):] or "/") + (f"?{query}" if query else "")
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])
                   if k.decode("latin-1").lower() not in _HOP_HEADERS and k.decode("latin-1").lower() != DEMO_TOKEN_HEADER]
        # Authorization is forwarded only when it carries the simulated platform's own credentials, i.e. the demo
        # token came separately in X-IncuBrix-Demo-Token; the demo token itself never reaches the mock platforms.
        platform_auth = _header(scope, "authorization")
        if _header(scope, DEMO_TOKEN_HEADER) and platform_auth and platform_auth != f"Bearer {settings.API_TOKEN.get_secret_value()}":
            headers.append(("Authorization", platform_auth))
        client: httpx.AsyncClient | None = getattr(scope["app"].state, "sim_client", None)
        try:
            if client is None:
                raise httpx.ConnectError("the demo is not started")
            upstream = await client.request(method, url, headers=headers, content=body)
        except httpx.HTTPError as exc:
            err = IbxError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"The mock platforms did not answer ({type(exc).__name__}).")
            await error_response(err)(scope, receive, send)
            return
        out = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in upstream.headers.multi_items()
               if k.lower() not in _HOP_HEADERS]
        out.append((b"content-length", str(len(upstream.content)).encode()))
        await send({"type": "http.response.start", "status": upstream.status_code, "headers": out})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else upstream.content})


# ------------------------------------------------------------------ the app


async def _queue_seed_when_ready(app: FastAPI, stop: asyncio.Event) -> None:
    """Queue the start-up ``demo.seed`` job; retried while the database is not reachable yet."""
    from app.demo.seed import enqueue_seed_once

    while not stop.is_set():
        try:
            job_id = await enqueue_seed_once(app.state.session_factory, now=app.state.clock.now())
            log.info("demo_seed_queued", job_id=job_id)
            return
        except Exception as exc:  # noqa: BLE001 - e.g. the database is still waking up; try again
            log.warning("demo_seed_queue_failed", error=redact(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=10)


def _start_background(app: FastAPI, settings: Settings, stop: asyncio.Event) -> tuple[Any, list[asyncio.Task[Any]]]:
    from app.jobs.scheduler import Scheduler
    from app.jobs.worker import Worker

    registry = app.state.registry
    worker = Worker(settings=settings, session_factory=app.state.session_factory, storage=app.state.storage,
                    clock=app.state.clock, connector_versions={k: m.version for k, m in registry.manifests.items()})
    tasks = [asyncio.create_task(worker.listen(stop))]
    tasks += [asyncio.create_task(worker.consume(stop)) for _ in range(max(1, settings.WORKER_CONCURRENCY))]
    if settings.SCHEDULER_ENABLED:
        tasks.append(asyncio.create_task(Scheduler(settings, app.state.engine, app.state.session_factory,
                                                   clock=app.state.clock).run(stop)))
    if settings.DEMO_SEED_ON_START:
        tasks.append(asyncio.create_task(_queue_seed_when_ready(app, stop)))
    return worker, tasks


async def _check_alias(host: str, port: int) -> None:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos or infos[0][4][0] != LOOPBACK:
        raise RuntimeError(f"'{host}' does not resolve to {LOOPBACK} in this process; start uvicorn with --loop asyncio "
                           "(uvloop bypasses the in-process host alias).")


def create_demo_app(settings: Settings | None = None, **app_kwargs: Any) -> FastAPI:
    """The uvicorn factory: ``uvicorn --factory app.demo.allinone:create_demo_app --loop asyncio``."""
    from app.main import create_app

    settings = settings or get_settings()
    check_demo_settings(settings)
    mock_host, mock_port = mock_endpoint(settings)
    install_host_aliases({mock_host: LOOPBACK})
    app = create_app(settings, **app_kwargs)
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(the_app: FastAPI) -> AsyncIterator[Any]:
        await _check_alias(mock_host, mock_port)
        mocks = MockPlatforms(mock_port)
        await mocks.start()
        the_app.state.sim_client = httpx.AsyncClient(timeout=30)
        try:
            async with inner(the_app) as state:
                stop = asyncio.Event()
                worker, tasks = _start_background(the_app, settings, stop)
                log.info("demo_started", worker_id=worker.worker_id, consumers=settings.WORKER_CONCURRENCY,
                         scheduler=settings.SCHEDULER_ENABLED, seed_on_start=settings.DEMO_SEED_ON_START)
                try:
                    yield state
                finally:
                    stop.set()
                    worker.wake.set()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await worker.http.aclose()
        finally:
            await the_app.state.sim_client.aclose()
            await mocks.stop()

    app.router.lifespan_context = lifespan

    app.state.sim_client = None
    app.add_middleware(SimProxyMiddleware, settings=settings, target=f"http://{LOOPBACK}:{mock_port}")
    return app
