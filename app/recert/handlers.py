"""Recertification jobs: ``recert.tick`` (daily), ``recert.run`` (one destination), ``recert.self_heal`` (frequent scan)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.config import get_settings
from app.db.tables import CertificationRunRow
from app.jobs import queue
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.jobs.scheduler import periodic
from app.recert import scheduler

DEFER_S = 30  # no free slot: try again 30..60 s later


@job_handler("recert.tick", max_attempts=1)
async def recert_tick(ctx: JobContext) -> dict[str, Any]:
    now = ctx.clock.now()
    async with ctx.session_factory() as session, session.begin():
        summary = await scheduler.tick(session, ctx.settings, as_of=now, now=now)
    await ctx.put_artifact("recert_tick.json", summary)
    return {k: v for k, v in summary.items() if k not in ("enqueued", "skipped")} | {
        "run_ids": [e.get("run_id") for e in summary.get("enqueued", [])]}


@job_handler("recert.run", max_attempts=1)
async def recert_run(ctx: JobContext) -> dict[str, Any]:
    run_id = ctx.payload["run_id"]
    async with scheduler.recert_slot(ctx.session_factory, ctx.settings.RECERT_CONCURRENCY) as slot:
        if slot is None:
            now = ctx.clock.now()
            delay = DEFER_S + scheduler.jitter_s(run_id, now, DEFER_S)
            async with ctx.session_factory() as session, session.begin():
                job = await queue.enqueue(session, "recert.run", {**ctx.payload, "deferrals": int(ctx.payload.get("deferrals") or 0) + 1},
                                          now=now, run_after=now + timedelta(seconds=delay))
                run = await session.get(CertificationRunRow, run_id, with_for_update=True)
                if run is not None:
                    run.job_id = job.id
            ctx.warn(f"all {ctx.settings.RECERT_CONCURRENCY} recertification slots are busy; deferred {delay} s as job {job.id}")
            return {"run_id": run_id, "deferred": True, "next_job_id": job.id}
        with ctx.stage("recertification"):
            result = await scheduler.run_recert(run_id, session_factory=ctx.session_factory, settings=ctx.settings,
                                                storage=ctx.storage, clock=ctx.clock, progress=ctx.progress)
    for name, key in (result.pop("evidence_files", None) or {}).items():
        ctx.links[name.replace("/", "_").rsplit(".", 1)[0]] = key
    if not result.get("passed"):
        ctx.warn(f"recertification {result.get('state')} at {result.get('failed_step')}: {result.get('failure_code')}")
    if (result.get("rules_drift") or {}).get("status") == "drift":
        ctx.warn("rules drift: the rule set needs review")
    return result | {"slot": slot}


@job_handler("recert.self_heal", max_attempts=1)
async def recert_self_heal(ctx: JobContext) -> dict[str, Any]:
    return await scheduler.self_heal_scan(session_factory=ctx.session_factory, settings=ctx.settings, storage=ctx.storage,
                                          clock=ctx.clock)


_settings = get_settings()
periodic("recert.tick", "recert.tick", every_s=max(60, _settings.RECERT_TICK_EVERY_S))
periodic("recert.self_heal", "recert.self_heal", every_s=max(10, _settings.RECERT_SELF_HEAL_EVERY_S))
