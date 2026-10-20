"""MH8 evidence: bring the local platform instances up from scratch, measure, check, tear down.

Writes evidence/must_haves/MH8_local_instances.txt (or --out). Host-side script, Python standard library only:
    python3 deploy/bootstrap/measure_local_instances.py [--runs 2] [--full] [--project ibx_p2a]

For each run: `down -v` -> `up -d` -> poll every second until every service is healthy (records per-service
time-to-healthy) -> idle settle -> `docker stats --no-stream` -> functional checks run inside the compose network
(bootstrap ok, federation off, communities exist, Ghost key works, egress blocked, toxiproxy latency toxic works).
Always ends with `down -v`. No secrets are printed or written.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASE_FILES = ["lemmy.yaml", "ghost.yaml", "toxiproxy.yaml", "bootstrap.yaml"]
CHECK_SCRIPT = r"""
import json, sys, time, urllib.request
sys.path.insert(0, "/ibx/bootstrap")
from ibxboot import Http, ghost_admin_jwt
res = {}
st = json.load(open("/bootstrap/out/status.json")); res["bootstrap_status_ok"] = st.get("ok")
lc = json.load(open("/bootstrap/out/lemmy.json"))
site = Http("http://lemmy:8536").request("GET", "/api/v3/site", headers={"Authorization": "Bearer " + lc["jwt"]}).json()
res["lemmy_version"] = site["version"]
res["lemmy_federation_enabled"] = site["site_view"]["local_site"]["federation_enabled"]
rl = site["site_view"]["local_site_rate_limit"]; res["lemmy_post_rate_limit"] = f"{rl['post']} per {rl['post_per_second']} s"
res["lemmy_communities"] = sorted(lc["communities"])
gc = json.load(open("/bootstrap/out/ghost.json"))
r = Http("http://ghost:2368").request("GET", "/ghost/api/admin/posts/?limit=1",
                                      headers={"Authorization": "Ghost " + ghost_admin_jwt(gc["admin_api_key"])})
res["ghost_version"] = gc["version"]; res["ghost_admin_key_http"] = r.status
try:
    urllib.request.urlopen("https://example.com", timeout=5); res["internet_egress_from_sandbox"] = "OPEN"
except Exception as e:
    res["internet_egress_from_sandbox"] = f"blocked ({type(e).__name__})"
tp = Http("http://toxiproxy:8474")
res["toxiproxy_proxies"] = sorted(tp.request("GET", "/proxies").json())
gp = Http("http://toxiproxy:12368")
t0 = time.monotonic(); s0 = gp.request("GET", "/ghost/api/admin/site/").status; base = time.monotonic() - t0
tp.request("POST", "/proxies/ghost_proxy/toxics", json_body={"name": "mh8_latency", "type": "latency",
           "stream": "downstream", "attributes": {"latency": 1500}})
t0 = time.monotonic(); s1 = gp.request("GET", "/ghost/api/admin/site/").status; slow = time.monotonic() - t0
tp.request("DELETE", "/proxies/ghost_proxy/toxics/mh8_latency")
res["toxiproxy_ghost_proxy"] = f"HTTP {s0} in {base*1000:.0f} ms; with 1500 ms latency toxic HTTP {s1} in {slow*1000:.0f} ms"
lp = Http("http://toxiproxy:18536").request("GET", "/api/v3/site").status
res["toxiproxy_lemmy_proxy_http"] = lp
try:
    dc = json.load(open("/bootstrap/out/discourse.json"))
    r = Http("http://discourse").request("GET", "/site/basic-info.json")
    res["discourse_version"] = dc["version"]; res["discourse_basic_info_http"] = r.status
except FileNotFoundError:
    pass
print(json.dumps(res))
"""


def sh(args: list[str], check: bool = True, capture: bool = True) -> str:
    p = subprocess.run(args, capture_output=capture, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(args[:6])}...\n{(p.stderr or '')[-2000:]}")
    return p.stdout or ""


def compose(project: str, full: bool) -> list[str]:
    files = BASE_FILES + (["discourse.yaml"] if full else [])
    cmd = ["docker", "compose", "-p", project]
    for f in files:
        cmd += ["-f", os.path.join(REPO, "deploy", "compose", f)]
    if full:
        cmd += ["--profile", "full"]
    return cmd


def ps(base: list[str]) -> list[dict]:
    out = sh(base + ["ps", "-a", "--format", "json"])
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        rows.extend(data if isinstance(data, list) else [data])
    return rows


def mem_mib(s: str) -> float:
    s = s.strip()
    for unit, mul in (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024), ("kB", 1 / 1024), ("B", 1 / 1024 / 1024)):
        if s.endswith(unit):
            return float(s[: -len(unit)]) * mul
    return float("nan")


def one_run(base: list[str], project: str, settle_s: int, timeout_s: int) -> dict:
    sh(base + ["down", "-v", "--remove-orphans"])
    t0 = time.monotonic()
    # `up -d` itself blocks on depends_on health, so run it in the background and poll concurrently.
    up = subprocess.Popen(base + ["up", "-d"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    healthy_at: dict[str, float] = {}
    services: set[str] = set()
    while time.monotonic() - t0 < timeout_s:
        rows = ps(base)
        services = {r["Service"] for r in rows}
        for r in rows:
            if r["Service"] not in healthy_at and r.get("Health") == "healthy":
                healthy_at[r["Service"]] = time.monotonic() - t0
            if r.get("State") == "exited" and r.get("ExitCode", 0) != 0:
                raise RuntimeError(f"service {r['Service']} exited with {r.get('ExitCode')}")
        if services and all(s in healthy_at for s in services):
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"not all services healthy within {timeout_s}s: {sorted(services - set(healthy_at))}")
    total = time.monotonic() - t0
    if up.wait(timeout=120) != 0:
        raise RuntimeError(f"`up -d` failed: {(up.stderr.read() if up.stderr else '')[-2000:]}")
    # `up --wait` must agree (it is the documented gate command) and return immediately now.
    w0 = time.monotonic()
    sh(base + ["up", "-d", "--wait"])
    wait_s = time.monotonic() - w0
    time.sleep(settle_s)
    stats_raw = sh(["docker", "stats", "--no-stream", "--format", "{{json .}}"])
    stats = []
    for line in stats_raw.splitlines():
        d = json.loads(line)
        if d["Name"].startswith(f"{project}-"):
            used = d["MemUsage"].split("/")[0]
            stats.append({"name": d["Name"], "mem": used.strip(), "mem_mib": mem_mib(used), "cpu": d["CPUPerc"]})
    checks = json.loads(sh(base + ["exec", "-T", "bootstrap", "python", "-c", CHECK_SCRIPT]).strip().splitlines()[-1])
    return {"healthy_at": healthy_at, "total_s": total, "wait_recheck_s": wait_s, "stats": stats, "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--full", action="store_true", help="include the real Discourse (profile full)")
    ap.add_argument("--project", default="ibx_p2a")
    ap.add_argument("--settle", type=int, default=20, help="idle seconds before docker stats")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", default=None, help="default evidence/must_haves/MH8_local_instances[_full].txt")
    args = ap.parse_args()
    if args.out is None:
        name = "MH8_local_instances_full.txt" if args.full else "MH8_local_instances.txt"
        args.out = os.path.join(REPO, "evidence", "must_haves", name)
    base = compose(args.project, args.full)

    started = datetime.now(UTC).isoformat(timespec="seconds")
    runs = []
    try:
        for i in range(args.runs):
            print(f"[mh8] run {i + 1}/{args.runs}: down -v, up from scratch ...", file=sys.stderr, flush=True)
            runs.append(one_run(base, args.project, args.settle, args.timeout))
            print(f"[mh8] run {i + 1}: all healthy in {runs[-1]['total_s']:.1f}s", file=sys.stderr, flush=True)
    finally:
        sh(base + ["down", "-v", "--remove-orphans"], check=False)

    images = sorted({line for line in sh(base + ["config", "--images"]).splitlines() if line.strip()})
    lines = [
        "MH8 - local real platform instances via docker compose (generated by deploy/bootstrap/measure_local_instances.py)",
        f"generated_at_utc: {datetime.now(UTC).isoformat(timespec='seconds')}  (started {started})",
        f"host: {platform.system()} {platform.release()} {platform.machine()}, cpus={os.cpu_count()}, "
        f"mem_total={_mem_total()}",
        f"docker: {sh(['docker', 'version', '--format', '{{.Server.Version}}']).strip()}, "
        f"compose: {sh(['docker', 'compose', 'version', '--short']).strip()}",
        f"profile: {'default + full (real Discourse)' if args.full else 'default (Lemmy, Ghost, toxiproxy, bootstrap)'}",
        "command: docker compose -p " + args.project + " " + " ".join(
            a for a in base[4:] if a not in ("-p", args.project)).replace(REPO + "/", "") + " up -d --wait",
        "images (pinned by digest; all images were already present locally, so pull time is NOT included):",
        *[f"  {img}" for img in images],
        "",
    ]
    for i, r in enumerate(runs, 1):
        lines.append(f"== run {i}: cold start from empty volumes (after `down -v`)")
        lines.append(f"all services healthy after: {r['total_s']:.1f} s   (`up -d --wait` re-check: {r['wait_recheck_s']:.1f} s)")
        for svc, t in sorted(r["healthy_at"].items(), key=lambda kv: kv[1]):
            lines.append(f"  {svc:<22} healthy at {t:6.1f} s")
        total = sum(s["mem_mib"] for s in r["stats"])
        lines.append(f"RAM after {args.settle} s idle (docker stats --no-stream), total {total:.0f} MiB:")
        for s in sorted(r["stats"], key=lambda s: -s["mem_mib"]):
            lines.append(f"  {s['name']:<34} {s['mem']:>10}  cpu {s['cpu']}")
        lines.append("functional checks (run inside the compose network):")
        for k, v in r["checks"].items():
            lines.append(f"  {k}: {v}")
        lines.append("")
    lines.append("teardown: docker compose down -v (done)")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


def _mem_total() -> str:
    try:
        with open("/proc/meminfo") as f:
            kb = int(f.readline().split()[1])
        return f"{kb / 1024 / 1024:.1f} GiB"
    except (OSError, ValueError, IndexError):
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
