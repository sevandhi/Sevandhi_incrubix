"""OUT3: the recertification tick (clock jump), demotion + exclusion + alert delivery, restore on pass, self-heal,
bounded concurrency, rules drift, network health. In process, simulation mode, real Postgres, real worker."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
from sqlalchemy import select, update

from app.db.tables import AlertRow, CertificationRunRow, CircuitStateRow, LevelHistoryRow, RuleSetRow
from app.jobs.callbacks import verify
from app.recert import scheduler
from tests.p2d.test_p2d_flows import WEEKLY, blocked_publication, forum_row, post
from tests.p5a.conftest import WEBHOOK_SECRET

DAY = 86400


async def tick(env, **body):
    response = await env.client.post("/api/v1/recert/tick", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def dest_of(env, did):
    return (await env.client.get(f"/api/v1/destinations/{did}")).json()


def deliveries(env):
    out = []
    for d in httpx.get(f"{env.mock}/alertsink/_deliveries").json()["deliveries"]:
        assert verify(WEBHOOK_SECRET.encode(), d["body"].encode(), d["headers"]["x-incubrix-signature"])  # HMAC checked
        out.append(json.loads(d["body"]))
    return out


async def test_clock_jump_recertifies_demotes_alerts_excludes_and_restores(p5a):
    dest = await p5a.add(**forum_row(p5a.mock, "Recert Forum", **WEEKLY))
    did = dest["destination_id"]
    assert (await p5a.certify(did))["level_after"] == "L4"

    # day 20: not due yet (RECERT_AFTER_DAYS=21)
    early = await tick(p5a, now=(p5a.clock.now() + timedelta(days=20)).isoformat(), dry_run=True)
    assert did not in [e["destination_id"] for e in early["would_enqueue"]]

    # clock jump to day 22: due → a scheduled run passes, last_certified_at moves on
    p5a.clock.advance(22 * DAY)
    first = await tick(p5a)
    [item] = [e for e in first["enqueued"] if e["destination_id"] == did]
    assert item["reason"] == "stale" and item["trigger"] == "scheduled"
    again = await tick(p5a)  # the run is queued: a second tick does not double it
    assert did in [s["destination_id"] for s in again["skipped"]]
    await p5a.drain()
    after = await dest_of(p5a, did)
    assert after["level"] == "L4" and after["status"] == "active"
    assert after["last_certified_at"].startswith(p5a.clock.now().date().isoformat())

    # the platform renames a field; 22 days later the recertification fails → demoted, excluded, alert delivered
    httpx.post(f"{p5a.mock}/_faults", json={"platform": "fakeforum", "tenant": "*", "method": "POST",
                                            "fault": "rename_field:id->post_id", "count": 1}).raise_for_status()
    p5a.clock.advance(22 * DAY)
    second = await tick(p5a)
    run_id = next(e["run_id"] for e in second["enqueued"] if e["destination_id"] == did)
    await p5a.drain()
    run = (await p5a.client.get(f"/api/v1/certifications/{run_id}")).json()
    assert run["state"] == "DEMOTED" and run["trigger"] == "scheduled" and run["result"]["failed_step"] == "PUBLISH"
    demoted = await dest_of(p5a, did)
    assert demoted["level"] == "L0" and demoted["status"] == "excluded"
    async with p5a.app.state.session_factory() as session:
        history = (await session.execute(select(LevelHistoryRow).where(LevelHistoryRow.destination_id == did)
                                         .order_by(LevelHistoryRow.id))).scalars().all()
        alert = (await session.execute(select(AlertRow).where(AlertRow.destination_id == did))).scalar_one()
    assert (history[-1].level_before, history[-1].level_after, history[-1].run_id) == ("L4", "L0", run_id)
    assert alert.kind == "certification_failed" and alert.status == "open"
    assert set(alert.delivered_to) >= {"log", "webhook", "github_issue#1"}
    [opened] = [d for d in deliveries(p5a) if d["alert_id"] == alert.id]
    assert opened["event"] == "alert.opened" and opened["destination"]["status"] == "excluded"
    assert opened["run"]["trigger"] == "scheduled" and "demoted to L0" in opened["summary"]
    assert "response_schema_mismatch" in opened["summary"] and "outcome unknown" not in opened["summary"]
    await blocked_publication(p5a, await post(p5a, did, expect=409), "DESTINATION_EXCLUDED")  # the exclusion check
    health = (await p5a.client.get("/api/v1/network/health", params={"destination_id": did})).json()["destinations"][0]
    assert health["failure_streak"] == 1 and health["open_alerts"] == ["certification_failed"]

    # not retried before RECERT_RETRY_EXCLUDED_AFTER_S; then it passes again → restored, alert resolved + delivered
    soon = await tick(p5a)
    assert any(s["destination_id"] == did and "next retry" in s["why"] for s in soon["skipped"])
    p5a.clock.advance(DAY + 60)
    retry = await tick(p5a)
    assert [e["trigger"] for e in retry["enqueued"] if e["destination_id"] == did] == ["recert_retry"]
    await p5a.drain()
    restored = await dest_of(p5a, did)
    assert restored["level"] == "L4" and restored["status"] == "active" and restored["excluded_reason"] is None
    async with p5a.app.state.session_factory() as session:
        alert = await session.get(AlertRow, alert.id)
    assert alert.status == "resolved" and {"webhook:resolved", "github_issue#1:closed"} <= set(alert.delivered_to)
    assert [d["event"] for d in deliveries(p5a) if d["alert_id"] == alert.id] == ["alert.opened", "alert.resolved"]
    issues = httpx.get(f"{p5a.mock}/alertsink/github/_issues").json()["issues"]
    assert issues[0]["state"] == "closed" and issues[0]["authorized"] is True
    assert (await p5a.client.get("/api/v1/network/health", params={"destination_id": did})).json()["destinations"][0][
        "failure_streak"] == 0


async def test_manually_excluded_destinations_are_never_retried(p5a):
    dest = await p5a.add(**forum_row(p5a.mock, "Manual Out", **WEEKLY))
    did = dest["destination_id"]
    await p5a.certify(did)
    patched = await p5a.client.patch(f"/api/v1/destinations/{did}", json={"status": "excluded", "excluded_reason": "owner asked"})
    assert patched.status_code == 200, patched.text
    p5a.clock.advance(40 * DAY)
    due = await tick(p5a, dry_run=True)
    assert did not in [e["destination_id"] for e in due["would_enqueue"]]


async def test_self_heal_after_schema_mismatch_and_open_breaker(p5a_factory):
    env = await p5a_factory(RECERT_SELF_HEAL_COOLDOWN_S=0)
    did = (await env.add(**forum_row(env.mock, "Self Heal", **WEEKLY)))["destination_id"]
    await env.certify(did)
    published = await post(env, did)
    await env.drain()
    pub = (await env.client.get(f"/api/v1/publications/{published['publication']['publication_id']}")).json()
    httpx.post(f"{env.mock}/_faults", json={"platform": "fakeforum", "tenant": "*", "method": "GET",
                                            "fault": "rename_field:views->views_total", "count": 1}).raise_for_status()
    failed = await env.client.post("/api/v1/metrics/fetch", json={"publication_id": pub["publication_id"]})
    assert failed.status_code == 503 and failed.json()["error"]["details"]["reason"] == "response_schema_mismatch"

    scan = (await env.client.post("/api/v1/recert/self-heal")).json()
    [heal] = [e for e in scan["enqueued"] if e["destination_id"] == did]
    assert heal["reason"] == "schema_mismatch"
    await env.drain()
    async with env.app.state.session_factory() as session:
        run = await session.get(CertificationRunRow, heal["run_id"])
    assert run.trigger == "self_heal" and run.state == "PASSED"  # the platform is fine again: level kept
    assert not (await env.client.post("/api/v1/recert/self-heal")).json()["enqueued"]  # nothing new since that run

    # an open breaker on the same scope → a self-heal run that starts after open_until
    async with env.app.state.session_factory() as session, session.begin():
        await session.execute(update(CircuitStateRow).where(CircuitStateRow.scope == heal["scope"])
                              .values(state="open", consecutive_failures=5, open_until=env.clock.now() + timedelta(seconds=300)))
    scan = (await env.client.post("/api/v1/recert/self-heal")).json()
    [heal2] = [e for e in scan["enqueued"] if e["destination_id"] == did]
    assert heal2["reason"] == "breaker_open" and heal2["run_after"] > env.clock.now().isoformat()


async def test_bounded_concurrency_slots(p5a_factory):
    env = await p5a_factory(RECERT_CONCURRENCY=1)
    factory = env.app.state.session_factory
    async with scheduler.recert_slot(factory, 1) as first:
        assert first == 0
        async with scheduler.recert_slot(factory, 1) as second:
            assert second is None  # the only slot is taken (cluster-wide advisory lock)
    async with scheduler.recert_slot(factory, 1) as again:
        assert again == 0

    did = (await env.add(**forum_row(env.mock, "Busy Slots", **WEEKLY)))["destination_id"]
    await env.certify(did)
    env.clock.advance(22 * DAY)
    run_id = next(e["run_id"] for e in (await tick(env))["enqueued"] if e["destination_id"] == did)
    async with scheduler.recert_slot(factory, 1):
        assert await env.worker.run_once() is not None  # deferred: no slot
    async with factory() as session:
        run = await session.get(CertificationRunRow, run_id)
    assert run.state == "QUEUED"
    env.clock.advance(120)
    await env.drain()
    async with factory() as session:
        run = await session.get(CertificationRunRow, run_id)
    assert run.state == "PASSED"


async def test_rules_drift_needs_review_alert_and_reconfirm(p5a):
    page = f"{p5a.mock}/alertsink/pages/drift-rules"
    httpx.put(page, json={"html": "<html><body><h1>Rules</h1><p>One post per week. No self-promotion.</p></body></html>"})
    did = (await p5a.add(**forum_row(p5a.mock, "Drift Forum", rules_url=page, **WEEKLY)))["destination_id"]
    await p5a.certify(did)
    p5a.clock.advance(22 * DAY)
    await tick(p5a)
    await p5a.drain()
    jobs = (await p5a.client.get("/api/v1/certifications", params={"destination_id": did})).json()
    assert jobs  # the recert ran
    health = (await p5a.client.get("/api/v1/network/health", params={"destination_id": did})).json()["destinations"][0]
    assert health["rules"]["status"] == "confirmed" and health["rules"]["watch"]["baseline_sha256"]

    httpx.put(page, json={"html": "<html><body><h1>Rules</h1><p>No links at all. Posts by humans only.</p></body></html>"})
    p5a.clock.advance(22 * DAY)
    await tick(p5a)
    await p5a.drain()
    async with p5a.app.state.session_factory() as session:
        sets = (await session.execute(select(RuleSetRow).where(RuleSetRow.destination_id == did))).scalars().all()
        alert = (await session.execute(select(AlertRow).where(AlertRow.destination_id == did,
                                                              AlertRow.kind == "rules_drift"))).scalar_one()
    assert [s.status for s in sets] == ["needs_review"] and alert.status == "open" and "webhook" in alert.delivered_to
    assert alert.body["baseline_sha256"] != alert.body["current_sha256"]
    await blocked_publication(p5a, await post(p5a, did, expect=409), "DEST_RULES_UNCONFIRMED")

    confirmed = await p5a.client.put(f"/api/v1/destinations/{did}/rules",
                                     json={"rules": {"frequency_cap": "1/P7D", "self_promo": "disallowed", "link_policy": "none"},
                                           "confirmed_by": "p5a-test"})
    assert confirmed.status_code == 200, confirmed.text
    from app.jobs import queue

    async with p5a.app.state.session_factory() as session, session.begin():
        await queue.enqueue(session, "alerts.deliver", {}, now=p5a.clock.now())
    await p5a.drain()
    async with p5a.app.state.session_factory() as session:
        alert = await session.get(AlertRow, alert.id)
    assert alert.status == "resolved" and "webhook:resolved" in alert.delivered_to


async def test_network_health_page_and_schedule(p5a):
    page = await p5a.client.get("/ui/network", headers={"Authorization": ""})
    assert page.status_code == 200 and "Content-Security-Policy" in page.headers and "/api/v1/network/health" in page.text
    assert (await p5a.client.get("/api/v1/network/health", headers={"Authorization": ""})).status_code == 401
    schedule = (await p5a.client.get("/api/v1/recert/schedule")).json()
    assert schedule["recert_after_days"] == 21 and set(schedule["alert_sinks"]) == {"log", "webhook", "github_issue"}
    health = (await p5a.client.get("/api/v1/network/health")).json()
    assert set(health["summary"]) >= {"destinations", "by_level", "recert_overdue", "failing", "rules_need_review", "open_alerts"}
