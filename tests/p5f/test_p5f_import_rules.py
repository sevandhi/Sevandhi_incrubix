"""Catalogue rule columns and capture warnings in process (real Postgres, real worker, simulation mode).

1. A catalogue import row's rule columns are a *proposal* unless the row says ``rules_status=confirmed`` AND
``rules_source=manual`` (an operator typed them). A proposal is shown on the confirm page, is never used for posting,
and becomes the confirmed rule set only when a person confirms it.
2. Capture warnings come back in the job result, in GET /api/v1/rule-proposals/{id} and on the confirm page."""

from __future__ import annotations

import csv
import io
from pathlib import Path

FORUM = {"connector_key": "fakeforum", "category": "saas", "audience_tags": "ai;founders", "language": "en"}
RULES = {"frequency_cap": "1/P7D", "self_promo_allowed": "false", "link_policy": "any"}
SEED = Path(__file__).resolve().parents[2] / "seed" / "hard_case_destinations.csv"


def _csv(rows: list[dict[str, str]]) -> bytes:
    columns: list[str] = []
    for row in rows:
        columns += [k for k in row if k not in columns]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def _row(site: str, name: str, page: str, **extra: str) -> dict[str, str]:
    # the page itself is the destination URL: the importer's reachability check reads it from the local rules site
    return {"name": name, "url": f"{site}/rules/{page}", **FORUM, **extra}


async def _import(env, rows: list[dict[str, str]]) -> dict[str, dict]:
    started = await env.client.post("/api/v1/catalogue/imports",
                                    files={"file": ("cat.csv", _csv(rows), "text/csv")})
    assert started.status_code == 202, started.text
    await env.drain()
    job = await env.job(started.json()["job_id"])
    assert job["status"] == "succeeded", job
    report = await env.client.get(started.json()["report_url"], params={"format": "json"})
    by_name = {r["name"]: r for r in report.json()["rows"]}
    assert all(r["status"] in ("ok", "warning") for r in by_name.values()), by_name
    return by_name


async def _post(env, destination_id: str, expect: int, **content) -> dict:
    body = {"title": "How do you test long-tail forums?", "body": "We compared three approaches to testing forum posts. "
            "What has worked for you?", "ai_generated": False, "is_promotional": False, "language": "en", **content}
    response = await env.client.post("/api/v1/publications", json={"destination_id": destination_id, "content": body})
    assert response.status_code == expect, response.text
    return response.json()


async def test_imported_rule_columns_are_a_proposal_until_a_person_confirms(p2d, rules_site):
    report = await _import(p2d, [
        _row(rules_site, "Researched Forum", "01_indie_makers_forum.html", **RULES, rules_source="agent_research"),
        _row(rules_site, "Unmarked Forum", "02_python_learners.html", **RULES),
        _row(rules_site, "Claimed Forum", "03_creator_economy_lemmy.html", **RULES, rules_status="confirmed",
             rules_source="agent_research"),
    ])
    for name, source in (("Researched Forum", "agent_research"), ("Unmarked Forum", "import"),
                         ("Claimed Forum", "agent_research")):
        line = report[name]
        assert line["rules_status"] == "proposed" and line["rules_proposal_id"].startswith("rprop_"), line
        dest = (await p2d.client.get(f"/api/v1/destinations/{line['destination_id']}")).json()
        assert dest["rules"] is None and dest["rules_status"] == "proposed", dest  # nothing confirmed by the import
        assert dest["rules_proposal_id"] == line["rules_proposal_id"]
        proposal = (await p2d.client.get(f"/api/v1/rule-proposals/{line['rules_proposal_id']}")).json()
        assert proposal["status"] == "proposed" and proposal["source"] == source
        assert any(w["code"] == "UNVERIFIED_ROW_RULES" for w in proposal["warnings"]), proposal["warnings"]
        fields = {f["field"]: f for f in proposal["fields"]}
        assert fields["frequency_cap"]["status"] == "stated_in_row" and fields["frequency_cap"]["applied"] == "1/P7D"
        assert fields["automation_allowed"]["status"] == "not_stated"  # conservative default, shown as a warning
        assert any(w["code"] == "FIELD_DEFAULTED" and w["field"] == "automation_allowed" for w in proposal["warnings"])
    claimed = (await p2d.client.get(f"/api/v1/rule-proposals/{report['Claimed Forum']['rules_proposal_id']}")).json()
    assert any(w["code"] == "CONFIRMATION_NOT_ACCEPTED" for w in claimed["warnings"])

    # never used for posting, even after certification
    dest_id = report["Researched Forum"]["destination_id"]
    assert (await p2d.certify(dest_id))["state"] == "PASSED"
    assert (await _post(p2d, dest_id, 409))["error"]["code"] == "DEST_RULES_UNCONFIRMED"

    # the confirm page shows the row's values and the warnings
    pid = report["Researched Forum"]["rules_proposal_id"]
    page = await p2d.client.get(f"/ui/rules/{pid}")
    assert page.status_code == 200 and "UNVERIFIED_ROW_RULES" in page.text and "catalogue row" in page.text

    # a person confirms (allowing automation for this community) -> the rules are enforced
    confirmed = await p2d.client.post(f"/api/v1/rule-proposals/{pid}/confirm",
                                      json={"confirmed_by": "reviewer@example.org", "overrides": {"automation_allowed": True}})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["rules"]["confirmed_by"] == "reviewer@example.org"
    dest = (await p2d.client.get(f"/api/v1/destinations/{dest_id}")).json()
    assert dest["rules_status"] == "confirmed" and dest["rules"]["rules"]["frequency_cap"] == "1/P7D"
    promo = await _post(p2d, dest_id, 409, title="Our new tool is live", is_promotional=True,
                        body="Sign up today at https://mytool.example.com/", owner_domains=["mytool.example.com"])
    assert promo["error"]["code"] == "DEST_RULE_SELF_PROMO"
    first = await _post(p2d, dest_id, 202)
    await p2d.drain()
    assert (await p2d.client.get(first["publication_url"])).json()["state"] == "published"
    second = await _post(p2d, dest_id, 202, title="Another question this week?")
    assert second["publication"]["code"] == "DEST_RULE_FREQUENCY_CAP"


async def test_operator_row_manual_confirmed_is_confirmed_and_certifies(p2d, rules_site):
    """MH3 through the importer: one operator-authored row (rules typed in, marked manual/confirmed) is enough."""
    report = await _import(p2d, [_row(rules_site, "Operator Forum", "07_saas_founders.html", **RULES,
                                      rules_status="confirmed", rules_source="manual")])
    line = report["Operator Forum"]
    assert line["rules_status"] == "confirmed" and not line["rules_proposal_id"]
    dest = (await p2d.client.get(f"/api/v1/destinations/{line['destination_id']}")).json()
    assert dest["rules"]["status"] == "confirmed" and dest["rules"]["confirmed_by"] == "catalogue_import"
    assert dest["rules_status"] == "confirmed"
    assert (await p2d.certify(dest["destination_id"]))["state"] == "PASSED"
    created = await _post(p2d, dest["destination_id"], 202)
    await p2d.drain()
    assert (await p2d.client.get(created["publication_url"])).json()["state"] == "published"


async def test_destinations_api_honours_explicit_markings(p2d, rules_site):
    proposed = await p2d.add(name="Api Proposed", url="https://api-proposed.example.org/", **FORUM, **RULES,
                             rules_status="proposed", rules_source="page")
    assert proposed["rules"] is None and proposed["rules_status"] == "proposed"
    assert any(w.startswith("RULES_PROPOSED") for w in proposed["warnings"])
    agent = await p2d.add(name="Api Agent", url="https://api-agent.example.org/", **FORUM, **RULES,
                          rules_status="confirmed", rules_source="agent_research")
    assert agent["rules"] is None and agent["rules_status"] == "proposed"
    operator = await p2d.add(name="Api Operator", url="https://api-operator.example.org/", **FORUM, **RULES,
                             rules_status="confirmed", rules_source="manual")
    assert operator["rules"]["status"] == "confirmed" and operator["rules_status"] == "confirmed"


def test_seed_rows_are_marked_operator_authored():
    rows = list(csv.DictReader(io.StringIO(SEED.read_text(encoding="utf-8"))))
    assert rows and all((r["rules_status"], r["rules_source"]) == ("confirmed", "manual") for r in rows)


async def test_seed_rows_through_the_importer_stay_confirmed(p2d):
    started = await p2d.client.post("/api/v1/catalogue/imports",
                                    files={"file": ("seed.csv", SEED.read_bytes(), "text/csv")}, data={"dry_run": "true"})
    assert started.status_code == 202, started.text
    await p2d.drain()
    report = (await p2d.client.get(started.json()["report_url"], params={"format": "json"})).json()
    assert {r["rules_status"] for r in report["rows"] if r["status"] in ("ok", "warning")} == {"confirmed"}


async def test_capture_warnings_are_in_the_job_result_the_proposal_and_the_page(p2d, rules_site, site_hits):
    dest = await p2d.add(name="Warned Forum", url="https://warned-forum.example.org/", **FORUM)
    started = await p2d.client.post(f"/api/v1/destinations/{dest['destination_id']}/rules/capture", json={
        "rules_url": f"{rules_site}/private/rules", "text": "Wait at least 3 days between your posts.",
        "use_platform_api": False})
    assert started.status_code == 202, started.text
    await p2d.drain()
    job = await p2d.job(started.json()["job_id"])
    assert job["status"] == "succeeded", job
    codes = {w["code"] for w in job["result"]["warnings"]}
    assert {"ROBOTS_DISALLOWED", "FIELD_DEFAULTED"} <= codes, job["result"]["warnings"]
    assert "/private/" not in site_hits
    proposal = (await p2d.client.get(f"/api/v1/rule-proposals/{job['result']['proposal_id']}")).json()
    assert {w["code"] for w in proposal["warnings"]} == codes
    page = await p2d.client.get(f"/ui/rules/{proposal['proposal_id']}")
    assert "ROBOTS_DISALLOWED" in page.text and "FIELD_DEFAULTED" in page.text


async def test_failed_capture_returns_structured_warnings(p2d, rules_site):
    dest = await p2d.add(name="Blocked Forum", url="https://blocked-forum.example.org/", **FORUM)
    started = await p2d.client.post(f"/api/v1/destinations/{dest['destination_id']}/rules/capture",
                                    json={"rules_url": f"{rules_site}/private/rules"})
    await p2d.drain()
    job = await p2d.job(started.json()["job_id"])
    assert job["status"] == "failed"
    warnings = job["error"]["error"]["details"]["warnings"]
    assert warnings and warnings[0]["code"] == "ROBOTS_DISALLOWED"
