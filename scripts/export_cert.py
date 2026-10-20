"""Export certification evidence packs from the running service into the repo.

    uv run python scripts/export_cert.py --run cert_xxx
    uv run python scripts/export_cert.py --destination dest_xxx        # latest run of that destination
    uv run python scripts/export_cert.py --all-latest                  # latest run of every destination

Writes ``evidence/certification/<connector>/<destination>/<run_id>/`` (run.json, steps/, manifest.snapshot.yaml,
deletion_confirmation.json, summary.md). Files are downloaded through the API's signed links; nothing is typed by hand.
Options: ``--api`` (default $IBX_API_URL or http://127.0.0.1:8088), ``--token`` (default $API_TOKEN or the dev token),
``--out`` (default evidence/certification).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "destination"


def export_run(client: httpx.Client, api: str, run: dict, out: Path) -> Path:
    dest = client.get(f"/api/v1/destinations/{run['destination_id']}").raise_for_status().json()
    folder = out / dest["connector_key"] / slug(dest["name"]) / run["run_id"]
    for name, link in sorted((run.get("evidence") or {}).items()):
        parts = urlsplit(link)
        local = f"{parts.path}?{parts.query}"  # same signed link, through --api (PUBLIC_BASE_URL may differ)
        response = client.get(local, headers={"Authorization": ""})
        response.raise_for_status()
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
    print(f"{run['run_id']}: {run['state']} level={run.get('level_after')} -> {folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder}")
    return folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export certification evidence packs")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run")
    group.add_argument("--destination")
    group.add_argument("--all-latest", action="store_true")
    parser.add_argument("--api", default=os.environ.get("IBX_API_URL", "http://127.0.0.1:8088"))
    parser.add_argument("--token", default=os.environ.get("API_TOKEN", "ibx-dev-token"))
    parser.add_argument("--out", type=Path, default=ROOT / "evidence" / "certification")
    args = parser.parse_args(argv)
    with httpx.Client(base_url=args.api, headers={"Authorization": f"Bearer {args.token}"}, timeout=60) as client:
        if args.run:
            runs = [client.get(f"/api/v1/certifications/{args.run}").raise_for_status().json()]
        elif args.destination:
            runs = client.get("/api/v1/certifications", params={"destination_id": args.destination, "limit": 1}).raise_for_status().json()["runs"]
        else:
            latest: dict[str, dict] = {}
            for run in client.get("/api/v1/certifications", params={"limit": 500}).raise_for_status().json()["runs"]:
                latest.setdefault(run["destination_id"], run)
            runs = list(latest.values())
        finished = [r for r in runs if r["state"] in ("PASSED", "DEMOTED", "FAILED_PRECONDITION", "RESCHEDULED", "INCONCLUSIVE")]
        for run in finished:
            export_run(client, args.api, run, args.out)
    if not finished:
        print("no finished certification runs to export", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
