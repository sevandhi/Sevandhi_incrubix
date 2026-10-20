"""Postgres job queue (IS3): ``enqueue`` inserts a row and NOTIFYs; workers ``claim`` with SKIP LOCKED.

Statuses are only ``queued | running | succeeded | failed``. A retryable failure goes back to
``queued`` with a later ``run_after`` (and the last error kept for diagnosis).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import JobRow, new_id
from app.errors import ErrorCode, IbxError
from app.jobs.registry import HANDLERS

NOTIFY_CHANNEL = "ibx_jobs"


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    now: datetime,
    callback_url: str | None = None,
    run_after: datetime | None = None,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
) -> JobRow:
    """Add a job in the caller's transaction (so it commits together with e.g. the idempotency row)."""
    if kind not in HANDLERS:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"Unknown job kind '{kind}'.", details={"kinds": sorted(HANDLERS)})
    spec = HANDLERS[kind]
    job = JobRow(
        id=new_id("job"),
        kind=kind,
        status="queued",
        progress=0,
        payload=payload or {},
        callback_url=callback_url,
        run_after=run_after or now,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        max_attempts=spec.max_attempts or max_attempts,
        attempts=0,
    )
    session.add(job)
    await session.flush()
    await session.execute(text("SELECT pg_notify(:channel, :job_id)"), {"channel": NOTIFY_CHANNEL, "job_id": job.id})
    return job


async def claim(session: AsyncSession, worker_id: str, now: datetime) -> JobRow | None:
    """Atomically take the oldest due job. Two workers never get the same row (FOR UPDATE SKIP LOCKED)."""
    due = (
        select(JobRow.id)
        .where(JobRow.status == "queued", JobRow.run_after <= now)
        .order_by(JobRow.run_after, JobRow.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(JobRow)
        .where(JobRow.id == due)
        .values(status="running", locked_by=worker_id, locked_at=now, attempts=JobRow.attempts + 1, started_at=now)
        .returning(JobRow)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_job(session: AsyncSession, job_id: str) -> JobRow:
    job = await session.get(JobRow, job_id)
    if job is None:
        raise IbxError(ErrorCode.JOB_NOT_FOUND, "Unknown job id.")
    return job


async def set_progress(session: AsyncSession, job_id: str, progress: int) -> None:
    await session.execute(update(JobRow).where(JobRow.id == job_id).values(progress=max(0, min(100, int(progress)))))


async def renew_lease(session: AsyncSession, job_id: str, worker_id: str, now: datetime) -> None:
    """A running job's heartbeat: move ``locked_at`` forward so housekeeping never sees it as stale."""
    await session.execute(update(JobRow).where(JobRow.id == job_id, JobRow.status == "running",
                                               JobRow.locked_by == worker_id).values(locked_at=now))


async def requeue_stale(session: AsyncSession, older_than: datetime, *, now: datetime | None = None) -> int:
    """Put jobs whose worker died (running, lease not renewed since ``older_than``) back in the queue.
    A job that already used all its attempts fails instead, so a job that crashes its worker can not loop forever.
    Returns the number requeued."""
    stale = (JobRow.status == "running", JobRow.locked_at < older_than)
    error = IbxError(ErrorCode.INTERNAL_ERROR, "The job's worker stopped responding on its last attempt; not retried.",
                     retryable=False).to_body()
    await session.execute(
        update(JobRow).where(*stale, JobRow.attempts >= JobRow.max_attempts)
        .values(status="failed", locked_by=None, error=error, finished_at=now or older_than)
    )
    stmt = (
        update(JobRow)
        .where(*stale, JobRow.attempts < JobRow.max_attempts)
        .values(status="queued", locked_by=None, locked_at=None)
    )
    return (await session.execute(stmt)).rowcount or 0
