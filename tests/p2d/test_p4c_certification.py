"""ADV5: ONE catalogue row (no connection, no code) certifies a destination of each new connector type in
simulation, through the real certification harness, job queue and evidence pack.

MRSS and PeerTube need the certification harness to confirm "live" and "deleted" through the platform API
(``Connector.live_check_by_api``): the MRSS item URL is on PUBLIC_BASE_URL, which the worker can not reach inside
compose, and PeerTube's watch page answers 200 even after deletion (measured on v8.3.0). Until the proposed two-line
harness change is applied, these tests apply exactly that change's effect with ``monkeypatch``
(adding the connector to ``LIVE_BY_API``); once the harness reads the attribute itself, nothing is patched.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.certification import harness
from app.contract.registry import get_registry, set_registry
from app.jobs.worker import Worker
from tests.conftest import api_client
from tests.p2d.conftest import Env

LEVEL = {"slack": "L4", "peertube": "L4", "mrss": "L2"}
ROWS = {
    "slack": [{"url": "https://ibx-test.slack.com/archives/C0ONEROW1", "instance_channel_id": "C0ONEROW1"},
              {"url": "https://ibx-test.slack.com/archives/C0ONEROW2", "instance_channel_id": "C0ONEROW2",
               "instance_api_base": "{mock}/slack/one-row/api"}],
    "peertube": [{"url": "https://videos-one.example.org"}, {"url": "{mock}/peertube/one-row"}],
    "mrss": [{"url": "https://feeds-one.example.org/api/v1/feeds/one-row/mrss.xml", "instance_feed_key": "one-row"}],
}
CASES = [(key, i, row) for key, rows in ROWS.items() for i, row in enumerate(rows)]


def harness_reads_the_attribute() -> bool:
    return "live_check_by_api" in inspect.getsource(harness.Harness)


@pytest.fixture
async def env(app_factory: Any, pg_url: str, p2d_mock_url: str, p2d_registry: Any, fake_clock: Any,
              monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    if not harness_reads_the_attribute():  # the proposed harness diff, applied in-test only (see module doc)
        for key in ("mrss", "peertube"):
            monkeypatch.setitem(harness.LIVE_BY_API, key, p2d_registry.classes[key].live_check_by_api)
    previous = get_registry()
    set_registry(p2d_registry)
    # PUBLIC_BASE_URL like compose's (127.0.0.1:<published port>): an own host the worker can not reach
    app = await app_factory(pg_url=pg_url, registry=p2d_registry, clock=fake_clock, MOCK_PLATFORM_URL=p2d_mock_url,
                            SIMULATION_MODE=True, PUBLIC_BASE_URL="http://127.0.0.1:9", CERT_LIVE_TIMEOUT_S=3.0,
                            PLATFORM_TIMEOUT_S=3.0)
    http = httpx.AsyncClient()
    worker = Worker(settings=app.state.settings, session_factory=app.state.session_factory, storage=app.state.storage,
                    clock=fake_clock, http=http, worker_id="env-test")
    async with api_client(app) as client:
        yield Env(app, client, worker, p2d_mock_url, fake_clock)
    await http.aclose()
    set_registry(previous)


@pytest.mark.parametrize(("connector", "i", "row"), CASES, ids=[f"{k}-{i}" for k, i, _ in CASES])
async def test_one_row_certifies_each_new_connector(env, connector, i, row):
    values = {k: v.format(mock=env.mock) for k, v in row.items()}
    dest = await env.add(name=f"One row {connector} {i}", connector_key=connector, **values)
    run = await env.certify(dest["destination_id"])
    failed = [(s["step"], s.get("detail")) for s in run["steps"] if s["outcome"] != "ok"]
    assert run["state"] == "PASSED", failed
    assert run["level_after"] == LEVEL[connector]
    steps = [s["step"] for s in run["steps"]]
    assert steps[:4] == ["PREFLIGHT", "VALIDATE", "PUBLISH", "CONFIRM_LIVE"] and steps[-2:] == ["DELETE", "CONFIRM_DELETED"]
    assert "METRICS" in steps
    audit = (await env.client.get("/api/v1/test-posts")).json()
    assert audit["still_live"] == 0
    # the evidence pack is in storage: run.json + one file per step + the deletion confirmation
    keys = [k for k in env.storage.objects if k.startswith(f"certifications/{dest['destination_id']}/")]
    assert any(k.endswith("run.json") for k in keys) and any(k.endswith("deletion_confirmation.json") for k in keys)
