"""Shared helpers for the proof scripts (``scripts/prove_*.py``). Nothing here is typed evidence: every evidence file
is written from real HTTP exchanges with the running stack, at the moment the proof runs.

A proof records checks; at the end it writes
  * its evidence files (named in plan §13), with a generated header (UTC time, API URL, stack mode), and
  * ``evidence/proof_runs/<ID>.json``: {id, status Yes|Partial|No, checks, partial reasons, evidence, command, times},
    which ``scripts/prove_ledger.py`` turns into ``evidence/LEDGER.md``.
Exit code: 0 = Yes (every check passed), 2 = Partial (critical checks passed, something is missing), 1 = No.

Environment: IBX_API_URL (default http://127.0.0.1:$IBX_API_PORT or :8088), IBX_MOCK_URL (mock-platforms as seen
from the host, default http://127.0.0.1:$IBX_MOCK_PORT or :8089), API_TOKEN (default the dev token),
COMPOSE_PROJECT_NAME (for ``docker compose exec`` into the api container), IBX_PROVE_RUN_ID (set by prove_all.sh).
"""

from __future__ import annotations

import csv
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reliability.redact import redact  # noqa: E402  (after the sys.path insert)

API = os.environ.get("IBX_API_URL") or f"http://127.0.0.1:{os.environ.get('IBX_API_PORT', '8088')}"
MOCK = os.environ.get("IBX_MOCK_URL") or f"http://127.0.0.1:{os.environ.get('IBX_MOCK_PORT', '8089')}"
TOKEN = os.environ.get("API_TOKEN") or os.environ.get("IBX_API_TOKEN") or "ibx-dev-token"
MOCK_IN = "http://mock-platforms:8080"  # mock-platforms as the api/worker containers see it
RUN_ID = os.environ.get("IBX_PROVE_RUN_ID") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
EVIDENCE = ROOT / "evidence"
PROOF_RUNS = EVIDENCE / "proof_runs"
SIG_RE = re.compile(r"(sig=)[0-9a-f]+")

# Appendix A: method → exact output keys; capability word in the manifest → contract method
OUTPUT_KEYS = {
    "validate": {"ok", "violations"},
    "publish": {"platform_post_id", "live_url", "status"},
    "get_status": {"status", "live_url"},
    "delete": {"deleted"},
    "fetch_metrics": {"views", "likes", "comments", "shares", "saves", "watch_time_s", "clicks"},
    "refresh_auth": {"expires_at"},
    "build_assist_pack": {"files", "caption", "deep_link", "checklist"},
}
CAPABILITY_METHOD = {"publish": "publish", "status": "get_status", "delete": "delete", "metrics": "fetch_metrics",
                     "refresh_auth": "refresh_auth", "assist": "build_assist_pack"}
CORE = ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"]


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def tag(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def mock_url(platform_name: str, tenant: str, suffix: str = "") -> str:
    """A destination URL on mock-platforms as the API sees it, e.g. mock_url('discourse', 't1')."""
    return f"{MOCK_IN}/{platform_name}/{tenant}{suffix}"


MOCK_DEST = {  # connector → (mock platform, URL suffix after the tenant)
    "discourse": ("discourse", ""),
    "lemmy": ("lemmy", "/c/ibx_test"),
    "devto": ("forem", ""),
    "ghost": ("ghost", ""),
    "webhook": ("partner", ""),
    "assisted": ("assist", "/submit"),
}
REAL_DEST = {"lemmy": "http://lemmy:8536/c/ibx_test", "ghost": "http://ghost:2368"}  # the real local instances


def dest_url(connector: str, tenant: str) -> str:
    plat, suffix = MOCK_DEST[connector]
    return mock_url(plat, tenant, suffix)


def scrub(text: str) -> str:
    return SIG_RE.sub(r"\1***", text)


def dumps(data: Any, limit: int = 4000) -> str:
    text = data if isinstance(data, str) else json.dumps(redact(data), indent=1, ensure_ascii=False, default=str)
    text = scrub(text)
    return text if len(text) <= limit else text[:limit] + f"\n... ({len(text) - limit} more characters)"


class Proof:
    """Collects checks and an exchange log for one proof id (MH1..MH9, CORE1..3, ...)."""

    def __init__(self, pid: str, title: str, command: str) -> None:
        self.pid, self.title, self.command = pid, title, command
        self.started = now_iso()
        self.t0 = time.monotonic()
        self.lines: list[str] = []
        self.checks: list[dict[str, Any]] = []
        self.partial: list[str] = []
        self.evidence: list[str] = []
        self.sub: dict[str, dict[str, Any]] = {}
        self.client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=90)
        self.mock = httpx.Client(base_url=MOCK, timeout=30)
        self._mode: str | None = None
        print(f"== {pid}: {title} (API {API})", flush=True)

    # ------------------------------------------------------------------ logging and checks
    def log(self, text: str) -> None:
        self.lines.append(scrub(text))

    def section(self, title: str) -> None:
        self.lines.append(f"\n## {title}\n")
        print(f"-- {title}", flush=True)

    def check(self, label: str, ok: bool, detail: Any = "", *, critical: bool = True, group: str | None = None) -> bool:
        detail_text = detail if isinstance(detail, str) else json.dumps(redact(detail), default=str)[:600]
        outcome = "PASS" if ok else ("FAIL" if critical else "WARN")
        line = f"{outcome}  {label}" + (f": {scrub(detail_text)}" if detail_text else "")
        self.lines.append(line)
        print(line, flush=True)
        self.checks.append({"label": label, "ok": bool(ok), "critical": critical, "detail": scrub(detail_text)[:600],
                            "group": group})
        return bool(ok)

    def add_partial(self, reason: str) -> None:
        if reason not in self.partial:
            self.partial.append(reason)
            self.lines.append(f"PARTIAL  {reason}")
            print(f"PARTIAL  {reason}", flush=True)

    def set_sub(self, key: str, status: str, reasons: Iterable[str] = (), evidence: Iterable[str] = ()) -> None:
        self.sub[key] = {"status": status, "reasons": list(reasons), "evidence": list(evidence)}

    # ------------------------------------------------------------------ HTTP
    def http(self, method: str, path: str, *, quiet: bool = False, client: httpx.Client | None = None,
             **kwargs: Any) -> httpx.Response:
        started = time.monotonic()
        response = (client or self.client).request(method, path, **kwargs)
        elapsed = (time.monotonic() - started) * 1000
        if not quiet:
            body = kwargs.get("json", kwargs.get("content"))
            sent_headers = {k: v for k, v in (kwargs.get("headers") or {}).items() if k.lower() != "authorization"}
            keep = ("retry-after", "idempotent-replayed", "x-incubrix-warning", "location", "x-correlation-id")
            got_headers = {k: v for k, v in response.headers.items() if k.lower() in keep}
            try:
                got: Any = response.json()
            except ValueError:
                got = response.text[:1500]
            self.lines.append(
                f"> {method} {path}" + (f"  headers={json.dumps(sent_headers)}" if sent_headers else "") + "\n"
                + (dumps(body, 2500) + "\n" if body is not None else "")
                + f"< {response.status_code} ({elapsed:.0f} ms)" + (f"  headers={json.dumps(got_headers)}" if got_headers else "")
                + "\n" + dumps(got, 3000) + "\n")
        return response

    def mock_call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.http(method, path, client=self.mock, **kwargs)

    def mock_counts(self, platform_name: str, tenant: str) -> dict[str, int]:
        data = self.mock.get("/_counts", params={"platform": platform_name, "tenant": tenant}).json()["counts"]
        return dict(data.get(platform_name, {}).get(tenant, {}))

    def creates(self, connector: str, tenant: str) -> int:
        """Create requests the mock platform received for this tenant (the 'mock hit count')."""
        route = {"discourse": "POST /posts.json", "lemmy": "POST /api/v3/post", "devto": "POST /api/articles",
                 "ghost": "POST /ghost/api/admin/posts", "webhook": "POST /posts"}[connector]
        return self.mock_counts(MOCK_DEST[connector][0], tenant).get(route, 0)

    def wait_job(self, job_id: str, timeout_s: float = 180) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            job = self.client.get(f"/api/v1/jobs/{job_id}").json()
            if job.get("status") in ("succeeded", "failed") or time.monotonic() > deadline:
                self.lines.append(f"< job {job_id}: status={job.get('status')} result={dumps(job.get('result'), 1500)}"
                                  f" error={dumps(job.get('error'), 800)}\n")
                return job
            time.sleep(0.5)

    # ------------------------------------------------------------------ catalogue / certification helpers
    def create_dest(self, row: dict[str, Any]) -> dict[str, Any]:
        response = self.http("POST", "/api/v1/destinations", json=row)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"destination row refused: {response.status_code} {response.text[:300]}")
        return response.json()

    def certify(self, destination_id: str, timeout_s: float = 240) -> dict[str, Any]:
        started = self.http("POST", "/api/v1/certifications", json={"destination_id": destination_id})
        if started.status_code != 202:
            raise RuntimeError(f"certification not started: {started.status_code} {started.text[:300]}")
        self.wait_job(started.json()["job_id"], timeout_s)
        return self.http("GET", f"/api/v1/certifications/{started.json()['run_id']}", quiet=True).json()

    def publish(self, connector: str, rendition: dict[str, Any], destination: dict[str, Any],
                connection: dict[str, Any] | None = None, key: str | None = None, headers: dict[str, str] | None = None,
                path_prefix: str = "/connector/v1") -> httpx.Response:
        body = {"rendition": rendition, "destination": {"connector_key": connector, **destination},
                "connection": connection or {"connector_key": connector}, "idempotency_key": key or tag(f"{connector}-")}
        return self.http("POST", f"{path_prefix}/{connector}/publish", json=body, headers=headers or {})

    def mode(self) -> str:
        """'simulation' or 'local', read from a finished job's manifest (settings are never guessed)."""
        if self._mode is None:
            job = self.client.post("/api/v1/jobs", json={"kind": "selfcheck"}).json()
            done = self.wait_job(job["job_id"], 60)
            link = done["result"]["links"]["manifest"]
            manifest = self.client.get(local_path(link)).json()
            self._mode = "simulation" if manifest.get("simulation") else "local"
            self.lines.append(f"stack mode from job manifest: {self._mode}")
        return self._mode

    # ------------------------------------------------------------------ results
    def write_text(self, rel: str, text: str) -> Path:
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scrub(text), encoding="utf-8")
        if rel not in self.evidence:
            self.evidence.append(rel)
        return path

    def write_csv(self, rel: str, rows: list[dict[str, Any]], fields: list[str] | None = None) -> Path:
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = fields or (list(rows[0]) if rows else ["empty"])
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: scrub(str(v)) if v is not None else "" for k, v in row.items()})
        if rel not in self.evidence:
            self.evidence.append(rel)
        return path

    def header(self) -> str:
        return (f"# {self.pid}: {self.title}\n# generated by {self.command} at {now_iso()} (run {RUN_ID})\n"
                f"# API {API}  mock-platforms {MOCK}  stack mode {self._mode or 'not read'}  host {platform.node()} "
                f"({platform.system()} {platform.machine()}, {os.cpu_count()} CPUs)\n"
                "# nothing in this file is typed by hand; secrets are redacted and signed-link signatures masked (sig=***)\n")

    def finish(self, log_rel: str | None = None) -> int:
        if log_rel:
            self.write_text(log_rel, self.header() + "\n".join(self.lines) + "\n")
        failed = [c for c in self.checks if not c["ok"] and c["critical"]]
        warned = [c for c in self.checks if not c["ok"] and not c["critical"]]
        if failed:
            status = "No"
        elif warned or self.partial:
            status = "Partial"
        else:
            status = "Yes"
        reasons = [f"FAIL {c['label']}: {c['detail'][:200]}" for c in failed]
        reasons += [f"WARN {c['label']}: {c['detail'][:200]}" for c in warned] + list(self.partial)
        result = {"id": self.pid, "title": self.title, "status": status, "reasons": reasons, "run_id": RUN_ID,
                  "started_at": self.started, "finished_at": now_iso(), "duration_s": round(time.monotonic() - self.t0, 1),
                  "api": API, "mode": self._mode, "command": self.command, "evidence": self.evidence,
                  "checks_total": len(self.checks), "checks_passed": sum(c["ok"] for c in self.checks),
                  "checks": self.checks, "sub": self.sub}
        PROOF_RUNS.mkdir(parents=True, exist_ok=True)
        (PROOF_RUNS / f"{self.pid}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"== {self.pid}: {status} ({result['checks_passed']}/{result['checks_total']} checks; "
              f"{len(self.partial)} partial reasons) in {result['duration_s']} s", flush=True)
        self.client.close()
        self.mock.close()
        return {"Yes": 0, "Partial": 2, "No": 1}[status]


def local_path(link: str) -> str:
    """A signed link from the API (PUBLIC_BASE_URL may differ from IBX_API_URL) as a path on IBX_API_URL."""
    parts = httpx.URL(link)
    return f"{parts.path}?{parts.query.decode()}" if parts.query else parts.path


def compose(*args: str, timeout: int = 600, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=check)


def in_stack(code: str) -> Any:
    """Run a Python snippet inside the api container (Lemmy, Ghost and toxiproxy are on the internal network only).
    The snippet prints one JSON value as its last line."""
    result = compose("exec", "-T", "api", "python", "-c", code, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"in_stack failed: {result.stderr[-1500:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def stack_reachable_by_exec() -> bool:
    try:
        return compose("ps", "--status", "running", "--services", timeout=30).stdout.split().count("api") == 1
    except (OSError, subprocess.SubprocessError):
        return False


def toxic(proxy: str, latency_ms: int | None) -> int:
    if latency_ms is None:
        code = (f"import httpx,json; r=httpx.delete('http://toxiproxy:8474/proxies/{proxy}/toxics/ibx_latency');"
                "print(json.dumps(r.status_code))")
    else:
        body = {"name": "ibx_latency", "type": "latency", "stream": "downstream", "attributes": {"latency": latency_ms}}
        code = (f"import httpx,json; r=httpx.post('http://toxiproxy:8474/proxies/{proxy}/toxics', json={body!r});"
                "print(json.dumps(r.status_code))")
    return int(in_stack(code))


LEMMY_PRELUDE = ("import json, httpx; cfg = json.load(open('/bootstrap/out/lemmy.json')); B = cfg['base_url'];"
                 "H = {'Authorization': 'Bearer ' + cfg['jwt']};")
GHOST_PRELUDE = ("import json, time, httpx\nfrom app.connectors.ghost.connector import admin_token, split_admin_key\n"
                 "cfg = json.load(open('/bootstrap/out/ghost.json'))\n"
                 "token, _ = admin_token(*split_admin_key(cfg['admin_api_key']), time.time())\n"
                 "H = {'Authorization': 'Ghost ' + token}\n")


def lemmy_posts_with(text: str) -> list[int]:
    """Ids of the admin's newest posts on the REAL local Lemmy whose body contains ``text``."""
    code = LEMMY_PRELUDE + (
        "r = httpx.get(B + '/api/v3/user', params={'username': cfg['admin_username'], 'sort': 'New', 'limit': 50}, headers=H);"
        f"print(json.dumps([v['post']['id'] for v in r.json()['posts'] if {text!r} in (v['post'].get('body') or '')]))")
    return in_stack(code)


def ghost_posts_with_slug_suffix(marker: str) -> list[str]:
    code = GHOST_PRELUDE + (
        f"r = httpx.get(cfg['base_url'] + '/ghost/api/admin/posts/', params={{'filter': \"slug:~$'{marker}'\", 'limit': 'all'}},"
        " headers=H)\nprint(json.dumps([p['id'] for p in r.json()['posts']]))")
    return in_stack(code)


def lemmy_set_post_limit(count: int, per_second: int) -> bool:
    code = LEMMY_PRELUDE + (
        "import time;"
        f"r = httpx.put(B + '/api/v3/site', json={{'rate_limit_post': {count}, 'rate_limit_post_per_second': {per_second}}}, headers=H);"
        "ok = False\n"
        "for _ in range(30):\n"
        "    lim = httpx.get(B + '/api/v3/site').json()['site_view']['local_site_rate_limit']\n"
        f"    ok = (lim['post'], lim['post_per_second']) == ({count}, {per_second})\n"
        "    if ok: break\n"
        "    time.sleep(1)\n"
        "print(json.dumps([r.status_code, ok]))")
    return in_stack(code) == [200, True]


def read_proof(pid: str) -> dict[str, Any] | None:
    path = PROOF_RUNS / f"{pid}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
