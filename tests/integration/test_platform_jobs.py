"""IS3 jobs: 202 → worker → status/progress/links, IS9 manifest, retries, SKIP LOCKED, signed callback, scheduler."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.session import make_engine, make_session_factory
from app.db.tables import JobRow, ScheduledTaskRow
from app.jobs import callbacks, queue
from app.jobs.registry import load_handlers
from app.jobs.scheduler import Scheduler, tick
from app.jobs.worker import Worker
from tests.conftest import api_client


@pytest.fixture
async def env(app_factory, pg_url, make_settings):
    app = await app_factory(pg_url=pg_url)
    storage = app.state.storage
    http = httpx.AsyncClient()
    worker = Worker(
        settings=app.state.settings, session_factory=app.state.session_factory, storage=storage,
        clock=app.state.clock, http=http, worker_id="w1",
    )
    async with api_client(app) as client:
        yield app, client, worker, storage
    await http.aclose()


async def test_job_lifecycle_with_links_and_manifest(env):
    app, client, worker, storage = env
    accepted = await client.post("/api/v1/jobs", json={"kind": "selfcheck", "payload": {"echo": "hi", "steps": 2}})
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    assert accepted.headers["Location"] == f"/api/v1/jobs/{job_id}"
    queued = (await client.get(f"/api/v1/jobs/{job_id}")).json()
    assert queued["status"] == "queued" and queued["progress"] == 0

    assert (await worker.run_once()).id == job_id
    done = (await client.get(f"/api/v1/jobs/{job_id}")).json()
    assert done["status"] == "succeeded" and done["progress"] == 100 and done["error"] is None
    assert done["result"]["echo"] == "hi"
    assert set(done["result"]["links"]) >= {"manifest", "report"}
    assert "/api/v1/files/jobs/" in done["result"]["links"]["manifest"]  # signed API link (IS7)

    manifest = json.loads(await storage.get_bytes(f"jobs/{job_id}/manifest.json"))
    for key in ("service", "providers", "settings", "seed", "timings_ms", "cost", "warnings", "licence", "ai_generated", "simulation"):
        assert key in manifest
    assert "API_TOKEN" not in manifest["settings"] and "S3_SECRET_KEY" not in manifest["settings"]
    assert "test-api-token" not in json.dumps(manifest)


async def test_unknown_job_is_404_envelope(env):
    _, client, _, _ = env
    response = await client.get("/api/v1/jobs/job_missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


async def test_non_public_kind_rejected(env):
    _, client, _, _ = env
    response = await client.post("/api/v1/jobs", json={"kind": "platform.housekeeping"})
    assert response.status_code == 422
    assert "selfcheck" in response.json()["error"]["details"]["public_kinds"]


async def test_retryable_failure_requeues_then_fails_after_max_attempts(env, fake_clock):
    app, client, worker, _ = env
    worker.clock = fake_clock
    job_id = (await client.post("/api/v1/jobs", json={"kind": "selfcheck", "payload": {"fail": True, "fail_retryable": True}})).json()["job_id"]
    async with app.state.session_factory() as session, session.begin():  # make it due on the fake clock
        (await session.get(JobRow, job_id)).run_after = fake_clock.now()
    await worker.run_once()
    job = (await client.get(f"/api/v1/jobs/{job_id}")).json()
    assert job["status"] == "queued" and job["attempts"] == 1
    assert job["error"]["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert await worker.run_once() is None  # back-off: not due yet
    for _ in range(2):  # selfcheck allows 3 attempts
        fake_clock.advance(3600)
        await worker.run_once()
    job = (await client.get(f"/api/v1/jobs/{job_id}")).json()
    assert job["status"] == "failed" and job["attempts"] == 3
    assert job["error"]["error"]["retryable"] is True


async def test_non_retryable_failure_fails_at_once(env):
    _, client, worker, _ = env
    job_id = (await client.post("/api/v1/jobs", json={"kind": "selfcheck", "payload": {"fail": True}})).json()["job_id"]
    await worker.run_once()
    job = (await client.get(f"/api/v1/jobs/{job_id}")).json()
    assert job["status"] == "failed" and job["attempts"] == 1
    assert job["error"]["error"]["code"] == "INPUT_FETCH_FAILED"
    assert "manifest" in job["result"]["links"]


async def test_two_workers_never_claim_the_same_job(env, pg_url):
    app, client, _, _ = env
    for _ in range(6):
        await client.post("/api/v1/jobs", json={"kind": "selfcheck"})
    engine = make_engine(pg_url)
    factory = make_session_factory(engine)
    claimed: list[str] = []

    async def claimer(worker_id: str) -> None:
        while True:
            async with factory() as session, session.begin():
                job = await queue.claim(session, worker_id, app.state.clock.now())
            if job is None:
                return
            claimed.append(job.id)

    await asyncio.gather(*[claimer(f"w{i}") for i in range(4)])
    await engine.dispose()
    assert len(claimed) == 6 and len(set(claimed)) == 6


async def test_signed_callback_is_delivered(env):
    app, client, worker, _ = env
    received: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        received["body"] = request.content
        received["signature"] = request.headers.get(callbacks.SIGNATURE_HEADER)
        return httpx.Response(204)

    job_id = (await client.post("/api/v1/jobs", json={"kind": "selfcheck", "callback_url": "http://127.0.0.1:9/hook"})).json()["job_id"]
    with respx.mock(assert_all_called=True) as router:
        router.post("http://127.0.0.1:9/hook").mock(side_effect=_capture)
        await worker.run_once()
    secret = app.state.settings.callback_key()
    assert callbacks.verify(secret, received["body"], received["signature"])
    assert not callbacks.verify(b"wrong-secret", received["body"], received["signature"])
    payload = json.loads(received["body"])
    assert payload["job_id"] == job_id and payload["status"] == "succeeded"
    async with app.state.session_factory() as session:
        assert (await session.get(JobRow, job_id)).callback_status == "delivered"


async def test_scheduler_enqueues_due_tasks_once_per_period(env, fake_clock):
    app, _, _, _ = env
    load_handlers()
    first = await tick(app.state.session_factory, fake_clock.now())
    assert len(first) >= 1  # housekeeping is due immediately
    assert await tick(app.state.session_factory, fake_clock.now()) == []
    fake_clock.advance(3601)
    assert len(await tick(app.state.session_factory, fake_clock.now())) >= 1
    async with app.state.session_factory() as session:
        task = await session.get(ScheduledTaskRow, "platform.housekeeping")
        assert task.last_job_id is not None


async def test_only_one_scheduler_is_leader(env):
    app, _, _, _ = env
    first = Scheduler(app.state.settings, app.state.engine, app.state.session_factory)
    second = Scheduler(app.state.settings, app.state.engine, app.state.session_factory)
    assert await first.try_lead() is True
    assert await second.try_lead() is False
    await first.release()
    assert await second.try_lead() is True
    await second.release()


async def test_housekeeping_purges_expired_keys(env, fake_clock):
    app, client, worker, _ = env
    await client.post("/api/v1/jobs", json={"kind": "selfcheck"}, headers={"Idempotency-Key": "old"})
    await worker.run_once()
    async with app.state.session_factory() as session, session.begin():
        await queue.enqueue(session, "platform.housekeeping", now=fake_clock.now())
    worker.clock = fake_clock
    # the key row is stamped with the app's real clock: move the fake clock 25 h past *real* now (date-proof)
    fake_clock.set(datetime.now(UTC) + timedelta(hours=25))
    job = await worker.run_once()
    async with app.state.session_factory() as session:
        row = await session.get(JobRow, job.id)
        assert row.result["idempotency_keys_purged"] == 1


async def test_listen_notify_wakes_the_worker(env):
    app, client, worker, _ = env
    stop = asyncio.Event()
    worker.settings = app.state.settings.model_copy(update={"WORKER_POLL_S": 30.0})
    listener = asyncio.create_task(worker.listen(stop))
    consumer = asyncio.create_task(worker.consume(stop))
    await asyncio.sleep(0.5)  # let LISTEN start
    job_id = (await client.post("/api/v1/jobs", json={"kind": "selfcheck"})).json()["job_id"]
    for _ in range(50):  # well under the 30 s poll interval
        await asyncio.sleep(0.1)
        if (await client.get(f"/api/v1/jobs/{job_id}")).json()["status"] == "succeeded":
            break
    stop.set()
    worker.wake.set()
    await asyncio.gather(listener, consumer)
    assert (await client.get(f"/api/v1/jobs/{job_id}")).json()["status"] == "succeeded"
    async with app.state.session_factory() as session:
        assert (await session.execute(select(JobRow.status))).scalars().all() == ["succeeded"]
