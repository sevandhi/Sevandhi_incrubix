"""In-worker scheduler for periodic jobs (recertification, TTL sweeper, housekeeping).

* ``periodic(name, kind, every_s)`` registers a task in code; ``tick()`` syncs them into
  ``scheduled_tasks`` and enqueues every task whose ``next_run_at`` has passed.
* Only one worker is the leader: the one holding ``pg_try_advisory_lock(ibx:scheduler)`` on a dedicated
  connection. If it dies, the connection closes and another worker takes over.
* Time comes from the injectable clock, so a test (or the recert demo) can jump forward and ``tick()``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.clock import Clock, get_clock
from app.config import Settings
from app.db.session import advisory_key
from app.db.tables import ScheduledTaskRow
from app.jobs import queue
from app.logging import get_logger
from app.reliability.redact import redact

LEADER_LOCK = advisory_key("ibx:scheduler")
log = get_logger("app.jobs.scheduler")


@dataclass(frozen=True)
class PeriodicSpec:
    name: str
    kind: str
    every_s: int
    payload: dict[str, Any] = field(default_factory=dict)


PERIODIC: dict[str, PeriodicSpec] = {}


def periodic(name: str, kind: str, every_s: int, payload: dict[str, Any] | None = None) -> None:
    PERIODIC[name] = PeriodicSpec(name, kind, every_s, payload or {})


async def tick(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> list[str]:
    """Enqueue every due periodic task once; returns the new job ids."""
    job_ids: list[str] = []
    async with session_factory() as session, session.begin():
        for spec in PERIODIC.values():
            stmt = insert(ScheduledTaskRow).values(
                name=spec.name, kind=spec.kind, every_s=spec.every_s, payload=spec.payload, next_run_at=now
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[ScheduledTaskRow.name],
                    set_={"kind": spec.kind, "every_s": spec.every_s, "payload": spec.payload},
                )
            )
        due = (
            await session.execute(
                select(ScheduledTaskRow)
                .where(ScheduledTaskRow.next_run_at <= now, ScheduledTaskRow.name.in_(list(PERIODIC)))
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for task in due:
            job = await queue.enqueue(session, task.kind, task.payload, now=now)
            task.last_run_at, task.last_job_id = now, job.id
            task.next_run_at = now + timedelta(seconds=task.every_s)
            job_ids.append(job.id)
    if job_ids:
        log.info("scheduler_enqueued", jobs=job_ids)
    return job_ids


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.session_factory = session_factory
        self.clock = clock or get_clock()
        self._leader_conn: AsyncConnection | None = None

    async def try_lead(self) -> bool:
        """Become (or stay) the leader. Returns True while this process holds the lock."""
        if self._leader_conn is not None:
            try:
                await self._leader_conn.execute(text("SELECT 1"))
                return True
            except Exception:  # noqa: BLE001 - connection lost: leadership lost
                self._leader_conn = None
        conn = await self.engine.connect()
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_LOCK})).scalar()
        await conn.commit()
        if got:
            self._leader_conn = conn
            log.info("scheduler_leader_acquired")
            return True
        await conn.close()
        return False

    async def release(self) -> None:
        if self._leader_conn is not None:
            with contextlib.suppress(Exception):
                await self._leader_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LEADER_LOCK})
                await self._leader_conn.close()
            self._leader_conn = None

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    if await self.try_lead():
                        await tick(self.session_factory, self.clock.now())
                except Exception as exc:  # noqa: BLE001 - keep scheduling after transient DB errors
                    log.error("scheduler_error", error=redact(exc))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.SCHEDULER_TICK_S)
        finally:
            await self.release()
