"""End to end (in process, simulation mode): one-row destinations, certification + levels, demotion on
schema drift, frequency cap (sequential and concurrent), no-promo block, sweeper, assisted packs, contract cap."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from app.db.tables import AlertRow, LevelHistoryRow, TestPostRow
from tests.p2d.conftest import CANARY

WEEKLY = {"frequency_cap": "1/P7D", "self_promo_allowed": "false", "link_policy": "allow", "disclosure_required": "true",
          "automation_allowed": "true"}


def forum_row(mock: str, name: str, **extra: object) -> dict[str, object]:
    return {"name": name, "url": f"https://{name.lower().replace(' ', '-')}.example.org/", "connector_key": "fakeforum",
            "category": "saas", "audience_tags": "ai;founders", "language": "en", **extra}


async def post(env, destination_id: str, expect: int = 202, **content: object) -> dict:
    body = {"title": "How do you test long-tail forums?", "body": "We compared three approaches to testing forum posts. "
            "What has worked for you?", "ai_generated": False, "is_promotional": False, "language": "en", **content}
    response = await env.client.post("/api/v1/publications", json={"destination_id": destination_id, "content": body})
    assert response.status_code == expect, response.text
    return response.json()


async def blocked_publication(env, refused: dict, code: str) -> dict:
    """A rules block is a synchronous 409 with the rule's envelope; the publication is kept."""
    error = refused["error"]
    assert error["code"] == code and error["retryable"] is False and error["message"]
    pub = (await env.client.get(f"/api/v1/publications/{error['details']['publication_id']}")).json()
    assert pub["state"] == "blocked" and pub["code"] == code
    return pub


# ------------------------------------------------------------------ Must-have 3: one catalogue row


async def test_forum_41_is_one_row_then_certified_to_l3_or_l4(p2d):
    row = "name,url,platform_key,surface,connector_key,category,audience_tags,language,frequency_cap,self_promo_allowed," \
          "link_policy,disclosure_required,automation_allowed,connection_ref\n" \
          "Forum 41,https://forum-41.example.org/,discourse,topic,discourse,saas,\"ai;founders\",en,1/P7D,false,allow,true,true,conn-discourse-sim\n"
    created = await p2d.client.post("/api/v1/destinations", content=row, headers={"Content-Type": "text/csv"})
    assert created.status_code == 201, created.text
    result = created.json()["results"][0]
    assert result["status"] == "created"
    dest = result["destination"]
    assert dest["level"] == "L0" and dest["instance"] == {"base_url": "https://forum-41.example.org"}
    assert dest["destination_key"] == "discourse|forum-41.example.org|topic"
    assert dest["rules"]["status"] == "confirmed" and dest["frequency_cap"] == "1/P7D"
    assert any("CONNECTION_REF_UNRESOLVED" in w for w in result["warnings"])

    again = await p2d.client.post("/api/v1/destinations", content=row, headers={"Content-Type": "text/csv"})
    assert again.json()["results"][0]["status"] == "duplicate"
    assert again.json()["results"][0]["destination_id"] == dest["destination_id"]

    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED", json.dumps(run, indent=1)[:3000]
    assert run["level_after"] in ("L3", "L4")
    steps = [s["step"] for s in run["steps"]]
    assert steps[:6] == ["PREFLIGHT", "VALIDATE", "PUBLISH", "CONFIRM_LIVE", "STATUS", "METRICS"]
    assert steps[-2:] == ["DELETE", "CONFIRM_DELETED"]
    assert {"run.json", "summary.md", "manifest.snapshot.yaml", "deletion_confirmation.json"} <= set(run["evidence"])
    after = (await p2d.client.get(f"/api/v1/destinations/{dest['destination_id']}")).json()
    assert after["level"] == run["level_after"] and after["last_certified_at"] and after["status"] == "active"

    audit = (await p2d.client.get("/api/v1/test-posts")).json()
    assert audit["count"] == 1 and audit["still_live"] == 0 and audit["max_lifetime_s"] < 600


async def test_fake_forum_certifies_to_l4_and_evidence_downloads_without_token(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Forum Fake", **WEEKLY))
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED" and run["level_after"] == "L4"
    assert run["result"]["delete_mode"] == "deleted"
    link = run["evidence"]["run.json"]
    assert link.startswith("http://ibx.test/api/v1/files/certifications/")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=p2d.app), base_url="http://ibx.test") as anon:
        doc = (await anon.get(link)).json()
    assert doc["result"]["passed"] is True and [t["state"] for t in doc["timeline"]][0] == "QUEUED"
    # a certification post never uses up the destination's weekly cap
    first = await post(p2d, dest["destination_id"])
    assert first["publication"]["state"] == "pending"


async def test_unpublish_to_draft_is_not_promoted_to_l3(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Draft Only"))
    tenant = "draft-only-example-org"
    httpx.post(f"{p2d.mock}/fakeforum/{tenant}/_mode", json={"delete_mode": "draft"}).raise_for_status()
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED", run["steps"][-1]
    assert run["result"]["delete_mode"] == "unpublish_to_draft"
    assert run["level_after"] == "L2" and "not claimed" in run["result"]["level_reason"]
    delete_step = next(s for s in run["steps"] if s["step"] == "DELETE")
    assert delete_step["detail"]["response"] == {"deleted": False}  # never reported as deleted


# ------------------------------------------------------------------ hidden test: field rename → FAILED → DEMOTED


async def test_field_rename_fails_certification_cleans_up_and_demotes(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Renamed", **WEEKLY))
    did = dest["destination_id"]
    assert (await p2d.certify(did))["level_after"] == "L4"
    httpx.post(f"{p2d.mock}/_faults", json={"platform": "fakeforum", "tenant": "*", "method": "POST",
                                             "fault": "rename_field:id->post_id", "count": 1}).raise_for_status()
    p2d.clock.advance(60)
    run = await p2d.certify(did)
    assert run["state"] == "DEMOTED" and run["passed"] is False
    assert run["result"]["failed_step"] == "PUBLISH"
    failed = next(s for s in run["steps"] if s["step"] == "PUBLISH")
    assert failed["detail"]["error"]["details"]["reason"] == "response_schema_mismatch"
    # HC5: the run result names the schema change, not "timeout or server error"
    assert run["result"]["failure_reason"] == "response_schema_mismatch" and run["result"]["failure_fields"]
    assert "response_schema_mismatch" in run["result"]["failure_message"]
    assert "timeout or server error" not in run["result"]["failure_message"]
    cleanup = next(s for s in run["steps"] if s["step"] == "CLEANUP")
    assert cleanup["detail"]["found_by"] == "marker" and cleanup["detail"]["action"] == "deleted"
    posts = httpx.get(f"{p2d.mock}/fakeforum/renamed-example-org/posts").json()["posts"]
    assert posts == []  # the test post the platform did create was found by marker and deleted

    after = (await p2d.client.get(f"/api/v1/destinations/{did}")).json()
    assert after["level"] == "L0" and after["status"] == "excluded" and "PUBLISH" in after["excluded_reason"]
    async with p2d.app.state.session_factory() as session:
        alerts = (await session.execute(select(AlertRow).where(AlertRow.destination_id == did))).scalars().all()
        history = (await session.execute(select(LevelHistoryRow).where(LevelHistoryRow.destination_id == did))).scalars().all()
    assert [a.kind for a in alerts] == ["certification_failed"] and alerts[0].status == "open"
    assert alerts[0].body["reason"] == "response_schema_mismatch" and "response_schema_mismatch" in after["excluded_reason"]
    assert "outcome unknown" not in alerts[0].body["message"]
    assert [(h.level_before, h.level_after) for h in history] == [("L0", "L4"), ("L4", "L0")]

    await blocked_publication(p2d, await post(p2d, did, expect=409), "DESTINATION_EXCLUDED")

    # it passes again → restored and the alert closes
    p2d.clock.advance(60)
    again = await p2d.certify(did)
    assert again["state"] == "PASSED"
    async with p2d.app.state.session_factory() as session:
        alert = (await session.execute(select(AlertRow).where(AlertRow.destination_id == did))).scalar_one()
    assert alert.status == "resolved"


# ------------------------------------------------------------------ Must-have 4: rules before every post


async def test_second_post_inside_weekly_cap_is_rescheduled_with_retry_at(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Weekly Cap", **WEEKLY))
    assert (await p2d.certify(dest["destination_id"]))["state"] == "PASSED"  # certify before publishing
    first = await post(p2d, dest["destination_id"])
    assert first["publication"]["state"] == "pending"
    await p2d.drain()
    published = (await p2d.client.get(first["publication_url"])).json()
    assert published["state"] == "published" and published["live_url"]
    assert published["tailoring"]["profile"]  # tailoring ran and recorded what it changed

    p2d.clock.advance(3600)
    second = await post(p2d, dest["destination_id"], title="A second question this week?")
    pub = second["publication"]
    assert pub["state"] == "rescheduled" and pub["code"] == "DEST_RULE_FREQUENCY_CAP"
    expected = datetime.fromisoformat(published["published_at"].replace("Z", "+00:00")) + timedelta(days=7)
    assert datetime.fromisoformat(pub["retry_at"].replace("Z", "+00:00")) == expected
    assert "DEST_RULE_FREQUENCY_CAP" in pub["reason"]
    await p2d.drain()  # nothing due yet: still rescheduled
    assert (await p2d.client.get(second["publication_url"])).json()["state"] == "rescheduled"
    p2d.clock.set(expected + timedelta(seconds=1))
    await p2d.drain()
    done = (await p2d.client.get(second["publication_url"])).json()
    assert done["state"] == "published", done


async def test_two_concurrent_posts_inside_cap_one_publishes_one_rescheduled(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Concurrent Cap", **WEEKLY))
    assert (await p2d.certify(dest["destination_id"]))["state"] == "PASSED"  # certify before publishing
    a, b = await asyncio.gather(post(p2d, dest["destination_id"], title="Question A for the forum?"),
                                post(p2d, dest["destination_id"], title="Question B for the forum?"))
    states = sorted([a["publication"]["state"], b["publication"]["state"]])
    assert states == ["pending", "rescheduled"]
    later = a if a["publication"]["state"] == "rescheduled" else b
    assert later["publication"]["retry_at"]
    await p2d.drain()
    creates = httpx.get(f"{p2d.mock}/_counts", params={"platform": "fakeforum", "tenant": "concurrent-cap-example-org"}).json()
    assert creates["counts"]["fakeforum"]["concurrent-cap-example-org"].get("POST /posts") == 2  # 1 certification + 1 real post


async def test_promo_to_no_promo_destination_is_blocked_with_reason(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "No Promo", **WEEKLY))
    refused = await post(p2d, dest["destination_id"], expect=409, title="Our new tool is live",
                         body="Sign up today with code LAUNCH20 at https://mytool.example.com/?utm_source=forum",
                         is_promotional=True, owner_domains=["mytool.example.com"])
    pub = await blocked_publication(p2d, refused, "DEST_RULE_SELF_PROMO")
    assert pub["reason"] and "DEST_RULE_SELF_PROMO" in pub["reason"]
    assert pub["job_ids"] == []  # nothing is queued for a blocked post
    await p2d.drain()
    counts = httpx.get(f"{p2d.mock}/_counts", params={"platform": "fakeforum"}).json()["counts"]["fakeforum"]
    assert "no-promo-example-org" not in counts  # the platform never saw it


async def test_rules_unconfirmed_blocks_publications_but_not_certification(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "No Rules"))
    assert dest["rules"] is None and any("RULES_UNCONFIRMED" in w for w in dest["warnings"])
    await blocked_publication(p2d, await post(p2d, dest["destination_id"], expect=409), "DEST_RULES_UNCONFIRMED")
    assert (await p2d.certify(dest["destination_id"]))["state"] == "PASSED"
    confirmed = await p2d.client.put(f"/api/v1/destinations/{dest['destination_id']}/rules",
                                     json={"rules": {"frequency_cap": "2/P1D", "self_promo_allowed": False}, "confirmed_by": "reviewer"})
    assert confirmed.status_code == 200 and confirmed.json()["rules"]["version"] == 1
    assert (await post(p2d, dest["destination_id"]))["publication"]["state"] == "pending"


async def test_contract_publish_cap_is_concurrency_safe_and_ignores_test_posts(p2d):
    dest = await p2d.add(**forum_row(p2d.mock, "Contract Cap", **WEEKLY))
    assert (await p2d.certify(dest["destination_id"]))["state"] == "PASSED"
    contract_dest = {"destination_id": dest["destination_id"], "connector_key": "fakeforum", "surface": "topic",
                     "instance": dest["instance"]}

    def body(key: str) -> dict:
        return {"rendition": {"title": f"Contract question {key}?", "body": "Context first, then the question?",
                              "ai_generated": False},
                "destination": contract_dest, "connection": {"connector_key": "fakeforum"}, "idempotency_key": key}

    r1, r2 = await asyncio.gather(p2d.client.post("/connector/v1/fakeforum/publish", json=body("k1")),
                                  p2d.client.post("/connector/v1/fakeforum/publish", json=body("k2")))
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], (r1.text, r2.text)
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["error"]["code"] == "DEST_RULE_FREQUENCY_CAP" and loser.json()["error"]["details"]["retry_at"]
    assert loser.headers.get("Retry-After")


# ------------------------------------------------------------------ safety: own instances only, sweeper, secrets


async def test_real_site_is_refused_outside_simulation(p2d):
    from app.certification.harness import Harness

    dest = await p2d.add(**forum_row(p2d.mock, "Real Site"))
    started = await p2d.client.post("/api/v1/certifications", json={"destination_id": dest["destination_id"]})
    live_settings = p2d.app.state.settings.model_copy(update={"SIMULATION_MODE": False})
    harness = Harness(settings=live_settings, session_factory=p2d.app.state.session_factory, storage=p2d.storage,
                      clock=p2d.clock)
    result = await harness.run(started.json()["run_id"])
    assert result["passed"] is False and result["failure_code"] == "DEST_NOT_OWN_INSTANCE"
    assert result["failed_step"] == "PREFLIGHT"
    counts = httpx.get(f"{p2d.mock}/_counts").json()["counts"]
    assert "fakeforum" not in counts  # nothing was sent anywhere


async def test_sweeper_deletes_leftover_test_posts_within_ttl(p2d):
    from app.certification.sweeper import sweep

    dest = await p2d.add(**forum_row(p2d.mock, "Leftover", **WEEKLY))
    created = httpx.post(f"{p2d.mock}/fakeforum/leftover-example-org/posts", headers={"Authorization": "Bearer x"},
                         json={"title": "left by a crashed run", "body": "x", "marker": "ibx-left"}).json()
    now = p2d.clock.now()
    async with p2d.app.state.session_factory() as session, session.begin():
        session.add(TestPostRow(connector_key="fakeforum", destination_id=dest["destination_id"], platform_post_id=str(created["id"]),
                                live_url=created["url"], run_id="cert_crashed", created_at=now, expires_at=now + timedelta(seconds=300)))
    s = p2d.app.state.settings
    kwargs = {"settings": s, "session_factory": p2d.app.state.session_factory, "storage": p2d.storage, "clock": p2d.clock}
    assert (await sweep(**kwargs))["checked"] == 0  # not expired yet
    p2d.clock.advance(301)
    result = await sweep(**kwargs)
    assert len(result["deleted"]) == 1 and result["failed"] == []
    audit = (await p2d.client.get("/api/v1/test-posts")).json()
    assert audit["test_posts"][0]["delete_mode"] == "sweeper_deleted" and audit["max_lifetime_s"] <= 600
    assert httpx.get(created["url"]).status_code == 404


async def test_sweeper_is_a_scheduled_job_every_60s():
    from app.jobs.registry import load_handlers
    from app.jobs.scheduler import PERIODIC

    load_handlers()
    assert PERIODIC["certification.sweep"].every_s == 60


async def test_canary_secret_never_leaves_the_store(p2d, capsys):
    conn = await p2d.client.post("/api/v1/connections", json={"connector_key": "fakeforum", "credentials": {"token": CANARY},
                                                             "ref": "conn-canary"})
    assert conn.status_code == 201 and CANARY not in conn.text and conn.json()["credential_names"] == ["token"]
    dest = await p2d.add(**forum_row(p2d.mock, "Canary", connection_ref="conn-canary", **WEEKLY))
    assert dest["connection_id"] == conn.json()["connection_id"]
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED"
    p = await post(p2d, dest["destination_id"])
    await p2d.drain()
    texts = [conn.text, json.dumps(run), json.dumps(p), (await p2d.client.get("/api/v1/connections")).text,
             (await p2d.client.get(f"/api/v1/destinations/{dest['destination_id']}")).text]
    texts += [data.decode(errors="replace") for data, _ in p2d.storage.objects.values()]
    out = capsys.readouterr()
    texts += [out.out, out.err]
    assert all(CANARY not in t for t in texts)


# ------------------------------------------------------------------ assisted (L1) and publications to it


async def test_assisted_destination_certifies_to_l1_and_revokes_pack_links(p2d):
    dest = await p2d.add(name="Show Forum", url=f"{p2d.mock}/fakeassist/show/submit", connector_key="fakeassist",
                         frequency_cap="1/P7D", self_promo_allowed="false", automation_allowed="false")
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED", run["steps"]
    assert run["level_after"] == "L1" and run["result"]["delete_mode"] == "pack_revoked"
    pack_step = next(s for s in run["steps"] if s["step"] == "CONFIRM_PACK")
    key = pack_step["detail"]["files"][0]["key"]
    assert await p2d.storage.stat(key) is None  # revoked after certification

    pub = await post(p2d, dest["destination_id"])
    assert pub["publication"]["state"] == "pending"  # automation_allowed=false is fine: a person posts the pack
    await p2d.drain()
    waiting = (await p2d.client.get(pub["publication_url"])).json()
    assert waiting["state"] == "awaiting_human" and waiting["assist_pack"]["checklist"]
    done = await p2d.client.post(f"/api/v1/assist-tasks/{waiting['publication_id']}/complete",
                                 json={"live_url": "https://show.example.org/item/1", "completed_by": "ops"})
    assert done.status_code == 200 and done.json()["state"] == "published"
