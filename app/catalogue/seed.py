"""Load the hard-case destinations: ``python -m app.catalogue.seed [--file seed/hard_case_destinations.csv]``.

* default: writes straight to the database from ``DATABASE_URL`` (run it inside the api container:
  ``docker compose exec api python -m app.catalogue.seed``);
* ``--api URL --token T``: posts the CSV to ``POST /api/v1/destinations`` instead (works from the host).
Re-running is safe: an existing destination is reported as ``duplicate``, never created twice.
The mock-only placeholder connections the rows reference (``seed/mock_connections.json``) are created first
when they do not exist yet.
Rows whose connector is not registered are reported as ``error`` and the others still load.
``--certify`` (with ``--api``) then certifies every loaded row and waits: a destination must
be certified (level L1+) before ``/api/v1/publications`` posts to it. In simulation every row passes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "seed" / "hard_case_destinations.csv"
CONNECTIONS_FILE = DEFAULT_FILE.with_name("mock_connections.json")


def _seed_connections() -> list[dict[str, Any]]:
    if not CONNECTIONS_FILE.exists():
        return []
    return json.loads(CONNECTIONS_FILE.read_text(encoding="utf-8"))["connections"]


async def load_db(path: Path) -> list[dict[str, Any]]:
    from pydantic import ValidationError

    from app.catalogue import service
    from app.catalogue.models import DestinationIn
    from app.clock import get_clock
    from app.config import get_settings
    from app.contract.registry import get_registry
    from app.db.migrate import upgrade_head
    from app.db.session import make_engine, make_session_factory
    from app.errors import IbxError

    settings = get_settings()
    engine = make_engine(settings.DATABASE_URL, 2)
    await upgrade_head(engine)
    factory = make_session_factory(engine)
    registry = get_registry()
    out: list[dict[str, Any]] = []
    try:
        from app.catalogue.models import ConnectionIn

        for item in _seed_connections():
            if registry.manifest(item["connector_key"]) is None:
                continue
            async with factory() as session, session.begin():
                if await service.find_connection(session, item["connector_key"], item["ref"]) is None:
                    await service.create_connection(session, registry, ConnectionIn.model_validate(item), settings.fernet_key())
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for n, raw in enumerate(rows, start=1):
            try:
                row = DestinationIn.model_validate(raw)
                async with factory() as session, session.begin():
                    dest, created, warnings = await service.create_destination(session, registry, row, now=get_clock().now())
                out.append({"row": n, "name": dest.name, "status": "created" if created else "duplicate",
                            "destination_id": dest.id, "warnings": warnings})
            except (IbxError, ValidationError) as exc:
                out.append({"row": n, "name": raw.get("name"), "status": "error", "message": str(exc)[:300]})
    finally:
        await engine.dispose()
    return out


def load_api(path: Path, api: str, token: str) -> list[dict[str, Any]]:
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    existing = httpx.get(f"{api.rstrip('/')}/api/v1/connections", headers=headers, timeout=60).json()["connections"]
    refs = {c.get("ref") for c in existing}
    for item in _seed_connections():
        if item["ref"] not in refs:
            httpx.post(f"{api.rstrip('/')}/api/v1/connections", json=item, headers=headers, timeout=60)
    response = httpx.post(f"{api.rstrip('/')}/api/v1/destinations", content=path.read_bytes(), timeout=60,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "text/csv"})
    if response.status_code >= 400:
        raise SystemExit(f"API error {response.status_code}: {response.text[:500]}")
    return [{k: r.get(k) for k in ("row", "status", "destination_id", "warnings", "code", "message")} |
            {"name": (r.get("destination") or {}).get("name")} for r in response.json()["results"]]


def certify_api(rows: list[dict[str, Any]], api: str, token: str, timeout_s: float = 120) -> list[dict[str, Any]]:
    """POST /api/v1/certifications for each loaded row, then poll until every run has finished."""
    import time

    import httpx

    with httpx.Client(base_url=api.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60) as client:
        runs = {}
        for row in rows:
            if row.get("destination_id") and row["status"] in ("created", "duplicate"):
                runs[row["destination_id"]] = client.post("/api/v1/certifications",
                                                          json={"destination_id": row["destination_id"]}).json()["run_id"]
        deadline, results = time.time() + timeout_s, {}
        while len(results) < len(runs) and time.time() < deadline:
            for dest_id, run_id in runs.items():
                if dest_id not in results:
                    run = client.get(f"/api/v1/certifications/{run_id}").json()
                    if run["passed"] is not None:
                        results[dest_id] = {"run_id": run_id, "state": run["state"], "level": run["level_after"]}
            time.sleep(1)
    for row in rows:
        row["certification"] = results.get(row.get("destination_id"), {"state": "not finished"})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--api", help="load through the HTTP API instead of the database")
    parser.add_argument("--token", default=None, help="API bearer token (with --api)")
    parser.add_argument("--certify", action="store_true", help="certify every loaded row and wait (with --api)")
    args = parser.parse_args(argv)
    if args.api:
        import os

        token = args.token or os.environ.get("API_TOKEN", "ibx-dev-token")
        result = load_api(args.file, args.api, token)
        if args.certify:
            result = certify_api(result, args.api, token)
    else:
        result = asyncio.run(load_db(args.file))
    print(json.dumps(result, indent=2))
    return 0 if all(r["status"] in ("created", "duplicate") for r in result) else 1


if __name__ == "__main__":
    sys.exit(main())
