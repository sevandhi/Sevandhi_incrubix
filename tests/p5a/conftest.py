"""Metrics/recertification fixtures: the in-process environment (API + worker on one fake clock, mock-platforms on a real socket, real connectors +
test-only fakes), with the alert sinks pointed at our own receiver (mocks/platforms/routes/alertsink.py)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.contract.registry import get_registry, set_registry
from app.jobs.worker import Worker
from tests.conftest import api_client
from tests.p2d.conftest import Env, _p2d_mock_server, p2d_mock_url, p2d_registry  # noqa: F401 - fixtures

WEBHOOK_SECRET = "p5a-test-webhook-secret-0001"
GITHUB_TOKEN = "p5a-test-github-token-0001"


@pytest.fixture
async def p5a_factory(app_factory: Any, pg_url: str, p2d_mock_url: str, p2d_registry: Any,  # noqa: F811
                      fake_clock: Any) -> AsyncIterator[Callable[..., Any]]:
    previous = get_registry()
    set_registry(p2d_registry)
    httpx.post(f"{p2d_mock_url}/alertsink/_reset", timeout=5).raise_for_status()
    clients: list[Any] = []

    async def _make(**overrides: Any) -> Env:
        settings = {"MOCK_PLATFORM_URL": p2d_mock_url, "SIMULATION_MODE": True, "PUBLIC_BASE_URL": "http://ibx.test",
                    "CERT_LIVE_TIMEOUT_S": 3.0, "PLATFORM_TIMEOUT_S": 3.0, "RECERT_JITTER_S": 0,
                    "ALERT_WEBHOOK_URL": f"{p2d_mock_url}/alertsink/hook", "ALERT_WEBHOOK_SECRET": WEBHOOK_SECRET,
                    "GITHUB_TOKEN": GITHUB_TOKEN, "GITHUB_REPO": "ibx-test/alerts",
                    "GITHUB_API_URL": f"{p2d_mock_url}/alertsink/github", **overrides}
        app = await app_factory(pg_url=pg_url, registry=p2d_registry, clock=fake_clock, **settings)
        http = httpx.AsyncClient()
        worker = Worker(settings=app.state.settings, session_factory=app.state.session_factory, storage=app.state.storage,
                        clock=fake_clock, http=http, worker_id="p5a-test")
        client = api_client(app)
        clients.extend([client, http])
        return Env(app, client, worker, p2d_mock_url, fake_clock)

    yield _make
    for c in clients:
        await c.aclose()
    set_registry(previous)


@pytest.fixture
async def p5a(p5a_factory: Callable[..., Any]) -> Env:
    return await p5a_factory()
