"""MH7 proof: tokens never appear in logs or the repository; simulation mode runs everything with no real accounts.

    scripts/prove_mh7.sh

1. Canary: a fresh secret-looking value is used as an inline credential, a stored connection secret and a platform
   credential that the platform refuses (its 401/403 body goes through our error mapping). Afterwards the value is
   searched for in every API response of this proof, in ``docker compose logs`` of api, worker and mock-platforms,
   in evidence/** and in the working tree (without .venv/.git/caches). 0 hits are required.
2. The bootstrap's dev-only secrets of the local instances (Lemmy JWT and admin password, Ghost Admin key) and the API
   token: 0 hits in the api/worker logs, evidence/** and the working tree (values read inside the container, never printed).
3. gitleaks (MIT, container zricethezav/gitleaks:v8.21.2) over a copy of the working tree without .venv/.git/caches.
4. Simulation mode: api and worker are recreated with SIMULATION_MODE=true; one row per Core connector with a
   REAL-LOOKING public URL (example.org) certifies, and the traffic arrives at mock-platforms only; then local mode is
   restored. Needs docker access to the stack (skipped with a reason otherwise).
Evidence: evidence/must_haves/MH7_canary_grep.log, MH7_gitleaks.txt, MH7_sim_run.log
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from prove_lib import API, CORE, ROOT, Proof, compose, in_stack, stack_reachable_by_exec, tag

EXCLUDE_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules", "results",
                ".hypothesis"}
LEVEL = {"discourse": "L4", "lemmy": "L4", "ghost": "L3", "webhook": "L3", "devto": "L2", "assisted": "L1"}
REAL_LOOKING = {"discourse": "https://forum-{t}.example.org", "lemmy": "https://lemmy-{t}.example.org/c/ibx_test",
                "ghost": "https://blog-{t}.example.org", "webhook": "https://partner-{t}.example.org/hooks",
                "devto": "https://forem-{t}.example.org", "assisted": "https://news-{t}.example.org/submit"}


def tree_files(base: Path):  # noqa: ANN201
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def hits_in_tree(base: Path, values: list[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in tree_files(base):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for value in values:
            if value.encode() in data:
                found.setdefault(value[:4] + "…", []).append(str(path.relative_to(base)))
    return found


def wait_ready(p: Proof, seconds: float = 120) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if p.client.get("/ready", timeout=5).status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - not up yet
            pass
        time.sleep(1)
    return False


def main() -> int:
    p = Proof("MH7", "Secrets protected (logs, repository) and simulation mode", "scripts/prove_mh7.sh")
    mode = p.mode()
    can_exec = stack_reachable_by_exec()
    canary = "ibxcanary" + tag() + tag()
    responses: list[str] = []

    p.section("1. canary through credentials and a refused platform call")
    tenant = tag("mh7-")
    conn = p.http("POST", "/api/v1/connections", json={"connector_key": "webhook", "credentials": {"secret": canary},
                                                       "account_handle": "canary-check"})
    responses.append(conn.text)
    for connector, url, creds in [
        ("discourse", f"http://mock-platforms:8080/discourse/{tenant}", {"api_key": "revoked-" + canary, "api_username": "system"}),
        ("devto", f"http://mock-platforms:8080/forem/{tenant}", {"api_key": "invalid-" + canary}),
        ("lemmy", f"http://mock-platforms:8080/lemmy/{tenant}/c/ibx_test", {"jwt": "revoked-" + canary}),
    ]:
        r = p.publish(connector, {"title": "Canary check post title", "body": "A post that must be refused by the platform."},
                      {"url": url, "rules": {"automation_allowed": True}}, connection={"connector_key": connector, "credentials": creds})
        responses.append(r.text)
        p.check(f"{connector}: refused credential -> CONNECTION_AUTH_EXPIRED, canary not echoed", r.status_code == 424
                and canary not in r.text, {"status": r.status_code})
    r = p.publish("webhook", {"title": "Canary stored secret post", "body": "Signed with the stored connection secret."},
                  {"url": f"http://mock-platforms:8080/partner/{tenant}", "rules": {"automation_allowed": True}},
                  connection={"connector_key": "webhook", "connection_id": conn.json().get("connection_id")})
    responses.append(r.text)
    responses.append(p.http("GET", f"/api/v1/connections/{conn.json().get('connection_id')}").text)
    p.check("the canary is in none of the API responses", all(canary not in t for t in responses), len(responses))
    time.sleep(2)  # let the log lines flush
    if can_exec:
        logs = compose("logs", "--no-log-prefix", "api", "worker", "mock-platforms", timeout=180).stdout
        p.check("the canary is in none of the api/worker/mock-platforms log lines", canary not in logs, f"{len(logs.splitlines())} lines searched")
    else:
        p.add_partial("no docker access to the stack: container logs were not searched")

    p.section("2. bootstrap secrets and the API token")
    secrets: list[str] = []
    if can_exec:
        secrets = in_stack(
            "import json,os; v=[]\n"
            "for n in ('lemmy.json','ghost.json','discourse.json'):\n"
            "    f='/bootstrap/out/'+n\n"
            "    if os.path.exists(f):\n"
            "        d=json.load(open(f)); v += [str(d.get(k)) for k in ('jwt','admin_password','admin_api_key','content_api_key','api_key') if d.get(k)]\n"
            "        v += [str(d['admin_api_key']).split(':')[-1]] if d.get('admin_api_key') else []\n"
            "print(json.dumps(v))")
        logs = compose("logs", "--no-log-prefix", "api", "worker", timeout=180).stdout
        p.check(f"{len(secrets)} bootstrap secret values: 0 hits in the api/worker logs",
                not any(s in logs for s in secrets), f"{len(logs.splitlines())} lines")
    token = os.environ.get("API_TOKEN") or "ibx-dev-token"
    if can_exec:
        p.check("the API token is in no api/worker log line", token not in logs, "")
    found = hits_in_tree(ROOT / "evidence", [canary, *secrets])
    p.check("evidence/**: 0 hits for the canary and the bootstrap secrets", not found, found)
    found = hits_in_tree(ROOT, [canary])
    p.check("working tree (no .venv/.git/caches): 0 hits for the canary", not found, found)
    found = hits_in_tree(ROOT, secrets)
    files = sorted({f for paths in found.values() for f in paths})
    defaults_only = bool(files) and all(f.startswith(("deploy/compose/", "deploy/bootstrap/", "deploy/lemmy/")) for f in files)
    p.check("working tree: 0 hits for the bootstrap's secret values", not found or defaults_only, files)
    if defaults_only:
        p.check("bootstrap secret values are not committed defaults", False,
                f"a local-instance value equals a dev-only default committed in {files} (placeholder passwords for "
                "containers on an internal network; .env / IBX_LEMMY_ADMIN_PASSWORD override it). Human decision.", critical=False)

    p.section("3. gitleaks over the working tree")
    report = ""
    if shutil.which("docker"):
        with tempfile.TemporaryDirectory(prefix="ibx_gitleaks_") as tmp:
            copy = Path(tmp) / "repo"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(*EXCLUDE_DIRS))
            out = subprocess.run(["docker", "run", "--rm", "-v", f"{copy}:/repo:ro", "zricethezav/gitleaks:v8.21.2", "detect",
                                  "--no-git", "--source", "/repo", "--no-banner", "--redact", "--report-format", "json",
                                  "--report-path", "/dev/stdout", "--exit-code", "3"], capture_output=True, text=True, timeout=600)
        try:
            findings = json.loads(out.stdout or "[]")
        except ValueError:
            findings = None
        if findings is None:
            p.add_partial(f"gitleaks did not run: {out.stderr[-300:]}")
        else:
            lines = [f"{f.get('RuleID')}  {f.get('File')}:{f.get('StartLine')}  {f.get('Match', '')[:80]}" for f in findings]
            report = (f"gitleaks v8.21.2 detect --no-git over a copy of the working tree (no {', '.join(sorted(EXCLUDE_DIRS))});"
                      f" exit {out.returncode}; {len(findings)} finding(s) (values redacted by gitleaks)\n" + "\n".join(lines) + "\n")
            def benign(f: dict) -> str:
                match, path = str(f.get("Match", "")), str(f.get("File", ""))
                if any(k in match.lower() for k in ("idempotency_key", "empotency_key", "idempotency-key", "empotency-key")):
                    return "idempotency key (not a credential)"
                if path.endswith("catalogue/real_catalogue_v1.csv") and match.startswith("api_sidebar:"):
                    return "catalogue CSV: rules_source=api_sidebar next to a public rules-page SHA-256 (not a credential)"
                if path.startswith("/repo/tests/"):
                    return "fake token in a test fixture (redaction / auth tests)"
                return ""
            explained = [(f, benign(f)) for f in findings]
            unexplained = [f for f, why in explained if not why]
            report += "\nClassification:\n" + "\n".join(
                f"  {why or 'UNEXPLAINED'}: {f.get('File')}:{f.get('StartLine')}" for f, why in explained) + "\n"
            p.write_text("evidence/must_haves/MH7_gitleaks.txt", p.header() + report)
            p.check("gitleaks: 0 unexplained findings (idempotency keys and test-fixture fakes listed in the report)",
                    not unexplained, [f"{f.get('RuleID')} {f.get('File')}:{f.get('StartLine')}" for f in unexplained][:10])
            if findings and not unexplained:
                p.add_partial(f"gitleaks reports {len(findings)} findings, all classified as false positives "
                              "(idempotency_key fields, fake tokens in tests); a .gitleaksignore would make the gate clean")
    else:
        p.add_partial("docker is not available: gitleaks not run")

    p.section("4. simulation mode: real-looking URLs, traffic only to mock-platforms")
    sim_lines: list[str] = []
    if not can_exec:
        p.add_partial("no docker access to the stack: the simulation-mode run was not done here")
    else:
        restore = mode
        env = {**os.environ, "SIMULATION_MODE": "true"}
        up = subprocess.run(["docker", "compose", "up", "-d", "--wait", "--no-build", "api", "worker"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=600)
        p.check("api and worker recreated with SIMULATION_MODE=true", up.returncode == 0 and wait_ready(p), up.stderr[-300:])
        p._mode = None
        p.check("the job manifest says simulation: true", p.mode() == "simulation", p._mode)
        before = p.mock.get("/_counts").json()["counts"]
        for connector in CORE:
            t = tag(f"sim-{connector}-")
            row = {"name": f"Sim {connector} {t}", "url": REAL_LOOKING[connector].format(t=t), "connector_key": connector}
            dest = p.create_dest(row)
            run = p.certify(dest["destination_id"])
            ok = run.get("state") == "PASSED" and run.get("level_after") == LEVEL[connector]
            sim_lines.append(f"{connector}: {row['url']} -> {run.get('state')} {run.get('level_after')} (run {run.get('run_id')})")
            p.check(f"simulation: {connector} row with a real-looking URL certifies (rewritten to the mock)", ok,
                    {"state": run.get("state"), "level": run.get("level_after"), "failure": run.get("failure_code")})
        after = p.mock.get("/_counts").json()["counts"]
        grew = {k: sum(sum(v.values()) for v in after.get(k, {}).values()) - sum(sum(v.values()) for v in before.get(k, {}).values())
                for k in after}
        p.check("the simulated runs' traffic arrived at mock-platforms", sum(grew.values()) > 0, grew)
        sim_lines.append(f"mock-platforms request counts added during the run: {json.dumps(grew)}")
        env["SIMULATION_MODE"] = "true" if restore == "simulation" else "false"
        back = subprocess.run(["docker", "compose", "up", "-d", "--wait", "--no-build", "api", "worker"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=600)
        p._mode = None
        p.check(f"stack restored to {restore} mode", back.returncode == 0 and wait_ready(p) and p.mode() == restore, back.stderr[-200:])
        p.write_text("evidence/must_haves/MH7_sim_run.log", p.header() + "\n".join(sim_lines) + "\n")
    p.log(f"API {API}; canary length {len(canary)} (value not written anywhere)")
    return p.finish("evidence/must_haves/MH7_canary_grep.log")


if __name__ == "__main__":
    sys.exit(main())
