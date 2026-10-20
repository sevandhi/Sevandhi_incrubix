"""OUT3 evidence: recertification with a clock jump, demotion + exclusion + alert, restore on pass, rules drift.

Runs against a compose stack in LOCAL mode with the alert sinks pointed at our own receiver on mock-platforms:
    ALERT_WEBHOOK_URL=http://mock-platforms:8080/alertsink/hook  ALERT_WEBHOOK_SECRET=<random, also exported here>
    GITHUB_TOKEN=<random dev value> GITHUB_REPO=ibx-demo/alerts GITHUB_API_URL=http://mock-platforms:8080/alertsink/github
    RECERT_JITTER_S=5

    uv run python scripts/p5a_out3_recert.py --api http://127.0.0.1:<port> --mock http://127.0.0.1:<mock port> [-f ...]

Destinations (all ours): A = real local Lemmy behind toxiproxy (outage = the proxy is disabled), B = the Discourse mock
(a field rename, the brief's hard case), C = real local Lemmy direct (rules drift = the admin edits the sidebar).
The clock jump is ``python -m app.recert tick --now <date>`` inside the api container; the worker runs the jobs.
Writes evidence/levels/out/OUT3_recert.log.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.jobs.callbacks import verify  # noqa: E402
from scripts.p5a_common import PERMISSIVE, ROOT, Stack, now_iso  # noqa: E402

ACTIONS = ROOT / "scripts" / "p5a_instance_actions.py"
LEVEL_SQL = """
import asyncio, json
from sqlalchemy import select
from app.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.db.tables import LevelHistoryRow
async def main():
    engine = make_engine(get_settings().DATABASE_URL, 2); f = make_session_factory(engine)
    async with f() as s:
        rows = (await s.execute(select(LevelHistoryRow).where(LevelHistoryRow.destination_id.in_({ids})).order_by(LevelHistoryRow.id))).scalars()
        print(json.dumps([{"destination_id": r.destination_id, "level_before": r.level_before, "level_after": r.level_after,
                           "run_id": r.run_id, "reason": r.reason[:160], "at": r.created_at.isoformat()} for r in rows]))
    await engine.dispose()
asyncio.run(main())
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--mock", required=True)
    parser.add_argument("--token", default=os.environ.get("IBX_API_TOKEN", "ibx-dev-token"))
    parser.add_argument("-f", dest="files", action="append", default=[])
    parser.add_argument("--out", default=str(ROOT / "evidence" / "levels" / "out" / "OUT3_recert.log"))
    args = parser.parse_args()
    secret = os.environ["ALERT_WEBHOOK_SECRET"].encode()
    st = Stack(args.api, args.token, args.files or ["compose.yaml"], args.mock)
    lines: list[str] = []

    def say(text: str = "", data: Any = None) -> None:
        lines.append(text if data is None else f"{text}\n{json.dumps(data, indent=2, sort_keys=True, default=str)}")
        sys.stdout.write(lines[-1] + "\n")
        sys.stdout.flush()

    def recert(as_of: datetime) -> dict[str, Any]:
        out = st.exec_api_code(f"import sys; sys.argv=['app.recert','tick','--now','{as_of.isoformat()}','--wait','400']; "
                               "from app.recert.__main__ import main; main(sys.argv[1:])")
        return json.loads(out[out.index("{\n"):])

    def toxiproxy(enabled: bool) -> str:
        return st.exec_api_code("import httpx; r=httpx.post('http://toxiproxy:8474/proxies/lemmy_proxy', "
                                f"json={{'enabled': {enabled!s}}}, timeout=10); print(r.status_code, r.json().get('enabled'))").strip()

    def summary(did: str) -> dict[str, Any]:
        d = st.ok(st.http.get("/api/v1/network/health", params={"destination_id": did}))["destinations"][0]
        return {k: d[k] for k in ("name", "trust_level", "status", "excluded_reason", "last_certified_at", "recert_due_at",
                                  "failure_streak", "open_alerts")} | {"rules": d["rules"]["status"] + f" v{d['rules']['version']}",
                                                                      "last_run": d["last_run"]}

    def deliveries() -> list[dict[str, Any]]:
        out = []
        for d in st.ok(st.http.get(f"{args.mock}/alertsink/_deliveries", headers={"Authorization": ""}))["deliveries"]:
            ok = verify(secret, d["body"].encode(), d["headers"].get("x-incubrix-signature"))
            out.append({"signature_valid": ok, "event_header": d["headers"].get("x-incubrix-event"),
                        "delivery_header": d["headers"].get("x-incubrix-delivery"), "payload": json.loads(d["body"])})
        return out

    say(f"# OUT3 recertification evidence, generated {now_iso()} by scripts/p5a_out3_recert.py")
    say(f"api {args.api}; SIMULATION_MODE=false (local mode); alert sinks: log + HMAC webhook + GitHub-issues emulation "
        "(both receivers are our own mock-platforms /alertsink, NOT a third party)")
    schedule = st.ok(st.http.get("/api/v1/recert/schedule"))
    say("## schedule (GET /api/v1/recert/schedule)", {k: schedule[k] for k in ("recert_after_days", "guarantee", "tick_every_s",
                                                                                "concurrency", "jitter_s", "alert_sinks")})
    httpx_reset = st.http.post(f"{args.mock}/alertsink/_reset", headers={"Authorization": ""})
    say(f"alertsink reset: HTTP {httpx_reset.status_code}")

    a = st.add(name="OUT3 A: local Lemmy via toxiproxy", connector_key="lemmy", url="http://toxiproxy:18536/c/ibx_test_2", **PERMISSIVE)
    b = st.add(name="OUT3 B: Discourse mock (field rename)", connector_key="discourse",
               url="http://mock-platforms:8080/discourse/out3-forum", **PERMISSIVE)
    c = st.add(name="OUT3 C: local Lemmy direct (rules drift)", connector_key="lemmy", url="http://lemmy:8536/c/ibx_test_2", **PERMISSIVE)
    names = {a: "A", b: "B", c: "C"}
    say("## 1. three destinations added (one row each) and certified")
    for did in (a, b, c):
        run = st.certify(did)
        say(f"{names[did]} {did}: certification {run['run_id']} -> {run['state']} level {run.get('level_after')}")
    real_now = datetime.now(UTC).replace(microsecond=0)

    say("## 2. day +20: nothing is due yet (GET /api/v1/recert/due?now=...)")
    due = st.ok(st.http.get("/api/v1/recert/due", params={"now": (real_now + timedelta(days=20)).isoformat()}))
    say(f"as_of {due['as_of']} cutoff {due['cutoff']}: would_enqueue={[e['destination_id'] for e in due['would_enqueue']]}")

    say("## 3. CLOCK JUMP +22 days: python -m app.recert tick --now <+22d> --wait 400 (inside the api container)")
    t1 = recert(real_now + timedelta(days=22))
    say(f"as_of {t1['as_of']} (clock: {t1['clock']['as_of_source']}), cutoff {t1['cutoff']}, due {t1['due']}")
    for r in t1.get("results", []):
        say(f"  {names.get(r['destination_id'], r['destination_id'])} run {r['run_id']} trigger={r['trigger']} -> {r['state']} level {r['level_after']}")
    for did in (a, b, c):
        say(f"  {names[did]}: rules drift check of that run", st.ok(st.http.get("/api/v1/jobs/" + st.run(
            next(r["run_id"] for r in t1["results"] if r["destination_id"] == did))["job_id"]))["result"].get("rules_drift"))

    say("## 4. failures injected on our own systems")
    say(f"A: toxiproxy lemmy_proxy disabled -> {toxiproxy(False)} (an outage of the Lemmy instance as seen through the proxy)")
    fault = st.http.post(f"{args.mock}/_faults", headers={"Authorization": ""}, json={
        "platform": "discourse", "tenant": "out3-forum", "method": "POST", "fault": "rename_field:topic_id->topicId", "count": 1})
    say(f"B: mock Discourse renames topic_id -> topicId on the next create (HTTP {fault.status_code})")
    side = st.exec_api(ACTIONS, "lemmy_sidebar", json.dumps({"community": "ibx_test_2", "text":
                       "Rules (updated by the admin): one post per week, no link shorteners, humans only."}))
    say(f"C: the Lemmy admin edited the ibx_test_2 sidebar (rules) -> HTTP {side.get('http_status')}")

    say("## 5. CLOCK JUMP +44 days: tick")
    t2 = recert(real_now + timedelta(days=44))
    for r in t2.get("results", []):
        say(f"  {names.get(r['destination_id'], r['destination_id'])} run {r['run_id']} trigger={r['trigger']} -> {r['state']} "
            f"failure_code={r['failure_code']} level {r['level_after']}")
    ids = json.dumps([a, b, c])
    say("demotion rows (level_history)", json.loads(st.exec_api_code(LEVEL_SQL.replace("{ids}", ids))))
    for did in (a, b, c):
        say(f"{names[did]} network health", summary(did))
    say("## 6. exclusion check: POST /api/v1/publications to the demoted destinations")
    content = {"title": "Exclusion check", "body": "This must be refused.", "language": "en", "ai_generated": False,
               "is_promotional": False}
    for did in (a, b, c):
        r = st.http.post("/api/v1/publications", json={"destination_id": did, "content": content})
        err = r.json().get("error") or {}
        say(f"{names[did]}: HTTP {r.status_code} {err.get('code')} {err.get('message', '')[:140]}")
    due2 = st.ok(st.http.get("/api/v1/recert/due", params={"now": (real_now + timedelta(days=44, hours=1)).isoformat()}))
    say("not retried before RECERT_RETRY_EXCLUDED_AFTER_S", [s for s in due2["skipped"] if s["destination_id"] in (a, b)])
    say("## 7. alert payloads received by the webhook (HMAC verified here with the shared secret)")
    for d in deliveries():
        say(f"{d['event_header']} {d['delivery_header']} signature_valid={d['signature_valid']}", d["payload"])
    alerts = st.ok(st.http.get("/api/v1/network/alerts", params={"status": "open"}))
    say("open alerts (GET /api/v1/network/alerts?status=open)", [{k: x[k] for k in ("alert_id", "kind", "destination_id", "delivered_to")}
                                                                  for x in alerts["items"]])

    say("## 8. recovery: proxy re-enabled (B's fault was one-shot); the admin re-confirms C's rules")
    say(f"A: toxiproxy lemmy_proxy enabled -> {toxiproxy(True)}")
    put = st.http.put(f"/api/v1/destinations/{c}/rules", json={"rules": {"frequency_cap": "1/P7D", "link_policy": "no_shorteners",
                                                                         "automation_allowed": True}, "confirmed_by": "out3-demo-admin"})
    say(f"C: PUT /api/v1/destinations/{{id}}/rules (a person re-confirms) -> HTTP {put.status_code}")
    say("## 9. CLOCK JUMP +46 days: tick (demoted destinations are re-tested: trigger recert_retry)")
    t3 = recert(real_now + timedelta(days=46))
    for r in t3.get("results", []):
        say(f"  {names.get(r['destination_id'], r['destination_id'])} run {r['run_id']} trigger={r['trigger']} -> {r['state']} level {r['level_after']}")
    deliver = st.exec_api_code("import sys; from app.recert.__main__ import main; main(['deliver-alerts'])")
    say("python -m app.recert deliver-alerts", json.loads(deliver[deliver.index("{\n"):]))
    for did in (a, b, c):
        say(f"{names[did]} network health after recovery", summary(did))
    say("resolution events received by the webhook", [{"event": d["event_header"], "signature_valid": d["signature_valid"],
                                                       "kind": d["payload"]["kind"], "summary": d["payload"]["summary"]}
                                                      for d in deliveries() if d["event_header"] == "alert.resolved"])
    issues = st.ok(st.http.get(f"{args.mock}/alertsink/github/_issues", headers={"Authorization": ""}))["issues"]
    say("GitHub-issues emulation (our mock; a real repo is used only when GITHUB_TOKEN/GITHUB_REPO point at github.com)",
        [{k: i[k] for k in ("number", "title", "state", "labels", "authorized")} for i in issues])
    say("network summary (GET /api/v1/network/health)", st.ok(st.http.get("/api/v1/network/health"))["summary"])
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
