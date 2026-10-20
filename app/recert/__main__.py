"""Recertification CLI (runs against the configured database; the worker executes the queued runs).

    python -m app.recert tick [--now 2026-11-20T09:00:00Z] [--dry-run] [--wait 300]
    python -m app.recert due  [--now ...]
    python -m app.recert self-heal
    python -m app.recert deliver-alerts

``--now`` is the injectable clock of the tick: destinations are judged due "as of" that instant (a clock jump),
while the queued jobs run at the real time. ``--wait N`` polls until every queued run is finished (N seconds max)
and prints each result. Output is JSON on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

from app.clock import get_clock


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def _wait(factory: Any, run_ids: list[str], timeout_s: float) -> list[dict[str, Any]]:
    from app.certification.harness import TERMINAL_STATES
    from app.db.tables import CertificationRunRow

    deadline = time.monotonic() + timeout_s
    while True:
        async with factory() as session:
            runs = [await session.get(CertificationRunRow, rid) for rid in run_ids]
        if all(r is not None and r.state in TERMINAL_STATES for r in runs) or time.monotonic() > deadline:
            return [{"run_id": r.id, "destination_id": r.destination_id, "trigger": r.trigger, "state": r.state,
                     "failure_code": r.failure_code, "level_after": (r.result or {}).get("level_after"),
                     "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in runs if r is not None]
        await asyncio.sleep(2)


async def _main(args: argparse.Namespace) -> int:
    from app.config import get_settings
    from app.db.session import make_engine, make_session_factory
    from app.jobs.registry import load_handlers
    from app.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_JSON, settings.secret_values())
    load_handlers()
    from app.recert import scheduler

    engine = make_engine(settings.DATABASE_URL, 4)
    factory = make_session_factory(engine)
    real_now = get_clock().now()
    as_of = _parse_now(getattr(args, "now", None)) or real_now
    out: dict[str, Any]
    try:
        if args.command in ("tick", "due"):
            dry = args.command == "due" or args.dry_run
            async with factory() as session, session.begin():
                out = await scheduler.tick(session, settings, as_of=as_of, now=real_now, dry_run=dry)
            out["clock"] = {"as_of_source": "--now (clock jump)" if getattr(args, "now", None) else "system clock",
                            "jobs_run_at": "real time (the worker's clock)"}
            if args.command == "tick" and args.wait and out.get("enqueued"):
                out["results"] = await _wait(factory, [e["run_id"] for e in out["enqueued"]], args.wait)
        elif args.command == "self-heal":
            from app.storage import S3Storage

            out = await scheduler.self_heal_scan(session_factory=factory, settings=settings, storage=S3Storage(settings),
                                                 clock=get_clock())
        else:
            from app.providers.alerts import deliver_pending
            from app.recert.drift import resolve_reconfirmed

            async with factory() as session, session.begin():
                resolved = await resolve_reconfirmed(session)
            out = {"rules_drift_resolved": resolved, "deliveries": await deliver_pending(factory, settings, real_now)}
    finally:
        await engine.dispose()
    sys.stdout.write(json.dumps(out, indent=2, default=str) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.recert", description="Recertification scheduler (OUT3).")
    sub = parser.add_subparsers(dest="command", required=True)
    tick = sub.add_parser("tick", help="queue recertifications for every due destination")
    tick.add_argument("--now", help="judge staleness as of this ISO-8601 instant (clock jump)")
    tick.add_argument("--dry-run", action="store_true", help="only list what would be queued")
    tick.add_argument("--wait", type=float, default=0, help="seconds to wait for the queued runs to finish")
    due = sub.add_parser("due", help="list the destinations a tick would queue")
    due.add_argument("--now")
    sub.add_parser("self-heal", help="scan breakers / schema mismatches and queue self-heal runs")
    sub.add_parser("deliver-alerts", help="deliver pending alert events now")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
