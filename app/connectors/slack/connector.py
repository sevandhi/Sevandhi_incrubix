"""Slack app connector: one connector, any channel the app's bot was invited to (``instance.channel_id``).

Platform calls (Slack Web API, ``Authorization: Bearer xoxb-...``, JSON bodies; base ``https://slack.com/api``):
    publish   POST chat.postMessage        {channel, text, metadata {event_type ibx_publication, event_payload {marker}}}
    permalink GET  chat.getPermalink        ?channel&message_ts                  -> live_url
    lookup    GET  conversations.history    ?channel&include_all_metadata=true&limit=100   (finds the marker)
    status    GET  conversations.history    ?channel&latest=ts&oldest=ts&inclusive=true&limit=1  (gone = deleted)
    delete    POST chat.delete              {channel, ts}        message_not_found = already gone
    metrics   GET  conversations.history    (the message's reactions[].count -> likes, reply_count -> comments)

Slack answers almost every error with **HTTP 200** and ``{"ok": false, "error": "<code>"}`` (red_team #5), so
every answer whose ``ok`` is not true goes through ``map_error``, whose keys are ``"200:<code>"``. The rate limit
is HTTP 429 + ``Retry-After`` (the base parses the reset). ``internal_error`` / ``fatal_error`` on a create may
mean the message was posted anyway ("part of the operation may have succeeded"), so the outcome is UNKNOWN and
the retry reconciles by the marker instead of posting twice.

The marker travels in Slack *message metadata* (invisible to people), so the message text stays clean.
``platform_post_id`` is ``"<channel>:<ts>"`` (a Slack message is identified by both).

The real workspace is only used when a caller sends a real bot token; the shipped tests use the mock in
``mocks/platforms/routes/slack.py`` (and an opt-in live test that runs only when SLACK_BOT_TOKEN is set).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from app.connectors.base import BaseConnector, ConnectorContext, normalise_instance, note
from app.contract.models import (
    Connection,
    DeleteResult,
    Destination,
    MetricsResult,
    PublishResult,
    Rendition,
    StatusResult,
)
from app.errors import ErrorCode, IbxError

SLACK_API = "https://slack.com/api"
EVENT_TYPE = "ibx_publication"
LOOKUP_LIMIT = 100  # newest messages scanned for a marker (Slack's default page size)
SIM_TOKEN = "xoxb-sim-0000-ibx-placeholder"  # what our Slack mock accepts (simulation only, never a real token)
UNKNOWN_OUTCOME = {"internal_error", "fatal_error", "service_unavailable", "request_timeout"}

# ---------------------------------------------------------------- strict platform models
# Only fields we read are declared; each is required, so a renamed field fails loudly (schema mismatch).


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PostMessageOk(_Strict):
    ok: bool
    channel: str
    ts: str


class PermalinkOk(_Strict):
    ok: bool
    permalink: str


class Metadata(_Strict):
    event_type: str
    event_payload: dict[str, Any] = {}


class Reaction(_Strict):
    name: str
    count: int


class Message(_Strict):
    ts: str
    text: str = ""
    subtype: str | None = None
    metadata: Metadata | None = None
    reactions: list[Reaction] = []
    reply_count: int | None = None


class HistoryOk(_Strict):
    ok: bool
    messages: list[Message]


# ---------------------------------------------------------------- pure helpers (unit tested)


def message_text(rendition: Rendition) -> str:
    """Slack mrkdwn: an optional bold title line, the body, and the link if the body does not carry it."""
    parts = []
    if rendition.title:
        parts.append(f"*{rendition.title.strip()}*")
    if rendition.body:
        parts.append(rendition.body.strip())
    if rendition.link_url and rendition.link_url not in (rendition.body or ""):
        parts.append(rendition.link_url)
    return "\n".join(parts)


def split_post_id(platform_post_id: str, default_channel: str | None) -> tuple[str, str]:
    """``"C123:1700000000.000100"`` -> (channel, ts); a bare ts uses the destination's channel."""
    channel, sep, ts = platform_post_id.rpartition(":")
    if sep and channel:
        return channel, ts
    if not default_channel:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "A Slack post id is '<channel>:<ts>' (or send the channel).")
    return default_channel, platform_post_id


def has_marker(message: Message, marker: str) -> bool:
    meta = message.metadata
    return meta is not None and meta.event_type == EVENT_TYPE and meta.event_payload.get("marker") == marker


class Connector(BaseConnector):
    key = "slack"

    # ------------------------------------------------------------ simulation

    def sim_credentials(self, connection: Connection) -> dict[str, SecretStr]:
        return {"bot_token": SecretStr(SIM_TOKEN)}

    def simulate(self, destination: Destination, connection: Connection) -> tuple[Destination, Connection]:
        """The channel id stays; only the API base moves to our Slack mock (the base class would replace the
        channel id itself with a URL)."""
        mock = self.ctx.settings.MOCK_PLATFORM_URL.rstrip("/")
        api = self._api_base(destination, connection)
        if api.startswith(mock):
            if not connection.credentials and (self.ctx.simulation or self.ctx.settings.APP_ENV in ("dev", "test")):
                connection = connection.model_copy(update={"credentials": self.sim_credentials(connection)})
            return destination, connection
        if not self.ctx.simulation or not self._has_instance(destination, connection):
            return destination, connection
        # the tenant comes from the channel id only, so publish and a later get_status (which may carry no
        # destination URL) always reach the same mock workspace
        channel = normalise_instance(self.instance_value(destination, connection))
        tenant = re.sub(r"[^a-z0-9]+", "-", channel).strip("-")[:60] or "default"
        instance = {**destination.instance, "api_base": f"{mock}/{self.mock_route or self.key}/{tenant}/api"}
        return (destination.model_copy(update={"instance": instance}),
                connection.model_copy(update={"credentials": self.sim_credentials(connection)}))

    # ------------------------------------------------------------ hooks

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        channel = self._channel(destination, connection)
        payload = {
            "channel": channel,
            "text": message_text(rendition),
            "unfurl_links": False,
            "metadata": {"event_type": EVENT_TYPE, "event_payload": {"marker": marker}},
        }
        data = await self._api(ctx, "POST", "chat.postMessage", destination, connection, json=payload, writing=True)
        posted = PostMessageOk.model_validate(data)
        return PublishResult(
            platform_post_id=f"{posted.channel}:{posted.ts}",
            live_url=await self._permalink(ctx, posted.channel, posted.ts, destination, connection),
            status="published",
        )

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        channel = self._channel(destination, connection)
        data = await self._api(ctx, "GET", "conversations.history", destination, connection,
                               params={"channel": channel, "include_all_metadata": "true", "limit": LOOKUP_LIMIT})
        for message in HistoryOk.model_validate(data).messages:
            if has_marker(message, marker):
                return PublishResult(platform_post_id=f"{channel}:{message.ts}",
                                     live_url=await self._permalink(ctx, channel, message.ts, destination, connection),
                                     status="published")
        return None

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        channel, ts = split_post_id(platform_post_id, self._channel_or_none(destination, connection))
        message = await self._message(ctx, channel, ts, destination, connection)
        if message is None or message.subtype == "tombstone":  # a deleted thread parent stays as a tombstone
            return StatusResult(status="deleted", live_url=None)
        return StatusResult(status="published", live_url=await self._permalink(ctx, channel, ts, destination, connection))

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        channel, ts = split_post_id(platform_post_id, self._channel_or_none(destination, connection))
        response = await self._send(ctx, "POST", "chat.delete", destination, connection, json={"channel": channel, "ts": ts})
        body = self._json(response)
        if body.get("ok") is True:
            mode = "deleted"
        elif body.get("error") == "message_not_found":
            mode = "already_gone"
        else:
            raise self._error(response, writing=False)
        note(delete_mode=mode)
        self.log.info("slack_message_deleted", post_id=platform_post_id, delete_mode=mode)
        return DeleteResult(deleted=True)

    async def _fetch_metrics(
        self,
        ctx: ConnectorContext,
        platform_post_id: str,
        destination: Destination,
        connection: Connection,
        since: datetime | None,
    ) -> MetricsResult:
        channel, ts = split_post_id(platform_post_id, self._channel_or_none(destination, connection))
        message = await self._message(ctx, channel, ts, destination, connection)
        if message is None:
            raise IbxError(ErrorCode.NOT_FOUND, "The Slack message no longer exists.", details={"platform_post_id": platform_post_id})
        # Slack exposes reactions and thread replies; views, clicks, shares, saves and watch time stay null.
        return MetricsResult(likes=sum(r.count for r in message.reactions), comments=message.reply_count or 0)

    # ------------------------------------------------------------ Slack Web API plumbing

    async def _message(
        self, ctx: ConnectorContext, channel: str, ts: str, destination: Destination, connection: Connection
    ) -> Message | None:
        data = await self._api(ctx, "GET", "conversations.history", destination, connection,
                               params={"channel": channel, "latest": ts, "oldest": ts, "inclusive": "true", "limit": 1,
                                       "include_all_metadata": "true"})
        return next((m for m in HistoryOk.model_validate(data).messages if m.ts == ts), None)

    async def _permalink(
        self, ctx: ConnectorContext, channel: str, ts: str, destination: Destination, connection: Connection
    ) -> str:
        data = await self._api(ctx, "GET", "chat.getPermalink", destination, connection,
                               params={"channel": channel, "message_ts": ts})
        return PermalinkOk.model_validate(data).permalink

    async def _api(
        self,
        ctx: ConnectorContext,
        method: str,
        name: str,
        destination: Destination,
        connection: Connection,
        *,
        writing: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """One Web API call; any answer that is not ``ok: true`` (HTTP 200 included) is a mapped IbxError."""
        response = await self._send(ctx, method, name, destination, connection, **kwargs)
        body = self._json(response)
        if response.is_error or body.get("ok") is not True:
            raise self._error(response, writing=writing)
        return body

    async def _send(
        self, ctx: ConnectorContext, method: str, name: str, destination: Destination, connection: Connection, **kwargs: Any
    ) -> httpx.Response:
        url = f"{self._api_base(destination, connection)}/{name}"
        headers = {"Authorization": f"Bearer {self._token(connection)}"}
        if "json" in kwargs:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return await ctx.http.request(method, url, headers=headers, **kwargs)

    def _error(self, response: httpx.Response, *, writing: bool) -> IbxError:
        err = self.map_error(response)
        code = self._json(response).get("error")
        if isinstance(code, str):
            err.details["platform_code"] = code
        if writing and code in UNKNOWN_OUTCOME:
            err.details["outcome"] = "unknown"  # Slack: "part of the operation may have succeeded"
        return err

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _api_base(self, destination: Destination | None, connection: Connection | None) -> str:
        candidates: list[Any] = []
        if destination is not None:
            candidates.append(destination.instance.get("api_base"))
        if connection is not None:
            candidates.append((connection.model_extra or {}).get("api_base"))
        return next((c.rstrip("/") for c in candidates if isinstance(c, str) and c.strip()), SLACK_API)

    def _channel(self, destination: Destination, connection: Connection) -> str:
        return self.instance_value(destination, connection)

    def _channel_or_none(self, destination: Destination, connection: Connection) -> str | None:
        return self._channel(destination, connection) if self._has_instance(destination, connection) else None

    @staticmethod
    def _token(connection: Connection) -> str:
        token = connection.secret("bot_token") or connection.secret("token") or connection.secret("access_token")
        if not token:
            raise IbxError(ErrorCode.CONNECTION_AUTH_EXPIRED, "Slack needs a bot token (credentials.bot_token = 'xoxb-...').",
                           details={"reason": "missing_credentials", "action": "reconnect_required"})
        return token
