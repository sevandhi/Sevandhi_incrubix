"""Scheduled metrics poll (``metrics.poll``): snapshots recent publications on destinations that offer metrics."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.jobs.scheduler import periodic
from app.metrics import service


@job_handler("metrics.poll", max_attempts=1)
async def metrics_poll(ctx: JobContext) -> dict[str, Any]:
    with ctx.stage("metrics_poll"):
        result = await service.poll(session_factory=ctx.session_factory, settings=ctx.settings, storage=ctx.storage,
                                    clock=ctx.clock, destination_id=ctx.payload.get("destination_id"),
                                    limit=ctx.payload.get("limit"))
    await ctx.put_artifact("metrics_poll.json", result)
    for failure in (r for r in result["results"] if not r["ok"]):
        ctx.warn(f"{failure['publication_id']}: {failure['code']}")
    return {k: v for k, v in result.items() if k != "results"}


periodic("metrics.poll", "metrics.poll", every_s=max(60, get_settings().METRICS_POLL_EVERY_S))
