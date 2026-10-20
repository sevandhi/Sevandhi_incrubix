"""Integration Standard self-check against a running API (no curl needed).

    uv run python scripts/selfcheck.py --api http://127.0.0.1:8088 --token "$API_TOKEN" \
        [--out evidence/integration/IS_selfcheck.txt]

Checks IS 2 (OpenAPI 3.1), 3 (jobs), 4 (idempotency), 5 (errors), 6 (config), 8 (logs/health), 10 (batch).
Prints PASS/FAIL per check and writes the same lines (with a UTC timestamp) to --out. Exit 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Check:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed = 0

    def run(self, label: str, fn: Callable[[], str]) -> None:
        try:
            detail = fn()
            line = f"PASS  {label}: {detail}"
        except Exception as exc:  # noqa: BLE001 - every failure is reported, the run continues
            self.failed += 1
            line = f"FAIL  {label}: {type(exc).__name__}: {exc}"
        self.lines.append(line)
        print(line, flush=True)


def envelope(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, f"status {response.status_code} != {status}: {response.text[:200]}"
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "details"}, error
    assert error["code"] == code, error["code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=os.environ.get("IBX_API_URL"))
    parser.add_argument("--token", default=os.environ.get("IBX_API_TOKEN"))
    parser.add_argument("--out", type=Path, default=ROOT / "evidence" / "integration" / "IS_selfcheck.txt")
    args = parser.parse_args(argv)
    if not args.api or not args.token:
        parser.error("--api and --token (or IBX_API_URL / IBX_API_TOKEN) are required")

    auth = {"Authorization": f"Bearer {args.token}"}
    client = httpx.Client(base_url=args.api, headers=auth, timeout=30)
    anon = httpx.Client(base_url=args.api, timeout=30)
    check = Check()

    def is2() -> str:
        spec = anon.get("/openapi.json").json()
        assert spec["openapi"] == "3.1.0", spec["openapi"]
        committed = yaml.safe_load((ROOT / "openapi.yaml").read_text())
        assert spec == committed, "the running service's OpenAPI document differs from openapi.yaml"
        assert any(p.startswith("/api/v1/") for p in spec["paths"])
        return f"openapi {spec['openapi']}, {len(spec['paths'])} paths, matches openapi.yaml"

    def is3() -> str:
        accepted = client.post("/api/v1/jobs", json={"kind": "selfcheck", "payload": {"echo": "is3"}})
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        seen = set()
        for _ in range(120):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            seen.add(job["status"])
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.5)
        assert job["status"] == "succeeded", job
        assert job["progress"] == 100 and "manifest" in job["result"]["links"], job
        return f"202 {job_id} -> {sorted(seen)}, progress 100, links {sorted(job['result']['links'])}"

    def is4() -> str:
        key = f"selfcheck-{uuid.uuid4().hex[:8]}"
        body = {"kind": "selfcheck", "payload": {"echo": key}}
        first = client.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": key})
        second = client.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": key})
        assert first.json()["job_id"] == second.json()["job_id"], "same key gave two jobs"
        assert second.headers.get("Idempotent-Replayed") == "true"
        conflict = client.post("/api/v1/jobs", json={**body, "payload": {"echo": "other"}}, headers={"Idempotency-Key": key})
        envelope(conflict, 409, "IDEMPOTENCY_KEY_REUSED")
        generated = client.post("/api/v1/jobs", json=body)
        assert generated.headers.get("Idempotency-Key", "").startswith("gen-")
        return "same key -> same job_id (replayed); different body -> 409; missing key -> generated + echoed"

    def is5() -> str:
        envelope(client.get("/api/v1/does-not-exist"), 404, "NOT_FOUND")
        envelope(anon.get("/api/v1/errors"), 401, "UNAUTHORIZED")
        secret = "SELFCHECK-CANARY-" + uuid.uuid4().hex[:6]
        bad = client.post("/connector/v1/x/validate", content=b'{"connection": {"credentials": {"token": "' + secret.encode() + b'"}', headers={"Content-Type": "application/json"})
        assert secret not in bad.text
        codes = client.get("/api/v1/errors").json()["codes"]
        assert all(re.fullmatch(r"[A-Z][A-Z0-9_]+", c["code"]) for c in codes)
        return f"404/401/422 use the envelope, input not echoed, {len(codes)} UPPER_SNAKE_CASE codes"

    def is6() -> str:
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ("app/config.py", "app/reliability/redact.py"):
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bsk-[A-Za-z0-9]{8,}|/home/|https?://localhost", text):
                offenders.append(rel)
        assert not offenders, offenders
        example = (ROOT / ".env.example").read_text()
        from app.config import Settings

        missing = [k for k in Settings.model_fields if f"{k}=" not in example]
        assert not missing, missing
        return f"no hard-coded keys/paths/hosts in app/, all {len(Settings.model_fields)} settings in .env.example"

    def is8() -> str:
        health = anon.get("/health")
        assert health.status_code == 200
        ready = anon.get("/ready")
        assert ready.status_code == 200, ready.text[:300]
        echoed = anon.get("/health", headers={"X-Correlation-Id": "selfcheck-corr-1"})
        assert echoed.headers.get("X-Correlation-Id") == "selfcheck-corr-1"
        checks = ready.json()["checks"]
        return f"/health 200, /ready 200 ({', '.join(k for k, v in checks.items() if v['ok'])}), correlation id echoed"

    def is10() -> str:
        from app.batch.runner import load_cases, run_cases

        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "cases.yaml"
            cases.write_text(yaml.safe_dump({"cases": [
                {"name": "health", "path": "/health", "expect": {"status": 200}},
                {"name": "error catalogue", "path": "/api/v1/errors", "expect": {"status": 200}},
                {"name": "unknown connector", "method": "POST", "path": "/connector/v1/nope/validate", "body": {},
                 "expect": {"status": 404, "code": "CONNECTOR_NOT_FOUND"}},
                {"name": "job", "method": "POST", "path": "/api/v1/jobs", "headers": {"Idempotency-Key": "batch-{{run_id}}"},
                 "body": {"kind": "selfcheck"}, "repeat": 2, "expect": {"status": 202}},
            ]}))
            out = Path(tmp) / "results"
            results = run_cases(load_cases(cases), out, client)
            assert (out / "summary.csv").exists()
            folders = [p for p in out.iterdir() if p.is_dir()]
            assert all((f / "request.json").exists() and (f / "response.json").exists() for f in folders)
        assert all(r.passed for r in results), [vars(r) for r in results if not r.passed]
        return f"{len(results)} cases, one folder each + summary.csv, all expectations met"

    for label, fn in [("IS2 OpenAPI 3.1", is2), ("IS3 background jobs", is3), ("IS4 idempotency", is4),
                      ("IS5 error envelope", is5), ("IS6 configuration", is6), ("IS8 logs/health", is8),
                      ("IS10 batch runner", is10)]:
        check.run(label, fn)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    header = f"# Integration Standard self-check, {datetime.now(UTC).isoformat(timespec='seconds')} against {args.api}\n"
    args.out.write_text(header + "\n".join(check.lines) + f"\n# {len(check.lines) - check.failed}/{len(check.lines)} passed\n")
    print(f"{len(check.lines) - check.failed}/{len(check.lines)} passed -> {args.out}")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
