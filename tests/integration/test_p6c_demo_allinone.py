"""The all-in-one demo app (API + in-process worker/scheduler + mock platforms) on a real Postgres.

Starts ``create_demo_app`` with its real lifespan: the mock platforms listen on 127.0.0.1:<free port> behind the
``mock-platforms`` alias, the start-up ``demo.seed`` job creates and certifies the 14 demo destinations, and
``POST /api/v1/sim/reset`` resets them. Also covers the /sim proxy rules and the simulation-only guard.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.tables import DestinationRow, PublishIntentRow
from app.demo.allinone import create_demo_app, uninstall_host_aliases
from app.demo.seed import demo_rows
from app.storage import MemoryStorage
from tests.conftest import api_client

LEVELS = {"demo-discourse": "L4", "demo-lemmy": "L4", "demo-ghost": "L3", "demo-webhook": "L3", "demo-devto": "L2",
          "demo-assisted": "L1", "demo-slack": "L4", "demo-peertube": "L4", "demo-mrss": "L2",
          "dest-weekly-cap-1": "L4", "dest-no-promo": "L4", "dest-ai-disclosure-es": "L4", "dest-strict-limits": "L4",
          "dest-partner-revocable": "L3"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_job(client: Any, job_id: str, timeout_s: float = 240) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        job = (await client.get(f"/api/v1/jobs/{job_id}")).json()
        if job["status"] in ("succeeded", "failed") or asyncio.get_running_loop().time() > deadline:
            return job
        await asyncio.sleep(0.5)


async def _levels(client: Any) -> dict[str, tuple[str, str]]:
    body = (await client.get("/api/v1/destinations", params={"limit": 500})).json()
    items = body.get("destinations") or body.get("items") or []
    return {d["name"]: (d["level"], d.get("destination_id") or d.get("id")) for d in items if d["name"] in LEVELS}


async def _wait_levels(client: Any, timeout_s: float = 240) -> dict[str, tuple[str, str]]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        levels = await _levels(client)
        if (len(levels) == len(LEVELS) and all(levels[n][0] == LEVELS[n] for n in LEVELS)) \
                or asyncio.get_running_loop().time() > deadline:
            return levels
        await asyncio.sleep(0.5)


@pytest.fixture
async def demo(make_settings: Any, pg_url: str) -> AsyncIterator[Any]:
    settings = make_settings(DATABASE_URL=pg_url, SIMULATION_MODE=True, APP_ENV="test",
                             MOCK_PLATFORM_URL=f"http://mock-platforms:{_free_port()}", DEMO_SEED_ON_START=True,
                             WORKER_CONCURRENCY=3, WORKER_POLL_S=0.2, SCHEDULER_TICK_S=0.5, CERT_LIVE_TIMEOUT_S=5.0,
                             PLATFORM_TIMEOUT_S=5.0, RATE_LIMIT_PER_MIN=0, PUBLIC_BASE_URL="http://ibx.test")
    app = create_demo_app(settings, storage=MemoryStorage())
    try:
        async with app.router.lifespan_context(app):
            async with api_client(app) as client:
                yield app, client
    finally:
        uninstall_host_aliases()


async def test_demo_seeds_certifies_serves_sim_and_resets(demo):
    app, client = demo
    assert len(demo_rows()) == len(LEVELS)

    ready = await client.get("/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["checks"]["mock_platforms"]["ok"] is True

    levels = await _wait_levels(client)
    assert {n: lv for n, (lv, _) in levels.items()} == LEVELS

    # /sim: GET is open (no token), anything else needs the bearer token
    async with api_client(app, token=None) as anon:
        counts = await anon.get("/sim/_counts", params={"platform": "discourse"})
        assert counts.status_code == 200 and "discourse" in counts.json()["counts"]
        refused = await anon.post("/sim/_reset")
        assert refused.status_code == 401 and refused.json()["error"]["code"] == "UNAUTHORIZED"
        assert (await anon.post("/api/v1/sim/reset")).status_code == 401
        # the demo token may also come in X-IncuBrix-Demo-Token, so Authorization can carry the simulated platform's
        # own credentials (a mock Lemmy admin lowering its post rate limit); the demo header is not forwarded
        demo_token = app.state.settings.API_TOKEN.get_secret_value()
        lowered = await anon.put("/sim/lemmy/demo-rl/api/v3/site", json={"rate_limit_post": 1, "rate_limit_post_per_second": 60},
                                  headers={"X-IncuBrix-Demo-Token": demo_token, "Authorization": "Bearer mock-admin"})
        assert lowered.status_code == 200, lowered.text
        wrong = await anon.post("/sim/_reset", headers={"X-IncuBrix-Demo-Token": "not-the-token"})
        assert wrong.status_code == 401
    assert (await client.get("/sim/health")).json()["status"] == "ok"

    # use up dest-weekly-cap-1's cap through the publications API, so the reset has history to clear
    cap_id = levels["dest-weekly-cap-1"][1]
    first = await client.post("/api/v1/publications", json={
        "destination_id": cap_id, "content": {"title": "Demo reset check", "body": "Plain update, no links.",
                                              "is_promotional": False}})
    assert first.status_code in (200, 201, 202), first.text
    factory = app.state.session_factory
    publication_id = first.json()["publication_id"]
    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        state = (await client.get(f"/api/v1/publications/{publication_id}")).json()["state"]
        if state not in ("queued", "pending", "publishing"):
            break
        await asyncio.sleep(0.3)
    assert state == "published", state
    # a fault queued on the mock platforms is dropped by the reset
    assert (await client.post("/api/v1/sim/faults", json={"platform": "discourse", "fault": "500", "count": 5})).status_code == 201

    async def intents() -> int:
        async with factory() as session:
            return (await session.execute(select(func.count()).select_from(PublishIntentRow).where(
                PublishIntentRow.destination_key.like("discourse|%dest-weekly-cap-1%")))).scalar_one()

    assert await intents() >= 2  # the certification test post + the real publication

    started = await client.post("/api/v1/sim/reset")
    assert started.status_code == 202, started.text
    job = await _wait_job(client, started.json()["job_id"])
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["reset"]["destinations_deleted"] == len(LEVELS)
    assert result["reset"]["mock_faults_cleared"] is True
    assert sorted(result["created"]) == sorted(LEVELS)
    assert {n: r["state"] for n, r in result["certification_results"].items()} == {n: "PASSED" for n in LEVELS}
    assert await intents() >= 1  # only the new certification test post
    assert (await client.get("/api/v1/sim/faults")).json()["queued"] == []

    after = await _levels(client)
    assert {n: lv for n, (lv, _) in after.items()} == LEVELS
    assert after["dest-weekly-cap-1"][1] != cap_id  # created again
    async with factory() as session:
        total = (await session.execute(select(func.count()).select_from(DestinationRow))).scalar_one()
    assert total == len(LEVELS)


async def test_reset_is_refused_outside_simulation(client):
    response = await client.post("/api/v1/sim/reset")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SIMULATION_ONLY"
