"""CLI for the batch runner. Exit code 1 if any case did not meet its ``expect`` (with --strict)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from app.batch.runner import load_cases, run_cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.batch")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run test cases against the API")
    run.add_argument("source", type=Path, help="a YAML/JSON case file or a folder of them")
    run.add_argument("--out", type=Path, default=Path("results"))
    run.add_argument("--api", default=os.environ.get("IBX_API_URL"), help="API base URL (or IBX_API_URL)")
    run.add_argument("--token", default=os.environ.get("IBX_API_TOKEN"), help="bearer token (or IBX_API_TOKEN)")
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--job-timeout", type=float, default=300.0, help="seconds to follow a 202 job to its end")
    run.add_argument("--strict", action="store_true", help="exit 1 if any case fails its expectations")
    args = parser.parse_args(argv)

    if not args.api:
        parser.error("--api (or IBX_API_URL) is required")
    cases = load_cases(args.source)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(base_url=args.api, headers=headers, timeout=args.timeout) as client:
        results = run_cases(cases, args.out, client, job_timeout_s=args.job_timeout)
    passed = sum(r.passed for r in results)
    print(f"{passed}/{len(results)} cases passed; summary: {args.out / 'summary.csv'}")
    return 1 if args.strict and passed != len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
