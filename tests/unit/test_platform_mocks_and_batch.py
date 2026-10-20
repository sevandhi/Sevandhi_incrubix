"""mock-platforms framework (tenants, faults, counters) and the IS10 batch runner, all in-process."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.batch.runner import load_cases, run_cases
from mocks.platforms.app import create_app as create_mock_app
from mocks.platforms.faults import parse_fault, rename_key


@pytest.fixture
def mock_client():
    from mocks.platforms.app import counter
    from mocks.platforms.faults import faults
    from mocks.platforms.tenants import store

    app = create_mock_app()

    @app.post("/demo/{tenant}/posts", status_code=201)
    async def create(tenant: str) -> dict:
        post_id = store.next_id("demo", tenant)
        return {"id": post_id, "url": f"http://demo/{tenant}/{post_id}"}

    for reset in (faults.clear, counter.reset, store.reset):
        reset()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")


async def test_tenants_auto_provision_and_are_isolated(mock_client):
    a1 = (await mock_client.post("/demo/forum-41/posts")).json()
    a2 = (await mock_client.post("/demo/forum-41/posts")).json()
    b1 = (await mock_client.post("/demo/forum-42/posts")).json()
    assert (a1["id"], a2["id"], b1["id"]) == (1, 2, 1)
    counts = (await mock_client.get("/_counts")).json()["counts"]
    assert counts["demo"]["forum-41"]["POST /posts"] == 2 and counts["demo"]["forum-42"]["POST /posts"] == 1


async def test_header_faults(mock_client):
    r429 = await mock_client.post("/demo/t/posts", headers={"X-Sim-Fault": "429:17"})
    assert r429.status_code == 429 and r429.headers["Retry-After"] == "17"
    r400 = await mock_client.post("/demo/t/posts", headers={"X-Sim-Fault": "400:rate_limit_error"})
    assert r400.status_code == 400 and r400.json()["error"] == "rate_limit_error"
    renamed = await mock_client.post("/demo/t/posts", headers={"X-Sim-Fault": "rename_field:id->post_id"})
    assert "post_id" in renamed.json() and "id" not in renamed.json()
    bad = await mock_client.post("/demo/t/posts", headers={"X-Sim-Fault": "explode"})
    assert bad.status_code == 400


async def test_queued_faults_respect_tenant_method_and_count(mock_client):
    queued = await mock_client.post("/_faults", json={"tenant": "t1", "method": "publish", "fault": "500", "count": 2})
    assert queued.status_code == 201
    statuses = [(await mock_client.post("/demo/t1/posts")).status_code for _ in range(3)]
    assert statuses == [500, 500, 201]
    assert (await mock_client.post("/demo/t2/posts")).status_code == 201
    assert (await mock_client.post("/_faults", json={"fault": "nonsense"})).status_code == 400


async def test_timeout_after_create_creates_then_hangs(mock_client):
    import asyncio

    from mocks.platforms.tenants import store

    with pytest.raises(TimeoutError):  # ASGITransport has no socket timeout, so emulate the client giving up
        await asyncio.wait_for(
            mock_client.post("/demo/slow/posts", headers={"X-Sim-Fault": "timeout_after_create:2"}), timeout=0.5
        )
    assert store.state("demo", "slow")["next_id"] == 2  # the post exists


async def test_reset_clears_everything(mock_client):
    await mock_client.post("/demo/t/posts")
    await mock_client.post("/_faults", json={"fault": "401"})
    await mock_client.post("/_reset")
    assert (await mock_client.get("/_counts")).json()["counts"] == {}
    assert (await mock_client.get("/_faults")).json()["queued"] == []


def test_fault_parsing_and_rename():
    assert parse_fault("timeout_after_create").hang_s == 30.0
    assert parse_fault("429:5").status == 429 and parse_fault("429:5").arg == "5"
    assert parse_fault("rename_field:posts.url->permalink").rename_to == "permalink"
    with pytest.raises(ValueError):
        parse_fault("999")
    assert rename_key({"posts": [{"url": 1}]}, "posts.url", "permalink") == {"posts": [{"permalink": 1}]}


# ------------------------------------------------------------------ batch runner


def test_batch_runner_writes_folders_and_summary(tmp_path: Path):
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict:
        return {"fine": True}

    @app.post("/fail")
    async def fail() -> None:
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": {"code": "PLATFORM_RATE_LIMIT", "message": "x", "retryable": True, "details": {}}}, status_code=429)

    cases_file = tmp_path / "cases.yaml"
    cases_file.write_text(
        """
cases:
  - name: health ok
    path: /ok
    expect: {status: 200}
  - name: rate limited
    method: POST
    path: /fail
    headers: {Idempotency-Key: "k-{{run_id}}", Authorization: "Bearer should-not-be-saved"}
    body: {n: 1}
    repeat: 2
    expect: {status: 429, code: PLATFORM_RATE_LIMIT}
  - name: wrong expectation
    path: /ok
    expect: {status: 201}
"""
    )
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        results = run_cases(load_cases(cases_file), tmp_path / "out", client, run_id="r1")
    assert [r.passed for r in results] == [True, True, True, False]
    rows = list(csv.DictReader((tmp_path / "out" / "summary.csv").open()))
    assert len(rows) == 4 and rows[1]["code"] == "PLATFORM_RATE_LIMIT"
    folder = tmp_path / "out" / rows[1]["folder"]
    request = json.loads((folder / "request.json").read_text())
    assert request["headers"]["Idempotency-Key"] == "k-r1"
    assert "should-not-be-saved" not in (folder / "request.json").read_text()
    assert json.loads((folder / "response.json").read_text())["status"] == 429
