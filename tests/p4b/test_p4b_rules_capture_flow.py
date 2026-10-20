"""ADV2 end to end in process: capture -> proposal (not used) -> confirm page -> confirm -> rules enforced on real
publications; reject; robots; the job manifest's LLM record. ADV4 API: POST /api/v1/tailorings."""

from __future__ import annotations

import hashlib
import json

FORUM = {"connector_key": "fakeforum", "category": "saas", "audience_tags": "ai;founders", "language": "en"}


async def add_forum(env, site: str, name: str, page: str | None = "13_javascript_weekly.html", **extra) -> dict:
    row = {"name": name, "url": f"https://{name.lower().replace(' ', '-')}.example.org/", **FORUM, **extra}
    if page:
        row["rules_url"] = f"{site}/rules/{page}"
    return await env.add(**row)


async def post(env, destination_id: str, expect: int, **content) -> dict:
    body = {"title": "How do you test long-tail forums?", "body": "We compared three approaches to testing forum posts. "
            "What has worked for you?", "ai_generated": False, "is_promotional": False, "language": "en", **content}
    response = await env.client.post("/api/v1/publications", json={"destination_id": destination_id, "content": body})
    assert response.status_code == expect, response.text
    return response.json()


async def capture(env, destination_id: str, **body) -> dict:
    started = await env.client.post(f"/api/v1/destinations/{destination_id}/rules/capture", json=body)
    assert started.status_code == 202, started.text
    await env.drain()
    job = await env.job(started.json()["job_id"])
    return job


async def test_capture_confirm_then_rules_are_enforced(p2d, rules_site, site_hits):
    dest = await add_forum(p2d, rules_site, "JS Weekly")
    dest_id = dest["destination_id"]
    assert dest["rules"] is None
    assert (await p2d.certify(dest_id))["state"] == "PASSED"
    unconfirmed = await post(p2d, dest_id, 409)
    assert unconfirmed["error"]["code"] == "DEST_RULES_UNCONFIRMED"

    job = await capture(p2d, dest_id)
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["status"] == "proposed" and result["provider"] == "mock" and result["fallback_used"] is False
    assert site_hits[:2] == ["/robots.txt", "/rules/13_javascript_weekly.html"]  # robots.txt first

    # IS9 manifest records the provider, model, fallback flag, tokens and cost 0
    manifest = json.loads(await p2d.storage.get_bytes(f"jobs/{job['job_id']}/manifest.json"))
    llm = [p for p in manifest["providers"] if p["name"] == "llm"][0]
    assert (llm["provider"], llm["model"], llm["fallback_used"], llm["cost_usd"]) == ("mock", "mock-heuristic-v1", False, 0.0)
    assert llm["tokens"] == {"prompt": 0, "completion": 0, "total": 0} and manifest["cost"]["total_usd"] == 0.0

    proposal = (await p2d.client.get(f"/api/v1/rule-proposals/{result['proposal_id']}")).json()
    fields = {f["field"]: f for f in proposal["fields"]}
    assert fields["frequency_cap"]["status"] == "verified" and fields["frequency_cap"]["applied"] == "1/P3D"
    assert fields["frequency_cap"]["quote"] == "Wait at least 3 days between your posts."
    assert fields["link_policy"]["applied"] == "no_shorteners"
    assert fields["automation_allowed"]["status"] == "not_stated" and fields["automation_allowed"]["applied"] is False
    page_text = (await p2d.storage.get_bytes(f"jobs/{job['job_id']}/rules_page_text.txt")).decode()
    assert proposal["rules_page_sha256"] == hashlib.sha256(page_text.encode()).hexdigest()
    assert proposal["sources"][0]["kind"] == "rules_url" and len(proposal["sources"][0]["sha256"]) == 64

    # a proposal is never used for posting
    assert (await post(p2d, dest_id, 409))["error"]["code"] == "DEST_RULES_UNCONFIRMED"

    # the confirm page shows the quotes beside the fields; no token needed to read it, CSP set
    page = await p2d.client.get(f"/ui/rules/{proposal['proposal_id']}", headers={"Authorization": ""})
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Wait at least 3 days between your posts." in page.text and "quote verified" in page.text
    assert "script-src 'nonce-" in page.headers["content-security-policy"]

    # the person confirms, correcting one field (automation is fine for this community)
    confirmed = await p2d.client.post(f"/api/v1/rule-proposals/{proposal['proposal_id']}/confirm",
                                      json={"confirmed_by": "reviewer@example.org", "overrides": {"automation_allowed": True}})
    assert confirmed.status_code == 200, confirmed.text
    rules = confirmed.json()["rules"]
    assert rules["status"] == "confirmed" and rules["confirmed_by"] == "reviewer@example.org"
    assert rules["rules"]["automation_allowed"] is True and rules["rules"]["frequency_cap"] == "1/P3D"
    again = await p2d.client.post(f"/api/v1/rule-proposals/{proposal['proposal_id']}/confirm",
                                  json={"confirmed_by": "someone-else"})
    assert again.status_code == 409 and again.json()["error"]["details"]["reason"] == "proposal_not_pending"
    assert (await p2d.client.get(f"/api/v1/rule-proposals/{proposal['proposal_id']}")).json()["status"] == "confirmed"
    dest_now = (await p2d.client.get(f"/api/v1/destinations/{dest_id}")).json()
    assert dest_now["rules"]["version"] == rules["version"] and dest_now["frequency_cap"] == "1/P3D"

    # the confirmed rules are enforced: self-promotion blocked, first post goes out, the second waits 3 days
    promo = await post(p2d, dest_id, 409, title="Our new tool is live", is_promotional=True,
                       body="Sign up today at https://mytool.example.com/?utm_source=forum", owner_domains=["mytool.example.com"])
    assert promo["error"]["code"] == "DEST_RULE_SELF_PROMO"
    first = await post(p2d, dest_id, 202)
    await p2d.drain()
    assert (await p2d.client.get(first["publication_url"])).json()["state"] == "published"
    second = await post(p2d, dest_id, 202, title="Another question this week?")
    assert second["publication"]["state"] == "rescheduled" and second["publication"]["code"] == "DEST_RULE_FREQUENCY_CAP"


async def test_reject_keeps_the_old_rules_and_page_escapes_page_text(p2d, rules_site):
    dest = await add_forum(p2d, rules_site, "Pasted Rules", page=None, frequency_cap="2/P1D", self_promo_allowed="true")
    before = dest["rules"]
    job = await capture(p2d, dest["destination_id"], text="Rules: <img src=x onerror=alert(1)> No links. No bots allowed here.",
                        use_platform_api=False)
    assert job["status"] == "succeeded", job
    pid = job["result"]["proposal_id"]
    page = await p2d.client.get(f"/ui/rules/{pid}")
    assert "<img src=x" not in page.text and "&lt;img src=x onerror=alert(1)&gt;" in page.text
    rejected = await p2d.client.post(f"/api/v1/rule-proposals/{pid}/reject", json={"rejected_by": "reviewer", "reason": "stale"})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    late = await p2d.client.post(f"/api/v1/rule-proposals/{pid}/confirm", json={"confirmed_by": "reviewer"})
    assert late.status_code == 409
    after = (await p2d.client.get(f"/api/v1/destinations/{dest['destination_id']}")).json()["rules"]
    assert after["version"] == before["version"] and after["rules_id"] == before["rules_id"]
    listed = (await p2d.client.get(f"/api/v1/destinations/{dest['destination_id']}/rule-proposals")).json()
    assert [p["status"] for p in listed["proposals"]] == ["rejected"]


async def test_robots_disallowed_page_is_not_read_and_job_explains(p2d, rules_site, site_hits):
    dest = await add_forum(p2d, rules_site, "Robots Says No", page=None)
    started = await p2d.client.post(f"/api/v1/destinations/{dest['destination_id']}/rules/capture",
                                    json={"rules_url": f"{rules_site}/private/rules"})
    assert started.status_code == 202
    await p2d.drain()
    job = await p2d.job(started.json()["job_id"])
    assert job["status"] == "failed" and job["error"]["error"]["code"] == "INPUT_FETCH_FAILED"
    assert "/private/" not in site_hits and "/robots.txt" in site_hits


async def test_capture_needs_something_to_read_and_unknown_ids_are_404(p2d, rules_site):
    dest = await add_forum(p2d, rules_site, "Nothing To Read", page=None)
    refused = await p2d.client.post(f"/api/v1/destinations/{dest['destination_id']}/rules/capture", json={})
    assert refused.status_code == 422
    assert (await p2d.client.get("/api/v1/rule-proposals/rprop_nope")).status_code == 404
    assert (await p2d.client.get("/ui/rules/rules_not_a_proposal")).status_code == 404
    unauth = await p2d.client.post("/api/v1/rule-proposals/rprop_x/confirm", json={"confirmed_by": "x"},
                                   headers={"Authorization": "Bearer wrong"})
    assert unauth.status_code == 401


async def test_tailorings_return_renditions_and_diffs_per_destination(p2d, rules_site):
    dest = await add_forum(p2d, rules_site, "Tailor Forum", page=None, frequency_cap="1/P7D", self_promo_allowed="false",
                           disclosure_required="true")
    body = {"content": {"title": "We launched a video tool! Subscribe now", "ai_generated": True, "language": "en",
                        "body": "Here is what we learned building it.\n\nSubscribe to our channel for more!\n\n"
                                "What would you test first?", "canonical_url": "https://blog.example.com/launch",
                        "tags": ["video", "ai"]},
            "destination_ids": [dest["destination_id"], "dest_missing"]}
    started = await p2d.client.post("/api/v1/tailorings", json=body)
    assert started.status_code == 202, started.text
    await p2d.drain()
    job = await p2d.job(started.json()["job_id"])
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["count"] == 1 and result["errors"][0]["destination_id"] == "dest_missing"
    item = result["tailorings"][0]
    assert item["destination_id"] == dest["destination_id"] and item["rules"]["source"] == "confirmed"
    assert item["diff"] and item["warnings"] and item["rendition"]["body"]
    assert "tailorings" in job["result"]["links"]
    bad = await p2d.client.post("/api/v1/tailorings", json={"destination_ids": [dest["destination_id"]]})
    assert bad.status_code == 422
