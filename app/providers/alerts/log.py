"""Log sink: one structured JSON log line per alert event (always on)."""

from __future__ import annotations

from datetime import datetime

import httpx

from app.config import Settings
from app.logging import get_logger
from app.providers.alerts.base import OPENED, AlertEvent, Delivery

log = get_logger("app.alerts")


class LogSink:
    name = "log"

    def enabled(self, settings: Settings) -> bool:
        return True

    def done(self, event: AlertEvent) -> bool:
        # "log" = opened logged (the certification harness logs and records it when it opens the alert)
        marker = "log" if event.event == OPENED else "log:resolved"
        return marker in (event.delivered_to or [])

    async def send(self, event: AlertEvent, settings: Settings, http: httpx.AsyncClient, now: datetime) -> Delivery:
        log.warning("alert_event", alert_event=event.event, alert_id=event.alert_id, alert_kind=event.kind, alert=event.payload)
        return Delivery(True, "log" if event.event == OPENED else "log:resolved")
