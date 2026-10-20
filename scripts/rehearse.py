"""Hidden-test rehearsal: run test_pack/inputs/rehearsal/HC{1..7}_*.yaml through the batch runner and grade each case
4 / 2 / 0 the way the screening does (4 = handled correctly and says what it did, 2 = handled with a clear flaw,
0 = wrong, crash or silent failure).

    uv run python scripts/rehearse.py [--api URL] [--token T] [--mock URL] [--out DIR]
    scripts/prove_rehearsal.sh        (brings a stack up unless IBX_API_URL points at one)

A rehearsal file:
    hard_case: HC1
    title: ...
    setup:                                   # done here, before the batch runner; recorded in setup.json
      connections: {name: {connector_key, credentials, ...}}           -> {{conn.name}}
      destinations: {name: {row: {...}, certify: true}}                -> {{dest.name}}
      imports: {name: {csv: "..."}}          # POST text/csv to /api/v1/catalogue/imports, waited for -> {{import.name}}
    cases:                                   # batch-runner cases (IS10), plus:
      - name / method / path / headers / body / expect / repeat / parallel
        role: step                           # a setup step: run, not graded
        grade:                               # extra checks on top of expect (status, code, job_status)
          equals:   {dotted.path: value}     contains: {dotted.path or "": substring}
          present:  [dotted.path]            absent_text: [text]     headers: [name]
          min_len:  {dotted.path: n}         same_as: {dotted.path: "<other case name>"}
          copies_same: [dotted.path]         copies_sorted: {dotted.path: [values]}
          job_equals: {dotted.path: value}   job_contains: [text]    job_sum: {total: path, parts: [paths]}
          mock_counts: [{platform, tenant, route, equals}]            (checked after the whole run)
          followup: [{path, status, equals, contains, live: true}]    (after the run; {{resp:dotted.path}} = this
                                                                       case's response; live: GET the page at live_url)
Placeholders: {{run_id}}, {{mock}} (mock-platforms from this host), {{conn.x}}, {{dest.x}}, {{import.x}}.
Outputs: <out>/inputs (rendered cases), <out>/results (the batch runner's folders + summary.csv), <out>/grades.json;
evidence/hard_cases/rehearsal_summary.csv; evidence/hard_cases/HC{n}/ (request/response/result of every case);
evidence/proof_runs/REHEARSAL.json. Exit 0 if every case scored 4, 2 if every hard case scored > 0, else 1.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prove_lib import MOCK_IN, Proof  # noqa: E402

PLACEHOLDER = re.compile(r"\{\{(run_id|mock|conn\.[a-z0-9_]+|dest\.[a-z0-9_]+|import\.[a-z0-9_]+)\}\}")
MISSING = object()


def get(data: Any, path: str) -> Any:
    if path in ("", "."):
        return data
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return MISSING
    return data


def render(value: Any, names: dict[str, str]) -> Any:
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: names.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {render(k, names): render(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, names) for v in value]
    return value


class Rehearsal:
    def __init__(self, api: str, token: str, mock: str, out: Path) -> None:
        self.api, self.token, self.mock, self.out = api.rstrip("/"), token, mock.rstrip("/"), out
        self.client = httpx.Client(base_url=self.api, headers={"Authorization": f"Bearer {token}", "X-IncuBrix-Demo-Token": token}, timeout=120)
        self.run_id = uuid.uuid4().hex[:8]
        self.names: dict[str, str] = {"run_id": self.run_id, "mock": self.mock}
        self.setup_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ setup
    def wait_job(self, job_id: str, timeout_s: float = 240) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            job = self.client.get(f"/api/v1/jobs/{job_id}").json()
            if job.get("status") in ("succeeded", "failed") or time.monotonic() > deadline:
                return job
            time.sleep(0.5)

    def setup(self, hc: str, spec: dict[str, Any]) -> None:
        for name, conn in (spec.get("connections") or {}).items():
            r = self.client.post("/api/v1/connections", json=render(conn, self.names))
            self.names[f"conn.{name}"] = r.json().get("connection_id", "missing")
            self.setup_log.append({"hc": hc, "connection": name, "status": r.status_code, "id": self.names[f"conn.{name}"]})
        for name, dest in (spec.get("destinations") or {}).items():
            row = render(dest["row"], self.names)
            r = self.client.post("/api/v1/destinations", json=row)
            dest_id = r.json().get("destination_id", "missing")
            self.names[f"dest.{name}"] = dest_id
            entry: dict[str, Any] = {"hc": hc, "destination": name, "status": r.status_code, "id": dest_id, "url": row.get("url")}
            if dest.get("certify") and r.status_code in (200, 201):
                started = self.client.post("/api/v1/certifications", json={"destination_id": dest_id}).json()
                self.wait_job(started["job_id"])
                run = self.client.get(f"/api/v1/certifications/{started['run_id']}").json()
                entry.update(certification=run.get("state"), level=run.get("level_after"))
            self.setup_log.append(entry)
        for name, item in (spec.get("imports") or {}).items():
            r = self.client.post("/api/v1/catalogue/imports", content=render(item["csv"], self.names).encode(),
                                 headers={"Content-Type": "text/csv"})
            job_id = r.json().get("job_id", "missing")
            if r.status_code == 202:
                self.wait_job(job_id)
            self.names[f"import.{name}"] = job_id
            self.setup_log.append({"hc": hc, "import": name, "status": r.status_code, "job_id": job_id})

    # ------------------------------------------------------------------ run
    def run(self, files: list[Path]) -> list[dict[str, Any]]:
        inputs = self.out / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        specs: list[dict[str, Any]] = []
        for path in files:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.setup(doc["hard_case"], doc.get("setup") or {})
            cases = render(doc["cases"], self.names)
            for case in cases:
                # a live demo guards /sim with its own token; send it separately so Authorization can carry
                # the simulated platform's credentials (e.g. a mock Lemmy admin)
                if self.token and str(case.get("path", "")).startswith(self.mock):
                    case.setdefault("headers", {})["X-IncuBrix-Demo-Token"] = self.token
                specs.append({"hc": doc["hard_case"], "title": doc.get("title", ""), **case})
            (inputs / path.name).write_text(yaml.safe_dump({"cases": cases}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        (self.out / "setup.json").write_text(json.dumps(self.setup_log, indent=1) + "\n", encoding="utf-8")
        results = self.out / "results"
        cmd = [sys.executable, "-m", "app.batch", "run", str(inputs), "--out", str(results), "--api", self.api,
               "--token", self.token, "--job-timeout", "240"]
        batch = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=3600)
        print(batch.stdout.strip() or batch.stderr[-800:], flush=True)
        rows = list(csv.DictReader((results / "summary.csv").open(encoding="utf-8")))
        return self.grade(specs, rows, results)

    # ------------------------------------------------------------------ grading
    def grade(self, specs: list[dict[str, Any]], rows: list[dict[str, Any]], results: Path) -> list[dict[str, Any]]:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            base = re.sub(r" #\d+$", "", row["name"])
            folder = results / row["folder"]
            row["response"] = json.loads((folder / "response.json").read_text(encoding="utf-8"))
            job_file = folder / "job.json"
            row["job"] = json.loads(job_file.read_text(encoding="utf-8")) if job_file.exists() else None
            by_name.setdefault(base, []).append(row)
        graded = []
        for spec in specs:
            copies = by_name.get(spec["name"], [])
            graded.append(self.grade_case(spec, copies, by_name))
        return graded

    def grade_case(self, spec: dict[str, Any], copies: list[dict[str, Any]], by_name: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        expect = spec.get("expect") or {}
        checks: list[tuple[str, bool]] = []
        statuses = [int(c["status"]) if c["status"] else None for c in copies]
        codes = [c["code"] or None for c in copies]
        status_ok = bool(copies) and all(s == expect.get("status") for s in statuses) if "status" in expect else bool(copies)
        code_ok = bool(copies) and all(c == expect.get("code") for c in codes) if "code" in expect else True
        job_ok = all(c.get("job_status") == expect["job_status"] for c in copies) if "job_status" in expect else True
        crashed = not copies or any(s is None or s == 500 for s in statuses)
        grade = spec.get("grade") or {}
        first = copies[0]["response"] if copies else {}
        body, headers = first.get("body"), {k.lower(): v for k, v in (first.get("headers") or {}).items()}
        text = json.dumps(body, ensure_ascii=False)
        for path, value in (grade.get("equals") or {}).items():
            got = get(body, path)
            checks.append((f"{path} == {value!r} (got {got if got is not MISSING else 'missing'!r})", got == value))
        for path, sub in (grade.get("contains") or {}).items():
            got = get(body, path)
            hay = text if path == "" else (json.dumps(got, ensure_ascii=False) if not isinstance(got, str) else got)
            checks.append((f"{path or 'body'} contains {sub!r}", got is not MISSING and str(sub).lower() in hay.lower()))
        for path in grade.get("present") or []:
            got = get(body, path)
            checks.append((f"{path} present", got is not MISSING and got not in (None, "", [], {})))
        for needle in grade.get("absent_text") or []:
            checks.append((f"response does not contain {needle!r}", needle not in text))
        for name in grade.get("headers") or []:
            checks.append((f"header {name}", name.lower() in headers))
        for path, n in (grade.get("min_len") or {}).items():
            got = get(body, path)
            checks.append((f"len({path}) >= {n}", isinstance(got, list | str | dict) and len(got) >= n))
        for path, other in (grade.get("same_as") or {}).items():
            ref = by_name.get(other, [])
            want = get(ref[0]["response"].get("body"), path) if ref else MISSING
            got = get(body, path)
            checks.append((f"{path} same as in '{other}'", want is not MISSING and got == want))
        for path in grade.get("copies_same") or []:
            values = [json.dumps(get(c["response"].get("body"), path)) for c in copies]
            checks.append((f"every copy has the same {path}", len(set(values)) == 1 and "null" not in values))
        for path, want in (grade.get("copies_sorted") or {}).items():
            values = sorted(str(get(c["response"].get("body"), path)) for c in copies)
            checks.append((f"copies' {path} == {want}", values == sorted(want)))
        job = copies[0].get("job") if copies else None
        for path, value in (grade.get("job_equals") or {}).items():
            got = get(job, path) if job else MISSING
            checks.append((f"job {path} == {value!r} (got {got if got is not MISSING else 'missing'!r})", got == value))
        for needle in grade.get("job_contains") or []:
            checks.append((f"job result mentions {needle!r}", bool(job) and needle in json.dumps(job)))
        if grade.get("job_sum"):
            total, parts = grade["job_sum"]["total"], grade["job_sum"]["parts"]
            values = [get(job or body, p) for p in [total, *parts]]
            ok = all(isinstance(v, int) for v in values) and values[0] == sum(values[1:])
            checks.append((f"{total} == {' + '.join(parts)} ({values})", ok))
        for item in grade.get("mock_counts") or []:
            data = httpx.get(f"{self.mock}/_counts", params={"platform": item["platform"], "tenant": item["tenant"]}).json()
            got = data["counts"].get(item["platform"], {}).get(item["tenant"], {}).get(item["route"], 0)
            checks.append((f"platform count {item['platform']}/{item['tenant']} {item['route']} == {item['equals']} (got {got})",
                           got == item["equals"]))
        for item in grade.get("followup") or []:
            path = re.sub(r"\{\{resp:([a-zA-Z0-9_.]+)\}\}", lambda m: str(get(body, m.group(1))), item.get("path", ""))
            if item.get("live"):
                pub = self.client.get(path).json() if path else body
                url = str(get(pub, "live_url") if get(pub, "live_url") is not MISSING else "")
                got_r = httpx.get(url.replace(MOCK_IN, self.mock), timeout=30) if url.startswith("http") else None
                ftext, fbody, fstatus = (got_r.text if got_r else ""), None, (got_r.status_code if got_r else None)
            else:
                got_r = self.client.get(path)
                fstatus, ftext = got_r.status_code, got_r.text
                try:
                    fbody = got_r.json()
                except ValueError:
                    fbody = None
            if "status" in item:
                checks.append((f"GET {path or 'live page'} -> {item['status']} (got {fstatus})", fstatus == item["status"]))
            for p_, v in (item.get("equals") or {}).items():
                got = get(fbody, p_)
                checks.append((f"GET {path}: {p_} == {v!r} (got {got if got is not MISSING else 'missing'!r})", got == v))
            for sub in item.get("contains") or []:
                checks.append((f"GET {path or 'live page'} contains {sub!r}", sub.lower() in ftext.lower()))
        checks_ok = all(ok for _, ok in checks)
        base = status_ok and code_ok and job_ok
        expected_success = expect.get("code") is None and (expect.get("status") or 200) < 400
        if base and checks_ok:
            score = 4
        elif base or (not crashed and (status_ok or code_ok) and not (expected_success and not status_ok)):
            score = 2
        else:
            score = 0
        return {
            "hard_case": spec["hc"], "case": spec["name"], "role": spec.get("role", "graded"),
            "method": spec.get("method", "GET"), "path": spec.get("path"),
            "expected_status": expect.get("status"), "got_status": ";".join(str(s) for s in statuses),
            "expected_code": expect.get("code"), "got_code": ";".join(str(c) for c in codes),
            "expected_job_status": expect.get("job_status"), "got_job_status": ";".join(str(c.get("job_status")) for c in copies),
            "checks_passed": sum(ok for _, ok in checks), "checks_total": len(checks),
            "failed_checks": " | ".join(label for label, ok in checks if not ok)[:900],
            "score": score if spec.get("role") != "step" else "",
            "step_ok": (base if spec.get("role") == "step" else ""),
            "folders": ";".join(c["folder"] for c in copies),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hidden-test rehearsal with 4/2/0 grading")
    parser.add_argument("--api", default=os.environ.get("IBX_API_URL") or f"http://127.0.0.1:{os.environ.get('IBX_API_PORT', '8088')}")
    parser.add_argument("--token", default=os.environ.get("API_TOKEN") or os.environ.get("IBX_API_TOKEN") or "ibx-dev-token")
    parser.add_argument("--mock", default=os.environ.get("IBX_MOCK_URL") or f"http://127.0.0.1:{os.environ.get('IBX_MOCK_PORT', '8089')}")
    parser.add_argument("--cases", type=Path, default=ROOT / "test_pack/inputs/rehearsal")
    parser.add_argument("--out", type=Path, default=ROOT / "test_pack/outputs/rehearsal/latest")
    parser.add_argument("--summary", type=Path, default=ROOT / "evidence/hard_cases/rehearsal_summary.csv")
    args = parser.parse_args(argv)
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)
    files = sorted(args.cases.glob("HC*_*.yaml"))
    rehearsal = Rehearsal(args.api, args.token, args.mock, args.out)
    graded = rehearsal.run(files)
    (args.out / "grades.json").write_text(json.dumps(graded, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    scored = [g for g in graded if g["role"] != "step"]
    per_hc: dict[str, list[int]] = {}
    for g in scored:
        per_hc.setdefault(g["hard_case"], []).append(int(g["score"]))
    fields = ["hard_case", "case", "role", "method", "path", "expected_status", "got_status", "expected_code", "got_code",
              "expected_job_status", "got_job_status", "checks_passed", "checks_total", "failed_checks", "score", "step_ok", "folders"]
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with args.summary.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for g in graded:
            writer.writerow(g)
        for hc, scores in sorted(per_hc.items()):
            writer.writerow({"hard_case": hc, "case": f"TOTAL {hc}: {len(scores)} cases, mean {sum(scores) / len(scores):.2f}, "
                             f"4s={scores.count(4)} 2s={scores.count(2)} 0s={scores.count(0)}", "role": "summary",
                             "score": f"{sum(scores) / len(scores):.2f}"})
        allscores = [s for v in per_hc.values() for s in v]
        writer.writerow({"hard_case": "ALL", "case": f"TOTAL: {len(allscores)} graded cases, mean {sum(allscores) / max(1, len(allscores)):.2f}; "
                         f"run {rehearsal.run_id} at {stamp} against {args.api}", "role": "summary",
                         "score": f"{sum(allscores) / max(1, len(allscores)):.2f}"})
    # evidence copies per hard case (request/response/result/job of every case)
    results = args.out / "results"
    for g in graded:
        target = ROOT / "evidence/hard_cases" / g["hard_case"]
        for folder in filter(None, g["folders"].split(";")):
            if (results / folder).exists():
                shutil.copytree(results / folder, target / folder, dirs_exist_ok=True)
    proof = Proof("REHEARSAL", "Hidden-test rehearsal HC1-HC7 (batch runner, auto-graded 4/2/0)", "scripts/prove_rehearsal.sh")
    proof._mode = "n/a"
    for g in graded:
        if g["role"] == "step":
            proof.check(f"setup step {g['case']}", bool(g["step_ok"]), f"{g['got_status']} {g['got_code']}", critical=False)
        else:
            proof.check(f"{g['case']} scored {g['score']}/4", g["score"] == 4,
                        f"status {g['got_status']} code {g['got_code']}; {g['failed_checks'][:250]}", critical=g["score"] == 0)
    for hc, scores in sorted(per_hc.items()):
        status = "Yes" if all(s == 4 for s in scores) else ("Partial" if any(s > 0 for s in scores) else "No")
        bad = [g for g in scored if g["hard_case"] == hc and g["score"] != 4]
        proof.set_sub(hc, status, [f"{g['case']}: {g['score']}/4 ({g['failed_checks'][:160] or g['got_status'] + ' ' + g['got_code']})"
                                   for g in bad], [f"evidence/hard_cases/{hc}/", "evidence/hard_cases/rehearsal_summary.csv"])
    proof.evidence += ["evidence/hard_cases/rehearsal_summary.csv", "test_pack/outputs/rehearsal/latest/"]
    proof.log(f"run {rehearsal.run_id}; {len(scored)} graded cases; mean {sum(allscores) / max(1, len(allscores)):.2f}")
    print(f"rehearsal: {len(scored)} graded cases; " + "; ".join(
        f"{hc} mean {sum(v) / len(v):.2f} ({v.count(4)}x4 {v.count(2)}x2 {v.count(0)}x0)" for hc, v in sorted(per_hc.items())))
    return proof.finish("evidence/hard_cases/rehearsal.log")


if __name__ == "__main__":
    sys.exit(main())
