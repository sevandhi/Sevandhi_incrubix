"""OUT4 proof: re-verify the real catalogue's counts from ``catalogue/real_catalogue_v1.csv`` and
``catalogue/reports/real_catalogue_report.md`` (and, when present, ``catalogue/reports/real_run.json``).

The strict rule (plan §7 OUT4, decision D6) is re-implemented here on its own, not imported from the builder:
a row counts toward the 150 only when
  1. its URL answered 200 in the import (import_status ok|warning, http_status 200),
  2. its rules_url answered 200 at check time (preflight 200) and rules capture read it (capture_status succeeded),
  3. its rules came from the page or the platform API (rules_source page | api_sidebar | api_sidebar+page:
     at least one field whose quote was found verbatim in the fetched text),
  4. a proposal exists, and
  5. a person confirmed it (proposal_status confirmed, confirmed_by a person, never "pending-human" or "agent:*").
Rows meeting 1-4 are "eligible on confirmation". Rows meeting 1, 2 and 4 (defaulted proposals included) are
"reviewable": the upper bound if the person also states rules from the captured page while confirming.
Each row is counted once (distinct destination_id).

    uv run python scripts/prove_out4.py                    # offline: CSV + report (+ run JSON)
    uv run python scripts/prove_out4.py --api URL --token T  # also read each proposal's status now (after a person
                                                             # confirmed some in /ui/rules/<id>) and recount
    --require-met                                            # exit 1 unless at least 150 rows count

Exit 1 on any inconsistency between the CSV, the report and the rule.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "catalogue" / "real_catalogue_v1.csv"
REPORT_PATH = ROOT / "catalogue" / "reports" / "real_catalogue_report.md"
RUN_PATH = ROOT / "catalogue" / "reports" / "real_run.json"
TARGET = 150
APPENDIX_A = ["name", "url", "platform_key", "surface", "category", "audience_tags", "language", "geo", "est_audience",
              "rules_url", "frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required",
              "automation_allowed", "connector_key", "relevance_score", "relevance_reason", "level", "last_certified_at"]
OUT4 = ["researched_at", "discovered_via", "rules_source", "rules_status"]
MAJOR = ("reddit.com", "discord.com", "discord.gg", "t.me", "telegram.org", "linkedin.com", "facebook.com", "x.com",
         "twitter.com", "instagram.com", "tiktok.com", "youtube.com", "pinterest.com", "threads.net", "whatsapp.com")
SHA = re.compile(r"^[0-9a-f]{64}$")
PER_HOST_CAP = 15


def is_person(confirmed_by: str) -> bool:
    who = (confirmed_by or "").strip().lower()
    return bool(who) and who != "pending-human" and not who.startswith("agent") and who not in ("catalogue_import",
                                                                                            "catalogue_row")


def strict(row: dict[str, str]) -> tuple[bool, bool]:
    """(eligible_on_confirm, countable_now) recomputed from the row's evidence columns."""
    url_ok = row["import_status"] in ("ok", "warning") and row["http_status"] == "200"
    rules_ok = bool(row["rules_url"]) and row["rules_url_status"] == "200" and row["capture_status"] == "succeeded"
    source_ok = row["rules_source"] in ("page", "api_sidebar", "api_sidebar+page") and int(row["fields_verified"] or 0) >= 1
    proposal_ok = row["proposal_status"] in ("proposed", "confirmed") and bool(row["proposal_id"])
    eligible = url_ok and rules_ok and source_ok and proposal_ok
    return eligible, eligible and row["proposal_status"] == "confirmed" and is_person(row["confirmed_by"])


def report_counts(text: str) -> dict:
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    if not match:
        raise SystemExit("FAIL: the report has no ```json counts block")
    return json.loads(match.group(1))


def live_statuses(rows: list[dict[str, str]], api: str, token: str) -> dict[str, dict]:
    import httpx

    out = {}
    with httpx.Client(base_url=api, headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
        for row in rows:
            if not row["proposal_id"]:
                continue
            resp = client.get(f"/api/v1/rule-proposals/{row['proposal_id']}")
            out[row["proposal_id"]] = resp.json() if resp.status_code == 200 else {"status": f"http_{resp.status_code}"}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-verify the OUT4 real-catalogue counts.")
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--run", type=Path, default=RUN_PATH)
    parser.add_argument("--api")
    parser.add_argument("--token", default="ibx-dev-token")
    parser.add_argument("--require-met", action="store_true")
    args = parser.parse_args(argv)

    problems: list[str] = []
    with args.csv.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        rows = list(reader)
    missing = [c for c in APPENDIX_A + OUT4 if c not in columns]
    if missing:
        problems.append(f"CSV lacks columns: {missing}")

    if args.api:  # a person may have confirmed proposals since the CSV was built
        live = live_statuses(rows, args.api, args.token)
        for row in rows:
            p = live.get(row["proposal_id"])
            if p is None:
                continue
            row["proposal_status"] = p.get("status", row["proposal_status"])
            decision = p.get("decision") or {}
            row["confirmed_by"] = decision.get("confirmed_by") or row["confirmed_by"]
        print(f"live: read {len(live)} proposals from {args.api}: {dict(Counter(p.get('status') for p in live.values()))}")

    eligible, countable, reviewable = [], [], []
    for n, row in enumerate(rows, start=2):
        e, c = strict(row)
        if not args.api:
            if (row["eligible_on_confirm"] == "yes") != e:
                problems.append(f"line {n}: eligible_on_confirm={row['eligible_on_confirm']} but the rule says {e}")
            if (row["countable_now"] == "yes") != c:
                problems.append(f"line {n}: countable_now={row['countable_now']} but the rule says {c}")
        if row["proposal_status"] == "proposed" and (row["confirmed_by"] != "pending-human" or row["rules_status"] != "pending-human"):
            problems.append(f"line {n}: a proposed rule set must say confirmed_by=rules_status=pending-human (D6)")
        if row["confirmed_by"].lower().startswith("agent"):
            problems.append(f"line {n}: confirmed by an agent")
        for col in ("rules_url_sha256", "rules_page_sha256"):
            if row[col] and not SHA.match(row[col]):
                problems.append(f"line {n}: {col} is not a sha256")
        for item in filter(None, row["source_sha256s"].split(";")):
            if not SHA.match(item.split(":", 1)[-1]):
                problems.append(f"line {n}: bad source sha256 '{item}'")
        if row["destination_id"]:
            if not (row["relevance_score"].isdigit() and 1 <= int(row["relevance_score"]) <= 5):
                problems.append(f"line {n}: relevance_score {row['relevance_score']!r} is not 1-5")
            if not row["relevance_reason"]:
                problems.append(f"line {n}: no relevance_reason")
            if row["level"] not in ("L0", "L1", "L2", "L3", "L4"):
                problems.append(f"line {n}: level {row['level']!r}")
            if row["level"] == "L0" and row["last_certified_at"]:
                problems.append(f"line {n}: L0 with a certification time")
        host = (urlsplit(row["url"]).hostname or "").lower()
        if any(host == m or host.endswith("." + m) for m in MAJOR):
            problems.append(f"line {n}: major platform host {host}")
        rv = (row["import_status"] in ("ok", "warning") and row["http_status"] == "200" and bool(row["rules_url"])
              and row["rules_url_status"] == "200" and row["capture_status"] == "succeeded"
              and row["proposal_status"] in ("proposed", "confirmed") and bool(row["proposal_id"]))
        if not args.api and (row.get("reviewable_on_confirm") == "yes") != rv:
            problems.append(f"line {n}: reviewable_on_confirm={row.get('reviewable_on_confirm')} but the rule says {rv}")
        if e and not rv:
            problems.append(f"line {n}: eligible but not reviewable")
        if rv:
            reviewable.append(row)
        if e:
            eligible.append(row)
        if c:
            countable.append(row)
    for label, group in (("eligible", eligible), ("countable", countable)):
        ids = [r["destination_id"] for r in group]
        if len(ids) != len(set(ids)):
            problems.append(f"{label} rows share a destination_id (counted twice)")
    hosts = Counter((urlsplit(r["url"]).hostname or "").lower() for r in eligible)
    over = {h: k for h, k in hosts.items() if k > PER_HOST_CAP}
    if over:
        problems.append(f"more than {PER_HOST_CAP} eligible rows on one host: {over}")

    recount = {
        "seed_rows": len(rows),
        "imported": sum(r["import_status"] in ("ok", "warning") for r in rows),
        "url_200": sum(r["import_status"] in ("ok", "warning") and r["http_status"] == "200" for r in rows),
        "rows_with_rules_url": sum(bool(r["rules_url"]) for r in rows),
        "rules_url_200": sum(r["rules_url_status"] == "200" for r in rows if r["rules_url"]),
        "proposals": sum(bool(r["proposal_id"]) for r in rows),
        "proposals_pending_human": sum(r["proposal_status"] == "proposed" for r in rows if r["proposal_id"]),
        "proposals_confirmed": sum(r["proposal_status"] == "confirmed" for r in rows if r["proposal_id"]),
        "defaulted": sum(r["rules_source"] == "default" for r in rows),
        "eligible_on_confirm": len(eligible),
        "reviewable_on_confirm": len(reviewable),
        "countable_now": len(countable),
        "target": TARGET,
    }
    stated = report_counts(args.report.read_text(encoding="utf-8"))
    if not args.api:
        for key, value in recount.items():
            if stated.get(key) != value:
                problems.append(f"report says {key}={stated.get(key)}, the CSV gives {value}")

    if args.run.exists() and not args.api:
        run = json.loads(args.run.read_text(encoding="utf-8"))
        props = {p["proposal_id"]: p for ps in run.get("proposals", {}).values() for p in ps}
        for row in rows:
            p = props.get(row["proposal_id"]) if row["proposal_id"] else None
            if row["proposal_id"] and p is None:
                problems.append(f"{row['name']}: proposal {row['proposal_id']} not in real_run.json")
                continue
            if p is None:
                continue
            verified = [f for f in p.get("fields", []) if f.get("status") == "verified"]
            if len(verified) != int(row["fields_verified"] or 0):
                problems.append(f"{row['name']}: fields_verified {row['fields_verified']} != run JSON {len(verified)}")
            if any(not f.get("quote") for f in verified):
                problems.append(f"{row['name']}: a verified field has no quote")
            if p.get("rules_page_sha256") != row["rules_page_sha256"]:
                problems.append(f"{row['name']}: rules_page_sha256 differs from the run JSON")
        statuses = Counter(p.get("status") for p in props.values())
        print(f"run JSON: {len(props)} proposals, statuses {dict(statuses)}")

    print(json.dumps(recount, indent=1))
    by_conn = Counter(r["connector_key"] for r in eligible)
    print("eligible on confirmation by connector:", dict(sorted(by_conn.items())))
    met = recount["countable_now"] >= TARGET
    print(f"OUT4 (>= {TARGET} counted): {'MET' if met else 'NOT MET'} - counted now {recount['countable_now']}, "
          f"eligible once a person confirms {recount['eligible_on_confirm']}, upper bound if the person also states rules "
          f"for defaulted proposals {recount['reviewable_on_confirm']}")
    if problems:
        print(f"FAIL: {len(problems)} problem(s)")
        for p in problems[:50]:
            print("  -", p)
        return 1
    print("PASS: the CSV, the report and the strict rule agree")
    return 1 if args.require_met and not met else 0


if __name__ == "__main__":
    sys.exit(main())
