"""HMAC-signed alert webhook (``ALERT_WEBHOOK_URL``).

    POST <ALERT_WEBHOOK_URL>
    Content-Type: application/json
    X-IncuBrix-Event: alert.opened | alert.resolved
    X-IncuBrix-Delivery: <alert_id>:<event>            (stable: a receiver can de-duplicate retries)
    X-IncuBrix-Timestamp: <unix seconds>
    X-IncuBrix-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw body)>

The same signing scheme as job callbacks and the partner webhook (docs/PARTNER_WEBHOOK.md). The secret is
``ALERT_WEBHOOK_SECRET`` (defaults to ``CALLBACK_SECRET``, then ``API_TOKEN``) and is never logged. The URL goes
through the SSRF guard (``safe_fetch.check_url``). Any 2xx counts as delivered; anything else is retried later.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.config import Settings
from app.errors import IbxError
from app.jobs.callbacks import SIGNATURE_HEADER, TIMESTAMP_HEADER, encode, sign
from app.providers.alerts.base import OPENED, AlertEvent, Delivery
from app.reliability.safe_fetch import check_url


def webhook_secret(settings: Settings) -> bytes:
    if settings.ALERT_WEBHOOK_SECRET is not None and settings.ALERT_WEBHOOK_SECRET.get_secret_value():
        return settings.ALERT_WEBHOOK_SECRET.get_secret_value().encode()
    return settings.callback_key()


class WebhookSink:
    name = "webhook"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.ALERT_WEBHOOK_URL)

    def done(self, event: AlertEvent) -> bool:
        return ("webhook" if event.event == OPENED else "webhook:resolved") in (event.delivered_to or [])

    async def send(self, event: AlertEvent, settings: Settings, http: httpx.AsyncClient, now: datetime) -> Delivery:
        url = str(settings.ALERT_WEBHOOK_URL)
        try:
            await check_url(url, settings)
        except IbxError as err:
            return Delivery(False, detail=f"webhook URL refused: {err.message}")
        body = encode(event.payload)
        signature = sign(webhook_secret(settings), body)
        timestamp = signature.split(",", 1)[0].removeprefix("t=")
        headers = {"Content-Type": "application/json", "X-IncuBrix-Event": event.event,
                   "X-IncuBrix-Delivery": f"{event.alert_id}:{event.event}", TIMESTAMP_HEADER: timestamp,
                   SIGNATURE_HEADER: signature}
        try:
            response = await http.post(url, content=body, headers=headers, timeout=settings.CALLBACK_TIMEOUT_S)
        except httpx.HTTPError as exc:
            return Delivery(False, detail=f"webhook not reachable ({type(exc).__name__})")
        if 200 <= response.status_code < 300:
            return Delivery(True, "webhook" if event.event == OPENED else "webhook:resolved", f"HTTP {response.status_code}")
        return Delivery(False, detail=f"webhook answered HTTP {response.status_code}")
