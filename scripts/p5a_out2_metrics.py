"""OUT2 evidence: fetch_metrics on several connectors, normalised, against a running stack in LOCAL mode.

Real numbers come only from our own local instances (Lemmy, and PeerTube / Discourse when their profiles run) and
only from legitimate actions of the instance admin on our own test post (one comment, one like, one reply, one
playback view, one link click: see ``scripts/p5a_instance_actions.py``). Slack runs on mock-platforms (labelled
``mock``); MRSS is our own feed, whose clicks are real requests to our own redirect. No fake accounts.

    uv run python scripts/p5a_out2_metrics.py --api http://127.0.0.1:<port> --mock http://127.0.0.1:<mock port> \
        [--peertube] [--discourse] [-f compose.yaml -f deploy/compose/peertube.yaml]

Writes evidence/levels/out/OUT2_metrics.csv (one row per connector x phase x metric) and OUT2_metrics.log.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.metrics.normalize import KEYS, MAPPING  # noqa: E402
from scripts.p5a_common import PERMISSIVE, ROOT, Stack, now_iso  # noqa: E402

ACTIONS = ROOT / "scripts" / "p5a_instance_actions.py"
CONTENT = {"title": "How do small communities measure what works?", "language": "en", "ai_generated": False,
           "is_promotional": False,
           "body": "We are comparing how small communities track engagement. What do you look at first?"}
LINK = "https://example.com/ibx-out2-article"


def raw_value(fields: dict[str, Any], field: str | None) -> Any:
    if field is None:
        return ""
    if field in fields:
        value = fields[field]
        return json.dumps(value) if isinstance(value, list | dict) else value
    for name, value in fields.items():
        if field.startswith(name):
            return json.dumps(value) if isinstance(value, list | dict) else value
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--mock", required=True, help="mock-platforms on the host (Slack mock controls)")
    parser.add_argument("--token", default=os.environ.get("IBX_API_TOKEN", "ibx-dev-token"))
    parser.add_argument("-f", dest="files", action="append", default=[])
    parser.add_argument("--peertube", action="store_true")
    parser.add_argument("--discourse", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "evidence" / "levels" / "out" / "OUT2_metrics.csv"))
    args = parser.parse_args()
    st = Stack(args.api, args.token, args.files or ["compose.yaml"], args.mock)
    log: list[str] = [f"# OUT2 fetch_metrics evidence, {now_iso()}", f"api {args.api} (SIMULATION_MODE=false: local mode)"]
    targets: list[dict[str, Any]] = []

    def setup(name: str, connector: str, source: str, content: dict[str, Any], **row: Any) -> None:
        did = st.add(name=name, connector_key=connector, **PERMISSIVE, **row)
        run = st.certify(did)
        log.append(f"{connector}: destination {did} certified {run['state']} level {run.get('level_after')} (run {run['run_id']})")
        if run["state"] != "PASSED":
            raise SystemExit(f"{connector} certification failed: {run.get('failure_code')}")
        pub = st.publish(did, content)
        log.append(f"{connector}: publication {pub['publication_id']} {pub['state']} platform_post_id={pub.get('platform_post_id')}")
        if pub["state"] != "published":
            raise SystemExit(f"{connector} publication not published: {pub}")
        targets.append({"connector": connector, "source": source, "destination_id": did, "pub": pub, "actions": []})

    setup("OUT2 local Lemmy ibx_test", "lemmy", "real_local_instance", CONTENT, url="http://lemmy:8536/c/ibx_test")
    if args.peertube:
        password = os.environ.get("IBX_PEERTUBE_ROOT_PASSWORD", "ibx-local-dev-only-peertube")
        st.http.post("/api/v1/connections", json={"connector_key": "peertube", "auth_type": "oauth2", "ref": "out2-peertube-root",
                                                         "credentials": {"username": "root", "password": password}})  # 409 on a re-run: kept
        setup("OUT2 local PeerTube", "peertube", "real_local_instance", CONTENT, url="http://peertube.ibx.test:9000",
              connection_ref="out2-peertube-root")
    if args.discourse:
        info = json.loads(st.exec_api_code("import json; d=json.load(open('/bootstrap/out/discourse.json')); "
                                           "print(json.dumps({k: d[k] for k in ('api_key', 'api_username', 'category_id', 'base_url')}))"))
        st.ok(st.http.post("/api/v1/connections", json={"connector_key": "discourse", "auth_type": "api_key", "ref": "out2-discourse-admin",
                                                         "credentials": {"api_key": info["api_key"], "api_username": info["api_username"]}}))
        del info["api_key"]
        setup("OUT2 local Discourse", "discourse", "real_local_instance", {**CONTENT, "body": CONTENT["body"] + f"\n\nOur notes: {LINK}"},
              url=info["base_url"], instance_category_id=str(info["category_id"]), connection_ref="out2-discourse-admin")
    setup("OUT2 Slack (mock workspace)", "slack", "mock", CONTENT, url="https://ibx-out2b.slack.com/archives/C0OUT2MT2",
          instance_channel_id="C0OUT2MT2", instance_api_base="http://mock-platforms:8080/slack/out2-slack/api", own_sandbox="true")
    setup("OUT2 MRSS feed (own service)", "mrss", "own_service", {**CONTENT, "canonical_url": LINK, "link_url": LINK},
          url="http://127.0.0.1/api/v1/feeds/out2-feed2/mrss.xml", instance_feed_key="out2-feed2", own_sandbox="true")

    snaps: dict[tuple[str, str], dict[str, Any]] = {}
    for t in targets:
        snaps[(t["connector"], "before")] = st.ok(st.http.post("/api/v1/metrics/fetch", json={"publication_id": t["pub"]["publication_id"]}))

    # ---- engagement: legitimate admin actions on our own instances / our own redirect / labelled mock controls
    for t in targets:
        pid = t["pub"]["platform_post_id"]
        if t["connector"] == "lemmy":
            t["actions"].append("Lemmy counts the author's own automatic upvote (the admin posted it)")
            r = st.exec_api(ACTIONS, "lemmy_comment", json.dumps({"post_id": pid}))
            t["actions"].append(f"admin commented once (HTTP {r.get('http_status')})")
        elif t["connector"] == "peertube":
            r = st.exec_api(ACTIONS, "peertube_engage", json.dumps({"video_id": pid,
                            "password": os.environ.get("IBX_PEERTUBE_ROOT_PASSWORD", "ibx-local-dev-only-peertube")}))
            t["actions"].append(f"root liked (HTTP {r.get('rate_status')}), commented once (HTTP {r.get('comment_status')}), "
                                f"reported one playback view (HTTP {r.get('view_status')})")
        elif t["connector"] == "discourse":
            r = st.exec_api(ACTIONS, "discourse_reply", json.dumps({"topic_id": pid}))
            t["actions"].append(f"admin replied once (HTTP {r.get('http_status')})")
            r2 = st.exec_api_code("import json,httpx; d=json.load(open('/bootstrap/out/discourse.json')); "
                                  f"t=httpx.get(d['base_url'].rstrip('/')+'/t/{int(pid)}.json', headers={{'Api-Key': d['api_key'], "
                                  "'Api-Username': d['api_username']}}, timeout=20).json(); print(t['post_stream']['posts'][0]['id'])")
            first_post = int(r2.strip())
            r3 = st.exec_api(ACTIONS, "discourse_click", json.dumps({"topic_id": pid, "post_id": first_post, "url": LINK}))
            t["actions"].append(f"admin followed the link in the post via /clicks/track (HTTP {r3.get('http_status')})")
        elif t["connector"] == "slack":
            channel, ts = pid.split(":", 1)
            st.ok(st.http.post(f"{args.mock}/slack/out2-slack/_mock/react", json={"channel": channel, "ts": ts, "name": "tada", "count": 2},
                               headers={"Authorization": ""}))
            st.ok(st.http.post(f"{args.mock}/slack/out2-slack/_mock/reply", json={"channel": channel, "ts": ts},
                               headers={"Authorization": ""}))
            t["actions"].append("MOCK workspace: mock control added 2 reactions and 1 thread reply (not real engagement)")
        elif t["connector"] == "mrss":
            for _ in range(2):
                r = st.http.get(f"/api/v1/r/{pid}", headers={"Authorization": ""}, follow_redirects=False)
                t["actions"].append(f"GET /api/v1/r/{{guid}} on our own service -> HTTP {r.status_code}")
    time.sleep(5)  # let the instances commit counters
    for t in targets:
        snaps[(t["connector"], "after")] = st.ok(st.http.post("/api/v1/metrics/fetch", json={"publication_id": t["pub"]["publication_id"]}))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["connector", "source", "phase", "metric", "supported", "raw_field", "raw_value", "normalised_value",
                    "consistent", "publication_id", "platform_post_id", "snapshot_id", "captured_at", "engagement_action"])
        for t in targets:
            for phase in ("before", "after"):
                snap = snaps[(t["connector"], phase)]
                for key in KEYS:
                    field = MAPPING[t["connector"]][key].field
                    value = snap["metrics"][key]
                    w.writerow([t["connector"], snap["source"], phase, key, key in snap["supported"], field or "",
                                raw_value(snap["raw"]["fields"], field), "null" if value is None else value, snap["consistent"],
                                t["pub"]["publication_id"], t["pub"]["platform_post_id"], snap["snapshot_id"], snap["captured_at"],
                                "; ".join(t["actions"]) if phase == "after" else "none yet (right after publishing)"])
    for t in targets:
        b, a = snaps[(t["connector"], "before")], snaps[(t["connector"], "after")]
        log.append(f"{t['connector']} [{a['source']}] before={json.dumps(b['metrics'])} after={json.dumps(a['metrics'])} "
                   f"consistent={a['consistent']} actions: {'; '.join(t['actions'])}")
    # clean up: the posts were only for this evidence
    for t in targets:
        r = st.http.post(f"/api/v1/publications/{t['pub']['publication_id']}/delete", json={})
        log.append(f"{t['connector']}: delete publication -> HTTP {r.status_code}")
    out.with_suffix(".log").write_text("\n".join(log) + "\n")
    sys.stdout.write("\n".join(log) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
