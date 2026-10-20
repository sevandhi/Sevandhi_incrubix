"""Integration regression tests (real Postgres, mock-platforms on a real socket):

* IS4: the contract publish takes the ``Idempotency-Key`` header when the body has none; a mismatch is 409.
* a contract ``build_assist_pack`` runs the same rules gate as publish (a promo pack for a
  no-promo destination is 409 DEST_RULE_SELF_PROMO; "no automation" is satisfied because a person posts).
* Discourse 403 on a category the key may not post in is a content refusal that does not pause
  the connection; a dead key is still CONNECTION_AUTH_EXPIRED.
* a running job's lease is renewed, and a stale job that used up its attempts fails.
Each test was written before its fix and seen failing (except the IS4 guard, which already held).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from app.db.tables import JobRow
from app.jobs import queue
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.jobs.worker import Worker
from tests.conftest import SAMPLE_KEY, api_client

CONNECTION = {"connector_key": SAMPLE_KEY, "auth_type": "api_key", "credentials": {"token": "tok-p3a-1"}}


@pytest.fixture
async def sample_api(app_factory, pg_url, mock_platforms_url, sample_registry, fake_clock):
    app = await app_factory(pg_url=pg_url, registry=sample_registry, clock=fake_clock, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        yield client


@pytest.fixture
async def real_api(app_factory, pg_url, mock_platforms_url, fake_clock):
    app = await app_factory(pg_url=pg_url, clock=fake_clock, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


# ------------------------------------------------------------------ IS4 header on the contract publish


@pytest.mark.parametrize("prefix", ["/connector/v1", "/api/v1/connector"])
async def test_is4_header_key_is_used_when_the_body_has_none(sample_api, mock_platforms_url, prefix):
    tenant = f"is4{prefix.count('/')}"
    body = {"rendition": {"title": "Hello", "body": "A first post", "ai_generated": False},
            "destination": {"connector_key": SAMPLE_KEY, "instance": {"base_url": f"{mock_platforms_url}/{SAMPLE_KEY}/{tenant}"}},
            "connection": CONNECTION}
    url = f"{prefix}/{SAMPLE_KEY}/publish"
    first = await sample_api.post(url, json=body, headers={"Idempotency-Key": f"hdr-{tenant}"})
    assert first.status_code == 200, first.text
    assert first.headers["Idempotency-Key"] == f"hdr-{tenant}" and "X-IncuBrix-Warning" not in first.headers
    again = await sample_api.post(url, json=body, headers={"Idempotency-Key": f"hdr-{tenant}"})
    assert again.status_code == 200 and again.headers.get("Idempotent-Replayed") == "true"
    assert again.json()["platform_post_id"] == first.json()["platform_post_id"]
    mismatch = await sample_api.post(url, json={**body, "idempotency_key": "body-key"}, headers={"Idempotency-Key": "other"})
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert mismatch.json()["error"]["details"]["reason"] == "header_body_mismatch"
    counts = httpx.get(f"{mock_platforms_url}/_counts", params={"platform": SAMPLE_KEY, "tenant": tenant}).json()["counts"]
    assert counts[SAMPLE_KEY][tenant]["POST /posts"] == 1


# ------------------------------------------------------------------ rules gate on build_assist_pack


def _assist_request(**rendition: Any) -> dict[str, Any]:
    return {
        "rendition": {"title": "Our course is live", "body": "Buy my course today with code SAVE50 at https://mycourse.example.com/buy",
                      "is_promotional": True, "owner_domains": ["mycourse.example.com"], "ai_generated": False, **rendition},
        "destination": {"connector_key": "assisted", "instance": {"submit_url": "https://news-p3a.example.org/submit"},
                        "rules": {"self_promo_allowed": False, "automation_allowed": False}},
    }


async def test_promo_pack_for_a_no_promo_destination_is_blocked(real_api):
    response = await real_api.post("/connector/v1/assisted/build_assist_pack", json=_assist_request())
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "DEST_RULE_SELF_PROMO" and error["retryable"] is False and error["message"]
    storage = real_api.app.state.storage
    assert not [k for k in storage.objects if k.startswith("assist/")]  # no pack, no prefilled deep link


async def test_neutral_pack_passes_because_a_person_posts_it(real_api):
    request = _assist_request(title="How do you test forum posts?", body="We compare three ways to test posts. What works for you?",
                              is_promotional=False, owner_domains=[])
    response = await real_api.post("/connector/v1/assisted/build_assist_pack", json=request)
    assert response.status_code == 200, response.text
    pack = response.json()
    assert pack["deep_link"].startswith("https://news-p3a.example.org/submit")
    assert any("automated posting is not allowed" in line for line in pack["checklist"])


# ------------------------------------------------------------------ Discourse 403 on a category


async def _discourse_publish(api: httpx.AsyncClient, mock: str, tenant: str, connection_id: str, key: str, category: int) -> httpx.Response:
    body = {"rendition": {"title": "A question about long-tail forums", "body": "What has worked for you when testing posts?",
                          "ai_generated": False},
            "destination": {"connector_key": "discourse", "instance": {"base_url": f"{mock}/discourse/{tenant}", "category_id": category}},
            "connection": {"connector_key": "discourse", "connection_id": connection_id}, "idempotency_key": key}
    return await api.post("/connector/v1/discourse/publish", json=body)


async def test_discourse_403_on_a_category_is_content_rejected_and_keeps_the_connection(real_api, mock_platforms_url):
    tenant = "p3a-cat"
    conn = (await real_api.post("/api/v1/connections", json={"connector_key": "discourse",
                                                              "credentials": {"api_key": "good-key-p3a", "api_username": "system"}})).json()
    httpx.post(f"{mock_platforms_url}/discourse/{tenant}/_mock/restrict_category", json={"category": 7}).raise_for_status()
    refused = await _discourse_publish(real_api, mock_platforms_url, tenant, conn["connection_id"], "cat-1", 7)
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "PLATFORM_CONTENT_REJECTED" and error["details"]["reason"] == "category_permission"
    assert (await real_api.get(f"/api/v1/connections/{conn['connection_id']}")).json()["status"] == "active"
    ok = await _discourse_publish(real_api, mock_platforms_url, tenant, conn["connection_id"], "cat-2", 1)
    assert ok.status_code == 200, ok.text  # the connection still works for other categories


async def test_discourse_403_with_a_dead_key_still_pauses_the_connection(real_api, mock_platforms_url):
    conn = (await real_api.post("/api/v1/connections", json={"connector_key": "discourse",
                                                              "credentials": {"api_key": "revoked-key-p3a"}})).json()
    refused = await _discourse_publish(real_api, mock_platforms_url, "p3a-dead", conn["connection_id"], "dead-1", 1)
    assert refused.status_code == 424 and refused.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED"
    assert (await real_api.get(f"/api/v1/connections/{conn['connection_id']}")).json()["status"] != "active"


# ------------------------------------------------------------------ job lease renewal and stale jobs

SLOW: dict[str, Any] = {}


@job_handler("p3a.slow_test_job")
async def _slow_job(ctx: JobContext) -> dict[str, Any]:
    ctx.clock.advance(1000)  # type: ignore[attr-defined] - FakeClock: "1000 s later" while the job still runs
    await asyncio.sleep(SLOW.get("sleep_s", 0.6))
    async with ctx.session_factory() as session:
        SLOW["locked_at"] = (await session.get(JobRow, ctx.job_id)).locked_at
    return {"ok": True}


async def test_a_running_job_renews_its_lease(app_factory, pg_url, fake_clock):
    app = await app_factory(pg_url=pg_url, clock=fake_clock, JOB_STALE_AFTER_S=1)
    factory = app.state.session_factory
    async with factory() as session, session.begin():
        job = await queue.enqueue(session, "p3a.slow_test_job", {}, now=fake_clock.now())
    started = fake_clock.now()
    worker = Worker(settings=app.state.settings, session_factory=factory, storage=app.state.storage, clock=fake_clock,
                    http=httpx.AsyncClient(), worker_id="w-lease")
    assert (await worker.run_once()).id == job.id
    assert SLOW["locked_at"] >= started + timedelta(seconds=1000)  # renewed during the run, so it is never "stale"


async def test_a_stale_job_without_attempts_left_fails_instead_of_looping(app_factory, pg_url, fake_clock):
    app = await app_factory(pg_url=pg_url, clock=fake_clock)
    factory = app.state.session_factory
    now = fake_clock.now()
    async with factory() as session, session.begin():
        dead = await queue.enqueue(session, "selfcheck", {}, now=now)
        alive = await queue.enqueue(session, "selfcheck", {}, now=now)
        for job, attempts in ((dead, dead.max_attempts), (alive, 1)):
            await session.execute(update(JobRow).where(JobRow.id == job.id).values(
                status="running", locked_at=now - timedelta(hours=2), locked_by="gone", attempts=attempts))
    async with factory() as session, session.begin():
        assert await queue.requeue_stale(session, now - timedelta(hours=1), now=now) == 1
    async with factory() as session:
        failed, requeued = await session.get(JobRow, dead.id), await session.get(JobRow, alive.id)
    assert failed.status == "failed" and failed.error["error"]["code"] == "INTERNAL_ERROR"
    assert "worker" in failed.error["error"]["message"]
    assert requeued.status == "queued"
