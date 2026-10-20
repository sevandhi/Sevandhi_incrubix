"""Catalogue-rules stack check (imported rules stay proposed, capture warnings, and MH3 through the importer), against a running stack in simulation mode.

    uv run python scripts/p5f_e2e.py --api http://127.0.0.1:<port> [--token T] [--log evidence/advanced/P5F_import_rules_e2e.log]

1. Import a row whose rules are researched (rules_status=proposed): a proposal with warnings appears and nothing is
   confirmed; a publication is refused (DEST_RULES_UNCONFIRMED); a person confirms; the rules are then enforced
   (self-promotion blocked, the first post published, the second rescheduled by the 1/P7D cap).
2. Import an operator row (rules_status=confirmed, rules_source=manual): confirmed rules, certification PASSED (MH3).
3. Capture rules with a robots-disallowed rules_url plus pasted text: the job result, the proposal and the confirm page
   carry the warnings.
Exit code 0 only when every check passed. Prints one line per check (no secrets: the token is never printed).
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

MOCK = "http://mock-platforms:8080"
RULES = {"frequency_cap": "1/P7D", "self_promo_allowed": "false", "link_policy": "any", "disclosure_required": "false"}


class Run:
    def __init__(self, api: str, token: str) -> None:
        self.client = httpx.Client(base_url=api.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60)
        self.lines: list[str] = []
        self.failed = 0

    def check(self, ok: bool, what: str, detail: Any = "") -> None:
        self.failed += 0 if ok else 1
        line = f"{'PASS' if ok else 'FAIL'}  {what}" + (f"  [{detail}]" if detail != "" else "")
        self.lines.append(line)
        print(line, flush=True)

    def job(self, job_id: str, timeout_s: float = 180) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            job = self.client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in ("succeeded", "failed"):
                return job
            time.sleep(1)
        raise SystemExit(f"job {job_id} did not finish")

    def import_rows(self, rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=sorted({k for r in rows for k in r}), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        started = self.client.post("/api/v1/catalogue/imports", files={"file": ("p5f.csv", out.getvalue().encode(), "text/csv")},
                                   data={"import_unreachable": "true"})
        started.raise_for_status()
        self.job(started.json()["job_id"])
        report = self.client.get(started.json()["report_url"], params={"format": "json"}).json()
        return {r["name"]: r for r in report["rows"]}

    def post(self, dest_id: str, **content: Any) -> httpx.Response:
        body = {"title": "How do you test long-tail forums?", "body": "We compared three approaches to testing forum "
                "posts. What has worked for you?", "ai_generated": False, "is_promotional": False, "language": "en",
                **content}
        return self.client.post("/api/v1/publications", json={"destination_id": dest_id, "content": body})

    def certify(self, dest_id: str) -> dict[str, Any]:
        run_id = self.client.post("/api/v1/certifications", json={"destination_id": dest_id}).json()["run_id"]
        deadline = time.time() + 180
        while time.time() < deadline:
            run = self.client.get(f"/api/v1/certifications/{run_id}").json()
            if run["passed"] is not None:
                return run
            time.sleep(1)
        raise SystemExit("certification did not finish")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", required=True)
    parser.add_argument("--token", default=os.environ.get("API_TOKEN", "ibx-dev-token"))
    parser.add_argument("--log")
    args = parser.parse_args(argv)
    run = Run(args.api, args.token)
    tag = uuid.uuid4().hex[:6]
    run.lines.append(f"# import-rules e2e {datetime.now(UTC).isoformat(timespec='seconds')} api={args.api} tag={tag}")
    base = {"connector_key": "discourse", "platform_key": "discourse", "surface": "topic", "category": "saas",
            "audience_tags": "ai;founders", "language": "en", "connection_ref": "conn-discourse-sim"}

    # 1. researched rules -> proposal -> confirm -> enforced
    report = run.import_rows([
        {"name": f"p5f-researched-{tag}", "url": f"{MOCK}/discourse/p5f-researched-{tag}", **base, **RULES,
         "rules_status": "proposed", "rules_source": "agent_research", "rules_url": f"{MOCK}/discourse/p5f-researched-{tag}/faq"},
        {"name": f"p5f-operator-{tag}", "url": f"{MOCK}/discourse/p5f-operator-{tag}", **base, **RULES,
         "rules_status": "confirmed", "rules_source": "manual"},
    ])
    line = report[f"p5f-researched-{tag}"]
    run.check(line["rules_status"] == "proposed" and line["rules_proposal_id"].startswith("rprop_"),
              "import: researched row -> rules_status=proposed with a proposal id", line["rules_proposal_id"])
    dest_id, pid = line["destination_id"], line["rules_proposal_id"]
    dest = run.client.get(f"/api/v1/destinations/{dest_id}").json()
    run.check(dest["rules"] is None and dest["rules_status"] == "proposed", "import: nothing confirmed by the import",
              dest["rules_status"])
    proposal = run.client.get(f"/api/v1/rule-proposals/{pid}").json()
    codes = sorted({w["code"] for w in proposal["warnings"]})
    run.check({"UNVERIFIED_ROW_RULES", "FIELD_DEFAULTED"} <= set(codes), "GET rule-proposal: warnings returned", codes)
    page = run.client.get(f"/ui/rules/{pid}")
    run.check(page.status_code == 200 and "UNVERIFIED_ROW_RULES" in page.text, "confirm page shows the warnings")
    run.check(run.certify(dest_id)["state"] == "PASSED", "certification of the researched destination")
    refused = run.post(dest_id)
    run.check(refused.status_code == 409 and refused.json()["error"]["code"] == "DEST_RULES_UNCONFIRMED",
              "publication before confirmation refused", refused.status_code)
    confirmed = run.client.post(f"/api/v1/rule-proposals/{pid}/confirm",
                                json={"confirmed_by": "p5f-e2e (stand-in for the person)", "overrides": {"automation_allowed": True}})
    run.check(confirmed.status_code == 200 and confirmed.json()["rules"]["status"] == "confirmed", "proposal confirmed",
              confirmed.status_code)
    promo = run.post(dest_id, title="Our new tool is live", is_promotional=True,
                     body="Sign up today at https://mytool.example.com/", owner_domains=["mytool.example.com"])
    run.check(promo.status_code == 409 and promo.json()["error"]["code"] == "DEST_RULE_SELF_PROMO",
              "confirmed rules enforced: self-promotion blocked", promo.status_code)
    first = run.post(dest_id)
    pub_id = first.json()["publication"]["publication_id"] if first.status_code == 202 else None
    state = None
    for _ in range(60):
        state = run.client.get(f"/api/v1/publications/{pub_id}").json()["state"] if pub_id else None
        if state in ("published", "failed", "blocked"):
            break
        time.sleep(1)
    run.check(state == "published", "first post published", state)
    second = run.post(dest_id, title="Another question this week?")
    second_pub = second.json().get("publication", {}) if second.status_code == 202 else {}
    run.check(second_pub.get("code") == "DEST_RULE_FREQUENCY_CAP", "second post rescheduled by the 1/P7D cap",
              second_pub.get("state"))

    # 2. operator row (manual/confirmed) -> confirmed + certifies (MH3)
    op = report[f"p5f-operator-{tag}"]
    op_dest = run.client.get(f"/api/v1/destinations/{op['destination_id']}").json()
    run.check(op["rules_status"] == "confirmed" and op_dest["rules"]["confirmed_by"] == "catalogue_import",
              "import: operator row manual/confirmed -> confirmed rules", op_dest["rules_status"])
    cert = run.certify(op["destination_id"])
    run.check(cert["state"] == "PASSED", "MH3: operator row certifies", f"{cert['state']} {cert.get('level_after')}")

    # 3. capture warnings (robots-disallowed rules page + pasted text) in the job result / proposal / page
    robots_url = f"{MOCK}/internet/norobots-en-ai-p5f{tag}/rules"
    started = run.client.post(f"/api/v1/destinations/{op['destination_id']}/rules/capture", json={
        "rules_url": robots_url, "text": "Wait at least 3 days between your posts.", "use_platform_api": False})
    job = run.job(started.json()["job_id"])
    job_codes = sorted({w["code"] for w in (job.get("result") or {}).get("warnings", [])})
    run.check(job["status"] == "succeeded" and "ROBOTS_DISALLOWED" in job_codes, "capture job result has warnings", job_codes)
    if job["status"] == "succeeded":
        cap = run.client.get(f"/api/v1/rule-proposals/{job['result']['proposal_id']}").json()
        run.check(sorted({w["code"] for w in cap["warnings"]}) == job_codes, "GET capture proposal has the same warnings")
        page = run.client.get(f"/ui/rules/{cap['proposal_id']}")
        run.check("ROBOTS_DISALLOWED" in page.text, "confirm page shows the capture warnings")

    run.lines.append(f"# result: {'PASS' if run.failed == 0 else f'FAIL ({run.failed})'}")
    if args.log:
        with open(args.log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(run.lines) + "\n")
    return 0 if run.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
