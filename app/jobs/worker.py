"""Job worker: ``uv run python -m app.jobs.worker``.

Each consumer loop claims one due job (SKIP LOCKED), runs its handler, then records the outcome:
* success → ``succeeded``, progress 100, ``result = {links: {manifest, ...}, ...handler data}``;
* retryable ``IbxError`` with attempts left → back to ``queued`` with exponential back-off + jitter
  (deterministic per job via ``SEED``; never earlier than the error's ``retry_at``);
* anything else → ``failed`` with the error envelope.
A ``manifest.json`` (IS9) is written for every finished job, and the signed callback is sent if requested.
Loops wake on ``LISTEN ibx_jobs`` and also poll every ``WORKER_POLL_S`` as a safety net.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import signal
import socket
import time
from datetime import datetime, timedelta
from typing import Any

import asyncpg
import httpx
import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import Clock, get_clock
from app.config import Settings, ensure_safe_secrets, get_settings
from app.connectors.base import USED_CONNECTORS
from app.db.session import make_engine, make_session_factory, sync_dsn
from app.db.tables import JobRow
from app.errors import ErrorCode, IbxError
from app.jobs import callbacks, queue
from app.jobs.context import JobContext, build_manifest, public_job
from app.jobs.registry import HANDLERS, load_handlers
from app.logging import configure_logging, get_logger
from app.reliability.redact import redact

log = get_logger("app.jobs.worker")


def backoff_seconds(attempt: int, job_id: str, seed: int, err: IbxError, now: datetime) -> float:
    """2^attempt seconds (max 300) × jitter in [0.5, 1.5); at least until the error's retry_at."""
    rng = random.Random(f"{seed}:{job_id}:{attempt}")
    delay = min(300.0, 2.0**attempt) * (0.5 + rng.random())
    retry_at = err.details.get("retry_at")
    if isinstance(retry_at, str):
        with contextlib.suppress(ValueError):
            delay = max(delay, (datetime.fromisoformat(retry_at.replace("Z", "+00:00")) - now).total_seconds())
    return delay


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        storage: Any,
        clock: Clock | None = None,
        http: httpx.AsyncClient | None = None,
        worker_id: str | None = None,
        connector_versions: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.storage = storage
        self.clock = clock or get_clock()
        self.http = http or httpx.AsyncClient()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.connector_versions = connector_versions or {}
        self.wake = asyncio.Event()

    async def run_once(self) -> JobRow | None:
        """Claim and process one due job. Returns the claimed job, or None if the queue was empty."""
        async with self.session_factory() as session, session.begin():
            job = await queue.claim(session, self.worker_id, self.clock.now())
        if job is None:
            return None
        await self.process(job)
        return job

    async def process(self, job: JobRow) -> None:
        structlog.contextvars.bind_contextvars(job_id=job.id, correlation_id=job.correlation_id)
        bound = log.bind(job_id=job.id, kind=job.kind)
        ctx = JobContext(
            job_id=job.id,
            kind=job.kind,
            payload=job.payload or {},
            attempt=job.attempts,
            settings=self.settings,
            clock=self.clock,
            log=bound,
            session_factory=self.session_factory,
            storage=self.storage,
        )
        started = self.clock.now()
        data: dict[str, Any] = {}
        err: IbxError | None = None
        try:
            spec = HANDLERS.get(job.kind)
            if spec is None:
                raise IbxError(ErrorCode.INTERNAL_ERROR, f"No handler registered for job kind '{job.kind}'.", retryable=False)
            bound.info("job_started", attempt=job.attempts)
            used_token = USED_CONNECTORS.set(ctx.used_connectors)  # connectors record themselves here (IS9)
            lease = asyncio.create_task(self._renew_lease(job.id))
            try:
                data = await spec.fn(ctx) or {}
            finally:
                lease.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease
                USED_CONNECTORS.reset(used_token)
        except IbxError as exc:
            err = exc
        except Exception as exc:  # noqa: BLE001 - a handler bug fails the job, never the worker
            bound.error("job_handler_crashed", error=redact(exc))
            err = IbxError(ErrorCode.INTERNAL_ERROR, f"Job handler failed ({type(exc).__name__}).", retryable=False)
        finished = self.clock.now()

        if err is not None and err.retryable and job.attempts < job.max_attempts:
            delay = backoff_seconds(job.attempts, job.id, self.settings.SEED, err, finished)
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    update(JobRow)
                    .where(JobRow.id == job.id)
                    .values(status="queued", run_after=finished + timedelta(seconds=delay), locked_by=None,
                            locked_at=None, error=redact(err.to_body()))
                )
            bound.warning("job_retry_scheduled", code=err.code.value, delay_s=round(delay, 1))
            structlog.contextvars.unbind_contextvars("job_id")
            return

        status = "failed" if err is not None else "succeeded"
        manifest = build_manifest(ctx, status=status, started_at=started, finished_at=finished, connectors=self.connector_versions)
        links = {**(data.pop("links", None) or {}), **ctx.links}
        try:
            links["manifest"] = await self.storage.put_bytes(
                f"jobs/{job.id}/manifest.json", callbacks.encode(manifest), "application/json"
            )
        except Exception as exc:  # noqa: BLE001 - storage down: keep the manifest inline instead of losing it
            bound.warning("manifest_upload_failed", error=redact(exc))
            data["manifest"] = manifest
        result = redact({"links": links, **data})
        async with self.session_factory() as session, session.begin():
            row = await session.get(JobRow, job.id, with_for_update=True)
            row.status = status
            row.progress = 100 if err is None else row.progress
            row.result = result
            row.error = redact(err.to_body()) if err is not None else None
            row.finished_at = finished
            row.locked_by = None
        bound.info("job_finished", status=status, code=err.code.value if err else None)
        if job.callback_url:
            await self._callback(job.id)
        structlog.contextvars.unbind_contextvars("job_id")

    async def _renew_lease(self, job_id: str) -> None:
        """Heartbeat for a running job: renew ``locked_at`` several times per
        ``JOB_STALE_AFTER_S``, so a long job is never requeued and run twice while it is still alive."""
        every = max(0.05, self.settings.JOB_STALE_AFTER_S / 4)
        while True:
            await asyncio.sleep(every)
            try:
                async with self.session_factory() as session, session.begin():
                    await queue.renew_lease(session, job_id, self.worker_id, self.clock.now())
            except Exception as exc:  # noqa: BLE001 - a missed heartbeat must never fail the job itself
                log.warning("job_lease_renew_failed", job_id=job_id, error=redact(exc))

    async def _callback(self, job_id: str) -> None:
        async with self.session_factory() as session:
            job = await session.get(JobRow, job_id)
            payload = await public_job(job, self.storage, self.settings)
        outcome = await callbacks.deliver(
            self.http, job.callback_url, payload, self.settings.callback_key(), timeout_s=self.settings.CALLBACK_TIMEOUT_S,
            settings=self.settings,
        )
        async with self.session_factory() as session, session.begin():
            await session.execute(update(JobRow).where(JobRow.id == job_id).values(callback_status=outcome))

    async def consume(self, stop: asyncio.Event) -> None:
        """One consumer loop: drain due jobs, then sleep until NOTIFY or the poll interval."""
        while not stop.is_set():
            try:
                job = await self.run_once()
            except Exception as exc:  # noqa: BLE001 - e.g. DB restart: log, back off, keep going
                log.error("worker_loop_error", error=redact(exc))
                job = None
            if job is None:
                self.wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=self.settings.WORKER_POLL_S)

    async def listen(self, stop: asyncio.Event) -> None:
        """LISTEN for new jobs; reconnects if the database restarts."""
        while not stop.is_set():
            try:
                conn = await asyncpg.connect(sync_dsn(self.settings.DATABASE_URL))
                await conn.add_listener(queue.NOTIFY_CHANNEL, lambda *_: self.wake.set())
                try:
                    await stop.wait()
                finally:
                    await conn.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("listen_failed", error=redact(exc))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)


async def heartbeat(stop: asyncio.Event, path: str, every_s: float = 5.0) -> None:
    """Touch a file every few seconds; the compose healthcheck checks that it is fresh."""
    while not stop.is_set():
        with contextlib.suppress(OSError):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(time.time()))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)


async def main() -> None:
    from app.contract.registry import get_registry
    from app.db.migrate import upgrade_head
    from app.jobs.scheduler import Scheduler
    from app.storage import S3Storage

    settings = get_settings()
    ensure_safe_secrets(settings)
    configure_logging(settings.LOG_LEVEL, settings.LOG_JSON, settings.secret_values())
    engine = make_engine(settings.DATABASE_URL, settings.DB_POOL_SIZE)
    if settings.DB_AUTO_MIGRATE:
        await upgrade_head(engine)
    factory = make_session_factory(engine)
    storage = S3Storage(settings)
    loaded = load_handlers()
    registry = get_registry()
    worker = Worker(
        settings=settings,
        session_factory=factory,
        storage=storage,
        connector_versions={k: m.version for k, m in registry.manifests.items()},
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker_started", worker_id=worker.worker_id, handlers=sorted(HANDLERS), modules=loaded)
    tasks = [asyncio.create_task(worker.listen(stop)), asyncio.create_task(heartbeat(stop, settings.WORKER_HEARTBEAT_FILE))]
    tasks += [asyncio.create_task(worker.consume(stop)) for _ in range(max(1, settings.WORKER_CONCURRENCY))]
    if settings.SCHEDULER_ENABLED:
        tasks.append(asyncio.create_task(Scheduler(settings, engine, factory).run(stop)))
    await stop.wait()
    worker.wake.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    await worker.http.aclose()
    await engine.dispose()
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
