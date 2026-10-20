"""Rules drift (plan §6/§16): recertification re-reads a destination's rules sources and compares their hash.

Sources are the ones rules capture reads (``rules_capture.gather_sources``): the platform's own API (Lemmy
community sidebar, Discourse ``/about.json``) and the ``rules_url`` page, with robots.txt and 1 request/s/host.
The hash is sha256 of the extracted text (the same function capture uses), so markup noise does not count.

* The first check after a rule set is confirmed records the **baseline** (``destinations.extra.rules_watch``).
* A later check with a different hash is **drift**: the confirmed rule set becomes ``needs_review`` (so publications
  answer ``DEST_RULES_UNCONFIRMED`` until a person re-confirms) and a ``rules_drift`` alert opens.
* When a person confirms a new version (rules capture → confirm, or ``PUT /destinations/{id}/rules``), the next check
  re-baselines and the open ``rules_drift`` alert is resolved.

Real third-party pages are never fetched in simulation mode (only our own hosts and mock-platforms are).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalogue import rules_capture as rc
from app.catalogue import service
from app.catalogue.normalize import host_of
from app.config import Settings
from app.db.tables import AlertRow, DestinationRow, RuleSetRow
from app.errors import IbxError
from app.logging import get_logger
from app.reliability.redact import redact
from app.reliability.safe_fetch import local_host_allowed

log = get_logger("app.recert.drift")
API_SOURCES = ("lemmy", "discourse")  # connectors whose rules capture reads the platform API (rules_capture._platform_api)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def _source_urls(dest: DestinationRow) -> list[str]:
    instance = dest.instance or {}
    urls = []
    if dest.connector_key in API_SOURCES:  # the platform API rules capture reads (sidebar / about.json)
        urls = [str(v) for k, v in instance.items() if k in ("community_url", "base_url") and "://" in str(v)] or [dest.url]
    if dest.rules_url:
        urls.append(dest.rules_url)
    return urls


def _allowed_in_simulation(url: str, settings: Settings) -> bool:
    host = host_of(url)
    return host == host_of(settings.MOCK_PLATFORM_URL) or local_host_allowed(host, settings)


async def resolve_reconfirmed(session: AsyncSession, destination_id: str | None = None) -> int:
    """Resolve open ``rules_drift`` alerts whose destination now has a confirmed rule set newer than the drift."""
    stmt = select(AlertRow).where(AlertRow.kind == "rules_drift", AlertRow.status == "open")
    if destination_id:
        stmt = stmt.where(AlertRow.destination_id == destination_id)
    resolved = 0
    for alert in (await session.execute(stmt.with_for_update(skip_locked=True))).scalars():
        current = await service.confirmed_rules(session, alert.destination_id or "")
        if current is not None and current.version > int((alert.body or {}).get("rule_set_version") or 0):
            alert.status = "resolved"
            alert.body = {**(alert.body or {}), "resolved_by_rule_set_version": current.version}
            resolved += 1
    return resolved


async def check(destination_id: str, *, session_factory: async_sessionmaker[AsyncSession], settings: Settings,
                now: datetime, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Re-hash the rules sources of one destination. Returns what happened (stored on the recert job result)."""
    if not settings.RULES_DRIFT_ENABLED:
        return {"status": "disabled"}
    async with session_factory() as session, session.begin():
        await resolve_reconfirmed(session, destination_id)
        dest = await service.get_destination(session, destination_id)
        confirmed = await service.confirmed_rules(session, dest.id)
    if confirmed is None:
        return {"status": "skipped", "why": "no confirmed rule set (unconfirmed, or already waiting for review)"}
    urls = _source_urls(dest)
    if settings.SIMULATION_MODE and not all(_allowed_in_simulation(u, settings) for u in urls):
        return {"status": "skipped", "why": "simulation mode: real third-party pages are not fetched"}
    try:
        gathered = await rc.gather_sources(dest, settings, http, use_platform_api=True)
    except IbxError as err:  # e.g. the rules_url host is refused by the SSRF guard
        return {"status": "error", "why": f"{err.code.value}: {err.message}"}
    if not gathered.docs:
        return {"status": "no_source", "why": "no readable rules source (no sidebar/about API, no rules_url)",
                "warnings": gathered.warnings}
    current = hashlib.sha256(gathered.text.encode()).hexdigest()
    sources = [{"kind": d.kind, "url": d.url, "bytes": d.size} for d in gathered.docs]
    async with session_factory() as session, session.begin():
        dest = await session.get(DestinationRow, destination_id, with_for_update=True)
        row = await session.get(RuleSetRow, confirmed.id, with_for_update=True)
        watch = dict((dest.extra or {}).get("rules_watch") or {})
        if row is None or row.status != "confirmed":
            return {"status": "skipped", "why": "the rule set changed while checking"}
        if watch.get("rule_set_id") != row.id:  # first check of this confirmed version: record the baseline
            watch = {"rule_set_id": row.id, "rule_set_version": row.version, "baseline_sha256": current,
                     "baseline_at": _iso(now), "sources": sources}
            outcome: dict[str, Any] = {"status": "baseline", "sha256": current, "sources": sources}
        elif watch.get("baseline_sha256") == current:
            outcome = {"status": "unchanged", "sha256": current, "sources": sources}
        else:
            row.status = "needs_review"
            body = redact({"rule_set_id": row.id, "rule_set_version": row.version, "baseline_sha256": watch.get("baseline_sha256"),
                           "current_sha256": current, "baseline_at": watch.get("baseline_at"), "detected_at": _iso(now),
                           "sources": sources, "destination": dest.name,
                           "action": f"re-confirm the rules: POST /api/v1/destinations/{dest.id}/rules/capture then confirm "
                                     f"the proposal, or PUT /api/v1/destinations/{dest.id}/rules"})
            session.add(AlertRow(kind="rules_drift", destination_id=dest.id, connection_id=dest.connection_id, body=body,
                                 status="open", delivered_to=[]))
            watch["drift"] = {"detected_at": _iso(now), "current_sha256": current}
            outcome = {"status": "drift", "baseline_sha256": watch.get("baseline_sha256"), "sha256": current,
                       "rule_set_version": row.version, "rule_set_status": "needs_review", "sources": sources}
            log.warning("rules_drift_detected", destination_id=dest.id, rule_set_id=row.id,
                        hosts=sorted({urlsplit(s["url"] or "").hostname or "" for s in sources}))
        watch["last_checked_at"], watch["last_sha256"] = _iso(now), current
        dest.extra = {**(dest.extra or {}), "rules_watch": watch}
    return outcome

