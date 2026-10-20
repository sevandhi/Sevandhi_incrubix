"""Recertification (Outstanding OUT3; design in app/recert/scheduler.py).

    POST /api/v1/recert/tick        {now?, dry_run?} → queue a recertification for every due destination (200 summary)
    GET  /api/v1/recert/due         ?now=  what a tick would queue (nothing is written)
    POST /api/v1/recert/self-heal   scan breakers / schema mismatches now and queue self-heal runs
    GET  /api/v1/recert/schedule    the settings and the next periodic runs

``now`` is the tick's injectable clock (judge staleness as of that instant); the queued runs execute at real time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db.session import SessionDep
from app.db.tables import ScheduledTaskRow
from app.recert import scheduler

router = APIRouter(tags=["recertification"])
PERIODIC_TASKS = ("recert.tick", "recert.self_heal", "alerts.deliver", "metrics.poll")


class RecertTickIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now: datetime | None = Field(default=None, description="Judge staleness as of this instant (clock jump); default: now.")
    dry_run: bool = Field(default=False, description="Only list what would be queued.")


def _as_of(value: datetime | None, request: Request) -> datetime:
    if value is None:
        return request.app.state.clock.now()
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.post("/api/v1/recert/tick", summary="Queue recertification runs for every due destination")
async def recert_tick(body: RecertTickIn, request: Request, session: SessionDep) -> dict[str, Any]:
    summary = await scheduler.tick(session, request.app.state.settings, as_of=_as_of(body.now, request),
                                   now=request.app.state.clock.now(), dry_run=body.dry_run)
    await session.commit()
    return summary


@router.get("/api/v1/recert/due", summary="Destinations a tick would re-test (read-only)")
async def recert_due(request: Request, session: SessionDep, now: datetime | None = None) -> dict[str, Any]:
    summary = await scheduler.tick(session, request.app.state.settings, as_of=_as_of(now, request),
                                   now=request.app.state.clock.now(), dry_run=True)
    await session.rollback()
    return summary


@router.post("/api/v1/recert/self-heal", summary="Queue self-heal recertifications for open breakers / schema drift")
async def recert_self_heal(request: Request) -> dict[str, Any]:
    state = request.app.state
    return await scheduler.self_heal_scan(session_factory=state.session_factory, settings=state.settings,
                                          storage=state.storage, clock=state.clock, registry=state.registry)


@router.get("/api/v1/recert/schedule", summary="Recertification settings and the next periodic runs")
async def recert_schedule(request: Request, session: SessionDep) -> dict[str, Any]:
    s = request.app.state.settings
    rows = (await session.execute(select(ScheduledTaskRow).where(ScheduledTaskRow.name.in_(PERIODIC_TASKS)))).scalars()
    return {
        "recert_after_days": s.RECERT_AFTER_DAYS,
        "guarantee": f"an L1+ destination is re-tested once its certification is {s.RECERT_AFTER_DAYS} days old (daily tick), "
                     "before the 30-day limit",
        "tick_every_s": s.RECERT_TICK_EVERY_S, "max_per_tick": s.RECERT_MAX_PER_TICK, "concurrency": s.RECERT_CONCURRENCY,
        "jitter_s": s.RECERT_JITTER_S, "retry_excluded_after_s": s.RECERT_RETRY_EXCLUDED_AFTER_S,
        "self_heal_every_s": s.RECERT_SELF_HEAL_EVERY_S, "self_heal_cooldown_s": s.RECERT_SELF_HEAL_COOLDOWN_S,
        "rules_drift": s.RULES_DRIFT_ENABLED,
        "alert_sinks": ["log"] + (["webhook"] if s.ALERT_WEBHOOK_URL else [])
        + (["github_issue"] if s.GITHUB_TOKEN and s.GITHUB_TOKEN.get_secret_value() and s.GITHUB_REPO else []),
        "periodic": [{"name": r.name, "every_s": r.every_s, "next_run_at": r.next_run_at.isoformat().replace("+00:00", "Z"),
                      "last_run_at": r.last_run_at.isoformat().replace("+00:00", "Z") if r.last_run_at else None,
                      "last_job_id": r.last_job_id} for r in rows],
    }
