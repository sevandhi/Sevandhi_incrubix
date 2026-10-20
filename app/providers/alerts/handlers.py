"""``alerts.deliver`` (periodic): resolve re-confirmed rules drift, then deliver pending alert events to the sinks."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.jobs.scheduler import periodic
from app.providers.alerts import deliver_pending


@job_handler("alerts.deliver", max_attempts=1)
async def alerts_deliver(ctx: JobContext) -> dict[str, Any]:
    from app.recert.drift import resolve_reconfirmed

    async with ctx.session_factory() as session, session.begin():
        resolved = await resolve_reconfirmed(session)
    report = await deliver_pending(ctx.session_factory, ctx.settings, ctx.clock.now())
    failed = [r for r in report if not r["ok"]]
    for item in failed:
        ctx.warn(f"{item['sink']} {item['event']} {item['alert_id']}: {item['detail']}")
    return {"rules_drift_resolved": resolved, "delivered": len(report) - len(failed), "failed": len(failed)}


periodic("alerts.deliver", "alerts.deliver", every_s=max(10, get_settings().ALERT_DELIVER_EVERY_S))
