"""Batch runner (IS10): run YAML/JSON test cases against the API and keep every request, response and output.

A case file holds one case, a list of cases, or ``{cases: [...]}``. A case:

    name: publish twice with one key
    method: POST
    path: /connector/v1/sample/publish
    headers: {Idempotency-Key: "k-{{run_id}}"}
    body: {...}
    expect: {status: 200, code: null, job_status: succeeded}   # code = error.code; job_status for 202 jobs
    repeat: 1        # send the same request N times, one after the other (each result recorded)
    parallel: 1      # send N copies at the same time (e.g. concurrent duplicates)

``{{run_id}}`` is the same for the whole run; ``{{uuid}}`` is fresh per occurrence.
A 202 answer with a ``job_id`` is followed: the runner polls ``/api/v1/jobs/{id}`` until ``succeeded`` or
``failed`` (or ``job_timeout_s``), saves ``job.json`` and downloads every ``result.links`` file into ``outputs/``.
Output: ``<out>/<NNN>_<slug>/{request.json, response.json, result.json[, job.json, outputs/]}`` and
``<out>/summary.csv``. The Authorization header is never written to disk.
"""

from __future__ import annotations

import csv
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from app.reliability.redact import redact

SUMMARY_FIELDS = [
    "index", "name", "method", "path", "status", "code", "expected_status", "expected_code",
    "passed", "duration_ms", "correlation_id", "folder", "job_status",
]
TERMINAL = ("succeeded", "failed")


@dataclass
class CaseResult:
    index: int
    name: str
    method: str
    path: str
    status: int | None
    code: str | None
    expected_status: int | None
    expected_code: str | None
    passed: bool
    duration_ms: float
    correlation_id: str | None
    folder: str
    job_status: str | None = None


def load_cases(source: Path) -> list[dict[str, Any]]:
    files = sorted(p for p in source.rglob("*") if p.suffix.lower() in (".yaml", ".yml", ".json")) if source.is_dir() else [source]
    cases: list[dict[str, Any]] = []
    for file in files:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))  # YAML is a superset of JSON
        items = data.get("cases", [data]) if isinstance(data, dict) else data or []
        for item in items:
            if not isinstance(item, dict) or "path" not in item:
                raise ValueError(f"{file}: every case needs at least 'path'")
            item.setdefault("name", f"{file.stem}")
            cases.append(item)
    return cases


def _substitute(value: Any, run_id: str) -> Any:
    if isinstance(value, str):
        value = value.replace("{{run_id}}", run_id)
        return re.sub(r"\{\{uuid\}\}", lambda _m: uuid.uuid4().hex, value)
    if isinstance(value, dict):
        return {k: _substitute(v, run_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, run_id) for v in value]
    return value


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "case"


def run_cases(
    cases: list[dict[str, Any]],
    out: Path,
    client: httpx.Client,
    *,
    run_id: str | None = None,
    poll_interval_s: float = 1.0,
    job_timeout_s: float = 300.0,
) -> list[CaseResult]:
    run_id = run_id or uuid.uuid4().hex[:8]
    out.mkdir(parents=True, exist_ok=True)
    results: list[CaseResult] = []
    for case in cases:
        case = _substitute(case, run_id)
        copies = max(1, int(case.get("parallel", 1)))
        for attempt in range(max(1, int(case.get("repeat", 1)))):
            first = len(results) + 1
            jobs = [(first + i, case, attempt * copies + i, out, client, poll_interval_s, job_timeout_s) for i in range(copies)]
            if copies == 1:
                results.append(_run_one(*jobs[0]))
            else:  # the copies go out at the same time (httpx.Client is thread-safe)
                with ThreadPoolExecutor(max_workers=copies) as pool:
                    results.extend(pool.map(lambda job: _run_one(*job), jobs))
    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for result in results:
            writer.writerow(vars(result))
    return results


def _run_one(
    index: int, case: dict[str, Any], copy: int, out: Path, client: httpx.Client, poll_interval_s: float, job_timeout_s: float
) -> CaseResult:
    method = str(case.get("method", "GET")).upper()
    path = str(case["path"])
    headers = {str(k): str(v) for k, v in (case.get("headers") or {}).items()}
    body = case.get("body")
    several = int(case.get("repeat", 1)) > 1 or int(case.get("parallel", 1)) > 1
    name = str(case["name"]) + (f" #{copy + 1}" if several else "")
    folder = out / f"{index:03d}_{_slug(name)}"
    folder.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    status: int | None = None
    response_body: Any = None
    response_headers: dict[str, str] = {}
    try:
        response = client.request(method, path, headers=headers, json=body if body is not None else None)
        status = response.status_code
        response_headers = dict(response.headers)
        try:
            response_body = response.json()
        except ValueError:
            response_body = response.text
    except httpx.HTTPError as exc:
        response_body = {"transport_error": f"{type(exc).__name__}: {exc}"}
    job_status = None
    if status == 202 and isinstance(response_body, dict) and response_body.get("job_id"):
        job_status = _follow_job(client, response_body, folder, poll_interval_s, job_timeout_s)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    code = None
    if isinstance(response_body, dict) and isinstance(response_body.get("error"), dict):
        code = response_body["error"].get("code")
    expect = case.get("expect") or {}
    expected_status = expect.get("status")
    expected_code = expect.get("code")
    passed = status is not None
    if expected_status is not None:
        passed = passed and status == int(expected_status)
    if "code" in expect:
        passed = passed and code == expected_code
    if "job_status" in expect:
        passed = passed and job_status == expect["job_status"]
    _write(folder / "request.json", {"method": method, "path": path, "headers": headers, "body": body})
    _write(folder / "response.json", {"status": status, "headers": response_headers, "body": response_body})
    result = CaseResult(
        index, name, method, path, status, code, expected_status, expected_code, passed, duration_ms,
        response_headers.get("x-correlation-id"), folder.name, job_status,
    )
    _write(folder / "result.json", vars(result))
    return result


def _follow_job(client: httpx.Client, accepted: dict[str, Any], folder: Path, poll_interval_s: float, timeout_s: float) -> str:
    """Poll a 202 job to a terminal state, save job.json and download its result links into outputs/."""
    url = accepted.get("status_url") or f"/api/v1/jobs/{accepted['job_id']}"
    deadline = time.monotonic() + timeout_s
    job: dict[str, Any] = {}
    while True:
        try:
            job = client.get(url).json()
        except (httpx.HTTPError, ValueError) as exc:
            job = {"job_id": accepted["job_id"], "status": "unreachable", "error": f"{type(exc).__name__}"}
            break
        if job.get("status") in TERMINAL or time.monotonic() > deadline:
            break
        time.sleep(poll_interval_s)
    if job.get("status") not in TERMINAL and job.get("status") != "unreachable":
        job["status"] = "timeout"
    _write(folder / "job.json", job)
    links = ((job.get("result") or {}).get("links") or {}) if isinstance(job.get("result"), dict) else {}
    outputs = folder / "outputs"
    for name, link in links.items():
        outputs.mkdir(exist_ok=True)
        filename = urlsplit(str(link)).path.rsplit("/", 1)[-1]
        target = outputs / (filename if "." in filename else f"{_slug(name)}.bin")
        try:
            download = client.get(str(link))
            target.write_bytes(download.content if download.status_code == 200 else b"")
        except httpx.HTTPError:
            target.write_bytes(b"")
    return str(job.get("status"))


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(redact(data), indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
