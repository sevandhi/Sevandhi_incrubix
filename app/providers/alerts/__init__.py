"""Alert delivery (plan §6): log (always), HMAC-signed webhook (``ALERT_WEBHOOK_URL``), GitHub issue (``GITHUB_TOKEN``).

``deliver_pending()`` sends every alert event a configured sink has not received yet: ``alert.opened`` for each
alert, then ``alert.resolved`` once the row is resolved (the certification harness resolves
``certification_failed`` when the destination passes again; rules drift is resolved when a person re-confirms).
Each row is locked (``FOR UPDATE SKIP LOCKED``) while it is delivered, so two workers never double-send, and
``delivered_to`` records each success. It runs after every recertification and as the periodic ``alerts.deliver`` job.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.tables import AlertRow, CertificationRunRow, DestinationRow
from app.logging import get_logger
from app.providers.alerts.base import OPENED, RESOLVED, AlertEvent, Delivery, Sink
from app.providers.alerts.github_issue import GitHubIssueSink
from app.providers.alerts.log import LogSink
from app.providers.alerts.webhook import WebhookSink
from app.reliability.redact import redact

log = get_logger("app.alerts")
SINKS: list[Sink] = [LogSink(), WebhookSink(), GitHubIssueSink()]
LOOKBACK_DAYS = 30

__all__ = ["OPENED", "RESOLVED", "SINKS", "AlertEvent", "Delivery", "build_payload", "deliver_pending", "enabled_sinks"]


def enabled_sinks(settings: Settings) -> list[Sink]:
    return [s for s in SINKS if s.enabled(settings)]


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def _summary(kind: str, event: str, dest: DestinationRow | None, body: dict[str, Any], run: CertificationRunRow | None) -> str:
    name = dest.name if dest else "a destination"
    trigger = f"{run.trigger} " if run is not None and run.trigger else ""
    if kind == "certification_failed":
        if event == RESOLVED:
            level = dest.level if dest else "?"
            return f"{name} passed certification again: level {level} restored and the exclusion lifted."
        cause = ""
        if body.get("reason") == "response_schema_mismatch":
            fields = ", ".join(body.get("fields") or []) or "?"
            cause = f" - response_schema_mismatch: the platform's answer no longer has field(s) {fields} (renamed field?)"
        return (f"{name} failed {trigger}certification at {body.get('step')} ({body.get('code')}){cause}: demoted to L0 "
                "and excluded from publications and recommendations until it passes again.")
    if kind == "rules_drift":
        if event == RESOLVED:
            return f"The rules of {name} were re-confirmed by a person."
        return (f"The rules source of {name} changed since its rules were confirmed: rule set v{body.get('rule_set_version')} "
                "needs review. Publications are refused (DEST_RULES_UNCONFIRMED) until a person re-confirms.")
    if kind == "certification_inconclusive":
        return f"{name}: certification was rate limited {body.get('code')} several times in a row; the level is kept."
    return f"{kind} for {name}" + (" (resolved)" if event == RESOLVED else "")


async def build_payload(session: AsyncSession, alert: AlertRow, event: str, settings: Settings, now: datetime) -> dict[str, Any]:
    dest = await session.get(DestinationRow, alert.destination_id) if alert.destination_id else None
    body = dict(alert.body or {})
    run = await session.get(CertificationRunRow, body["run_id"]) if body.get("run_id") else None
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    payload: dict[str, Any] = {
        "event": event,
        "alert_id": alert.id,
        "kind": alert.kind,
        "status": alert.status,
        "summary": _summary(alert.kind, event, dest, body, run),
        "created_at": _iso(alert.created_at),
        "sent_at": _iso(now),
        "destination": None if dest is None else {
            "id": dest.id, "name": dest.name, "connector_key": dest.connector_key, "url": dest.url, "level": dest.level,
            "status": dest.status, "excluded_reason": dest.excluded_reason, "last_certified_at": _iso(dest.last_certified_at)},
        "run": None if run is None else {"run_id": run.id, "trigger": run.trigger, "state": run.state,
                                         "failure_code": run.failure_code, "finished_at": _iso(run.finished_at)},
        "details": body,
        "links": {"network_health": f"{base}/ui/network", "api": f"{base}/api/v1/network/health"
                  + (f"?destination_id={dest.id}" if dest else "")}
                 | ({"certification": f"{base}/api/v1/certifications/{run.id}"} if run is not None else {}),
        "service": "ibx-destination-network",
    }
    return redact(payload)


async def deliver_pending(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    now: datetime,
    *,
    http: httpx.AsyncClient | None = None,
    destination_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Deliver every pending alert event; returns one record per attempted delivery."""
    sinks = enabled_sinks(settings)
    own_http = http is None
    http = http or httpx.AsyncClient(follow_redirects=False)
    report: list[dict[str, Any]] = []
    try:
        async with session_factory() as session:
            stmt = (select(AlertRow.id).where(AlertRow.created_at >= func.now() - func.make_interval(0, 0, 0, LOOKBACK_DAYS))
                    .order_by(AlertRow.created_at).limit(limit))
            if destination_id:
                stmt = stmt.where(AlertRow.destination_id == destination_id)
            ids = list((await session.execute(stmt)).scalars())
        for alert_id in ids:
            async with session_factory() as session, session.begin():
                alert = (await session.execute(select(AlertRow).where(AlertRow.id == alert_id)
                                               .with_for_update(skip_locked=True))).scalar_one_or_none()
                if alert is None:
                    continue  # another worker is delivering it
                delivered = list(alert.delivered_to or [])
                events = [OPENED] + ([RESOLVED] if alert.status == "resolved" else [])
                for sink in sinks:
                    for name in events:
                        event = AlertEvent(name, alert.id, alert.kind, {}, delivered)
                        if sink.done(event):
                            continue
                        event.payload = await build_payload(session, alert, name, settings, now)
                        result = await sink.send(event, settings, http, now)
                        report.append({"alert_id": alert.id, "kind": alert.kind, "event": name, "sink": sink.name,
                                       "ok": result.ok, "detail": result.detail})
                        if not result.ok:
                            log.warning("alert_delivery_failed", alert_id=alert.id, sink=sink.name, alert_event=name,
                                        detail=result.detail)
                            break  # keep the order: never send "resolved" to a sink that missed "opened"
                        if result.marker:
                            delivered.append(result.marker)
                if delivered != list(alert.delivered_to or []):
                    alert.delivered_to = delivered
    finally:
        if own_http:
            await http.aclose()
    return report
