"""Regression tests (in process, simulation mode):

* ``/api/v1/publications`` refuses a destination below L1 (never certified) with DESTINATION_NOT_CERTIFIED;
  a rule block still names the rule; certification posts are the only exception.
* an assisted task can end as posted | rejected_by_community | removed_by_community | abandoned, with a reason.
* the test-post audit counts deleted, soft_deleted and unpublished_to_draft separately; not_created and
  sweeper_not_found never count as deleted.
* a rate limit during certification reschedules the run and keeps the level (no exclusion).
* a real-host refusal at PREFLIGHT is FAILED_PRECONDITION: no demotion, no exclusion.
* Dev.to certifies through the author path; its removal is named CONFIRM_REMOVED /
  removal_confirmation.json; the admin path is labelled as our own Forem mock only.
Each test was written before its fix and seen failing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.tables import AlertRow, CertificationRunRow, DestinationRow, JobRow, TestPostRow
from tests.p2d.test_p2d_flows import WEEKLY, forum_row, post

# ------------------------------------------------------------------ certify before publishing


async def test_publication_to_an_uncertified_destination_is_refused(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Never Certified", **WEEKLY))
    assert dest["level"] == "L0"
    refused = await post(p2d, dest["destination_id"], expect=409)
    error = refused["error"]
    assert error["code"] == "DESTINATION_NOT_CERTIFIED" and error["retryable"] is False
    assert "certif" in error["message"].lower()
    pub = (await p2d.client.get(f"/api/v1/publications/{error['details']['publication_id']}")).json()
    assert pub["state"] == "blocked" and pub["code"] == "DESTINATION_NOT_CERTIFIED" and pub["job_ids"] == []
    await p2d.drain()
    assert "never-certified-example-org" not in httpx.get(f"{p2d.mock}/_counts").json()["counts"].get("fakeforum", {})

    assert (await p2d.certify(dest["destination_id"]))["state"] == "PASSED"  # certification is the exception
    assert (await post(p2d, dest["destination_id"]))["publication"]["state"] == "pending"


async def test_a_rule_block_on_an_uncertified_destination_still_names_the_rule(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Uncertified No Promo", **WEEKLY))
    refused = await post(p2d, dest["destination_id"], expect=409, title="Our new tool is live",
                         body="Sign up today with code LAUNCH20 at https://mytool.example.com/?utm_source=forum",
                         is_promotional=True, owner_domains=["mytool.example.com"])
    assert refused["error"]["code"] == "DEST_RULE_SELF_PROMO"


# ------------------------------------------------------------------ assisted outcomes


async def _assisted(p2d, name: str) -> dict:
    dest = await p2d.add(name=name, url=f"{p2d.mock}/fakeassist/{name.lower().replace(' ', '-')}/submit", connector_key="fakeassist",
                         frequency_cap="1/P7D", self_promo_allowed="false", automation_allowed="false")
    assert (await p2d.certify(dest["destination_id"]))["state"] == "PASSED"
    return dest


async def _awaiting(p2d, dest: dict, title: str) -> str:
    pub = await post(p2d, dest["destination_id"], title=title)
    await p2d.drain()
    out = (await p2d.client.get(pub["publication_url"])).json()
    assert out["state"] == "awaiting_human", out
    return out["publication_id"]


async def test_rejected_by_the_community_is_recorded_with_its_reason(p2d):
    dest = await _assisted(p2d, "Rejects Posts")
    pub_id = await _awaiting(p2d, dest, "A question for the community?")
    missing = await p2d.client.post(f"/api/v1/assist-tasks/{pub_id}/complete", json={"outcome": "rejected_by_community"})
    assert missing.status_code == 422  # a reason is required for anything but "posted"
    done = await p2d.client.post(f"/api/v1/assist-tasks/{pub_id}/complete",
                                 json={"outcome": "rejected_by_community", "reason": "Mods: off-topic for this board",
                                       "completed_by": "ops"})
    assert done.status_code == 200, done.text
    out = done.json()
    assert out["state"] == "failed" and out["code"] == "PLATFORM_CONTENT_REJECTED"
    assert out["error"]["error"]["details"]["outcome"] == "rejected_by_community"
    assert out["error"]["error"]["details"]["platform_message"] == "Mods: off-topic for this board"
    assert out["reason"] == "Mods: off-topic for this board"


async def test_removed_after_posting_and_abandoned_frees_the_cap_slot(p2d):
    dest = await _assisted(p2d, "Removes Posts")
    pub_id = await _awaiting(p2d, dest, "First question for this week?")
    posted = await p2d.client.post(f"/api/v1/assist-tasks/{pub_id}/complete", json={"outcome": "posted",
                                                                                       "live_url": "https://removes.example.org/p/1"})
    assert posted.json()["state"] == "published"
    removed = await p2d.client.post(f"/api/v1/assist-tasks/{pub_id}/complete",
                                    json={"outcome": "removed_by_community", "reason": "removed by a moderator after 2 h"})
    assert removed.status_code == 200, removed.text
    out = removed.json()
    assert out["state"] == "removed_by_platform" and out["code"] == "PLATFORM_CONTENT_REJECTED"
    assert out["live_url"] == "https://removes.example.org/p/1"

    dest2 = await _assisted(p2d, "Abandoned Posts")
    pub2 = await _awaiting(p2d, dest2, "A question nobody posted?")
    gave_up = await p2d.client.post(f"/api/v1/assist-tasks/{pub2}/complete", json={"outcome": "abandoned", "reason": "no time"})
    assert gave_up.status_code == 200 and gave_up.json()["state"] == "abandoned"
    again = await post(p2d, dest2["destination_id"], title="Another question this week?")
    assert again["publication"]["state"] == "pending"  # an abandoned pack never used the weekly slot


# ------------------------------------------------------------------ honest test-post audit


async def test_audit_counts_each_removal_kind_separately(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Audit Rows", **WEEKLY))
    now = p2d.clock.now()
    modes = ["deleted", "purged", "sweeper_deleted", "soft_deleted", "admin_unpublish", "unpublish_to_draft",
             "sweeper_unpublish_to_draft", "pack_revoked", "not_created", "sweeper_not_found", None]
    async with p2d.app.state.session_factory() as session, session.begin():
        for n, mode in enumerate(modes):
            session.add(TestPostRow(connector_key="fakeforum", destination_id=dest["destination_id"], platform_post_id=f"p{n}",
                                    run_id=f"cert_a{n}", created_at=now, expires_at=now + timedelta(seconds=300),
                                    deleted_at=now + timedelta(seconds=2) if mode else None, delete_mode=mode))
    audit = (await p2d.client.get("/api/v1/test-posts")).json()
    assert audit["count"] == 11
    assert audit["deleted"] == 3
    assert audit["soft_deleted"] == 1
    assert audit["unpublished_to_draft"] == 3
    assert audit["pack_revoked"] == 1
    assert audit["not_created"] == 2
    assert audit["still_live"] == 1
    outcomes = {i["platform_post_id"]: i["outcome"] for i in audit["test_posts"]}
    assert outcomes["p8"] == outcomes["p9"] == "not_created" and outcomes["p4"] == "unpublished_to_draft"


# ------------------------------------------------------------------ a rate limit reschedules certification


async def test_rate_limit_during_certification_reschedules_and_keeps_the_level(p2d):
    dest = await p2d.add(name="Busy Forum", url="https://busy-forum.example.org", connector_key="discourse")
    httpx.post(f"{p2d.mock}/_faults", json={"platform": "discourse", "tenant": "*", "method": "POST", "fault": "429:120",
                                             "path": "/posts.json", "count": 1}).raise_for_status()
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "RESCHEDULED", run["steps"]
    assert run["failure_code"] == "PLATFORM_RATE_LIMIT" and run["result"]["level_after"] == "L0"
    after = (await p2d.client.get(f"/api/v1/destinations/{dest['destination_id']}")).json()
    assert after["status"] != "excluded" and after["level"] == "L0"
    next_run = run["result"]["next_run_id"]
    async with p2d.app.state.session_factory() as session:
        alerts = (await session.execute(select(AlertRow).where(AlertRow.destination_id == dest["destination_id"],
                                                               AlertRow.kind == "certification_failed"))).scalars().all()
        queued = (await session.execute(select(JobRow).where(JobRow.kind == "certification.run",
                                                             JobRow.status == "queued"))).scalars().all()
    assert alerts == []
    retry_at = datetime.fromisoformat(run["result"]["retry_at"].replace("Z", "+00:00"))
    assert retry_at >= p2d.clock.now() + timedelta(seconds=100)
    assert [j.payload["run_id"] for j in queued] == [next_run]
    p2d.clock.set(retry_at + timedelta(seconds=1))
    await p2d.drain()
    second = (await p2d.client.get(f"/api/v1/certifications/{next_run}")).json()
    assert second["state"] == "PASSED" and second["trigger"] == "rate_limit_retry"


async def test_rescheduling_stops_after_the_limit_without_excluding(p2d):
    dest = await p2d.add(name="Always Busy", url="https://always-busy.example.org", connector_key="discourse")
    httpx.post(f"{p2d.mock}/_faults", json={"platform": "discourse", "tenant": "*", "method": "POST", "fault": "429:30",
                                             "path": "/posts.json", "count": -1}).raise_for_status()
    run = await p2d.certify(dest["destination_id"])
    for _ in range(p2d.app.state.settings.CERT_RESCHEDULE_MAX):
        assert run["state"] == "RESCHEDULED"
        p2d.clock.advance(31)
        await p2d.drain()
        run = (await p2d.client.get(f"/api/v1/certifications/{run['result']['next_run_id']}")).json()
    assert run["state"] == "INCONCLUSIVE" and not run["result"].get("next_run_id")
    after = (await p2d.client.get(f"/api/v1/destinations/{dest['destination_id']}")).json()
    assert after["status"] != "excluded"
    httpx.post(f"{p2d.mock}/_reset")


# ------------------------------------------------------------------ real host at PREFLIGHT


async def test_real_host_refusal_is_failed_precondition_not_a_demotion(p2d):
    from app.certification.harness import Harness

    dest = await p2d.add(**forum_row(p2d.mock, "Certified Then Local", **WEEKLY))
    assert (await p2d.certify(dest["destination_id"]))["level_after"] == "L4"
    started = await p2d.client.post("/api/v1/certifications", json={"destination_id": dest["destination_id"]})
    local = p2d.app.state.settings.model_copy(update={"SIMULATION_MODE": False})
    harness = Harness(settings=local, session_factory=p2d.app.state.session_factory, storage=p2d.storage, clock=p2d.clock)
    result = await harness.run(started.json()["run_id"])
    assert result["state"] == "FAILED_PRECONDITION" and result["failure_code"] == "DEST_NOT_OWN_INSTANCE"
    assert result["level_after"] == "L4" and not result.get("excluded")
    async with p2d.app.state.session_factory() as session:
        row = await session.get(DestinationRow, dest["destination_id"])
        run = await session.get(CertificationRunRow, started.json()["run_id"])
    assert row.level == "L4" and row.status == "active" and run.state == "FAILED_PRECONDITION"
    assert "fakeforum" not in httpx.get(f"{p2d.mock}/_counts").json()["counts"].get("fakeforum", {}).get(
        "certified-then-local-example-org-x", {})


# ------------------------------------------------------------------ Dev.to naming and author path


@pytest.mark.parametrize("admin", [False, True])
async def test_devto_removal_is_named_removal_and_the_admin_path_is_labelled(p2d, admin):
    extra = {"instance": {"forem_admin": True}} if admin else {}
    dest = await p2d.add(name=f"Forem {admin}", url=f"https://forem-{str(admin).lower()}.example.org", connector_key="devto", **extra)
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED", run["steps"]
    steps = [s["step"] for s in run["steps"]]
    assert steps[-2:] == ["DELETE", "CONFIRM_REMOVED"] and "CONFIRM_DELETED" not in steps
    assert "removal_confirmation.json" in run["evidence"] and "deletion_confirmation.json" not in run["evidence"]
    delete = next(s for s in run["steps"] if s["step"] == "DELETE")["detail"]
    assert delete["response"]["deleted"] is False
    if admin:
        assert run["result"]["delete_mode"] == "admin_unpublish"
        assert "own Forem mock" in delete["admin_path"]
    else:
        assert run["result"]["delete_mode"] == "unpublish_to_draft" and "admin_path" not in delete
    publish = next(s for s in run["steps"] if s["step"] == "PUBLISH")["detail"]
    assert "deleted within 10 minutes" not in publish["request"]["rendition"]["body"]
