"""Circuit breaker, stored in Postgres so the api and the worker share it (plan §4.2).

The state machine is pure (``decide``, ``on_failure``, ``on_success``) and is unit-tested with a fake
clock. ``check`` and ``record`` load and save it per scope (``connector_key|instance``).

    closed --(N consecutive PLATFORM_UNAVAILABLE)--> open --(open_s passed)--> half_open (one probe)
    half_open --probe ok--> closed          half_open --probe fails--> open again
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import CircuitStateRow
from app.errors import ErrorCode, IbxError


class Verdict(StrEnum):
    ALLOW = "allow"
    PROBE = "probe"
    REJECT = "reject"


@dataclass(frozen=True)
class BreakerState:
    state: str = "closed"  # closed | open | half_open
    failures: int = 0
    open_until: datetime | None = None
    probe_started_at: datetime | None = None


def decide(st: BreakerState, now: datetime, probe_timeout_s: float) -> tuple[Verdict, BreakerState]:
    """May a call go out now? Returns the verdict and the (possibly changed) state."""
    if st.state == "closed":
        return Verdict.ALLOW, st
    if st.state == "open":
        if st.open_until is not None and now < st.open_until:
            return Verdict.REJECT, st
        return Verdict.PROBE, replace(st, state="half_open", probe_started_at=now)
    # half_open: one probe at a time; a probe that never reported back (crash) expires
    if st.probe_started_at is not None and now < st.probe_started_at + timedelta(seconds=probe_timeout_s):
        return Verdict.REJECT, st
    return Verdict.PROBE, replace(st, probe_started_at=now)


def on_failure(st: BreakerState, now: datetime, threshold: int, open_s: float) -> tuple[BreakerState, bool]:
    """Count a PLATFORM_UNAVAILABLE result. Returns the new state and whether the breaker just opened."""
    failures = st.failures + 1
    if st.state == "half_open" or failures >= threshold:
        opened = st.state != "open"
        return BreakerState("open", failures, now + timedelta(seconds=open_s), None), opened
    return replace(st, failures=failures), False


def on_success(_st: BreakerState) -> BreakerState:
    return BreakerState()


# ------------------------------------------------------------------ persistence


def _load(row: CircuitStateRow | None) -> BreakerState:
    if row is None:
        return BreakerState()
    return BreakerState(row.state, row.consecutive_failures, row.open_until, row.probe_started_at)


def _save(row: CircuitStateRow, st: BreakerState) -> None:
    row.state, row.consecutive_failures = st.state, st.failures
    row.open_until, row.probe_started_at = st.open_until, st.probe_started_at


async def _locked_row(session: AsyncSession, scope: str) -> CircuitStateRow | None:
    stmt = select(CircuitStateRow).where(CircuitStateRow.scope == scope).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def check(session: AsyncSession, scope: str, now: datetime, probe_timeout_s: float) -> Verdict:
    """Raise ``PLATFORM_UNAVAILABLE`` (circuit open) if no call may go out; otherwise allow or probe."""
    row = await _locked_row(session, scope)
    verdict, new_state = decide(_load(row), now, probe_timeout_s)
    if row is not None and new_state != _load(row):
        _save(row, new_state)
    if verdict == Verdict.REJECT:
        open_until = row.open_until if row is not None else None
        raise IbxError(
            ErrorCode.PLATFORM_UNAVAILABLE,
            "Circuit open: the platform failed repeatedly, so calls are paused without contacting it.",
            details={
                "circuit": "open" if row is not None and row.state == "open" else "half_open",
                "consecutive_failures": row.consecutive_failures if row is not None else 0,
                "retry_at": open_until.isoformat().replace("+00:00", "Z") if open_until else None,
            },
        )
    return verdict


async def record(
    session: AsyncSession,
    scope: str,
    connector_key: str,
    *,
    unavailable: bool,
    now: datetime,
    threshold: int,
    open_s: float,
    error: str | None = None,
) -> bool:
    """Record one outcome. ``unavailable`` = PLATFORM_UNAVAILABLE. Returns True if the breaker just opened."""
    row = await _locked_row(session, scope)
    if row is None:
        if not unavailable:
            return False  # nothing to reset
        # Two first failures can arrive at once: insert-if-absent, then lock the one row (no unique violation).
        await session.execute(
            pg_insert(CircuitStateRow)
            .values(scope=scope, connector_key=connector_key, state="closed", consecutive_failures=0)
            .on_conflict_do_nothing(index_elements=["scope"])
        )
        row = await _locked_row(session, scope)
    current = _load(row)
    if unavailable:
        new_state, opened = on_failure(current, now, threshold, open_s)
        row.last_error = (error or "")[:500]
    else:
        new_state, opened = on_success(current), False
    _save(row, new_state)
    return opened
