"""Recertification (Outstanding OUT3, plan §6): re-test destinations before 30 days, demote and exclude failures.

* **Daily tick** (``recert.tick``, every ``RECERT_TICK_EVERY_S``): every active L1+ destination whose
  ``last_certified_at`` is older than ``RECERT_AFTER_DAYS`` (21 < 30) gets a ``scheduled`` certification run, queued
  as a ``recert.run`` job with a stable per-destination jitter (0..``RECERT_JITTER_S``). At most
  ``RECERT_MAX_PER_TICK`` per tick, oldest first. A destination that certification demoted (``excluded`` with a
  ``certification ...`` reason) is re-tested (``recert_retry``) ``RECERT_RETRY_EXCLUDED_AFTER_S`` after its last
  run, so a destination that recovers is restored automatically. Destinations excluded by a person are never touched.
* **Bounded concurrency**: a ``recert.run`` job first takes one of ``RECERT_CONCURRENCY`` Postgres advisory-lock
  slots (shared by every worker); with no free slot it re-queues itself 30-60 s later.
* **Self-heal** (``recert.self_heal``, every ``RECERT_SELF_HEAL_EVERY_S``): a breaker that opened, or a schema
  mismatch seen by any call (both are in ``circuit_state``, shared by api and worker), queues a ``self_heal`` run
  for the active L1+ destinations on that connector + instance; when the breaker is open the run starts after
  ``open_until``. One per destination per ``RECERT_SELF_HEAL_COOLDOWN_S`` and only for a failure newer than its
  last run.
* Each run (``run_recert``): rules drift check → the certification harness (a pass sets the level, clears the
  exclusion and resolves the alert; a failure cleans up, demotes to L0, excludes and opens an alert) → alert delivery.

The tick takes ``as_of`` (which destinations are due) separately from ``now`` (when the jobs run): the demo
``python -m app.recert tick --now 2026-11-20`` evaluates staleness as of that date while the worker runs the jobs now.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalogue import service
from app.certification.harness import TERMINAL_STATES, Harness, create_run
from app.certification.runtime import connector_for
from app.clock import Clock
from app.config import Settings
from app.contract.registry import Registry, get_registry
from app.db.session import advisory_key
from app.db.tables import CertificationRunRow, CircuitStateRow, DestinationRow
from app.errors import ErrorCode, IbxError
from app.jobs import queue
from app.logging import get_logger
from app.providers.alerts import deliver_pending
from app.recert import drift
from app.reliability.redact import redact

log = get_logger("app.recert")
CERTIFIED_LEVELS = ("L1", "L2", "L3", "L4")
TICK_LOCK = "ibx:recert:tick"
SCHEMA_MISMATCH_TEXT = "did not match the expected schema"  # BaseConnector._schema_error message
OPEN_RUN_WINDOW = timedelta(days=1)  # a non-terminal run older than this is considered dead (ignored)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def jitter_s(destination_id: str, as_of: datetime, max_s: int) -> int:
    """Stable 0..max_s seconds per destination and day, so a tick spreads runs out and a re-tick changes nothing."""
    if max_s <= 0:
        return 0
    digest = hashlib.sha256(f"{destination_id}|{as_of.date().isoformat()}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (max_s + 1)


async def _open_runs(session: AsyncSession, destination_ids: list[str]) -> set[str]:
    if not destination_ids:
        return set()
    stmt = select(CertificationRunRow.destination_id).where(
        CertificationRunRow.destination_id.in_(destination_ids),
        CertificationRunRow.state.not_in(TERMINAL_STATES),
        CertificationRunRow.created_at >= func.now() - func.make_interval(0, 0, 0, OPEN_RUN_WINDOW.days))
    return set((await session.execute(stmt)).scalars())


async def _last_finished(session: AsyncSession, destination_id: str) -> datetime | None:
    stmt = (select(func.max(CertificationRunRow.finished_at))
            .where(CertificationRunRow.destination_id == destination_id, CertificationRunRow.state.in_(TERMINAL_STATES)))
    return (await session.execute(stmt)).scalar()


async def due(session: AsyncSession, settings: Settings, as_of: datetime) -> dict[str, Any]:
    """Which destinations a tick at ``as_of`` re-tests (and which it skips, with the reason)."""
    cutoff = as_of - timedelta(days=settings.RECERT_AFTER_DAYS)
    stale = (await session.execute(
        select(DestinationRow).where(DestinationRow.status == "active", DestinationRow.level.in_(CERTIFIED_LEVELS),
                                     (DestinationRow.last_certified_at.is_(None)) | (DestinationRow.last_certified_at < cutoff))
        .order_by(DestinationRow.last_certified_at.asc().nulls_first(), DestinationRow.id))).scalars().all()
    demoted = (await session.execute(
        select(DestinationRow).where(DestinationRow.status == "excluded", DestinationRow.last_certified_at.is_not(None),
                                     DestinationRow.excluded_reason.like("certification %"))
        .order_by(DestinationRow.last_certified_at.asc(), DestinationRow.id))).scalars().all()
    candidates: list[tuple[DestinationRow, str]] = [(d, "stale") for d in stale]
    skipped: list[dict[str, Any]] = []
    retry_after = timedelta(seconds=settings.RECERT_RETRY_EXCLUDED_AFTER_S)
    for dest in demoted:
        last = await _last_finished(session, dest.id)
        if last is None or last <= as_of - retry_after:
            candidates.append((dest, "excluded_retry"))
        else:
            skipped.append({"destination_id": dest.id, "why": f"demoted; next retry after {_iso(last + retry_after)}"})
    busy = await _open_runs(session, [d.id for d, _ in candidates])
    picked: list[tuple[DestinationRow, str]] = []
    for dest, reason in candidates:
        if dest.id in busy:
            skipped.append({"destination_id": dest.id, "why": "a certification run is already queued or running"})
        elif len(picked) >= settings.RECERT_MAX_PER_TICK:
            skipped.append({"destination_id": dest.id, "why": f"over RECERT_MAX_PER_TICK ({settings.RECERT_MAX_PER_TICK}); next tick"})
        else:
            picked.append((dest, reason))
    return {"cutoff": cutoff, "picked": picked, "skipped": skipped}


async def tick(session: AsyncSession, settings: Settings, *, as_of: datetime, now: datetime, dry_run: bool = False) -> dict[str, Any]:
    """Queue a recertification for every due destination, in the caller's transaction (one tick at a time)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_key(TICK_LOCK)})
    plan = await due(session, settings, as_of)
    enqueued: list[dict[str, Any]] = []
    for dest, reason in plan["picked"]:
        item: dict[str, Any] = {"destination_id": dest.id, "name": dest.name, "connector_key": dest.connector_key,
                                "level": dest.level, "status": dest.status, "last_certified_at": _iso(dest.last_certified_at),
                                "reason": reason}
        if not dry_run:
            trigger = "scheduled" if reason == "stale" else "recert_retry"
            run = await create_run(session, dest.id, trigger)
            run_after = now + timedelta(seconds=jitter_s(dest.id, as_of, settings.RECERT_JITTER_S))
            job = await queue.enqueue(session, "recert.run", {"run_id": run.id, "destination_id": dest.id, "reason": reason,
                                                              "as_of": _iso(as_of)}, now=now, run_after=run_after)
            run.job_id = job.id
            item.update(trigger=trigger, run_id=run.id, job_id=job.id, run_after=_iso(run_after))
        enqueued.append(item)
    summary = {"as_of": _iso(as_of), "now": _iso(now), "dry_run": dry_run, "recert_after_days": settings.RECERT_AFTER_DAYS,
               "cutoff": _iso(plan["cutoff"]), "max_per_tick": settings.RECERT_MAX_PER_TICK, "due": len(enqueued),
               "enqueued" if not dry_run else "would_enqueue": enqueued, "skipped": plan["skipped"]}
    log.info("recert_tick", as_of=_iso(as_of), due=len(enqueued), skipped=len(plan["skipped"]), dry_run=dry_run)
    return summary


# ------------------------------------------------------------------ bounded concurrency


@contextlib.asynccontextmanager
async def recert_slot(session_factory: async_sessionmaker[AsyncSession], slots: int) -> AsyncIterator[int | None]:
    """One of ``slots`` cluster-wide slots (session advisory locks on a dedicated connection), or None if all are taken."""
    engine = session_factory.kw["bind"]
    conn = await engine.connect()
    taken: int | None = None
    try:
        for n in range(max(1, slots)):
            if (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": advisory_key(f"ibx:recert:slot:{n}")})).scalar():
                taken = n
                break
        await conn.commit()
        yield taken
    finally:
        if taken is not None:
            with contextlib.suppress(Exception):
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": advisory_key(f"ibx:recert:slot:{taken}")})
                await conn.commit()
        await conn.close()


# ------------------------------------------------------------------ one recertification


async def run_recert(run_id: str, *, session_factory: async_sessionmaker[AsyncSession], settings: Settings, storage: Any,
                     clock: Clock, progress: Callable[[int, str], Awaitable[None]] | None = None,
                     http: httpx.AsyncClient | None = None, registry: Registry | None = None) -> dict[str, Any]:
    """Rules drift check, then the certification harness, then alert delivery."""
    async with session_factory() as session:
        run = await session.get(CertificationRunRow, run_id)
        if run is None:
            raise IbxError(ErrorCode.NOT_FOUND, "Unknown certification run.", details={"run_id": run_id})
        destination_id, trigger = run.destination_id, run.trigger
    try:
        rules = await drift.check(destination_id, session_factory=session_factory, settings=settings, now=clock.now(), http=http)
    except Exception as exc:  # noqa: BLE001 - a drift problem never blocks the certification itself
        log.error("rules_drift_check_failed", destination_id=destination_id, error=redact(exc))
        rules = {"status": "error", "why": type(exc).__name__}
    harness = Harness(settings=settings, session_factory=session_factory, storage=storage, clock=clock,
                      registry=registry, progress=progress)
    result = await harness.run(run_id)
    deliveries = await deliver_pending(session_factory, settings, clock.now(), destination_id=destination_id)
    out = {"run_id": run_id, "destination_id": destination_id, "trigger": trigger, "state": result.get("state"),
           "passed": result.get("passed"), "level_before": result.get("level_before"), "level_after": result.get("level_after"),
           "excluded": bool(result.get("excluded")), "failed_step": result.get("failed_step"),
           "failure_code": result.get("failure_code"), "rules_drift": rules, "alert_deliveries": deliveries,
           "evidence_files": result.get("evidence_files")}
    log.info("recert_run_finished", run_id=run_id, destination_id=destination_id, trigger=trigger, state=out["state"],
             level_after=out["level_after"], rules_drift=rules.get("status"))
    return out


# ------------------------------------------------------------------ self-heal


def _breaker_scope(dest: DestinationRow, conn_row: Any, settings: Settings, session_factory: Any, storage: Any,
                   clock: Clock, registry: Registry) -> str | None:
    """The circuit-breaker scope the connector uses for this destination (``connector|instance``)."""
    try:
        connector = connector_for(dest.connector_key, settings=settings, session_factory=session_factory, storage=storage,
                                  clock=clock, registry=registry)
        contract_dest = service.contract_destination(dest, None)
        connection = service.contract_connection(conn_row, dest.connector_key, connector.manifest.auth)
        contract_dest, connection = connector.simulate(contract_dest, connection)
        return connector._scopes(contract_dest, connection).breaker  # noqa: SLF001 - the one definition of the scope
    except Exception:  # noqa: BLE001 - an unknown connector or a malformed row has no breaker scope
        return None


async def self_heal_scan(*, session_factory: async_sessionmaker[AsyncSession], settings: Settings, storage: Any,
                         clock: Clock, registry: Registry | None = None) -> dict[str, Any]:
    """Queue a ``self_heal`` recertification for destinations behind an open breaker or a fresh schema mismatch."""
    registry = registry or get_registry()
    now = clock.now()
    enqueued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    async with session_factory() as session, session.begin():
        db_now = (await session.execute(select(func.now()))).scalar()
        signals = (await session.execute(select(CircuitStateRow).where(
            CircuitStateRow.updated_at >= db_now - timedelta(days=1),
            (CircuitStateRow.state != "closed")
            | ((CircuitStateRow.consecutive_failures > 0) & CircuitStateRow.last_error.contains(SCHEMA_MISMATCH_TEXT))))).scalars().all()
        for signal in signals:
            why = "breaker_open" if signal.state != "closed" else "schema_mismatch"
            dests = (await session.execute(select(DestinationRow).where(
                DestinationRow.connector_key == signal.connector_key, DestinationRow.status == "active",
                DestinationRow.level.in_(CERTIFIED_LEVELS)))).scalars().all()
            for dest in dests:
                conn_row = await service.destination_connection(session, dest)
                if _breaker_scope(dest, conn_row, settings, session_factory, storage, clock, registry) != signal.scope:
                    continue
                last = (await session.execute(select(CertificationRunRow).where(CertificationRunRow.destination_id == dest.id)
                                              .order_by(CertificationRunRow.created_at.desc()).limit(1))).scalar_one_or_none()
                if last is not None and last.state not in TERMINAL_STATES and last.created_at >= db_now - OPEN_RUN_WINDOW:
                    skipped.append({"destination_id": dest.id, "why": "a certification run is already queued or running"})
                    continue
                if last is not None and last.created_at >= signal.updated_at:
                    skipped.append({"destination_id": dest.id, "why": "no failure since the last certification run"})
                    continue
                if last is not None and last.created_at >= db_now - timedelta(seconds=settings.RECERT_SELF_HEAL_COOLDOWN_S):
                    skipped.append({"destination_id": dest.id, "why": "self-heal cooldown"})
                    continue
                run_after = now
                if signal.state == "open" and signal.open_until is not None and signal.open_until > now:
                    run_after = signal.open_until + timedelta(seconds=5)  # probe after the breaker's cool-down
                run = await create_run(session, dest.id, "self_heal")
                job = await queue.enqueue(session, "recert.run", {"run_id": run.id, "destination_id": dest.id,
                                                                  "reason": why, "scope": signal.scope}, now=now, run_after=run_after)
                run.job_id = job.id
                enqueued.append({"destination_id": dest.id, "name": dest.name, "reason": why, "scope": signal.scope,
                                 "run_id": run.id, "job_id": job.id, "run_after": _iso(run_after),
                                 "last_error": redact(signal.last_error)})
    if enqueued:
        log.warning("recert_self_heal_enqueued", runs=[e["run_id"] for e in enqueued], reasons=[e["reason"] for e in enqueued])
    return {"signals": len(signals), "enqueued": enqueued, "skipped": skipped}
