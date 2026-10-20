"""Demo destinations for the all-in-one live demo: seed, certify, and reset them.

* ``DEMO_FILES``: the hard-case rows (``seed/hard_case_destinations.csv``: dest-weekly-cap-1, dest-no-promo,
  dest-ai-disclosure-es, dest-partner-revocable, dest-strict-limits) plus one row per connector
  (``seed/demo_connector_destinations.csv``). All of them point at mock-platforms (simulation only).
* ``seed()`` is idempotent: a row that exists is left as it is (a destination demoted by a reviewer's test stays
  demoted across restarts); only rows that were never certified get a certification run.
* ``reset()`` (``POST /api/v1/sim/reset``) puts the demo back to its starting point: the demo destinations and
  their publish history are deleted and created again, every queued mock-platform fault is dropped, and circuit
  breakers, rate-limit state and kill switches are cleared. Test-post audit rows, alerts and the mock platforms'
  stored posts, logins and request counters are kept: they are history, and connectors cache their mock logins and
  community ids (wiping the mock store would make those caches stale).
"""

from __future__ import annotations

import asyncio
import csv
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.contract.registry import Registry
from app.db.session import advisory_key
from app.db.tables import (
    CertificationRunRow,
    CircuitStateRow,
    ConnectorFlagRow,
    DestinationRow,
    JobRow,
    PublishIntentRow,
    RateLimitStateRow,
)
from app.errors import ErrorCode, IbxError
from app.jobs import queue

ROOT = Path(__file__).resolve().parents[2]
DEMO_FILES = (ROOT / "seed" / "hard_case_destinations.csv", ROOT / "seed" / "demo_connector_destinations.csv")
CONNECTIONS_FILE = ROOT / "seed" / "mock_connections.json"
SEED_JOB_KIND = "demo.seed"
SEED_LOCK = advisory_key("ibx:demo-seed")


def demo_allowed(settings: Settings) -> bool:
    """Seeding and resetting only ever happen in simulation, outside prod (every call goes to mock-platforms)."""
    return settings.SIMULATION_MODE and settings.APP_ENV != "prod"


def refuse_unless_allowed(settings: Settings) -> None:
    if not demo_allowed(settings):
        raise IbxError(ErrorCode.SIMULATION_ONLY,
                       "The demo reset is only available in simulation mode (SIMULATION_MODE=true, APP_ENV demo/dev/test).")


def demo_rows(files: tuple[Path, ...] = DEMO_FILES) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in files:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows.extend(dict(r) for r in csv.DictReader(fh))
    return rows


def demo_keys(registry: Registry, rows: list[dict[str, Any]]) -> dict[str, str]:
    """{destination_key: name} for the demo rows, computed exactly as the catalogue computes it."""
    from app.catalogue.models import DestinationIn
    from app.catalogue.service import destination_key, instance_for

    keys: dict[str, str] = {}
    for raw in rows:
        row = DestinationIn.model_validate(raw)
        manifest = registry.manifest(row.connector_key)
        if manifest is None:
            continue
        surface = row.surface or manifest.surfaces[0].key
        keys[destination_key(manifest.key, instance_for(manifest, row)[manifest.instance_param], surface)] = row.name
    return keys


async def _ensure_connections(session: AsyncSession, registry: Registry, settings: Settings) -> None:
    from app.catalogue import service
    from app.catalogue.models import ConnectionIn

    if not CONNECTIONS_FILE.exists():
        return
    for item in json.loads(CONNECTIONS_FILE.read_text(encoding="utf-8"))["connections"]:
        if registry.manifest(item["connector_key"]) is None:
            continue
        if await service.find_connection(session, item["connector_key"], item["ref"]) is None:
            await service.create_connection(session, registry, ConnectionIn.model_validate(item), settings.fernet_key())


async def seed(
    factory: async_sessionmaker[AsyncSession], registry: Registry, settings: Settings, *, now: datetime, certify: bool = True,
) -> dict[str, Any]:
    """Create the demo destinations that do not exist yet and certify every one that was never certified."""
    from app.catalogue import service
    from app.catalogue.models import DestinationIn
    from app.certification.harness import create_run

    out: dict[str, Any] = {"destinations": {}, "created": [], "certifications": {}, "errors": {}}
    async with factory() as session, session.begin():
        await _ensure_connections(session, registry, settings)
    for raw in demo_rows():
        name = raw.get("name") or "?"
        try:
            async with factory() as session, session.begin():
                dest, created, _warnings = await service.create_destination(
                    session, registry, DestinationIn.model_validate(raw), now=now, confirmed_by="demo_seed")
                out["destinations"][dest.name] = dest.id
                if created:
                    out["created"].append(dest.name)
                if not certify or dest.level != "L0" or dest.status != "active":
                    continue
                ever = (await session.execute(select(CertificationRunRow.id).where(
                    CertificationRunRow.destination_id == dest.id).limit(1))).scalar_one_or_none()
                if ever is not None:  # certified before (and maybe demoted by a test since): leave it as it is
                    continue
                run = await create_run(session, dest.id, "demo_seed")
                job = await queue.enqueue(session, "certification.run", {"run_id": run.id}, now=now)
                run.job_id = job.id
                out["certifications"][dest.name] = run.id
        except IbxError as exc:
            out["errors"][name] = f"{exc.code.value}: {exc.message}"
    return out


async def reset(
    factory: async_sessionmaker[AsyncSession], registry: Registry, settings: Settings, *, http: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Delete the demo destinations + their history and clear the simulated platforms (see the module docstring)."""
    refuse_unless_allowed(settings)
    keys = demo_keys(registry, demo_rows())
    own = http is None
    client = http or httpx.AsyncClient()
    faults_cleared = False
    try:
        response = await client.delete(f"{settings.MOCK_PLATFORM_URL.rstrip('/')}/_faults", timeout=10)
        faults_cleared = response.status_code == 200
    except httpx.HTTPError:
        faults_cleared = False
    finally:
        if own:
            await client.aclose()
    async with factory() as session, session.begin():
        ids = list((await session.execute(select(DestinationRow.id).where(DestinationRow.destination_key.in_(list(keys))))).scalars())
        intents = await session.execute(delete(PublishIntentRow).where(PublishIntentRow.destination_key.in_(list(keys))))
        await session.execute(delete(DestinationRow).where(DestinationRow.id.in_(ids)))  # rules, runs, publications cascade
        await session.execute(delete(CircuitStateRow))
        await session.execute(delete(RateLimitStateRow))
        await session.execute(delete(ConnectorFlagRow))
    return {"destinations_deleted": len(ids), "publish_intents_deleted": intents.rowcount or 0, "mock_faults_cleared": faults_cleared,
            "cleared": ["circuit_state", "rate_limit_state", "connector_flags"]}


async def wait_for_runs(
    factory: async_sessionmaker[AsyncSession], run_ids: dict[str, str], timeout_s: float,
    progress: Callable[[int, str], Awaitable[None]] | None = None, poll_s: float = 1.0,
) -> dict[str, dict[str, Any]]:
    """Poll the certification runs until each one is terminal (or the timeout). {name: {state, level_after}}."""
    from app.certification.harness import TERMINAL_STATES

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    results: dict[str, dict[str, Any]] = {}
    while True:
        async with factory() as session:
            for name, run_id in run_ids.items():
                run = await session.get(CertificationRunRow, run_id)
                if run is not None and run.state in TERMINAL_STATES:
                    results[name] = {"run_id": run_id, "state": run.state, "level_after": (run.result or {}).get("level_after")}
        if progress is not None and run_ids:
            await progress(10 + int(85 * len(results) / len(run_ids)), f"{len(results)}/{len(run_ids)} demo destinations certified")
        if len(results) == len(run_ids) or loop.time() >= deadline:
            break
        await asyncio.sleep(poll_s)
    for name, run_id in run_ids.items():
        results.setdefault(name, {"run_id": run_id, "state": "not finished yet", "level_after": None})
    return results


async def enqueue_seed_once(factory: async_sessionmaker[AsyncSession], *, now: datetime, reset_first: bool = False,
                            wait_s: float = 0) -> str | None:
    """Queue one ``demo.seed`` job unless one is already queued or running (several instances may start at once)."""
    from sqlalchemy import text

    async with factory() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SEED_LOCK})
        busy = (await session.execute(select(JobRow.id).where(JobRow.kind == SEED_JOB_KIND,
                                                               JobRow.status.in_(("queued", "running"))).limit(1))).scalar_one_or_none()
        if busy is not None and not reset_first:
            return None
        job = await queue.enqueue(session, SEED_JOB_KIND, {"reset": reset_first, "wait_s": wait_s}, now=now, max_attempts=3)
        return job.id
