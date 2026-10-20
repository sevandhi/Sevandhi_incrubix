"""Recount OUT4 (real catalogue researched with rules) strictly from catalogue/research/rules_research_v1.csv.

Offline and stdlib-only. It does not trust the CSV's own `counted` column: every rule is re-derived.

A row counts only if all of these hold:
  * the destination URL answered HTTP 200 at check time (`url_http_status`);
  * every rules source used by the row answered 200 (`http_status`, `http_status_2`, `http_status_3`) and its saved
    plain text exists at catalogue/research/pages/<page_sha256>.txt with sha256(text) == the stored `text_sha256`;
  * at least 2 of the 6 rule fields have a value other than `not_stated` that is backed by a verbatim quote of
    at most 25 words which re-verifies against the saved text of the source it names (whitespace collapsed,
    typographic quotes/dashes folded to ASCII);
  * relevance_score is 3-5 with a non-empty reason, and connector_key is set;
  * the host is not one of the 15 major platforms, and at most 15 rows per host count (file order).
Fields that are not quote-backed must be exactly `not_stated` (the source was read and does not say).

Usage: uv run python scripts/prove_out4_research.py [--require-met] [--target 165]
Exit 0 when the recount matches the CSV (and, with --require-met, the target is met); 1 otherwise.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "catalogue" / "research" / "rules_research_v1.csv"
PAGES = ROOT / "catalogue" / "research" / "pages"
FIELDS = ["frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed",
          "ai_content_banned"]
VOCAB = {
    "self_promo_allowed": {"yes", "no", "conditional"},
    "link_policy": {"any", "none", "no_shorteners", "allowlist", "restricted"},
    "disclosure_required": {"yes", "no"},
    "automation_allowed": {"yes", "no", "approval"},
    "ai_content_banned": {"yes", "no", "restricted"},
}
FREQ = re.compile(r"^\d+/P(\d+[DWMY]|T\d+H)$")
MAJOR = ("reddit.com", "discord.com", "discord.gg", "t.me", "telegram.org", "linkedin.com", "facebook.com", "x.com",
         "twitter.com", "instagram.com", "tiktok.com", "youtube.com", "pinterest.com", "threads.net", "whatsapp.com")
SHA = re.compile(r"^[0-9a-f]{64}$")
QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "‛": "'", "′": "'", "“": '"', "”": '"',
                           "‟": '"', "″": '"', " ": " ", "–": "-", "—": "-", "−": "-",
                           "…": "..."})
MIN_QUOTED, MIN_RELEVANCE, PER_HOST_CAP = 2, 3, 15


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.translate(QUOTE_MAP)).strip()


def valid_value(field: str, value: str) -> bool:
    return bool(FREQ.match(value)) if field == "frequency_cap" else value in VOCAB[field]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-met", action="store_true")
    ap.add_argument("--target", type=int, default=165)
    args = ap.parse_args()
    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    problems: list[str] = []
    text_cache: dict[str, str] = {}
    counted, per_host = [], Counter()
    by_conn: dict[str, Counter] = defaultdict(Counter)
    per_field: dict[str, Counter] = defaultdict(Counter)
    values: Counter = Counter()
    for n, row in enumerate(rows, start=2):
        reasons: list[str] = []
        texts: dict[int, str] = {}
        sources_ok = True
        used = 0
        for k in (1, 2, 3):
            sfx = "" if k == 1 else f"_{k}"
            if not row[f"rules_source_url{sfx}"]:
                continue
            used += 1
            if row[f"http_status{sfx}"] != "200":
                sources_ok = False
                continue
            sha, tsha = row[f"page_sha256{sfx}"], row[f"text_sha256{sfx}"]
            if not (SHA.match(sha) and SHA.match(tsha)):
                problems.append(f"line {n}: bad sha256 for source {k}")
                sources_ok = False
                continue
            p = PAGES / f"{sha}.txt"
            if not p.exists():
                problems.append(f"line {n}: missing page text {p.name}")
                sources_ok = False
                continue
            if sha not in text_cache:
                text_cache[sha] = p.read_bytes().decode("utf-8")
            txt = text_cache[sha]
            if hashlib.sha256(txt.encode("utf-8")).hexdigest() != tsha:
                problems.append(f"line {n}: text sha256 mismatch for source {k}")
                sources_ok = False
                continue
            texts[k] = norm(txt)
        researched = used > 0 and sources_ok and bool(texts)
        quoted = 0
        for f in FIELDS:
            v, q, src = row[f], row[f"{f}_quote"], row[f"{f}_quote_source"]
            if not researched:
                continue
            if v == "not_stated":
                per_field[f]["not_stated"] += 1
                if q:
                    problems.append(f"line {n}: {f} is not_stated but has a quote")
                continue
            ok = (valid_value(f, v) and q and 0 < len(q.split()) <= 25 and src.isdigit() and int(src) in texts
                  and norm(q) in texts[int(src)])
            if ok:
                quoted += 1
                per_field[f]["quoted"] += 1
                values[f"{f}={v}"] += 1
            else:
                per_field[f]["failed"] += 1
                problems.append(f"line {n}: {f}={v!r} quote does not re-verify")
            if (row[f"{f}_quote_verified"] == "true") != bool(ok):
                problems.append(f"line {n}: {f}_quote_verified={row[f'{f}_quote_verified']} but recount says {bool(ok)}")
        if row["url_http_status"] != "200":
            reasons.append("url not 200")
        if not researched:
            reasons.append("rules sources not all 200/verified")
        if quoted < MIN_QUOTED:
            reasons.append("fewer than 2 quote-backed fields")
        rel = row["relevance_score"]
        if not (rel.isdigit() and MIN_RELEVANCE <= int(rel) <= 5 and row["relevance_reason"].strip()):
            reasons.append("relevance below 3 or missing")
        if not row["connector_key"]:
            reasons.append("no connector_key")
        host = urlsplit(row["url"]).netloc.lower()
        if any(host == m or host.endswith("." + m) for m in MAJOR):
            reasons.append("major platform")
        if not reasons and per_host[host] >= PER_HOST_CAP:
            reasons.append("per-host cap")
        is_counted = not reasons
        if is_counted:
            per_host[host] += 1
            counted.append(row)
        if (row["counted"] == "yes") != is_counted:
            problems.append(f"line {n}: CSV counted={row['counted']} but recount says {is_counted} ({'; '.join(reasons)})")
        if row["rules_status"].split(" ")[0] != "proposed":
            problems.append(f"line {n}: rules_status must stay 'proposed' until a person confirms")
        c = by_conn[row["connector_key"] or "(none)"]
        c["rows"] += 1
        c["researched"] += researched
        c["counted"] += is_counted
    print(f"rows: {len(rows)}   counted (recomputed): {len(counted)}   target: {args.target}")
    print("per connector_key (rows / researched / counted):")
    for k in sorted(by_conn):
        c = by_conn[k]
        print(f"  {k:<10} {c['rows']:>4} {c['researched']:>4} {c['counted']:>4}")
    print("per field over researched rows (quote-backed / not_stated / failed):")
    for f in FIELDS:
        c = per_field[f]
        print(f"  {f:<20} {c['quoted']:>4} {c['not_stated']:>4} {c['failed']:>3}")
    print("quote-backed values:", ", ".join(f"{k}:{v}" for k, v in sorted(values.items())))
    print(f"max counted rows on one host: {max(per_host.values()) if per_host else 0}")
    if problems:
        print(f"PROBLEMS ({len(problems)}):")
        for p in problems[:50]:
            print("  " + p)
        return 1
    met = len(counted) >= args.target
    print(("PASS: " if met else "PASS (consistent): ") + f"OUT4 research {'MET' if met else 'NOT MET'} - "
          f"{len(counted)} counted. Rules are AI-researched and 'proposed' until a person confirms them for posting.")
    return 0 if (met or not args.require_met) else 1


if __name__ == "__main__":
    sys.exit(main())
