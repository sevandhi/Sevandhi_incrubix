"""Optional GitHub issue sink: only when both ``GITHUB_TOKEN`` and ``GITHUB_REPO`` are set.

* ``alert.opened``  → ``POST {GITHUB_API_URL}/repos/{repo}/issues`` (title, body, labels ``ibx-alert`` + kind);
  the row records ``github_issue#<number>``.
* ``alert.resolved`` → a comment on that issue, then ``PATCH .../issues/<number> {"state": "closed"}``;
  the row records ``github_issue#<number>:closed``.

The token is sent only to ``GITHUB_API_URL`` (default https://api.github.com) and is never logged. A free GitHub
account is enough; nothing here costs money.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings
from app.errors import IbxError
from app.providers.alerts.base import OPENED, AlertEvent, Delivery, has
from app.reliability.safe_fetch import check_url

ISSUE_RX = re.compile(r"^github_issue#(\d+)$")


def _issue_number(delivered_to: list[str]) -> int | None:
    for item in delivered_to or []:
        match = ISSUE_RX.match(str(item))
        if match:
            return int(match.group(1))
    return None


def _title(payload: dict[str, Any]) -> str:
    dest = payload.get("destination") or {}
    return f"[IncuBrix alert] {payload.get('kind')}: {dest.get('name') or dest.get('id') or 'network'}"[:250]


def _body(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or ""
    return (f"{summary}\n\n```json\n{json.dumps(payload, indent=2, sort_keys=True, default=str)[:60000]}\n```\n\n"
            "Opened automatically by the IncuBrix destination network. It is closed when the alert is resolved.")


class GitHubIssueSink:
    name = "github_issue"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.GITHUB_TOKEN and settings.GITHUB_TOKEN.get_secret_value() and settings.GITHUB_REPO)

    def done(self, event: AlertEvent) -> bool:
        if event.event == OPENED:
            return _issue_number(event.delivered_to) is not None
        return has(event.delivered_to, "github_issue#", ":closed") or _issue_number(event.delivered_to) is None

    async def send(self, event: AlertEvent, settings: Settings, http: httpx.AsyncClient, now: datetime) -> Delivery:
        base = settings.GITHUB_API_URL.rstrip("/")
        try:
            await check_url(base, settings)
        except IbxError as err:
            return Delivery(False, detail=f"GitHub API URL refused: {err.message}")
        token = settings.GITHUB_TOKEN.get_secret_value() if settings.GITHUB_TOKEN else ""
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        issues = f"{base}/repos/{settings.GITHUB_REPO}/issues"
        try:
            if event.event == OPENED:
                response = await http.post(issues, headers=headers, timeout=settings.CALLBACK_TIMEOUT_S,
                                           json={"title": _title(event.payload), "body": _body(event.payload),
                                                 "labels": ["ibx-alert", str(event.kind)]})
                if response.status_code == 201:
                    number = response.json().get("number")
                    return Delivery(True, f"github_issue#{int(number)}", f"issue #{number} opened")
                return Delivery(False, detail=f"GitHub answered HTTP {response.status_code}")
            number = _issue_number(event.delivered_to)
            comment = await http.post(f"{issues}/{number}/comments", headers=headers, timeout=settings.CALLBACK_TIMEOUT_S,
                                      json={"body": f"Resolved: {event.payload.get('summary') or 'the alert is resolved'}"})
            closed = await http.patch(f"{issues}/{number}", headers=headers, timeout=settings.CALLBACK_TIMEOUT_S,
                                      json={"state": "closed", "state_reason": "completed"})
            if comment.status_code < 300 and closed.status_code < 300:
                return Delivery(True, f"github_issue#{number}:closed", f"issue #{number} closed")
            return Delivery(False, detail=f"GitHub answered HTTP {comment.status_code}/{closed.status_code}")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return Delivery(False, detail=f"GitHub not reachable ({type(exc).__name__})")
