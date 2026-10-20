"""Idempotent publish under timeouts (plan §4.1, Must-have 6).

The ledger is the ``publish_intents`` table, one row per idempotency key:

* ``request_hash`` = sha256 of the canonical rendition + destination key + connection reference;
* ``marker`` = ``ibx-`` + base32(sha256(key))[:12], placed on the platform so we can find the post later;
* ``state``: PENDING (write-ahead, before the platform call) → SUCCEEDED | FAILED | RETRYABLE | UNKNOWN.

The PENDING row itself is the lock for a running publish: it is written in one short
transaction under advisory locks (``lock_xact``), and no DB connection is held while the platform is
called. A concurrent duplicate sees a fresh PENDING row (``in_flight``), waits and then reads the stored
result. If the process dies mid-call, the row stays PENDING; once its lease runs out the next attempt
reconciles via ``_lookup``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import advisory_key
from app.db.tables import PublishIntentRow
from app.errors import ErrorCode, IbxError

LOCK_NOT_AVAILABLE = "55P03"  # Postgres SQLSTATE for lock_timeout


class IntentState(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYABLE = "RETRYABLE"
    UNKNOWN = "UNKNOWN"


class Action(StrEnum):
    NEW = "new"  # no intent yet: write-ahead, then publish
    REPLAY = "replay"  # SUCCEEDED: return the stored result, no platform call
    REPLAY_ERROR = "replay_error"  # FAILED (non-retryable): return the same error again
    RECONCILE = "reconcile"  # PENDING / UNKNOWN: look the marker up on the platform first
    RETRY = "retry"  # RETRYABLE: the platform did not create anything; publish again


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def request_hash(rendition: dict[str, Any], destination_key: str, connection_ref: str) -> str:
    payload = canonical_json({"rendition": rendition, "destination_key": destination_key, "connection": connection_ref})
    return hashlib.sha256(payload.encode()).hexdigest()


def marker_for(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).digest()
    return "ibx-" + base64.b32encode(digest).decode().lower()[:12]


def classify(intent: PublishIntentRow | None, req_hash: str) -> Action:
    """What to do with an existing intent. Raises 409 if the key was used for a different request."""
    if intent is None:
        return Action.NEW
    if intent.request_hash != req_hash:
        raise IbxError(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            "This idempotency_key was already used for a different publish request.",
            details={"idempotency_key": intent.idempotency_key, "intent_state": intent.state},
        )
    return {
        IntentState.SUCCEEDED.value: Action.REPLAY,
        IntentState.FAILED.value: Action.REPLAY_ERROR,
        IntentState.RETRYABLE.value: Action.RETRY,
    }.get(intent.state, Action.RECONCILE)


def _is_lock_timeout(exc: DBAPIError) -> bool:
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == LOCK_NOT_AVAILABLE or "lock timeout" in str(orig).lower()


def in_flight(intent: PublishIntentRow | None, now: datetime, lease_s: float) -> bool:
    """A PENDING intent touched less than ``lease_s`` ago belongs to a request that is still running."""
    return (
        intent is not None
        and intent.state == IntentState.PENDING.value
        and (now - intent.updated_at).total_seconds() < lease_s
    )


async def lock_xact(session: AsyncSession, name: str, wait_s: float) -> None:
    """``pg_advisory_xact_lock(name)`` in the session's open transaction; wait at most ``wait_s``.

    Raises ``IDEMPOTENCY_IN_PROGRESS`` (retryable) if another request holds the lock for too long.
    """
    try:
        await session.execute(text(f"SET LOCAL lock_timeout = '{int(wait_s * 1000)}ms'"))
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_key(name)})
    except DBAPIError as exc:
        if _is_lock_timeout(exc):
            raise IbxError(
                ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                f"Another request with this key is still running (waited {wait_s:g}s); retry shortly.",
                details={"waited_s": wait_s},
            ) from exc
        raise


@asynccontextmanager
async def advisory_lock(
    factory: async_sessionmaker[AsyncSession], name: str, wait_s: float
) -> AsyncIterator[None]:
    """Hold ``pg_advisory_xact_lock(name)`` on a dedicated connection for the whole ``with`` block.

    This keeps one DB connection busy for the whole block, so it is only for rare work (one certification
    run per destination), never for every publish.
    """
    async with factory() as session:
        try:
            await lock_xact(session, name, wait_s)
            yield
        finally:
            await session.rollback()  # ends the transaction, which releases the xact lock


class IntentLedger:
    """Reads and writes ``publish_intents``. Each write is its own short committed transaction."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def get(self, key: str) -> PublishIntentRow | None:
        async with self.factory() as session:
            return await session.get(PublishIntentRow, key)

    async def mark(
        self,
        key: str,
        state: IntentState,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        async with self.factory() as session, session.begin():
            row = await session.get(PublishIntentRow, key, with_for_update=True)
            if row is None:
                return
            row.state = state.value
            row.updated_at = now
            if result is not None:
                row.result = result
            row.error = error
