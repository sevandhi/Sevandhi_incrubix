"""ADV1 proof: 1,000-row catalogue import against the mock internet, 3 runs, plus a dry run of the real seed catalogue.

    # stack up first (simulation; the worker reaches the mock internet at http://mock-platforms:8080/internet)
    uv run python scripts/prove_adv1.py --api http://127.0.0.1:8088 --token "$API_TOKEN"
    uv run python scripts/prove_adv1.py --seed-only            # only the offline dry run of catalogue/seeds/seed_v1.csv

Each run generates a fresh fixture (``mocks.platforms.routes.internet.generate_fixture``: about 10 % duplicates,
bad URLs, mixed languages, 5 % hanging sites, 200-2000 ms latency per request), uploads it to
``POST /api/v1/catalogue/imports``, follows the job and downloads the row-by-row report. Every row's status is
compared with the outcome the generator expects.

Writes:
* ``evidence/metrics/catalogue_import.csv``: one line per run (rows, imported, duplicates, errors, seconds, hardware);
* ``evidence/levels/adv/ADV1_run<N>_report.csv``: the service's report for each run; ``ADV1_fixture_run1.csv``;
* ``evidence/levels/adv/ADV1_seed_dryrun_report.csv`` + ``ADV1_seed_dryrun_summary.json``: the real seed catalogue
  through the importer with ``dry_run`` (validate, map, normalise, de-duplicate, score; no network, no database).
Exit code 1 when a run fails, takes 300 s or more, or a row's status differs from the fixture's expectation.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import platform
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mocks.platforms.routes import internet  # noqa: E402

METRICS = ROOT / "evidence" / "metrics" / "catalogue_import.csv"
ADV = ROOT / "evidence" / "levels" / "adv"
TARGET_S = 300


def hardware() -> str:
    cpu = "unknown cpu"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
        mem_kb = next(int(x.split()[1]) for x in Path("/proc/meminfo").read_text().splitlines() if x.startswith("MemTotal"))
        mem = f"{mem_kb / 1024 / 1024:.0f} GiB RAM"
    except (OSError, StopIteration, ValueError):
        mem = "RAM unknown"
    return f"{cpu}; {os.cpu_count()} logical CPUs; {mem}; {platform.system()} {platform.release()}; api+worker+mock in docker compose"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def seed_dry_run() -> dict:
    from app.catalogue.importer import Importer, parse_csv, report_csv
    from app.config import Settings
    from app.contract.registry import get_registry

    seed = ROOT / "catalogue" / "seeds" / "seed_v1.csv"
    report = asyncio.run(Importer(Settings(_env_file=None, APP_ENV="test"), get_registry()).run(
        parse_csv(seed.read_bytes()), dry_run=True))
    ADV.mkdir(parents=True, exist_ok=True)
    (ADV / "ADV1_seed_dryrun_report.csv").write_text(report_csv(report), encoding="utf-8")
    rows = report["rows"]
    summary = {
        "generated_at": now(), "file": "catalogue/seeds/seed_v1.csv", "mode": "dry_run (no network, no database)",
        **{k: v for k, v in report.items() if k != "rows"},
        "by_connector_surface": dict(Counter(f"{r['connector_key']}|{r['surface']}" for r in rows if r["connector_key"])),
        "relevance_histogram": dict(sorted(Counter(str(r["relevance_score"]) for r in rows if r["relevance_score"] != "").items())),
        "invalid_rows": [{"row_no": r["row_no"], "name": r["name"], "code": r["code"]} for r in rows if r["status"] == "invalid"],
    }
    (ADV / "ADV1_seed_dryrun_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("seed dry run:", json.dumps(report["summary"]))
    return summary


def one_run(client: httpx.Client, n: int, rows: int, internet_base: str, job_timeout: float) -> dict:
    tag = f"r{n}t{int(time.time()) % 1_000_000}"
    text, expected = internet.generate_fixture(rows, base_url=internet_base, run_tag=tag, seed=1000 + n)
    if n == 1:
        (ADV / "ADV1_fixture_run1.csv").write_text(text, encoding="utf-8")
    started = time.perf_counter()
    response = client.post("/api/v1/catalogue/imports", files={"file": (f"fixture_{tag}.csv", text.encode("utf-8-sig"), "text/csv")})
    response.raise_for_status()
    body = response.json()
    job: dict = {}
    while time.perf_counter() - started < job_timeout:
        job = client.get(body["status_url"]).json()
        if job.get("status") in ("succeeded", "failed"):
            break
        time.sleep(0.5)
    wall = round(time.perf_counter() - started, 1)
    status = client.get(body["import_url"]).json()
    report_text = client.get(body["report_url"]).text if job.get("status") == "succeeded" else ""
    (ADV / f"ADV1_run{n}_report.csv").write_text(report_text, encoding="utf-8")
    report = list(csv.DictReader(io.StringIO(report_text)))
    by_row = {int(r["row_no"]): r for r in report}
    mismatches = []
    for exp in expected:
        got = by_row.get(exp["row_no"], {}).get("status")
        want = {"imported": ("ok", "warning")}.get(exp["expected"], (exp["expected"],))
        if got not in want:
            mismatches.append({"row_no": exp["row_no"], "expected": exp["expected"], "got": got,
                               "code": by_row.get(exp["row_no"], {}).get("code")})
    summary = status.get("summary") or {}
    timings = status.get("timings") or {}
    result = {
        "generated_at": now(), "run": n, "import_id": body["import_id"], "job_status": job.get("status"),
        "rows": summary.get("rows"), "imported": summary.get("imported"), "ok": summary.get("ok"),
        "warning": summary.get("warning"), "duplicates": summary.get("duplicate"), "invalid": summary.get("invalid"),
        "unreachable": summary.get("unreachable"), "errors_total": (summary.get("invalid") or 0) + (summary.get("unreachable") or 0),
        "seconds_wall": wall, "seconds_importer": timings.get("total_s"), "seconds_network": timings.get("network_s"),
        "network_requests": status.get("network_requests"), "rows_matching_fixture": len(expected) - len(mismatches),
        "language_detector": status.get("language_detector"), "target_s": TARGET_S,
        "pass": job.get("status") == "succeeded" and wall < TARGET_S and not mismatches, "hardware": hardware(),
    }
    print(json.dumps({k: v for k, v in result.items() if k != "hardware"}))
    if mismatches:
        print("  mismatches:", json.dumps(mismatches[:20]))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="ADV1 proof (see the module docstring)")
    parser.add_argument("--api", default=os.environ.get("IBX_API_URL", "http://127.0.0.1:8088"))
    parser.add_argument("--token", default=os.environ.get("API_TOKEN", "ibx-dev-token"))
    parser.add_argument("--internet-base", default="http://mock-platforms:8080",
                        help="mock-platforms as the worker sees it (inside compose)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--job-timeout", type=float, default=900)
    parser.add_argument("--seed-only", action="store_true")
    args = parser.parse_args()
    ADV.mkdir(parents=True, exist_ok=True)
    seed_dry_run()
    if args.seed_only:
        return 0
    results = []
    with httpx.Client(base_url=args.api, headers={"Authorization": f"Bearer {args.token}"}, timeout=60) as client:
        for n in range(1, args.runs + 1):
            results.append(one_run(client, n, args.rows, args.internet_base, args.job_timeout))
    METRICS.parent.mkdir(parents=True, exist_ok=True)
    with METRICS.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(results)
    print(f"wrote {METRICS}")
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
