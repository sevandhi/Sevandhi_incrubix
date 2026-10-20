"""MH5 proof: never spam, never cheat. Test posts go only to our own instances and are removed within 10 minutes;
platform rate limits are respected (back off on 429 using the reset time).

    scripts/prove_mh5.sh            (or: uv run python scripts/prove_mh5.py against IBX_API_URL)

1. Test-post audit (GET /api/v1/test-posts, written by the certification harness and the sweeper). Every row is put in
   exactly ONE category from its delete_mode, and the categories are reported SEPARATELY:
     deleted               the post is gone (Lemmy purge, Ghost delete, webhook delete)
     soft_deleted          Discourse soft delete (public 404; staff can still see it)
     unpublished_to_draft  Dev.to has no delete API: the article is unpublished to a draft (NOT deleted)
     pack_revoked          assisted: nothing was posted by us; the pack's files were removed
     not_created           the write-ahead row of a post that never reached the platform
     sweeper_not_found     the sweeper could not find the post any more (counted on its own, not as deleted)
     still_live / other
   Fails if any lifetime, or any still-live age, is over 600 s. Unpublished and not-created are never counted as deleted.
2. Own instances only: in local mode a row for a public host (example.com, IANA-reserved) is refused at PREFLIGHT and
   no PUBLISH step runs.
3. Back-off on 429 for every publishing connector (scripts/prove_scenarios.py rate_limit): the reset time is used and the
   next call short-circuits without calling the platform.
4. The sweeper tests (leftover test posts are removed within the TTL; the sweeper is a periodic job).
Evidence: evidence/must_haves/MH5_test_post_lifetimes.csv, MH5_test_post_audit.txt, MH5_backoff_429.csv, MH5_never_spam.log
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from typing import Any

from prove_lib import ROOT, Proof, tag
from prove_scenarios import rate_limit

LIMIT_S = 600.0
FIELDS = ["id", "connector_key", "destination_id", "run_id", "platform_post_id", "created_at", "deleted_at", "lifetime_s",
          "delete_mode", "outcome", "category", "alive_for_s"]
CATEGORIES = ["deleted", "soft_deleted", "unpublished_to_draft", "pack_revoked", "not_created", "already_gone",
              "unconfirmed", "sweeper_not_found", "still_live", "other"]


def category(item: dict[str, Any]) -> str:
    mode = str(item.get("delete_mode") or "").lower().removeprefix("sweeper_")
    if item.get("deleted_at") is None:
        return "still_live"
    if mode in ("deleted", "purged", "permanent", "removed"):
        return "deleted"
    if mode in ("soft_deleted", "soft"):
        return "soft_deleted"
    if mode in ("admin_unpublish", "unpublish_to_draft", "unpublished_to_draft", "draft", "unpublished"):
        return "unpublished_to_draft"
    if mode == "pack_revoked":
        return "pack_revoked"
    if mode == "not_created":
        return "not_created"
    if mode == "not_found":
        return "sweeper_not_found"
    return "other"


def main() -> int:
    p = Proof("MH5", "Never spam, never cheat: own instances only, test posts removed within 10 minutes, 429 back-off",
              "scripts/prove_mh5.sh")
    mode = p.mode()

    p.section("1. test-post audit")
    audit = p.http("GET", "/api/v1/test-posts", params={"limit": 10000}, quiet=True).json()
    items = sorted(audit["test_posts"], key=lambda i: str(i.get("created_at")))
    disagree = []
    for item in items:
        mine = category(item)
        item["category"] = item.get("outcome") or mine  # the service's own outcome when it reports one
        if item.get("outcome") and item["outcome"] != mine and mine != "other":
            disagree.append((item["id"], item.get("delete_mode"), item["outcome"], mine))
    counts = Counter(i["category"] for i in items)
    reported = {k: audit[k] for k in CATEGORIES if k in audit}
    lifetimes = [float(i["lifetime_s"]) for i in items if i.get("lifetime_s") is not None and i["category"] != "not_created"]
    over = [i["id"] for i in items if (i.get("lifetime_s") or 0) > LIMIT_S or (i.get("alive_for_s") or 0) > LIMIT_S]
    by_connector: dict[str, Counter[str]] = {}
    for i in items:
        by_connector.setdefault(i["connector_key"], Counter())[i["category"]] += 1
    lines = [f"test posts: {len(items)} (TTL {audit.get('ttl_s')} s, limit {LIMIT_S:g} s)",
             "as reported by GET /api/v1/test-posts: " + (json.dumps(reported) if reported else "(no per-outcome counts)"),
             "per row (this script):"]
    lines += [f"  {c}: {counts.get(c, 0)}" for c in CATEGORIES]
    lines.append(f"max lifetime of a removed post: {max(lifetimes) if lifetimes else None} s")
    lines.append("by connector: " + "; ".join(f"{k}: " + ", ".join(f"{c}={n}" for c, n in sorted(v.items()))
                                             for k, v in sorted(by_connector.items())))
    lines.append("Words: 'deleted' counts only real deletions; Discourse is 'soft_deleted'; Dev.to is 'unpublished_to_draft' "
                 "(the platform has no delete API, so its articles are NOT deleted); 'not_created' posts never existed.")
    p.write_text("evidence/must_haves/MH5_test_post_audit.txt", p.header() + "\n".join(lines) + "\n")
    for line in lines:
        p.log(line)
    p.write_csv("evidence/must_haves/MH5_test_post_lifetimes.csv", items, FIELDS)
    p.check("audit has test posts to judge", len(items) > 0, len(items))
    p.check(f"no test post lived (or is still live) longer than {LIMIT_S:g} s", not over, {"over": over[:10]})
    if reported:
        p.check("the service's per-outcome counts equal the per-row outcomes (nothing summed into 'deleted')",
                all(reported.get(k, 0) == counts.get(k, 0) for k in reported), {"reported": reported, "rows": dict(counts)})
    p.check("this script's reading of delete_mode agrees with the service's outcome", not disagree, disagree[:5],
            critical=False)
    if counts.get("unconfirmed"):
        p.check("no removal is unconfirmed", False, f"{counts['unconfirmed']} unconfirmed", critical=False)
    p.check("no test post has an unknown removal mode", counts.get("other", 0) == 0,
            sorted({str(i.get("delete_mode")) for i in items if i["category"] == "other"}))
    if counts.get("unpublished_to_draft"):
        p.log("NOTE: Dev.to test articles are unpublished to drafts, not deleted (platform limitation, never claimed as deleted).")

    p.section("2. own instances only")
    if mode == "local":
        row = {"name": f"Public forum (must be refused) {tag()}", "url": f"https://forum-{tag()}.example.com",
               "connector_key": "discourse", "automation_allowed": "true"}
        dest = p.create_dest(row)
        run = p.certify(dest["destination_id"])
        steps = [s["step"] for s in run.get("steps", [])]
        pre = next((s for s in run.get("steps", []) if s["step"] == "PREFLIGHT"), {})
        p.check("a public host is refused at PREFLIGHT and nothing is published", "PUBLISH" not in steps
                and pre.get("outcome") != "ok", {"steps": steps, "failure": run.get("failure_code"),
                                                  "detail": str(pre.get("detail"))[:200]})
    else:
        p.add_partial("simulation mode: every destination is rewritten to mock-platforms; the own-instance refusal was not exercised")

    p.section("3. back-off on 429 using the reset time")
    rows = []
    targets = [("discourse", "mock"), ("lemmy", "mock"), ("devto", "mock"), ("ghost", "mock"), ("webhook", "mock")]
    if mode == "local":
        targets.append(("lemmy", "real-local"))
    for connector, target in targets:
        row = rate_limit(p, connector, target)
        rows.append(row)
        p.check(f"{connector} ({target}): 429 -> PLATFORM_RATE_LIMIT with the reset time, next call short-circuits",
                row["status"] == "PASS", {k: row.get(k) for k in ("http_status", "error_code", "retry_after_s", "reset_source",
                                                                  "next_call", "platform_creates")})
    p.write_csv("evidence/must_haves/MH5_backoff_429.csv", rows,
                ["connector", "target", "trigger", "http_status", "error_code", "retry_after_s", "retry_at", "reset_source",
                 "retry_after_header", "next_call", "platform_creates", "after_reset", "status"])

    p.section("4. sweeper tests")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", "sweep", "tests/p2d"],
                            cwd=ROOT, capture_output=True, text=True, timeout=900)
    tail = (result.stdout.strip().splitlines() or [""])[-1]
    p.log(tail)
    p.check("sweeper tests pass (leftover test posts removed within the TTL; periodic job)", result.returncode == 0, tail)
    p.add_partial("'no scraping where it is not allowed': only the catalogue seed research fetched public pages (robots-aware, "
                  "see catalogue/seeds/README.md); the catalogue import/enrich fetcher is not built, so it is not proven here")
    return p.finish("evidence/must_haves/MH5_never_spam.log")


if __name__ == "__main__":
    sys.exit(main())
