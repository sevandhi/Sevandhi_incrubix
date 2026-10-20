"""The alert event every sink receives, and the sink interface.

An ``alerts`` row (kind, destination, body, status open/resolved) becomes up to two events per sink:
``alert.opened`` once, and ``alert.resolved`` once after the row is resolved (e.g. the destination passed
certification again). ``delivered_to`` on the row records what each sink already got, so delivery is idempotent
and a failed delivery is simply retried on the next ``alerts.deliver`` run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from app.config import Settings

OPENED, RESOLVED = "alert.opened", "alert.resolved"


@dataclass
class AlertEvent:
    event: str  # alert.opened | alert.resolved
    alert_id: str
    kind: str
    payload: dict[str, Any]  # the JSON document sent to webhook / GitHub (already redacted)
    delivered_to: list[str]  # the row's current delivery record (read-only for sinks)


@dataclass
class Delivery:
    ok: bool
    marker: str | None = None  # appended to ``delivered_to`` on success, e.g. "webhook" or "github_issue#12"
    detail: str | None = None


class Sink(Protocol):
    name: str

    def enabled(self, settings: Settings) -> bool: ...

    def done(self, event: AlertEvent) -> bool:
        """True when this sink already handled ``event`` (from ``delivered_to``)."""
        ...

    async def send(self, event: AlertEvent, settings: Settings, http: httpx.AsyncClient, now: datetime) -> Delivery: ...


def has(delivered_to: list[str], prefix: str, suffix: str = "") -> bool:
    return any(str(d).startswith(prefix) and str(d).endswith(suffix) for d in delivered_to or [])
