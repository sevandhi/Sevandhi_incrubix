"""Built-in job handlers: a public self-check job (IS3 demo) and periodic housekeeping."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete

from app.db.tables import IdempotencyKeyRow
from app.errors import ErrorCode, IbxError
from app.jobs import queue
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.jobs.scheduler import periodic


@job_handler("selfcheck", public=True, max_attempts=3)
async def selfcheck(ctx: JobContext) -> dict[str, Any]:
    """Walk through progress steps, store a small report in S3 and return it (used by the IS self-check).

    Payload: ``{"steps": 3, "echo": any, "fail": false, "fail_retryable": false}``.
    """
    steps = max(1, min(10, int(ctx.payload.get("steps", 3))))
    for step in range(1, steps + 1):
        with ctx.stage(f"step_{step}"):
            await ctx.progress(int(step * 100 / (steps + 1)), f"step {step}/{steps}")
    if ctx.payload.get("fail"):
        raise IbxError(
            ErrorCode.DEPENDENCY_UNAVAILABLE if ctx.payload.get("fail_retryable") else ErrorCode.INPUT_FETCH_FAILED,
            "Self-check failure requested by the payload.",
            retryable=bool(ctx.payload.get("fail_retryable")),
        )
    report = {"job_id": ctx.job_id, "steps": steps, "echo": ctx.payload.get("echo"), "attempt": ctx.attempt}
    await ctx.put_artifact("report.json", report)
    return {"ok": True, "steps": steps, "echo": ctx.payload.get("echo")}


@job_handler("platform.housekeeping")
async def housekeeping(ctx: JobContext) -> dict[str, Any]:
    """Purge expired idempotency keys and requeue jobs whose worker died."""
    now = ctx.clock.now()
    async with ctx.session_factory() as session, session.begin():
        purged = (await session.execute(delete(IdempotencyKeyRow).where(IdempotencyKeyRow.expires_at <= now))).rowcount
        requeued = await queue.requeue_stale(session, now - timedelta(seconds=ctx.settings.JOB_STALE_AFTER_S), now=now)
    return {"idempotency_keys_purged": purged or 0, "stale_jobs_requeued": requeued}


periodic("platform.housekeeping", "platform.housekeeping", every_s=3600)
