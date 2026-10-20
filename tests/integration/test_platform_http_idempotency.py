"""IS4: Idempotency-Key on every POST. Same key + body → same job; different body → 409; races → one job."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.db.tables import IdempotencyKeyRow, JobRow


async def _job_count(app) -> int:
    async with app.state.session_factory() as session:
        return (await session.execute(select(func.count()).select_from(JobRow))).scalar_one()


@pytest.fixture
async def api(app_factory, pg_url):
    from tests.conftest import api_client

    app = await app_factory(pg_url=pg_url, IDEMPOTENCY_LOCK_WAIT_S=10)
    async with api_client(app) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


async def test_ten_parallel_identical_requests_create_one_job(api):
    body = {"kind": "selfcheck", "payload": {"echo": "race"}}
    responses = await asyncio.gather(
        *[api.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": "race-1"}) for _ in range(10)]
    )
    assert [r.status_code for r in responses] == [202] * 10
    assert len({r.json()["job_id"] for r in responses}) == 1
    assert sum(r.headers.get("Idempotent-Replayed") == "true" for r in responses) == 9
    assert await _job_count(api.app) == 1


async def test_same_key_same_body_replays_same_job(api):
    body = {"kind": "selfcheck", "payload": {"b": 1, "a": 2}}
    first = await api.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": "k-same"})
    # same JSON with keys in another order is the same request
    second = await api.post(
        "/api/v1/jobs", content=b'{"payload": {"a": 2, "b": 1}, "kind": "selfcheck"}',
        headers={"Idempotency-Key": "k-same", "Content-Type": "application/json"},
    )
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.headers["Idempotency-Key"] == "k-same"
    assert await _job_count(api.app) == 1


async def test_same_key_different_body_is_409(api):
    await api.post("/api/v1/jobs", json={"kind": "selfcheck", "payload": {"n": 1}}, headers={"Idempotency-Key": "k-diff"})
    response = await api.post("/api/v1/jobs", json={"kind": "selfcheck", "payload": {"n": 2}}, headers={"Idempotency-Key": "k-diff"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert response.json()["error"]["retryable"] is False
    assert await _job_count(api.app) == 1


async def test_missing_key_is_generated_echoed_and_warned(api):
    first = await api.post("/api/v1/jobs", json={"kind": "selfcheck"})
    second = await api.post("/api/v1/jobs", json={"kind": "selfcheck"})
    assert first.status_code == second.status_code == 202
    assert first.headers["Idempotency-Key"].startswith("gen-")
    assert first.headers["X-IncuBrix-Warning"] == "IDEMPOTENCY_KEY_GENERATED"
    assert first.headers["Idempotency-Key"] != second.headers["Idempotency-Key"]
    assert await _job_count(api.app) == 2


async def test_errors_are_not_stored_so_a_fixed_retry_runs(api):
    bad = await api.post("/api/v1/jobs", json={"kind": "no-such-kind"}, headers={"Idempotency-Key": "k-err"})
    assert bad.status_code == 422
    async with api.app.state.session_factory() as session:
        stored = (await session.execute(select(func.count()).select_from(IdempotencyKeyRow))).scalar_one()
    assert stored == 0
    assert await _job_count(api.app) == 0  # the failed request's writes were rolled back


async def test_expired_key_is_treated_as_new(api, fake_clock):
    api.app.state.clock = fake_clock
    first = await api.post("/api/v1/jobs", json={"kind": "selfcheck"}, headers={"Idempotency-Key": "k-ttl"})
    fake_clock.advance(25 * 3600)
    second = await api.post("/api/v1/jobs", json={"kind": "selfcheck"}, headers={"Idempotency-Key": "k-ttl"})
    assert first.json()["job_id"] != second.json()["job_id"]


async def test_invalid_key_is_400(api):
    response = await api.post("/api/v1/jobs", json={"kind": "selfcheck"}, headers={"Idempotency-Key": "bad key with spaces"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


async def test_unauthenticated_requests_never_touch_the_store(api):
    from tests.conftest import api_client

    async with api_client(api.app, token="wrong") as anon:
        response = await anon.post("/api/v1/jobs", json={"kind": "selfcheck"}, headers={"Idempotency-Key": "k-anon"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert await _job_count(api.app) == 0
