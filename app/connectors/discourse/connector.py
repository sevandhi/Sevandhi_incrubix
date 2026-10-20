"""Discourse connector (native tier). One connector, any number of forums (``instance.base_url``).

Platform calls (Discourse REST API, ``Api-Key`` + ``Api-Username`` headers):
    publish   POST   {base}/posts.json  {title, raw, category, external_id=<marker>}
    lookup    GET    {base}/t/external_id/{marker}.json   -> 301 Location .../t/{slug}/{id}.json (404 = not found)
    status    GET    {base}/t/{id}.json                    (admin view; ``deleted_at`` set = soft deleted)
    delete    DELETE {base}/t/{id}.json                    -> 200, a SOFT delete (``delete_mode = "soft"``);
                                                              a second delete answers 422 (measured) = already deleted
    metrics   GET    {base}/t/{id}.json  views, like_count, posts_count-1 (replies), link_counts[].clicks

Measured on a real local Discourse 2026.10 (tests/fixtures/recorded/discourse):
* an unknown / revoked key answers 403 ``error_type: invalid_access`` -> CONNECTION_AUTH_EXPIRED;
  Discourse uses the same ``invalid_access`` type when a working key may not post in a category, so a 403 on
  create is checked with ``GET /session/current.json``: if the key still works, it is PLATFORM_CONTENT_REJECTED
  (``reason: category_permission``) and the connection is NOT paused;
* content problems answer 422 ``{"action": "create_post", "errors": [...]}`` -> PLATFORM_CONTENT_REJECTED;
* a deleted topic is still readable with the admin key (``deleted_at`` set) and 404 for the public.
Rate limits answer 429 with ``Retry-After`` and ``extras.wait_seconds`` (both parsed by the base).

A forum that queues new topics for review answers 200 ``{"action": "enqueued", "pending_post": {...}}``.
The connector then returns ``status = pending`` and ``platform_post_id = "pending:<marker>"``; status,
delete and metrics resolve that id through the external_id lookup once a moderator approves it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict

from app.connectors.base import BaseConnector, ConnectorContext, note, warn
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

PENDING_PREFIX = "pending:"
_TOPIC_PATH = re.compile(r"/t/(?:[^/]+/)?(\d+)(?:\.json)?/?$")

# ---------------------------------------------------------------- strict platform response models
# Only the fields we use are declared; a missing or renamed one raises ValidationError, which the
# base turns into PLATFORM_UNAVAILABLE (details.reason=response_schema_mismatch).


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _CreatedPost(_Loose):
    id: int
    topic_id: int
    topic_slug: str


class _Enqueued(_Loose):
    action: str
    pending_post: dict[str, Any]


class _LinkCount(_Loose):
    clicks: int = 0


class _FirstPost(_Loose):
    id: int
    hidden: bool = False
    link_counts: list[_LinkCount] | None = None


class _PostStream(_Loose):
    posts: list[_FirstPost]


class _Topic(_Loose):
    id: int
    slug: str
    posts_count: int
    views: int
    like_count: int
    deleted_at: str | None = None
    visible: bool = True
    external_id: str | None = None
    post_stream: _PostStream


class Connector(BaseConnector):
    key = "discourse"
    delete_mode: ClassVar[str] = "soft"  # Discourse keeps deleted topics for staff; the public gets 404

    # ------------------------------------------------------------ helpers

    def _base(self, destination: Destination, connection: Connection) -> str:
        return self.instance_value(destination, connection).rstrip("/")

    @staticmethod
    def _headers(destination: Destination, connection: Connection) -> dict[str, str]:
        extra = connection.model_extra or {}
        username = (
            connection.secret("api_username")
            or extra.get("api_username")
            or destination.instance.get("api_username")
            or "system"
        )
        key = connection.secret("api_key") or connection.secret("token") or ""
        return {"Api-Key": key, "Api-Username": str(username), "Accept": "application/json"}

    def _live_url(self, base: str, slug: str, topic_id: int) -> str:
        return f"{base}/t/{slug}/{topic_id}"

    async def _find_topic_id(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> int | None:
        """Topic id for an external_id, from the 301 Location (404 = no such topic)."""
        base = self._base(destination, connection)
        response = await ctx.http.get(
            f"{base}/t/external_id/{marker}.json", headers=self._headers(destination, connection)
        )
        if response.status_code == 404:
            return None
        if response.is_redirect:
            match = _TOPIC_PATH.search(httpx.URL(response.headers.get("location", "")).path)
            if match:
                return int(match.group(1))
            raise IbxError(
                ErrorCode.PLATFORM_UNAVAILABLE,
                "discourse: external_id lookup redirected to an unexpected location.",
                details={"reason": "response_schema_mismatch", "fields": ["location"]},
            )
        self.check(response)
        topic = self.parse(response, _Topic)  # some versions answer 200 with the topic itself
        return topic.id

    async def _topic_id(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> int | None:
        if platform_post_id.startswith(PENDING_PREFIX):
            return await self._find_topic_id(ctx, platform_post_id.removeprefix(PENDING_PREFIX), destination, connection)
        try:
            return int(platform_post_id)
        except ValueError as exc:
            raise IbxError(
                ErrorCode.REQUEST_VALIDATION_FAILED,
                "discourse: platform_post_id must be a topic id or 'pending:<marker>'.",
            ) from exc

    async def _get_topic(
        self, ctx: ConnectorContext, topic_id: int, destination: Destination, connection: Connection
    ) -> _Topic | None:
        response = await ctx.http.get(
            f"{self._base(destination, connection)}/t/{topic_id}.json", headers=self._headers(destination, connection)
        )
        if response.status_code == 404:
            return None
        return self.parse(response, _Topic)

    # ------------------------------------------------------------ hooks

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        base = self._base(destination, connection)
        payload: dict[str, Any] = {"title": rendition.title or "", "raw": _raw_body(rendition), "external_id": marker}
        category = destination.instance.get("category_id") or destination.instance.get("category")
        if category not in (None, ""):
            payload["category"] = category
        response = await ctx.http.post(f"{base}/posts.json", json=payload, headers=self._headers(destination, connection))
        if response.status_code == 403:
            await self._raise_403(ctx, response, base, destination, connection)
        self.check(response)
        body = response.json()
        if isinstance(body, dict) and body.get("action") == "enqueued":
            _Enqueued.model_validate(body)
            warn("DISCOURSE_ENQUEUED: the forum queued the topic for moderator review; status stays 'pending' until approved")
            return PublishResult(platform_post_id=f"{PENDING_PREFIX}{marker}", live_url=None, status="pending")
        post = _CreatedPost.model_validate(body)
        return PublishResult(
            platform_post_id=str(post.topic_id), live_url=self._live_url(base, post.topic_slug, post.topic_id), status="published"
        )

    async def _raise_403(
        self, ctx: ConnectorContext, response: httpx.Response, base: str, destination: Destination, connection: Connection
    ) -> None:
        """A 403 on create: a dead key (pause the connection) or a category this key may not post in (refuse
        this post only). The key is proven by reading its own session."""
        err = self.map_error(response)
        if "key is invalid" in response.text:  # measured body of a dead key: no need to ask
            raise err
        probe = await ctx.http.get(f"{base}/session/current.json", headers=self._headers(destination, connection))
        if probe.status_code != 200:
            raise err
        details = {k: v for k, v in err.details.items() if k not in ("retry_policy", "error_map_key")}
        raise IbxError(
            ErrorCode.PLATFORM_CONTENT_REJECTED,
            "discourse answered HTTP 403, but the API key works: it may not post in this category "
            f"(category {destination.instance.get('category_id') or destination.instance.get('category') or 'default'}).",
            details={**details, "reason": "category_permission"},
        )

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        topic_id = await self._find_topic_id(ctx, marker, destination, connection)
        if topic_id is None:
            return None
        topic = await self._get_topic(ctx, topic_id, destination, connection)
        if topic is None or topic.deleted_at:
            return None  # a deleted earlier attempt does not count as published
        base = self._base(destination, connection)
        return PublishResult(platform_post_id=str(topic.id), live_url=self._live_url(base, topic.slug, topic.id), status="published")

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        topic_id = await self._topic_id(ctx, platform_post_id, destination, connection)
        if topic_id is None:
            # still in the review queue (or rejected there): nothing public yet
            return StatusResult(status="pending", live_url=None)
        topic = await self._get_topic(ctx, topic_id, destination, connection)
        if topic is None or topic.deleted_at:
            return StatusResult(status="deleted", live_url=None)
        live_url = self._live_url(self._base(destination, connection), topic.slug, topic.id)
        first = topic.post_stream.posts[0] if topic.post_stream.posts else None
        if not topic.visible or (first is not None and first.hidden):
            note(platform_state="unlisted" if not topic.visible else "hidden_by_flags")
            return StatusResult(status="removed_by_platform", live_url=live_url)
        return StatusResult(status="published", live_url=live_url)

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        topic_id = await self._topic_id(ctx, platform_post_id, destination, connection)
        if topic_id is None:
            raise IbxError(
                ErrorCode.NOT_FOUND,
                "discourse: the topic is still in the review queue (or was rejected); there is nothing to delete yet.",
                details={"platform_post_id": platform_post_id},
            )
        response = await ctx.http.delete(
            f"{self._base(destination, connection)}/t/{topic_id}.json", headers=self._headers(destination, connection)
        )
        note(delete_mode=self.delete_mode)
        if response.status_code == 404:
            return DeleteResult(deleted=True)  # already gone
        if response.status_code == 422:
            # Measured on Discourse 2026.10: deleting an already deleted (or unknown) topic answers 422
            # "There was an error deleting that topic". Only when the topic really is gone does that count as
            # deleted; a 422 on a live topic is mapped as usual.
            topic = await self._get_topic(ctx, topic_id, destination, connection)
            if topic is None or topic.deleted_at:
                return DeleteResult(deleted=True)
        self.check(response)
        return DeleteResult(deleted=True)

    async def _fetch_metrics(
        self,
        ctx: ConnectorContext,
        platform_post_id: str,
        destination: Destination,
        connection: Connection,
        since: datetime | None,
    ) -> MetricsResult:
        topic_id = await self._topic_id(ctx, platform_post_id, destination, connection)
        topic = await self._get_topic(ctx, topic_id, destination, connection) if topic_id is not None else None
        if topic is None:
            raise IbxError(ErrorCode.NOT_FOUND, "discourse: no such topic.", details={"platform_post_id": platform_post_id})
        first = topic.post_stream.posts[0] if topic.post_stream.posts else None
        clicks = None
        if first is not None and first.link_counts is not None:
            clicks = sum(link.clicks for link in first.link_counts)
        if since is not None:
            warn("METRICS_LIFETIME_TOTALS: Discourse exposes lifetime totals only; 'since' is not applied")
        # shares / saves / watch time are not exposed by Discourse: None, never a fake 0.
        return MetricsResult(
            views=topic.views,
            likes=topic.like_count,
            comments=max(0, topic.posts_count - 1),
            clicks=clicks,
        )


def _raw_body(rendition: Rendition) -> str:
    """The first post's markdown. A link that is not already in the body is appended on its own line."""
    body = rendition.body or ""
    link = rendition.link_url
    if link and link not in body:
        body = f"{body}\n\n{link}" if body else link
    for media in rendition.media:
        if media.url not in body:
            body += f"\n\n![{media.alt or ''}]({media.url})" if (media.mime or "").startswith("image/") else f"\n\n{media.url}"
    return body
