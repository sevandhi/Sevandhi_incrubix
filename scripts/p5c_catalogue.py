"""Catalogue build (OUT4): the real destination catalogue through the shipped import and rules-capture pipeline.

Read-only on the web. Nothing is ever posted. Every request either goes through the shipped importer
(``app.catalogue.detect.Fetcher``: robots.txt on every hop, at most 1 request per second per site) or through
the shipped rules capture (``app.catalogue.rules_capture``: robots.txt, 1 request per second per host).

Subcommands (run from the repo root with ``uv run python scripts/p5c_catalogue.py <cmd>``):

  seed-v2      catalogue/seeds/seed_v1.csv -> seed_v2.csv + seed_v2_CHANGELOG.md. No network. seed_v1 is not changed.
  import-csv   seed_v2.csv -> catalogue/reports/real_import_input.csv, the file sent to the importer. The five rule
               columns are left out on purpose: the importer stores a row's rule columns as a CONFIRMED rule set
               (confirmed_by=catalogue_import), and decision D6 says only a person confirms real-catalogue rules.
  run          Against a running stack (--api, --token): the import job, a robots preflight of every rules page,
               one rules.capture job per row with a rules_url, then the proposals. Writes
               catalogue/reports/real_run.json (statuses, timestamps, sha256 values, quotes; never page text).
  refresh      Re-read the proposals by id from a stack (after a person confirmed some) into real_run.json.
  build        real_run.json + seed_v2.csv -> catalogue/real_catalogue_v1.csv + catalogue/reports/real_catalogue_report.md.

Proposals stay ``proposed`` with ``confirmed_by=pending-human``. This script never confirms or rejects one.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SEEDS = ROOT / "catalogue" / "seeds"
REPORTS = ROOT / "catalogue" / "reports"
SEED_V1 = SEEDS / "seed_v1.csv"
SEED_V2 = SEEDS / "seed_v2.csv"
CHANGELOG = SEEDS / "seed_v2_CHANGELOG.md"
IMPORT_CSV = REPORTS / "real_import_input.csv"
RUN_JSON = REPORTS / "real_run.json"
OUT_CSV = ROOT / "catalogue" / "real_catalogue_v1.csv"
OUT_MD = REPORTS / "real_catalogue_report.md"

RULE_COLUMNS = ("frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed")
V2_EXTRA = ("instance_feed_key", "ai_content_banned", "rules_text", "level_ceiling", "v2_changes")
IMPORT_COLUMNS = ("name", "url", "platform_key", "surface", "category", "audience_tags", "language", "geo", "est_audience",
                  "rules_url", "connector_key", "instance_feed_key", "discovered_via", "researched_at")
ENRICHED_PLATFORMS = ("lemmy", "discourse", "peertube")  # the importer reads a fresh member count for these
REQUESTED_BY = "agent:catalogue-finish"
PENDING = "pending-human"
PHASE_GAP_S = 1.5

# The Appendix A destination record, then the research and status columns of OUT4.
APPENDIX_A = ("name", "url", "platform_key", "surface", "category", "audience_tags", "language", "geo", "est_audience",
              "rules_url", "frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required",
              "automation_allowed", "connector_key", "relevance_score", "relevance_reason", "level", "last_certified_at")
OUT4_COLUMNS = ("researched_at", "discovered_via", "rules_source", "rules_status")
STATUS_COLUMNS = (
    "seed_row", "destination_id", "import_status", "import_codes", "http_status", "url_checked_window",
    "est_audience_source", "level_ceiling", "instance_feed_key", "ai_content_banned",
    "rules_url_status", "rules_url_checked_at", "rules_url_final", "rules_url_sha256",
    "capture_status", "rules_captured_at", "proposal_id", "proposal_status", "confirmed_by", "fields_verified",
    "verified_fields", "rules_field_sources", "rules_page_sha256", "source_sha256s",
    "reviewable_on_confirm", "eligible_on_confirm", "countable_now", "not_countable_reason", "rules_text", "notes",
)
CSV_COLUMNS = APPENDIX_A + OUT4_COLUMNS + STATUS_COLUMNS


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...] | list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})


# ====================================================================== seed v2

# Re-expressions that need a quote: (seed url, field) -> (new value, why). The quote must be the row's
# ``rules_quote`` (verified verbatim against the source in the seed research's final pass); build_seed_v2 re-checks that
# the quoted words are there, so a mapping can not silently lose its support.
QUOTED_MAPPINGS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("https://lemmy.world/c/technology", "frequency_cap"): (
        "10/P1D", "up to 10 articles per day", "the sidebar caps (mod-approved) bots at 10 articles per day"),
    ("https://lemmy.world/c/technology", "automation_allowed"): (
        "no", "Mod approved content bots", "only mod-approved bots may post; we hold no approval, so automation is not allowed"),
    ("https://makertube.net/", "automation_allowed"): (
        "no", "No automated accounts (unless approved by the moderators)",
        "automated accounts are not allowed without moderator approval; we hold none"),
}


def _parse_rules(row: dict[str, str]) -> str | None:
    """None when the row's rule columns parse as a RuleSet, else the parser's first message."""
    from pydantic import ValidationError

    from app.rules.models import RuleSet

    rules = {k: row[k] for k in RULE_COLUMNS if row.get(k)}
    if not rules:
        return None
    try:
        RuleSet.model_validate(rules)
    except (ValidationError, ValueError) as exc:
        return str(exc).splitlines()[0][:200]
    return None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "feed"


def level_ceiling(connector_key: str) -> str:
    """The highest level the connector's tier can reach after a passed certification (app.certification.levels).
    Real third-party rows are never certified by us (the harness only posts to own instances), so the row's
    ``level`` stays L0; this column says what the tier could reach."""
    from app.certification.levels import level_for
    from app.contract.registry import get_registry

    manifest = get_registry().manifest(connector_key)
    if manifest is None:
        return ""
    mode = "deleted" if "delete" in manifest.capabilities else None
    return level_for(manifest, delete_mode=mode, metrics_ok=True)[0]


def to_v2(row: dict[str, str], feed_keys: set[str]) -> tuple[dict[str, str], list[str]]:
    """One seed_v1 row -> (seed_v2 row, list of change notes)."""
    out = dict(row)
    for col in V2_EXTRA:
        out.setdefault(col, "")
    changes: list[str] = []
    rules_text: list[str] = []

    # D8: a third-party Slack workspace will not install our app, so a person posts (assisted, L1, no automation).
    if row["connector_key"] == "slack":
        out["connector_key"] = "assisted"
        changes.append("connector_key slack->assisted (D8: our app is not installed in a third-party workspace; a person posts)")
        if row["surface"] == "workspace":
            out["surface"] = "community"
            changes.append("surface workspace->community (assisted alias, maps to the 'post' surface)")
        if row["automation_allowed"] != "no":
            changes.append(f"automation_allowed {row['automation_allowed'] or '(blank)'}->no (D8)")
        out["automation_allowed"] = "no"

    # MRSS: we publish the feed, they consume it. Each consumer gets its own feed key on our service.
    if row["connector_key"] == "mrss":
        key = _slug(row["platform_key"] or row["name"])
        base, n = key, 2
        while key in feed_keys:
            key, n = f"{base}-{n}", n + 1
        feed_keys.add(key)
        out["instance_feed_key"] = key
        changes.append(f"instance_feed_key={key} (our feed /api/v1/feeds/{key}/mrss.xml; the site consumes it once a person "
                       "submits the feed URL)")

    # Rule columns: keep what parses, re-express what the quote supports, keep the rest as text (unconfirmed).
    for col in RULE_COLUMNS:
        value = out.get(col, "")
        if not value:
            continue
        mapped = QUOTED_MAPPINGS.get((row["url"], col))
        if mapped is not None:
            new, words, why = mapped
            if words not in row.get("rules_quote", ""):
                raise SystemExit(f"quote support for {row['url']} {col} not found in its rules_quote")
            if new != value:
                rules_text.append(f"{col}: {value}")
                changes.append(f"{col} '{value}'->'{new}' ({why}; quote: \"{words}\")")
                out[col] = new
            continue
        if _parse_rules({col: value}) is None:
            continue  # yes / no / N/ISO-duration: already a structured value
        if col == "disclosure_required" and value == "prohibited":
            out["ai_content_banned"] = "yes"
            rules_text.append("ai_content: prohibited (AI-generated content is banned)")
            changes.append("disclosure_required 'prohibited'->blank, ai_content_banned=yes (a ban is not a disclosure "
                           "rule; kept as a negative rules case, D8)")
        else:
            rules_text.append(f"{col}: {value}")
            changes.append(f"{col} '{value}'->blank (free text with no structured equivalent; kept in rules_text, "
                           "unconfirmed)")
        out[col] = ""
    if rules_text:
        out["rules_text"] = " | ".join(rules_text)
    problem = _parse_rules(out)
    if problem:
        raise SystemExit(f"seed_v2 row {row['url']} still has unparseable rules: {problem}")
    out["level_ceiling"] = level_ceiling(out["connector_key"])
    out["v2_changes"] = "; ".join(changes)
    return out, changes


def build_seed_v2(src: Path = SEED_V1, dst: Path = SEED_V2, changelog: Path | None = CHANGELOG) -> list[dict[str, str]]:
    rows = read_csv(src)
    columns = list(rows[0].keys()) + [c for c in V2_EXTRA if c not in rows[0]]
    feed_keys: set[str] = set()
    out_rows, log = [], []
    for n, row in enumerate(rows, start=1):
        new, changes = to_v2(row, feed_keys)
        out_rows.append(new)
        if changes:
            log.append((n, row["name"], changes))
    write_csv(dst, out_rows, columns)
    if changelog is not None:
        changelog.write_text(_changelog(rows, out_rows, log, src, dst), encoding="utf-8")
    return out_rows


def _changelog(old: list[dict[str, str]], new: list[dict[str, str]], log: list[tuple[int, str, list[str]]],
               src: Path, dst: Path) -> str:
    by_kind = Counter()
    for _, _, changes in log:
        for change in changes:
            by_kind[change.split(" ", 1)[0].split("=")[0]] += 1
    connectors_old = Counter(r["connector_key"] for r in old)
    connectors_new = Counter(r["connector_key"] for r in new)
    lines = [
        "# seed_v2 changelog",
        "",
        f"Generated by `scripts/p5c_catalogue.py seed-v2` on {now_iso()} from `{src.name}` (unchanged) to `{dst.name}`.",
        "No network was used. No row was added or removed: "
        f"{len(old)} rows in, {len(new)} rows out, same order, same URLs.",
        "",
        "## What changed and why",
        "",
        "1. **Slack rows -> `assisted` (decision D8).** These are third-party workspaces. Our Slack app is not installed "
        "there, so a person posts from an assist pack. `connector_key=assisted`, `surface=community` (maps to the "
        "assisted `post` surface), `automation_allowed=no`. Their level ceiling is L1. `platform_key` stays `slack`.",
        "2. **MRSS rows get our own feed key.** We publish a Media RSS feed and the site consumes it. Each row gets "
        "its own `instance_feed_key` (served at `/api/v1/feeds/<key>/mrss.xml`). The site only starts reading it "
        "after a person submits the feed URL there.",
        "3. **Free-text rule summaries.** The rules engine reads only structured values (`frequency_cap` as `N/<ISO-8601>`, "
        "`self_promo_allowed` yes/no/ratio(n), `link_policy` any/none/no_shorteners/allowlist, and yes/no flags).",
        "   - Values that already parse are unchanged.",
        "   - Three values are re-expressed because the row's verified quote supports them (listed below).",
        "   - `disclosure_required=prohibited` means AI-generated content is banned. That is not a disclosure rule, so "
        "the field is blank and the new column `ai_content_banned=yes` keeps the fact (negative rules cases, D8).",
        "   - Everything else (`restricted`, topical link guidance, launch-frequency prose) has no structured "
        "equivalent. The field is blank and the original text is kept in `rules_text`. These rules stay unconfirmed "
        "until a person confirms a rules proposal.",
        "4. **New columns:** `instance_feed_key`, `ai_content_banned`, `rules_text`, `level_ceiling` (the highest "
        "level the connector tier can reach after a certification; real rows are not certified, so their level is L0), "
        "`v2_changes` (this row's changes).",
        "",
        "**Do not import seed_v2 with its rule columns as they are.** The importer stores a row's rule columns as a "
        "confirmed rule set (`confirmed_by=catalogue_import`). Decision D6 says only a person confirms real-catalogue "
        "rules, so `scripts/p5c_catalogue.py import-csv` leaves the five rule columns out of the import file. The "
        "rules then come from rules capture as proposals (`confirmed_by=pending-human`).",
        "",
        "## Counts",
        "",
        "| connector_key | seed_v1 | seed_v2 |",
        "|---|---|---|",
    ]
    for key in sorted(set(connectors_old) | set(connectors_new)):
        lines.append(f"| {key} | {connectors_old.get(key, 0)} | {connectors_new.get(key, 0)} |")
    lines += ["", f"Rows changed: {len(log)}. Changes by field: "
              + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())) + ".", "", "## Row by row", "",
              "| seed row | name | changes |", "|---|---|---|"]
    for n, name, changes in log:
        lines.append(f"| {n} | {name.replace('|', '/')} | {'<br>'.join(c.replace('|', '/') for c in changes)} |")
    return "\n".join(lines) + "\n"


def build_import_csv(src: Path = SEED_V2, dst: Path = IMPORT_CSV) -> list[dict[str, str]]:
    rows = []
    for row in read_csv(src):
        out = {k: row.get(k, "") for k in IMPORT_COLUMNS}
        if row["connector_key"] in ENRICHED_PLATFORMS or row["platform_key"] in ENRICHED_PLATFORMS:
            out["est_audience"] = ""  # let the importer read a fresh count; the seed value is the fallback in `build`
        rows.append(out)
    write_csv(dst, rows, IMPORT_COLUMNS)
    return rows


# ====================================================================== run (network)


def _ua_names() -> list[str]:
    from app.catalogue import detect
    from app.catalogue import rules_capture as rc

    # the importer's agent, the rules reader's declared agent, and the agent httpx really sends from the capture job
    # (it calls safe_fetch.fetch_url with no client, so the default "python-httpx/<v>" header goes out)
    return [detect.USER_AGENT, rc.USER_AGENT, "python-httpx"]


def _robots_ok(robots: Any, url: str, agents: list[str]) -> tuple[bool, str]:
    if robots.disallow_all:
        return False, robots.note or "robots.txt unreachable"
    if robots.allowed_all or robots.parser is None:
        return True, robots.note or "no robots rules"
    blocked = [ua for ua in agents if not robots.parser.can_fetch(ua, url)]
    return (not blocked), ("allowed for every agent we send" if not blocked else f"disallowed for {', '.join(blocked)}")


def _api_url(dest: dict[str, Any]) -> str | None:
    """The platform-API URL rules capture will read (same rule as rules_capture._platform_api)."""
    instance = dest.get("instance") or {}
    if dest["connector_key"] == "lemmy":
        from app.connectors.lemmy.connector import parse_community

        ref = parse_community(str(instance.get("community_url") or dest["url"]))
        return f"{ref.api}/api/v3/community?name={ref.name}"
    if dest["connector_key"] == "discourse":
        return str(instance.get("base_url") or dest["url"]).rstrip("/") + "/about.json"
    return None


async def preflight(dests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """GET every rules page through the importer's Fetcher (robots.txt on every hop, 1 request/s/site) and check
    robots.txt for the platform-API URL. Returns destination_id -> result (no page text, only sha256)."""
    from app.catalogue.detect import MAX_BODY, Fetcher
    from app.config import get_settings

    agents = _ua_names()
    fetcher = Fetcher(get_settings(), timeout_s=15.0, public_min_interval_s=1.0)
    results: dict[str, dict[str, Any]] = {}

    async def one(dest: dict[str, Any]) -> None:
        out: dict[str, Any] = {"rules_url": dest.get("rules_url")}
        try:
            api = _api_url(dest)
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal
            api, out["api_error"] = None, type(exc).__name__
        if api:
            robots = await fetcher.robots(api)
            ok, why = _robots_ok(robots, api, agents)
            out.update(api_url=api, api_allowed=ok, api_robots=why)
        if dest.get("rules_url"):
            out["checked_at"] = now_iso()
            page = await fetcher.get(dest["rules_url"])
            out.update(status=page.status, error=page.error, message=page.message, final_url=page.url,
                       bytes=len(page.body), truncated=len(page.body) >= MAX_BODY,
                       sha256=hashlib.sha256(page.body).hexdigest() if page.body else None,
                       elapsed_ms=page.elapsed_ms)
            ok_first, why_first = _robots_ok(await fetcher.robots(dest["rules_url"]), dest["rules_url"], agents)
            ok_final, why_final = _robots_ok(await fetcher.robots(page.url), page.url, agents)
            out.update(robots_allowed=ok_first and ok_final,
                       robots=why_first if not ok_first else why_final)
        results[dest["destination_id"]] = out

    try:
        await asyncio.gather(*(one(d) for d in dests))
    finally:
        await fetcher.aclose()
    return results


def _poll(client: Any, job_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job.get("status") in ("succeeded", "failed", "dead", "cancelled"):
            return job
        if time.monotonic() > deadline:
            return {**job, "poll_timeout": True}
        time.sleep(2)


def run(api: str, token: str, *, capture_timeout_s: float = 3600, out: Path = RUN_JSON) -> dict[str, Any]:
    import httpx

    state: dict[str, Any] = {"api": api, "started_at": now_iso(), "requested_by": REQUESTED_BY}
    with httpx.Client(base_url=api, headers={"Authorization": f"Bearer {token}"}, timeout=120) as client:
        # 1. the import (the worker does reachability, enrichment, dedupe, relevance, insert)
        data = IMPORT_CSV.read_bytes()
        state["import_input_sha256"] = hashlib.sha256(data).hexdigest()
        resp = client.post("/api/v1/catalogue/imports", files={"file": (IMPORT_CSV.name, data, "text/csv")})
        resp.raise_for_status()
        accepted = resp.json()
        print(f"import {accepted['import_id']} queued (job {accepted['job_id']})", flush=True)
        job = _poll(client, accepted["job_id"], 3600)
        state["import_job"] = job
        state["import"] = client.get(f"/api/v1/catalogue/imports/{accepted['import_id']}").json()
        report = client.get(f"/api/v1/catalogue/imports/{accepted['import_id']}/report", params={"format": "json"})
        report.raise_for_status()
        state["import_report"] = report.json()
        summary = state["import_report"].get("summary")
        print(f"import {job.get('status')}: {json.dumps(summary)}", flush=True)

        # 2. the stored destinations
        ids = sorted({r["destination_id"] for r in state["import_report"]["rows"] if r.get("destination_id")})
        dests = {i: client.get(f"/api/v1/destinations/{i}").json() for i in ids}
        state["destinations"] = dests

        time.sleep(PHASE_GAP_S)  # never two phases' requests to one host within a second
        # 3. robots preflight of every rules page (and of the platform API URL rules capture would read)
        with_rules = [d for d in dests.values() if d.get("rules_url")]
        state["preflight_started_at"] = now_iso()
        pre = asyncio.run(preflight(with_rules))
        state["preflight_finished_at"] = now_iso()
        state["preflight"] = pre
        print(f"preflight: {sum(1 for p in pre.values() if p.get('status') == 200 and p.get('robots_allowed'))}"
              f"/{len(pre)} rules pages 200 and allowed", flush=True)

        time.sleep(PHASE_GAP_S)
        # 4. one rules.capture job per row whose rules page answered 200 and robots allows it for every agent we send
        captures: dict[str, Any] = {}
        state["capture_started_at"] = now_iso()
        for dest in with_rules:
            p = pre[dest["destination_id"]]
            if not (p.get("status") == 200 and p.get("error") is None and p.get("robots_allowed")):
                captures[dest["destination_id"]] = {"skipped": True,
                                                    "why": p.get("error") or f"HTTP {p.get('status')}"
                                                    if p.get("robots_allowed") else f"robots: {p.get('robots')}"}
                continue
            body = {"rules_url": p["final_url"],  # the final hop: the capture's own fetch then needs no redirect
                    "use_platform_api": bool(p.get("api_allowed")) if "api_url" in p else True,
                    "requested_by": REQUESTED_BY}
            r = client.post(f"/api/v1/destinations/{dest['destination_id']}/rules/capture", json=body)
            captures[dest["destination_id"]] = {"request": body, "http_status": r.status_code, "accepted": r.json()}
        for cap in captures.values():
            if cap.get("skipped") or cap.get("http_status") != 202:
                continue
            cap["job"] = _poll(client, cap["accepted"]["job_id"], capture_timeout_s)
        state["capture_finished_at"] = now_iso()
        state["captures"] = captures

        # 5. the proposals (none is confirmed or rejected here)
        proposals = {}
        for dest_id, cap in captures.items():
            if cap.get("skipped"):
                continue
            listing = client.get(f"/api/v1/destinations/{dest_id}/rule-proposals").json()
            proposals[dest_id] = listing.get("proposals", [])
        state["proposals"] = proposals
        state["finished_at"] = now_iso()
    state = strip_links(state)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, indent=1, sort_keys=True, default=str), encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}", flush=True)
    return state


def strip_links(value: Any) -> Any:
    """Job results carry signed download links to their artifacts (one is the analysed rules-page text). Keep only
    the artifact names: no signed URL and no route to a copied page goes into the repo."""
    if isinstance(value, dict):
        return {k: (sorted(v) if k == "links" and isinstance(v, dict) else strip_links(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_links(v) for v in value]
    return value


def refresh(api: str, token: str, run_path: Path = RUN_JSON) -> dict[str, Any]:
    """Re-read every proposal by id (e.g. after a person confirmed some in /ui/rules/<id>) and update real_run.json.
    Run ``build`` afterwards. Read-only on our own API; nothing is confirmed here."""
    import httpx

    state = json.loads(run_path.read_text(encoding="utf-8"))
    changed = Counter()
    with httpx.Client(base_url=api, headers={"Authorization": f"Bearer {token}"}, timeout=60) as client:
        for dest_id, props in state["proposals"].items():
            fresh = []
            for p in props:
                resp = client.get(f"/api/v1/rule-proposals/{p['proposal_id']}")
                if resp.status_code != 200:
                    raise SystemExit(f"proposal {p['proposal_id']} not found on {api} (HTTP {resp.status_code}): "
                                     "refresh against the stack that holds these proposals")
                new = resp.json()
                changed[f"{p['status']}->{new['status']}"] += 1
                fresh.append(new)
            state["proposals"][dest_id] = fresh
    state["refreshed_at"] = now_iso()
    run_path.write_text(json.dumps(state, indent=1, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(dict(changed)))
    return state


# ====================================================================== build (no network)

SOURCE_KIND = {"lemmy_sidebar": "api_sidebar", "discourse_about": "api_sidebar", "rules_url": "page", "pasted": "pasted"}


def _yes_no(value: Any) -> str:
    return "yes" if value is True else "no" if value is False else ""


def _field_value(name: str, value: Any) -> tuple[str, str] | None:
    """A verified extraction field -> (CSV column, CSV value). Language is not a CSV rule column."""
    if name == "frequency_cap":
        return "frequency_cap", str(value)
    if name == "self_promo":
        return "self_promo_allowed", {"allowed": "yes", "disallowed": "no"}.get(str(value), str(value))
    if name == "link_policy":
        return "link_policy", str(value)
    if name == "disclosure_required":
        return "disclosure_required", _yes_no(value)
    if name == "automation_allowed":
        return "automation_allowed", _yes_no(value)
    return None


def reviewable(row: dict[str, Any]) -> bool:
    """URL 200, rules page 200 at check time and read by the capture, proposal waiting: a person can review the
    captured text and confirm. A defaulted proposal counts only if the person states at least one rule from the page
    while confirming (otherwise the confirmed rules are the conservative default, which plan §7 does not count)."""
    return (row["import_status"] in ("ok", "warning") and str(row["http_status"]) == "200" and bool(row["rules_url"])
            and str(row["rules_url_status"]) == "200" and row["capture_status"] == "succeeded"
            and row["proposal_status"] in ("proposed", "confirmed"))


def classify(row: dict[str, Any]) -> tuple[bool, bool, str]:
    """(eligible_on_confirm, countable_now, reason) under the OUT4 rule: the URL answered 200, the rules_url answered
    200 at check time, the rules came from the page or the platform API (at least one quote-verified field), and a
    proposal is ready. It counts only after a person confirms it (D6)."""
    reasons = []
    if row["import_status"] not in ("ok", "warning") or str(row["http_status"]) != "200":
        reasons.append(f"url not 200 at check ({row['import_status']} {row['http_status'] or row['import_codes']})")
    if not row["rules_url"]:
        reasons.append("no rules_url")
    elif str(row["rules_url_status"]) != "200":
        reasons.append(f"rules_url not 200 at check ({row['rules_url_status'] or 'not checked'})")
    elif row["capture_status"] != "succeeded":
        reasons.append(f"rules capture: {row['capture_status'] or 'not run'}")
    if row["rules_source"] not in ("page", "api_sidebar", "api_sidebar+page"):
        reasons.append("rules defaulted (no quote-verified field)" if row["rules_source"] == "default" else "no rules proposal")
    if row["proposal_status"] not in ("proposed", "confirmed"):
        reasons.append("no proposal ready")
    eligible = not reasons
    who = str(row["confirmed_by"] or "").lower()
    countable = eligible and row["proposal_status"] == "confirmed" and who not in ("", PENDING, "catalogue_import") \
        and not who.startswith("agent")
    if eligible and not countable:
        reasons.append("waiting for human confirmation (D6)")
    return eligible, countable, "; ".join(reasons)


def build(seed_path: Path = SEED_V2, run_path: Path = RUN_JSON) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seed = read_csv(seed_path)
    state = json.loads(run_path.read_text(encoding="utf-8"))
    report_rows = {int(r["row_no"]): r for r in state["import_report"]["rows"]}
    job = state["import_job"]
    window = f"{job.get('created_at')}/{job.get('finished_at')}"
    seen_dest: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for n, s in enumerate(seed, start=1):
        rep = report_rows.get(n, {})
        dest_id = rep.get("destination_id") or ""
        dest = state["destinations"].get(dest_id) if rep.get("status") in ("ok", "warning") else None
        pre = state["preflight"].get(dest_id, {}) if dest else {}
        cap = state["captures"].get(dest_id, {}) if dest else {}
        props = state["proposals"].get(dest_id, []) if dest else []
        prop = props[0] if props else None
        row: dict[str, Any] = {k: s.get(k, "") for k in APPENDIX_A}
        row.update(seed_row=n, destination_id=dest_id if dest else "", import_status=rep.get("status", "missing"),
                   import_codes=rep.get("code", ""), http_status=rep.get("http_status", ""), url_checked_window=window,
                   researched_at=s["researched_at"], discovered_via=s["discovered_via"],
                   level_ceiling=s.get("level_ceiling", ""), instance_feed_key=s.get("instance_feed_key", ""),
                   ai_content_banned=s.get("ai_content_banned", ""), rules_text=s.get("rules_text", ""), notes=s.get("notes", ""))
        if dest:
            for k in ("platform_key", "surface", "connector_key", "language", "relevance_score", "relevance_reason", "level",
                      "last_certified_at", "geo", "category"):
                row[k] = "" if dest.get(k) is None else dest[k]
            row["audience_tags"] = ";".join(dest.get("audience_tags") or [])
            row["est_audience"] = "" if dest.get("est_audience") is None else dest["est_audience"]
            row["est_audience_source"] = "import (platform API at check time)" if row["est_audience"] != "" and \
                (s["connector_key"] in ENRICHED_PLATFORMS) else ("seed_v1 research" if row["est_audience"] != "" else "")
            if row["est_audience"] == "" and s.get("est_audience"):
                row["est_audience"], row["est_audience_source"] = s["est_audience"], "seed_v1 research (no fresh count)"
        else:
            row.update(level="", relevance_score=rep.get("relevance_score", ""), relevance_reason=rep.get("relevance_reason", ""))
            row["est_audience_source"] = "seed_v1 research" if s.get("est_audience") else ""
        # rules: verified proposal fields first, then the seed_v2 structured value (agent-read, unconfirmed)
        sources: dict[str, str] = {}
        kinds: set[str] = set()
        verified: list[str] = []
        if prop:
            for f in prop.get("fields", []):
                if f.get("status") != "verified":
                    continue
                verified.append(f["field"])
                kinds.add(SOURCE_KIND.get(f.get("source_kind") or "", f.get("source_kind") or "?"))
                mapped = _field_value(f["field"], f.get("proposed"))
                if mapped:
                    row[mapped[0]] = mapped[1]
                    sources[mapped[0]] = f"proposal:{SOURCE_KIND.get(f.get('source_kind') or '', '?')}"
        for col in RULE_COLUMNS:
            if col not in sources and s.get(col):
                row[col] = s[col]
                sources[col] = "seed_v2 (agent-read 2026-10-14/15)"
            elif col not in sources:
                row[col] = ""
        row["rules_field_sources"] = ";".join(f"{k}={v}" for k, v in sorted(sources.items()))
        row["verified_fields"] = ";".join(verified)
        row["fields_verified"] = len(verified)
        if prop:
            row["rules_source"] = "+".join(sorted(k for k in kinds if k in ("api_sidebar", "page"))) or "default"
        else:
            row["rules_source"] = ""
        row["proposal_id"] = prop["proposal_id"] if prop else ""
        row["proposal_status"] = prop["status"] if prop else ""
        decision = (prop or {}).get("decision") or {}
        row["confirmed_by"] = decision.get("confirmed_by") or (PENDING if prop and prop["status"] == "proposed" else "")
        row["rules_status"] = {"proposed": PENDING, "confirmed": "confirmed", "rejected": "rejected"}.get(
            prop["status"], prop["status"]) if prop else ("capture_failed" if cap.get("job") else
                                                          ("not_captured" if s.get("rules_url") else "no_rules_url"))
        row["rules_page_sha256"] = (prop or {}).get("rules_page_sha256") or ""
        row["source_sha256s"] = ";".join(f"{SOURCE_KIND.get(x.get('kind'), x.get('kind'))}:{x.get('sha256')}"
                                         for x in (prop or {}).get("sources", []))
        row["rules_captured_at"] = (prop or {}).get("captured_at") or ""
        row["rules_url_status"] = pre.get("status") if pre.get("status") is not None else (pre.get("error") or "")
        row["rules_url_checked_at"] = pre.get("checked_at", "")
        row["rules_url_final"] = pre.get("final_url", "") if pre.get("final_url") != s.get("rules_url") else ""
        row["rules_url_sha256"] = pre.get("sha256") or ""
        if cap.get("skipped"):
            row["capture_status"] = f"skipped: {cap['why']}"
        elif cap.get("job"):
            row["capture_status"] = cap["job"].get("status", "")
        # rules_url page actually read by the capture (200) and on the URL we sent (no redirect at capture time)
        page_src = [x for x in (prop or {}).get("sources", []) if x.get("kind") == "rules_url"]
        if prop and not page_src:
            row["capture_status"] = f"{row['capture_status']} (rules page not read)"
        elif page_src and cap.get("request") and page_src[0].get("url") != cap["request"]["rules_url"]:
            row["capture_status"] = f"{row['capture_status']} (redirected at capture)"
        # duplicates never count twice
        if dest_id and dest and dest_id in seen_dest:
            row["import_status"] = f"duplicate_of_seed_row_{seen_dest[dest_id]}"
        elif dest_id and dest:
            seen_dest[dest_id] = n
        eligible, countable, reason = classify(row)
        row.update(reviewable_on_confirm="yes" if reviewable(row) else "no", eligible_on_confirm="yes" if eligible else "no", countable_now="yes" if countable else "no",
                   not_countable_reason="" if countable else reason)
        rows.append(row)
    write_csv(OUT_CSV, rows, CSV_COLUMNS)
    counts = summarize(rows, state)
    OUT_MD.write_text(report_md(rows, state, counts), encoding="utf-8")
    return rows, counts


def summarize(rows: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    imported = [r for r in rows if r["import_status"] in ("ok", "warning")]
    url200 = [r for r in imported if str(r["http_status"]) == "200"]
    with_rules = [r for r in rows if r["rules_url"]]
    rules200 = [r for r in with_rules if str(r["rules_url_status"]) == "200"]
    proposals = [r for r in rows if r["proposal_id"]]
    eligible = [r for r in rows if r["eligible_on_confirm"] == "yes"]
    defaulted = [r for r in rows if r["rules_source"] == "default"]
    return {
        "seed_rows": len(rows),
        "imported": len(imported),
        "import_statuses": dict(Counter(r["import_status"].split("_of_")[0] for r in rows)),
        "url_200": len(url200),
        "url_reachability_pct": round(100 * len(url200) / len(rows), 1) if rows else 0.0,
        "rows_with_rules_url": len(with_rules),
        "rules_url_200": len(rules200),
        "rules_url_reachability_pct": round(100 * len(rules200) / len(with_rules), 1) if with_rules else 0.0,
        "proposals": len(proposals),
        "proposals_pending_human": sum(1 for r in proposals if r["proposal_status"] == "proposed"),
        "proposals_confirmed": sum(1 for r in proposals if r["proposal_status"] == "confirmed"),
        "rules_source": dict(Counter(r["rules_source"] or "none" for r in rows)),
        "defaulted": len(defaulted),
        "eligible_on_confirm": len(eligible),
        "reviewable_on_confirm": sum(1 for r in rows if r["reviewable_on_confirm"] == "yes"),
        "countable_now": sum(1 for r in rows if r["countable_now"] == "yes"),
        "target": 150,
        "by_connector": _by(rows, "connector_key"),
        "by_platform": _by(rows, "platform_key"),
    }


def _by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"rows": 0, "imported": 0, "url_200": 0, "rules_url_200": 0,
                                                          "proposals": 0, "eligible_on_confirm": 0, "countable_now": 0})
    for r in rows:
        b = out[r[key] or "(none)"]
        b["rows"] += 1
        b["imported"] += r["import_status"] in ("ok", "warning")
        b["url_200"] += r["import_status"] in ("ok", "warning") and str(r["http_status"]) == "200"
        b["rules_url_200"] += str(r["rules_url_status"]) == "200"
        b["proposals"] += bool(r["proposal_id"])
        b["eligible_on_confirm"] += r["eligible_on_confirm"] == "yes"
        b["countable_now"] += r["countable_now"] == "yes"
    return dict(sorted(out.items()))


def _table(by: dict[str, dict[str, int]], label: str) -> list[str]:
    cols = ["rows", "imported", "url_200", "rules_url_200", "proposals", "eligible_on_confirm", "countable_now"]
    lines = [f"| {label} | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for key, b in by.items():
        lines.append(f"| {key} | " + " | ".join(str(b[c]) for c in cols) + " |")
    total = {c: sum(b[c] for b in by.values()) for c in cols}
    lines.append("| **total** | " + " | ".join(f"**{total[c]}**" for c in cols) + " |")
    return lines


def report_md(rows: list[dict[str, Any]], state: dict[str, Any], c: dict[str, Any]) -> str:
    job = state["import_job"]
    imp = state["import"]
    llm = Counter()
    for props in state["proposals"].values():
        for p in props[:1]:
            llm[f"{(p.get('llm') or {}).get('provider')}:{(p.get('llm') or {}).get('model')}"] += 1
    cap_jobs = [c2.get("job") or {} for c2 in state["captures"].values() if not c2.get("skipped")]
    cap_status = Counter(j.get("status", "?") for j in cap_jobs)
    skipped = Counter(c2["why"].split(":")[0] for c2 in state["captures"].values() if c2.get("skipped"))
    late = [r for r in rows if str(r["rules_url_status"]) == "200" and r["capture_status"] != "succeeded"]
    late_text = "; ".join(f"{r['name']}: {r['capture_status']}{_capture_error(state, r)}" for r in late) or "-"
    with_both = sum(1 for r in rows if r["rules_url"] and r["destination_id"])
    gap = 150 - c["eligible_on_confirm"]
    short = f", short by {gap}" if gap > 0 else ""
    lines = [
        "# Real catalogue v1: report (OUT4)",
        "",
        "Generated by `scripts/p5c_catalogue.py build` from `catalogue/reports/real_run.json` and "
        "`catalogue/seeds/seed_v2.csv`. Re-check with `uv run python scripts/prove_out4.py`.",
        "",
        "Catalogue only: nothing was posted anywhere. Page text is not stored here; only HTTP statuses, timestamps "
        "and sha256 values of the bytes received (plus the short verbatim quotes inside each proposal in real_run.json).",
        "",
        "## Result",
        "",
        f"- **Counted toward the 150 now: {c['countable_now']}.** No proposal has been confirmed by a person yet (D6). "
        "The agent proposes; it never confirms.",
        f"- **Eligible on confirmation: {c['eligible_on_confirm']}.** These rows have every other part of the strict rule: "
        "the URL answered 200 in the import, the rules_url answered 200 at check time (preflight and capture), the rules "
        "came from the page or the platform API (at least one quote-verified field), and a proposal is waiting "
        f"(`confirmed_by={PENDING}`). Each one becomes countable when a person confirms its proposal; a rejected proposal "
        "drops the row.",
        f"- **Target 150: {'reachable' if c['eligible_on_confirm'] >= 150 else 'NOT reachable'} from this run alone** "
        f"({c['eligible_on_confirm']} eligible{short}).",
        f"- Defaulted (proposal ready, but the extractor quote-verified no field, so every field is the conservative "
        f"default): {c['defaulted']}. Listed separately below and not counted (plan §7 OUT4 step 5).",
        f"- **Upper bound after human review: {c['reviewable_on_confirm']}** rows have the URL 200, the rules page 200 at "
        "check time, its text captured (sha256 below) and a proposal waiting, defaulted ones included. A defaulted row "
        "becomes countable only if the person, reading the captured text on `/ui/rules/<proposal_id>`, states at least "
        "one rule from it while confirming. Confirming the bare default does not count. "
        f"{'Even this upper bound is below 150.' if c['reviewable_on_confirm'] < 150 else ''}",
        "",
        "```json",
        json.dumps({k: c[k] for k in ("seed_rows", "imported", "url_200", "rows_with_rules_url", "rules_url_200", "proposals",
                                      "proposals_pending_human", "proposals_confirmed", "defaulted", "eligible_on_confirm",
                                      "reviewable_on_confirm", "countable_now", "target")}, indent=1),
        "```",
        "",
        "## Reachability",
        "",
        f"- Row URLs that answered 200 in the import: {c['url_200']} of {c['seed_rows']} ({c['url_reachability_pct']} %).",
        f"- Import statuses: {', '.join(f'{k} {v}' for k, v in sorted(c['import_statuses'].items()))}.",
        f"- rules_url answered 200 in the preflight: {c['rules_url_200']} of {c['rows_with_rules_url']} rows with a rules_url "
        f"({c['rules_url_reachability_pct']} %). Only imported rows were checked: "
        f"{with_both} rows had both.",
        f"- Rules page 200 in the preflight but not captured: {len(late)} "
        f"({late_text}). "
        "Where the capture job's error says HTTP 403, the same page had answered 200 to the importer's user agent a "
        "minute earlier; the capture job sends the default `python-httpx` user agent.",
        f"- Rules capture jobs: {', '.join(f'{k} {v}' for k, v in sorted(cap_status.items())) or 'none'}; skipped "
        f"(preflight not 200 or robots): {', '.join(f'{k} {v}' for k, v in sorted(skipped.items())) or 'none'}.",
        "",
        "## Fetch timestamps (UTC)",
        "",
        f"- Import job `{job.get('job_id')}` (import `{imp.get('import_id')}`): queued {job.get('created_at')}, "
        f"finished {job.get('finished_at')}, status {job.get('status')}; {imp.get('network_requests')} requests; "
        f"timings {json.dumps(imp.get('timings'))}.",
        f"- Rules-page preflight: {state.get('preflight_started_at')} to {state.get('preflight_finished_at')} "
        "(per-row time in `rules_url_checked_at`).",
        f"- Rules capture: {state.get('capture_started_at')} to {state.get('capture_finished_at')} (per-row time in "
        "`rules_captured_at`).",
        f"- Import input sha256: `{state.get('import_input_sha256')}`.",
        f"- Proposal extractor: {', '.join(f'{k} ({v})' for k, v in llm.items()) or 'none'} (LLM_PROVIDER=mock: the "
        "deterministic extractor; quotes verified verbatim against the fetched text).",
        "",
        "## Counts per connector",
        "",
        *_table(c["by_connector"], "connector_key"),
        "",
        "## Counts per platform",
        "",
        *_table(c["by_platform"], "platform_key"),
        "",
        "## Rules source",
        "",
        "| rules_source | rows |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(c["rules_source"].items())],
        "",
        "## Method and politeness",
        "",
        "1. `seed_v2.csv` (see `seed_v2_CHANGELOG.md`) -> `real_import_input.csv`: the five rule columns are left out, "
        "because the importer would store them as confirmed rules (`confirmed_by=catalogue_import`), against D6.",
        "2. `POST /api/v1/catalogue/imports` on our own stack (real mode, `SIMULATION_MODE=false`). The worker's importer "
        "checks robots.txt on every hop, waits at least 1 s between two requests to the same site, follows redirects by "
        "hand through the SSRF guard, reads member counts from the platform API, detects the page language and scores "
        "relevance with the lexicon (no LLM).",
        "3. Preflight (this script, the importer's `Fetcher`): every rules page GET with robots.txt on every hop, 1 "
        "request/s/site, sha256 of the bytes received (capped at 256 KiB). robots.txt is also evaluated for the user "
        "agents the capture job sends (`IncuBrixRulesReader/1.0` and the default `python-httpx`), and for the platform "
        "API URL it reads. A row is captured only when everything is allowed and the page answered 200.",
        "4. `POST /api/v1/destinations/{id}/rules/capture` with the preflight's final URL (so the capture needs no redirect), "
        "`LLM_PROVIDER=mock`. The worker checks robots.txt again, waits 1 s per host, verifies every quote verbatim and "
        "stores a `proposed` rule set. No proposal was confirmed or rejected. Phases ran one after another, so no two "
        "phases hit one host at the same time.",
        "",
        "## Rows not eligible (and why)",
        "",
        "| seed row | name | connector | reason |",
        "|---|---|---|---|",
    ]
    for r in rows:
        if r["eligible_on_confirm"] != "yes" and r["rules_source"] != "default":
            lines.append(f"| {r['seed_row']} | {_cell(r['name'])} | {r['connector_key']} | {_cell(r['not_countable_reason'])} |")
    lines += ["", "## Defaulted rows (proposal ready, no quote-verified field; listed separately, not counted)", "",
              "| seed row | name | connector | rules_url |", "|---|---|---|---|"]
    for r in rows:
        if r["rules_source"] == "default":
            lines.append(f"| {r['seed_row']} | {_cell(r['name'])} | {r['connector_key']} | {r['rules_url']} |")
    lines += ["", "## Page hashes (sha256 only, no copied pages)", "",
              "`preflight` = bytes of the rules page received in the preflight (first 256 KiB). `analysed` = sha256 of the "
              "text the proposal was made from (`rules_page_sha256`). Per-source hashes of the bytes the capture received "
              "are in the CSV column `source_sha256s`.", "",
              "| seed row | rules_url status | checked at | preflight sha256 | captured at | analysed sha256 |",
              "|---|---|---|---|---|---|"]
    for r in rows:
        if r["rules_url"] and r["destination_id"]:
            lines.append(f"| {r['seed_row']} | {r['rules_url_status']} | {r['rules_url_checked_at']} | "
                         f"`{r['rules_url_sha256'] or '-'}` | {r['rules_captured_at'] or '-'} | `{r['rules_page_sha256'] or '-'}` |")
    return "\n".join(lines) + "\n"


def _capture_error(state: dict[str, Any], row: dict[str, Any]) -> str:
    """The capture job's warning (e.g. the rules page answered 403 to the job), from the stored proposal or job."""
    cap = state["captures"].get(row["destination_id"], {})
    job = cap.get("job") or {}
    warnings = (((job.get("error") or {}).get("error") or {}).get("details") or {}).get("warnings") or []
    warnings = warnings or (state.get("capture_warnings") or {}).get(row["destination_id"]) or []
    if not warnings and job.get("status") == "succeeded":
        warnings = ["rules page not in the proposal's sources"]
    return f" ({'; '.join(str(w) for w in warnings)})" if warnings else " (no text could be read)"


def _cell(text: Any) -> str:
    return str(text or "").replace("|", "/").replace("\n", " ")[:200]


# ====================================================================== CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed-v2")
    sub.add_parser("import-csv")
    p_run = sub.add_parser("run")
    p_run.add_argument("--api", required=True)
    p_run.add_argument("--token", required=True)
    p_run.add_argument("--capture-timeout", type=float, default=3600)
    p_refresh = sub.add_parser("refresh")
    p_refresh.add_argument("--api", required=True)
    p_refresh.add_argument("--token", required=True)
    sub.add_parser("strip-links")
    sub.add_parser("build")
    args = parser.parse_args(argv)
    if args.cmd == "seed-v2":
        rows = build_seed_v2()
        print(f"wrote {SEED_V2.relative_to(ROOT)} ({len(rows)} rows) and {CHANGELOG.relative_to(ROOT)}")
    elif args.cmd == "import-csv":
        rows = build_import_csv()
        buf = io.StringIO()
        print(f"wrote {IMPORT_CSV.relative_to(ROOT)} ({len(rows)} rows, columns: {', '.join(IMPORT_COLUMNS)})", file=buf)
        print(buf.getvalue().strip())
    elif args.cmd == "run":
        run(args.api, args.token, capture_timeout_s=args.capture_timeout)
    elif args.cmd == "refresh":
        refresh(args.api, args.token)
    elif args.cmd == "strip-links":
        RUN_JSON.write_text(json.dumps(strip_links(json.loads(RUN_JSON.read_text(encoding="utf-8"))), indent=1,
                                       sort_keys=True, default=str), encoding="utf-8")
    elif args.cmd == "build":
        _, counts = build()
        print(json.dumps({k: v for k, v in counts.items() if not k.startswith("by_")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
