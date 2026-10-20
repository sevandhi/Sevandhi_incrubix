"""Shared pytest fixtures.

* ``pg_url``: a fresh, fully migrated Postgres database per test (asyncpg URL). One testcontainers
  Postgres runs per session on a random host port; each test gets ``CREATE DATABASE ... TEMPLATE``
  of a migrated template, so tests never see each other's rows. Skips if Docker is unavailable.
* ``fake_clock``: a ``FakeClock`` installed as the process clock (restored afterwards).
* ``mock_platforms_url``: mock-platforms served by uvicorn in a thread on a random port (real sockets,
  so client timeouts behave as in production). State is reset before every test.
* ``make_settings(**overrides)``: test settings that ignore the developer's ``.env``.
* ``app_factory`` / ``client`` / ``contract_client``: the API with its lifespan running, MemoryStorage,
  and an authenticated ``httpx.AsyncClient`` over ASGI.
* ``sample_registry``: a connector scaffolded from ``app/connectors/_template`` (key ``sample``),
  with its generated mock route mounted on mock-platforms. Used by platform and conformance tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import socket
import sys
import threading
import time
import uuid
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.clock import FakeClock, set_clock
from app.config import Settings
from app.contract.registry import Registry, discover, get_registry
from app.storage import MemoryStorage

ROOT = Path(__file__).resolve().parent.parent
TEST_TOKEN = "test-api-token-123456"
SAMPLE_KEY = "sample"


# ------------------------------------------------------------------ Postgres (testcontainers)


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def _pg_container() -> Iterator[Any]:
    if not _docker_available():
        pytest.skip("Docker is not available: Postgres-backed tests are skipped")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:  # older testcontainers layout
            from testcontainers.postgres import PostgresContainer
    container = PostgresContainer("postgres:16-alpine", username="ibx", password="ibx-test", dbname="ibx_template", driver="asyncpg")
    container.start()
    try:
        url = container.get_connection_url()
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.db.migrate import upgrade_head

        async def _migrate() -> None:
            engine = create_async_engine(url)
            await upgrade_head(engine)
            await engine.dispose()

        asyncio.run(_migrate())
        yield container
    finally:
        container.stop()


def _psql(container: Any, sql: str) -> None:
    result = container.exec(["psql", "-U", "ibx", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", sql])
    if result.exit_code != 0:
        raise RuntimeError(result.output.decode(errors="replace"))


@pytest.fixture
def pg_url(_pg_container: Any) -> Iterator[str]:
    """asyncpg URL of a fresh migrated database for this test."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    _psql(_pg_container, f"CREATE DATABASE {name} TEMPLATE ibx_template")
    base = _pg_container.get_connection_url()
    yield base.rsplit("/", 1)[0] + f"/{name}"
    with contextlib.suppress(Exception):
        _psql(_pg_container, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


# ------------------------------------------------------------------ settings isolation


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests never see settings from the shell or the repo's ``.env``.

    ``prove_mh8`` (and anyone with a stack running) exports e.g. ``PUBLIC_BASE_URL`` before ``scripts/test.sh``;
    pydantic-settings would read it even with ``_env_file=None``. The repo's ``.env`` may hold a real
    ``GEMINI_API_KEY``, and tests must never call a real provider. ``IBX_*`` names are kept: the e2e/live tests
    read their stack URLs from them."""
    from app.config import Settings, get_settings
    from app.providers.llm.config import LLMSettings, get_llm_settings

    for model in (Settings, LLMSettings):
        for name in model.model_fields:
            if not name.startswith("IBX_"):
                monkeypatch.delenv(name, raising=False)
        monkeypatch.setitem(model.model_config, "env_file", None)
    get_settings.cache_clear()
    get_llm_settings.cache_clear()
    yield
    get_settings.cache_clear()
    get_llm_settings.cache_clear()


# ------------------------------------------------------------------ clock and settings


@pytest.fixture
def fake_clock() -> Iterator[FakeClock]:
    clock = FakeClock()
    previous = set_clock(clock)
    yield clock
    set_clock(previous)


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "APP_ENV": "test",
            "DATABASE_URL": "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
            "API_TOKEN": TEST_TOKEN,
            "RATE_LIMIT_PER_MIN": 0,
            "LOG_JSON": True,
            "LOG_LEVEL": "WARNING",
            "PLATFORM_TIMEOUT_S": 2.0,
            "IDEMPOTENCY_SETTLE_S": 0.0,
            "RATE_LIMIT_MAX_WAIT_S": 0.0,
            "SIMULATION_MODE": False,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make


# ------------------------------------------------------------------ mock-platforms in a thread


class _ServerThread:
    """Run an ASGI app with uvicorn on 127.0.0.1:<random port> in a daemon thread."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=1))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def __enter__(self) -> _ServerThread:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("mock-platforms did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="session")
def _sample_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Scaffold the ``sample`` connector + mock route from the template into a temp package."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from new_connector import scaffold

    root = tmp_path_factory.mktemp("scaffold")
    package = root / "ibx_sample_connectors"
    package.mkdir()
    (package / "__init__.py").write_text("")
    scaffold(SAMPLE_KEY, root=root, package_dir=package / SAMPLE_KEY)
    sys.path.insert(0, str(root))
    return root


@pytest.fixture(scope="session")
def sample_registry(_sample_package: Path) -> Registry:
    registry = discover("ibx_sample_connectors", _sample_package / "ibx_sample_connectors")
    assert not registry.errors, registry.errors
    return registry


@pytest.fixture(scope="session")
def _mock_server(_sample_package: Path) -> Iterator[_ServerThread]:
    from mocks.platforms.app import create_app as create_mock_app

    mock_app = create_mock_app()
    route_file = _sample_package / "mocks" / "platforms" / "routes" / f"{SAMPLE_KEY}.py"
    spec = importlib.util.spec_from_file_location(f"ibx_sample_mock_{SAMPLE_KEY}", route_file)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    mock_app.include_router(module.router, prefix=f"/{SAMPLE_KEY}")
    with _ServerThread(mock_app) as server:
        yield server


@pytest.fixture
def mock_platforms_url(_mock_server: _ServerThread) -> str:
    url = f"http://127.0.0.1:{_mock_server.port}"
    httpx.post(f"{url}/_reset", timeout=5).raise_for_status()
    return url


# ------------------------------------------------------------------ the API


@pytest.fixture
async def app_factory(make_settings: Callable[..., Settings]) -> AsyncIterator[Callable[..., Any]]:
    """``await app_factory(pg_url=..., registry=..., **settings)`` → started app (lifespan runs)."""
    from app.main import create_app

    stack = contextlib.AsyncExitStack()

    async def _start(*, pg_url: str, registry: Registry | None = None, clock: Any = None, **overrides: Any) -> Any:
        settings = make_settings(DATABASE_URL=pg_url, **overrides)
        app = create_app(settings, storage=MemoryStorage(), registry=registry or get_registry(), clock=clock)
        await stack.enter_async_context(app.router.lifespan_context(app))
        return app

    yield _start
    await stack.aclose()


def api_client(app: Any, token: str | None = TEST_TOKEN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ibx.test", headers=headers, timeout=30)


@pytest.fixture
async def client(app_factory: Callable[..., Any], pg_url: str) -> AsyncIterator[httpx.AsyncClient]:
    app = await app_factory(pg_url=pg_url)
    async with api_client(app) as c:
        yield c


@pytest.fixture
async def contract_client(
    app_factory: Callable[..., Any], pg_url: str, mock_platforms_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    """Client for connector tests: real registry, real DB, mock-platforms running."""
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as c:
        yield c
