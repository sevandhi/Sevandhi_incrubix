"""Test-post TTL sweeper (Must-have 5): every ``SWEEPER_EVERY_S`` (60 s) delete every ``test_posts`` row that is
still live after ``TEST_POST_TTL_S`` (300 s). Certification normally deletes its post within seconds; the sweeper
catches anything a crashed or failed run left behind, so no test post outlives 10 minutes.

A delete failure opens one ``test_post_delete_failed`` alert per post and is retried on the next sweep.
``scripts/prove_mh5.py`` exports the created → deleted audit (``GET /api/v1/test-posts``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalogue import service
from app.certification.levels import delete_mode_name
from app.certification.runtime import connector_for
from app.clock import Clock
from app.config import Settings
from app.connectors.base import CALL_META
from app.contract.registry import Registry
from app.db.tables import AlertRow, DestinationRow, TestPostRow
from app.logging import get_logger
from app.reliability.redact import redact

log = get_logger("app.certification.sweeper")


async def sweep(
    *, settings: Settings, session_factory: async_sessionmaker[AsyncSession], storage: Any, clock: Clock,
    registry: Registry | None = None, limit: int = 100,
) -> dict[str, Any]:
    now = clock.now()
    async with session_factory() as session:
        due = list((await session.execute(
            select(TestPostRow).where(TestPostRow.deleted_at.is_(None), TestPostRow.expires_at <= now)
            .order_by(TestPostRow.expires_at).limit(limit))).scalars())
    deleted, failed = [], []
    for post in due:
        try:
            mode = await _delete_one(post, settings=settings, session_factory=session_factory, storage=storage,
                                     clock=clock, registry=registry)
            async with session_factory() as session, session.begin():
                row = await session.get(TestPostRow, post.id, with_for_update=True)
                if row is not None and row.deleted_at is None:
                    row.deleted_at, row.delete_mode = clock.now(), mode
            deleted.append(post.id)
        except Exception as exc:  # noqa: BLE001 - every failure becomes an alert; the next sweep retries
            failed.append(post.id)
            await _alert(session_factory, post, exc)
    if deleted or failed:
        log.info("test_post_sweep", deleted=deleted, failed=failed)
    return {"checked": len(due), "deleted": deleted, "failed": failed}


async def _delete_one(post: TestPostRow, *, settings: Settings, session_factory: Any, storage: Any, clock: Clock,
                      registry: Registry | None) -> str:
    async with session_factory() as session:
        dest = await session.get(DestinationRow, post.destination_id) if post.destination_id else None
        if dest is None:
            raise RuntimeError("the test post's destination no longer exists")
        conn_row = await service.destination_connection(session, dest)
    connector = connector_for(post.connector_key, settings=settings, session_factory=session_factory,
                              storage=storage, clock=clock, registry=registry)
    connection = service.contract_connection(conn_row, dest.connector_key, connector.manifest.auth)
    contract_dest = service.contract_destination(dest, None)
    post_id = post.platform_post_id
    if post_id.startswith("marker:"):
        conn = await connector._resolve_connection(connection)
        sim_dest, sim_conn = connector.simulate(contract_dest, conn)
        found = await connector._lookup(connector.ctx, post_id.removeprefix("marker:"), sim_dest, sim_conn)
        if found is None:
            return "sweeper_not_found"
        post_id = found.platform_post_id
    unpublish = getattr(connector, "unpublish_to_draft", None)
    if "delete" not in connector.manifest.capabilities and unpublish is not None:
        record = await unpublish(post_id, connection, contract_dest)  # never reported as deleted
        return f"sweeper_{record.get('removal_mode') or 'unpublish_to_draft'}"
    meta: dict[str, Any] = {}
    token = CALL_META.set(meta)
    try:
        result = await connector.delete(post_id, connection, contract_dest)
    except Exception:
        # e.g. Discourse answers 422 for a topic that is already deleted: check before alerting
        status = await connector.get_status(post_id, connection, contract_dest)
        if status.status in ("deleted", "removed_by_platform"):
            return "sweeper_already_deleted"
        raise
    finally:
        CALL_META.reset(token)
    if result.deleted:  # e.g. sweeper_soft_deleted for Discourse, sweeper_purged for Lemmy
        return f"sweeper_{delete_mode_name(meta.get('delete_mode') or getattr(connector, 'delete_mode', None))}"
    status = await connector.get_status(post_id, connection, contract_dest)
    if status.status in ("draft", "deleted", "removed_by_platform"):
        return f"sweeper_{'unpublish_to_draft' if status.status == 'draft' else status.status}"
    raise RuntimeError(f"delete returned deleted=false and the post is still '{status.status}'")


async def _alert(session_factory: Any, post: TestPostRow, exc: Exception) -> None:
    async with session_factory() as session, session.begin():
        existing = (await session.execute(select(AlertRow).where(
            AlertRow.kind == "test_post_delete_failed", AlertRow.status == "open",
            AlertRow.body["test_post_id"].astext == str(post.id)))).scalar_one_or_none()
        if existing is None:
            session.add(AlertRow(kind="test_post_delete_failed", destination_id=post.destination_id,
                                 body=redact({"test_post_id": post.id, "connector_key": post.connector_key,
                                              "platform_post_id": post.platform_post_id, "run_id": post.run_id,
                                              "error": f"{type(exc).__name__}: {exc}"}),
                                 status="open", delivered_to=["log"]))
    log.error("test_post_delete_failed", test_post_id=post.id, connector=post.connector_key, error=redact(exc))
