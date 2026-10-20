"""Fetch, normalise and store post metrics (``metrics_snapshots``).

* ``fetch_snapshot`` calls the connector's contract ``fetch_metrics`` (the full pipeline: kill switch, connection,
  rate limits, breaker, error mapping) through a connector instance whose HTTP client records the platform's JSON
  answers for this call only. The normalised record, the raw source fields (per ``normalize.MAPPING``), a
  consistency check and where the numbers came from are stored as one ``metrics_snapshots`` row.
* ``poll`` does that for recent published publications (the scheduled ``metrics.poll`` job and
  ``POST /api/v1/metrics/poll``).

Response bodies themselves are never stored: only the mapped fields, plus method/path/status of each call.
"""

from __future__ import annotations

import asyncio
import contextvars
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalogue import service
from app.catalogue.normalize import host_of
from app.clock import Clock
from app.config import Settings
from app.connectors.base import CALL_META, BaseConnector, ConnectorContext
from app.contract.registry import Registry, get_registry
from app.db.tables import DestinationRow, MetricsSnapshotRow, PublicationRow
from app.errors import ErrorCode, IbxError
from app.logging import get_logger
from app.metrics import normalize as nm
from app.reliability.redact import redact

log = get_logger("app.metrics")
NORMALIZER_VERSION = "1.0.0"
MAX_RECORDED_BODY = 2_000_000
_CAPTURE: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar("ibx_metrics_capture", default=None)
_clients: dict[int, httpx.AsyncClient] = {}
_connectors: dict[tuple[Any, ...], BaseConnector] = {}


async def _record(response: httpx.Response) -> None:
    """httpx response hook: keep the JSON body of every platform answer made inside ``capture()``."""
    calls = _CAPTURE.get()
    if calls is None:
        return
    entry: dict[str, Any] = {"method": response.request.method, "path": response.request.url.path, "status": response.status_code}
    try:
        await response.aread()
        if "json" in response.headers.get("content-type", "") and len(response.content) <= MAX_RECORDED_BODY:
            entry["json"] = response.json()
    except Exception:  # noqa: BLE001 - recording is best effort; the connector reads the body itself
        pass
    calls.append(entry)


def _client(settings: Settings) -> httpx.AsyncClient:
    client = _clients.get(id(settings))
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.PLATFORM_TIMEOUT_S, connect=min(5.0, settings.PLATFORM_TIMEOUT_S)),
            follow_redirects=False,
            headers={"User-Agent": f"ibx-destination-network/{settings.SERVICE_VERSION}"},
            event_hooks={"response": [_record]},
        )
        _clients[id(settings)] = client
    return client


def metrics_connector(key: str, *, settings: Settings, session_factory: async_sessionmaker[AsyncSession], storage: Any,
                      clock: Clock, registry: Registry | None = None) -> BaseConnector:
    """A connector instance on the recording HTTP client (one per key, settings, database, clock and storage)."""
    registry = registry or get_registry()
    manifest = registry.manifest(key)
    if manifest is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, "Unknown connector key.", details={"connector_key": key})
    cache_key = (key, id(settings), id(session_factory), id(registry), id(clock), id(storage))
    connector = _connectors.get(cache_key)
    if connector is None or connector.ctx.http.is_closed:
        ctx = ConnectorContext(manifest=manifest, settings=settings, http=_client(settings), clock=clock,
                               log=get_logger(f"connector.{key}", connector=key), db_session_factory=session_factory,
                               simulation=settings.SIMULATION_MODE, storage=storage)
        connector = registry.build(key, ctx)
        _connectors[cache_key] = connector
    return connector


def source_of(settings: Settings, dest: DestinationRow) -> str:
    """Where the numbers come from: ``own_service`` (our MRSS feed), ``mock`` (mock-platforms / simulation),
    ``real_local_instance`` (our own Lemmy / Discourse / PeerTube containers) or ``real_platform``."""
    if dest.connector_key == "mrss":
        return "own_service"
    if settings.SIMULATION_MODE:
        return "mock"
    hosts = [host_of(str(v)) for v in (dest.instance or {}).values() if "://" in str(v)] or [host_of(dest.url or "")]
    mock_host = host_of(settings.MOCK_PLATFORM_URL)
    if any(h == mock_host or h == "mock-platforms" for h in hosts):
        return "mock"
    own = {h for h in settings.CERT_OWN_HOSTS if h not in ("localhost", "127.0.0.1")}
    if any(h in own or h.startswith("peertube") or h.endswith(".ibx.test") for h in hosts):
        return "real_local_instance"
    return "real_platform"


async def _own_fields(key: str, platform_post_id: str, storage: Any, factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Raw fields of platforms that are our own service (no HTTP call to record)."""
    if key != "mrss":
        return {}
    from app.connectors.mrss.feed import FeedStore

    return {"feeds/_clicks/<guid>.json:clicks": await FeedStore(storage, factory).clicks(platform_post_id)}


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def snapshot_out(row: MetricsSnapshotRow) -> dict[str, Any]:
    raw = dict(row.raw or {})
    return {
        "snapshot_id": row.id,
        "captured_at": _iso(row.captured_at),
        "connector_key": row.connector_key,
        "destination_id": row.destination_id,
        "publication_id": row.publication_id,
        "platform_post_id": row.platform_post_id,
        "metrics": {k: (row.metrics or {}).get(k) for k in nm.KEYS},
        "engagement": nm.engagement(row.metrics or {}),
        "supported": raw.get("supported", []),
        "source": raw.get("source"),
        "consistent": raw.get("consistent"),
        "raw": raw,
    }


async def fetch_snapshot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: Any,
    clock: Clock,
    destination_id: str,
    platform_post_id: str,
    publication_id: str | None = None,
    since: datetime | None = None,
    registry: Registry | None = None,
) -> dict[str, Any]:
    """One ``fetch_metrics`` call → one stored, normalised snapshot (returned as ``snapshot_out``)."""
    async with session_factory() as session:
        dest = await service.get_destination(session, destination_id)
        conn_row = await service.destination_connection(session, dest)
        await session.commit()
    connector = metrics_connector(dest.connector_key, settings=settings, session_factory=session_factory, storage=storage,
                                  clock=clock, registry=registry)
    connection = service.contract_connection(conn_row, dest.connector_key, connector.manifest.auth)
    calls: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    capture_token, meta_token = _CAPTURE.set(calls), CALL_META.set(meta)
    try:
        result = await connector.fetch_metrics(platform_post_id, connection, since, service.contract_destination(dest, None))
    finally:
        _CAPTURE.reset(capture_token)
        CALL_META.reset(meta_token)
    metrics = nm.normalize(result)
    fields = nm.source_fields(dest.connector_key, calls, platform_post_id,
                             await _own_fields(dest.connector_key, platform_post_id, storage, session_factory))
    derived = nm.derive(dest.connector_key, fields)
    check = nm.consistent(metrics, derived)
    if check is False:
        log.warning("metrics_inconsistent", connector=dest.connector_key, platform_post_id=platform_post_id,
                    metrics=metrics, derived=derived)
    raw = redact({
        "source": source_of(settings, dest),
        "simulation": settings.SIMULATION_MODE,
        "supported": nm.supported(dest.connector_key),
        "fields": fields,
        "derived": derived,
        "consistent": check,
        "calls": [{k: c.get(k) for k in ("method", "path", "status")} for c in calls],
        "warnings": list(meta.get("warnings") or []),
        "since": _iso(since),
        "normalizer_version": NORMALIZER_VERSION,
        "connector_version": connector.manifest.version,
    })
    async with session_factory() as session, session.begin():
        row = MetricsSnapshotRow(connector_key=dest.connector_key, destination_id=dest.id, publication_id=publication_id,
                                 platform_post_id=platform_post_id, metrics=metrics, raw=raw, captured_at=clock.now())
        session.add(row)
        await session.flush()
        out = snapshot_out(row)
    log.info("metrics_snapshot_stored", snapshot_id=out["snapshot_id"], connector=dest.connector_key,
             publication_id=publication_id, metrics=metrics, source=raw["source"], consistent=check)
    return out


async def fetch_for_publication(*, session_factory: async_sessionmaker[AsyncSession], settings: Settings, storage: Any,
                                clock: Clock, publication_id: str, since: datetime | None = None,
                                registry: Registry | None = None) -> dict[str, Any]:
    async with session_factory() as session:
        pub = await session.get(PublicationRow, publication_id)
        await session.commit()
    if pub is None:
        raise IbxError(ErrorCode.NOT_FOUND, "Unknown publication.", details={"publication_id": publication_id})
    if not pub.platform_post_id:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"The publication has no platform post (state '{pub.state}').",
                       retryable=False, details={"publication_id": publication_id, "state": pub.state})
    return await fetch_snapshot(session_factory=session_factory, settings=settings, storage=storage, clock=clock,
                                destination_id=pub.destination_id, platform_post_id=pub.platform_post_id,
                                publication_id=pub.id, since=since, registry=registry)


async def poll(*, session_factory: async_sessionmaker[AsyncSession], settings: Settings, storage: Any, clock: Clock,
               destination_id: str | None = None, limit: int | None = None, registry: Registry | None = None,
               concurrency: int = 4) -> dict[str, Any]:
    """Snapshot every recent, published, non-test publication on a destination whose connector offers metrics.
    Excluded destinations are skipped until they pass recertification."""
    registry = registry or get_registry()
    now = clock.now()
    since = now - timedelta(days=settings.METRICS_POLL_WINDOW_DAYS)
    stmt = (select(PublicationRow.id, DestinationRow.connector_key)
            .join(DestinationRow, DestinationRow.id == PublicationRow.destination_id)
            .where(PublicationRow.state == "published", PublicationRow.is_test.is_(False),
                   PublicationRow.platform_post_id.is_not(None), PublicationRow.published_at >= since,
                   DestinationRow.status != "excluded")
            .order_by(PublicationRow.published_at.desc())
            .limit(limit or settings.METRICS_POLL_MAX))
    if destination_id:
        stmt = stmt.where(PublicationRow.destination_id == destination_id)
    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()
    targets = [r.id for r in rows if "metrics" in ((registry.manifest(r.connector_key) or _NoManifest).capabilities)]
    skipped = len(rows) - len(targets)
    gate = asyncio.Semaphore(max(1, concurrency))
    results: list[dict[str, Any]] = []

    async def one(pub_id: str) -> None:
        async with gate:
            try:
                snap = await fetch_for_publication(session_factory=session_factory, settings=settings, storage=storage,
                                                   clock=clock, publication_id=pub_id, registry=registry)
                results.append({"publication_id": pub_id, "ok": True, "snapshot_id": snap["snapshot_id"],
                                "metrics": snap["metrics"], "source": snap["source"]})
            except IbxError as err:
                results.append({"publication_id": pub_id, "ok": False, "code": err.code.value, "message": err.message})

    await asyncio.gather(*(one(p) for p in targets))
    ok = sum(1 for r in results if r["ok"])
    log.info("metrics_poll_done", polled=len(targets), ok=ok, failed=len(targets) - ok, skipped_no_metrics=skipped)
    return {"polled": len(targets), "ok": ok, "failed": len(targets) - ok, "skipped_no_metrics_capability": skipped,
            "window_days": settings.METRICS_POLL_WINDOW_DAYS, "results": sorted(results, key=lambda r: r["publication_id"])}


class _NoManifest:
    capabilities: tuple[str, ...] = ()


async def list_snapshots(session: AsyncSession, *, publication_id: str | None = None, destination_id: str | None = None,
                         connector_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    stmt = select(MetricsSnapshotRow)
    if publication_id:
        stmt = stmt.where(MetricsSnapshotRow.publication_id == publication_id)
    if destination_id:
        stmt = stmt.where(MetricsSnapshotRow.destination_id == destination_id)
    if connector_key:
        stmt = stmt.where(MetricsSnapshotRow.connector_key == connector_key)
    rows = (await session.execute(stmt.order_by(MetricsSnapshotRow.captured_at.desc(), MetricsSnapshotRow.id.desc())
                                  .limit(max(1, min(limit, 1000))))).scalars()
    return [snapshot_out(r) for r in rows]
