"""Fixtures for the in-process flow tests: the API + a worker on one fake clock, mock-platforms (with the real platform mocks and
the test-only fake routes) on a real socket, and a registry = real connectors + test-only fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.contract.registry import Registry, discover, get_registry, set_registry
from app.jobs.worker import Worker
from tests.conftest import _ServerThread, api_client

FAKES = Path(__file__).resolve().parent.parent / "p2d_fakes" / "connectors"
CANARY = "canary-SECRET-p2d-5e1d77c0"


@pytest.fixture(scope="session")
def p2d_registry() -> Registry:
    real = discover()
    fakes = discover("tests.p2d_fakes.connectors", FAKES)
    assert not fakes.errors, fakes.errors
    merged = Registry()
    for reg in (real, fakes):
        for key, manifest in reg.manifests.items():
            merged.add(manifest, reg.classes[key])
        merged.errors.update(reg.errors)
    return merged


@pytest.fixture(scope="session")
def _p2d_mock_server() -> Any:
    from mocks.platforms.app import create_app as create_mock_app
    from tests.p2d_fakes.mock_routes import assist, forum

    app = create_mock_app()
    app.include_router(forum, prefix="/fakeforum")
    app.include_router(assist, prefix="/fakeassist")
    with _ServerThread(app) as server:
        yield server


@pytest.fixture
def p2d_mock_url(_p2d_mock_server: Any) -> str:
    url = f"http://127.0.0.1:{_p2d_mock_server.port}"
    httpx.post(f"{url}/_reset", timeout=5).raise_for_status()
    return url


class Env:
    def __init__(self, app: Any, client: httpx.AsyncClient, worker: Worker, mock: str, clock: Any) -> None:
        self.app, self.client, self.worker, self.mock, self.clock = app, client, worker, mock, clock

    @property
    def storage(self) -> Any:
        return self.app.state.storage

    async def drain(self, max_jobs: int = 50) -> int:
        """Run every job that is due now on the fake clock."""
        n = 0
        while n < max_jobs and await self.worker.run_once() is not None:
            n += 1
        return n

    async def add(self, **row: Any) -> dict[str, Any]:
        response = await self.client.post("/api/v1/destinations", json=row)
        assert response.status_code in (200, 201), response.text
        return response.json()

    async def certify(self, destination_id: str) -> dict[str, Any]:
        started = await self.client.post("/api/v1/certifications", json={"destination_id": destination_id})
        assert started.status_code == 202, started.text
        await self.drain()
        run = await self.client.get(started.json()["run_url"])
        return run.json()

    async def job(self, job_id: str) -> dict[str, Any]:
        return (await self.client.get(f"/api/v1/jobs/{job_id}")).json()


@pytest.fixture
async def p2d(app_factory: Any, pg_url: str, p2d_mock_url: str, p2d_registry: Registry, fake_clock: Any) -> AsyncIterator[Env]:
    previous = get_registry()
    set_registry(p2d_registry)
    app = await app_factory(pg_url=pg_url, registry=p2d_registry, clock=fake_clock, MOCK_PLATFORM_URL=p2d_mock_url,
                            SIMULATION_MODE=True, PUBLIC_BASE_URL="http://ibx.test", CERT_LIVE_TIMEOUT_S=3.0,
                            PLATFORM_TIMEOUT_S=3.0)
    http = httpx.AsyncClient()
    worker = Worker(settings=app.state.settings, session_factory=app.state.session_factory, storage=app.state.storage,
                    clock=fake_clock, http=http, worker_id="p2d-test")
    async with api_client(app) as client:
        yield Env(app, client, worker, p2d_mock_url, fake_clock)
    await http.aclose()
    set_registry(previous)
