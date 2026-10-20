"""MH4 proof: frequency cap, self-promotion, link policy and AI disclosure are checked before every post; a post that
breaks a rule is blocked or rescheduled with a reason, and never reaches the platform.

    scripts/prove_mh4.sh

1. Rule-safety table (scripts/rule_safety.py over tests/rules/rule_cases.yaml) -> evidence/metrics/rule_safety.csv;
   any missed violation or false positive fails the proof.
2. Catalogue path (POST /api/v1/publications) on CERTIFIED Discourse destinations (mock-platforms):
   second post inside a 1/P7D cap -> rescheduled with retry_at + reason; two posts at once -> one goes out;
   promotional post to a no-promotion destination -> 409 DEST_RULE_SELF_PROMO with the signals named (also when
   is_promotional is not set); a link to a no-links destination -> 409 DEST_RULE_LINK_POLICY;
   AI content to a disclosure destination -> published with the disclosure line inserted (checked on the platform).
   The platform's create counter shows that blocked/rescheduled posts were never sent.
3. Contract path (POST /connector/v1/discourse/publish with the Appendix A record fields as rules): the same four rules.
Evidence: evidence/must_haves/MH4_rules.log, evidence/must_haves/MH4_rules.csv, evidence/metrics/rule_safety.csv
"""

from __future__ import annotations

import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from prove_lib import MOCK, MOCK_IN, ROOT, Proof, mock_url, tag

QUESTION = {"title": "How do you test posts to long-tail communities?", "language": "en", "ai_generated": False,
            "body": "We compared three ways to test forum posting end to end before going live. Which one has worked for you?"}
PROMO = {"title": "Our new course launched this week", "language": "en", "ai_generated": False,
         "body": "Buy my course today with code SAVE50 at https://mycourse.example.com/?utm_source=forum - subscribe for more!"}
DISCLOSURE_EN = "Disclosure: this post was created with the help of AI."


def main() -> int:
    p = Proof("MH4", "Rules checked before every post (cap, self-promotion, links, AI disclosure)", "scripts/prove_mh4.sh")
    p.mode()
    rows: list[dict[str, Any]] = []

    def record(path: str, rule: str, expected: str, got: str, ok: bool, creates_delta: int | None, reason: str = "") -> None:
        rows.append({"path": path, "rule": rule, "expected": expected, "got": got, "platform_creates_delta": creates_delta,
                     "reason_given": reason[:200], "result": "PASS" if ok else "FAIL"})

    p.section("1. rule-safety table")
    out = subprocess.run([sys.executable, "scripts/rule_safety.py"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    summary = (out.stdout.strip().splitlines() or [""])[-1]
    p.log(summary)
    missed = re.search(r"missed (\d+)", summary)
    fps = re.search(r"false positives (\d+)", summary)
    p.check("rule_safety.py: 0 missed violations, 0 false positives, exit 0", out.returncode == 0 and missed is not None
            and missed.group(1) == "0" and fps is not None and fps.group(1) == "0", summary)
    if (ROOT / "evidence/metrics/rule_safety.csv").exists():
        p.evidence.append("evidence/metrics/rule_safety.csv")

    def certified_dest(name: str, **rules: str) -> tuple[str, str]:
        tenant = tag(f"mh4-{name}-")
        dest = p.create_dest({"name": f"MH4 {name} {tenant}", "url": mock_url("discourse", tenant), "connector_key": "discourse",
                              "language": "en", "automation_allowed": "true", **rules})
        run = p.certify(dest["destination_id"])
        p.check(f"{name}: destination certified before any publication", run.get("state") == "PASSED", run.get("state"))
        return dest["destination_id"], tenant

    def publication(dest_id: str, content: dict[str, Any]) -> Any:
        return p.http("POST", "/api/v1/publications", json={"destination_id": dest_id, "content": content})

    p.section("2a. weekly cap (catalogue path): second post inside 1/P7D")
    dest_id, tenant = certified_dest("weekly-cap", frequency_cap="1/P7D", self_promo_allowed="true")
    first = publication(dest_id, QUESTION)
    job = p.wait_job(first.json()["job_id"]) if first.status_code == 202 else {}
    pub = p.http("GET", f"/api/v1/publications/{first.json().get('publication_id')}", quiet=True).json() if first.status_code == 202 else {}
    p.check("first post inside the week is published", job.get("status") == "succeeded" and pub.get("state") == "published",
            {"job": job.get("status"), "state": pub.get("state")})
    before = p.creates("discourse", tenant)
    second = publication(dest_id, {**QUESTION, "title": "A second question in the same week?"})
    body = second.json()
    second_pub = body.get("publication") or {}
    ok = second.status_code == 202 and second_pub.get("state") == "rescheduled" and second_pub.get("code") == "DEST_RULE_FREQUENCY_CAP" \
        and bool(second_pub.get("retry_at")) and bool(second_pub.get("reason"))
    delta = p.creates("discourse", tenant) - before
    p.check("second post -> rescheduled, DEST_RULE_FREQUENCY_CAP, retry_at and a reason; nothing sent", ok and delta == 0,
            {"status": second.status_code, "publication": {k: second_pub.get(k) for k in ("state", "code", "retry_at", "reason")},
             "creates_delta": delta})
    record("publications", "frequency_cap 1/P7D", "rescheduled + retry_at", f"{second.status_code} {second_pub.get('state')}",
           ok and delta == 0, delta, str(second_pub.get("reason")))

    p.section("2b. weekly cap, two first posts at the same time")
    dest_id, tenant = certified_dest("concurrent-cap", frequency_cap="1/P7D", self_promo_allowed="true")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda t: publication(dest_id, {**QUESTION, "title": t}),
                                ["Concurrent question A for the forum?", "Concurrent question B for the forum?"]))
    states = sorted((r.json().get("publication") or {}).get("state", str(r.status_code)) for r in results)
    for r in results:
        if r.status_code == 202 and (r.json().get("publication") or {}).get("state") == "pending":
            p.wait_job(r.json()["job_id"])
    ok = states == ["pending", "rescheduled"]
    p.check("two concurrent first posts -> one pending (goes out), one rescheduled", ok, states)
    record("publications", "frequency_cap concurrent", "pending + rescheduled", str(states), ok, None)

    p.section("2c. promotional post to a no-promotion destination")
    dest_id, tenant = certified_dest("no-promo", self_promo_allowed="false", frequency_cap="5/P7D")
    before = p.creates("discourse", tenant)
    for flag in (True, None):
        content = {**PROMO, "owner_domains": ["mycourse.example.com"]}
        if flag is not None:
            content["is_promotional"] = flag
        refused = publication(dest_id, content)
        error = refused.json().get("error") or {}
        ok = refused.status_code == 409 and error.get("code") == "DEST_RULE_SELF_PROMO" and bool(error.get("message"))
        label = "is_promotional=true" if flag else "is_promotional not set"
        p.check(f"promo ({label}) -> 409 DEST_RULE_SELF_PROMO with the reason", ok,
                {"status": refused.status_code, "code": error.get("code"), "message": error.get("message")})
        record("publications", f"self_promo_allowed=false ({label})", "409 DEST_RULE_SELF_PROMO",
               f"{refused.status_code} {error.get('code')}", ok, None, str(error.get("message")))
    delta = p.creates("discourse", tenant) - before
    p.check("no promotional post reached the platform", delta == 0, {"creates_delta": delta})

    p.section("2d. link to a no-links destination (catalogue spellings 'none' and 'no_links')")
    for spelling in ("none", "no_links"):
        dest_id, tenant = certified_dest(f"no-links-{spelling.replace('_', '')}", link_policy=spelling, self_promo_allowed="true")
        stored = p.http("GET", f"/api/v1/destinations/{dest_id}", quiet=True).json().get("link_policy")
        linked = publication(dest_id, {**QUESTION, "body": QUESTION["body"] + " Notes: https://notes.example.org/forum-testing"})
        error = linked.json().get("error") or {}
        ok = linked.status_code == 409 and error.get("code") == "DEST_RULE_LINK_POLICY"
        p.check(f"row link_policy={spelling!r} (stored as {stored!r}): a link -> 409 DEST_RULE_LINK_POLICY", ok,
                {"status": linked.status_code, "code": error.get("code"), "message": error.get("message"), "stored": stored})
        record("publications", f"link_policy={spelling}", "409 DEST_RULE_LINK_POLICY", f"{linked.status_code} {error.get('code')}",
               ok, None, str(error.get("message")))
        if linked.status_code == 202:
            p.wait_job(linked.json()["job_id"])

    p.section("2e. AI content to a disclosure destination")
    dest_id, tenant = certified_dest("disclosure", disclosure_required="true", self_promo_allowed="true")
    ai = publication(dest_id, {**QUESTION, "title": "A summary of this week's release notes?", "ai_generated": True})
    job = p.wait_job(ai.json()["job_id"]) if ai.status_code == 202 else {}
    pub = p.http("GET", f"/api/v1/publications/{ai.json().get('publication_id')}").json() if ai.status_code == 202 else {}
    page = ""
    if pub.get("live_url"):
        page = p.http("GET", str(pub["live_url"]).replace(MOCK_IN, MOCK), client=p.mock, quiet=True).text
    ok = pub.get("state") == "published" and DISCLOSURE_EN in page
    p.check("AI content -> published WITH the disclosure line (read back from the platform page)", ok,
            {"state": pub.get("state"), "disclosure_on_page": DISCLOSURE_EN in page, "job": job.get("status")})
    record("publications", "disclosure_required=true", "published with disclosure", str(pub.get("state")), ok, None)

    p.section("3. contract path: Appendix A record fields as rules")
    tenant = tag("mh4-contract-")
    url = mock_url("discourse", tenant)
    cap_dest = {"url": url, "surface": "topic", "frequency_cap": "1/P7D"}
    first = p.publish("discourse", QUESTION, cap_dest)
    second = p.publish("discourse", {**QUESTION, "title": "Second question for the same forum this week?"}, cap_dest)
    err = second.json().get("error") or {}
    ok = first.status_code == 200 and second.status_code == 409 and err.get("code") == "DEST_RULE_FREQUENCY_CAP" \
        and bool((err.get("details") or {}).get("retry_at")) and second.headers.get("retry-after") is not None
    p.check("contract: 2nd post inside 1/P7D -> 409 DEST_RULE_FREQUENCY_CAP, retry_at, Retry-After", ok,
            {"first": first.status_code, "second": second.status_code, "code": err.get("code"),
             "retry_at": (err.get("details") or {}).get("retry_at")})
    record("contract", "frequency_cap 1/P7D", "409 DEST_RULE_FREQUENCY_CAP", f"{second.status_code} {err.get('code')}", ok,
           p.creates("discourse", tenant) - 1, str(err.get("message")))
    for rule, dest, content, code in [
        ("self_promo_allowed=false", {"self_promo_allowed": False}, PROMO, "DEST_RULE_SELF_PROMO"),
        ("link_policy=none", {"link_policy": "none"}, {**QUESTION, "body": QUESTION["body"] + " https://notes.example.org/x"},
         "DEST_RULE_LINK_POLICY"),
    ]:
        tenant = tag("mh4-contract-")
        refused = p.publish("discourse", content, {"url": mock_url("discourse", tenant), "surface": "topic", **dest})
        err = refused.json().get("error") or {}
        sent = p.creates("discourse", tenant)
        ok = refused.status_code == 409 and err.get("code") == code and sent == 0
        p.check(f"contract: {rule} -> 409 {code}, platform not called", ok,
                {"status": refused.status_code, "code": err.get("code"), "creates": sent, "message": err.get("message")})
        record("contract", rule, f"409 {code}", f"{refused.status_code} {err.get('code')}", ok, sent, str(err.get("message")))
    tenant = tag("mh4-contract-")
    ai = p.publish("discourse", {**QUESTION, "ai_generated": True}, {"url": mock_url("discourse", tenant), "surface": "topic",
                                                                     "disclosure_required": True})
    page = p.http("GET", str(ai.json().get("live_url", "")).replace(MOCK_IN, MOCK), client=p.mock, quiet=True).text \
        if ai.status_code == 200 else ""
    ok = ai.status_code == 200 and DISCLOSURE_EN in page
    p.check("contract: disclosure_required + ai_generated -> 200, disclosure line on the platform page", ok,
            {"status": ai.status_code, "disclosure_on_page": DISCLOSURE_EN in page})
    record("contract", "disclosure_required=true", "200 with disclosure", str(ai.status_code), ok, p.creates("discourse", tenant))

    p.write_csv("evidence/must_haves/MH4_rules.csv", rows,
                ["path", "rule", "expected", "got", "platform_creates_delta", "reason_given", "result"])
    return p.finish("evidence/must_haves/MH4_rules.log")


if __name__ == "__main__":
    sys.exit(main())
