"""MRSS feed connector: one connector, any number of self-served feeds (``instance.feed_key``).

Unlike the other connectors there is no remote platform: the "platform" is this service's own feed, served
publicly at ``{PUBLIC_BASE_URL}/api/v1/feeds/{feed_key}/mrss.xml`` (see ``feed.py`` and ``app/api/v1/feeds.py``).

    publish   write the item (guid = the publish marker) and put it at the top of the feed's index
    lookup    the item with guid = marker (so a retry after a timeout never adds a second item)
    status    the item exists and the rendered feed, parsed by feedparser, contains its guid -> published
    delete    remove it from the feed and delete it (its item URL then answers 404)
    metrics   clicks counted by the /api/v1/r/{guid} redirect; nothing else is knowable for a feed (null)

``live_url`` is the item's own URL ``/api/v1/feeds/{feed_key}/items/{guid}`` (a one-item RSS document), so a
live check sees it and a deletion check sees 404. Simulation changes nothing here: the feed is always ours.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

from app.connectors.base import BaseConnector, ConnectorContext, note
from app.connectors.mrss.feed import (
    FEED_MAX_ITEMS,
    FeedStore,
    check_feed_key,
    item_path,
    parse_feed,
    render_feed,
)
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


def feed_key_of(value: str) -> str:
    """``ai-weekly`` or ``https://host/api/v1/feeds/ai-weekly/mrss.xml`` -> ``ai-weekly``."""
    value = (value or "").strip()
    if "://" in value:
        parts = [p for p in urlsplit(value).path.split("/") if p]
        if "feeds" in parts and parts.index("feeds") + 1 < len(parts):
            value = parts[parts.index("feeds") + 1]
        else:
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED,
                           "An MRSS feed URL must look like .../api/v1/feeds/<feed_key>/mrss.xml.")
    return check_feed_key(value.lower())


def _http_url(value: str | None) -> str | None:
    return value if value and urlsplit(value).scheme in ("http", "https") and urlsplit(value).hostname else None


class Connector(BaseConnector):
    key = "mrss"
    # How certification should confirm the item: the worker can not reach PUBLIC_BASE_URL (the host-facing
    # address) from inside compose, so the harness should use get_status, which renders and parses the very
    # feed the public route serves. The harness reads it (``LIVE_BY_API``).
    live_check_by_api: ClassVar[str] = (
        "self-served feed: get_status renders the public feed and parses it with feedparser; the worker can not "
        "reach PUBLIC_BASE_URL from inside compose"
    )

    # ------------------------------------------------------------ the feed is ours: nothing to simulate

    def simulate(self, destination: Destination, connection: Connection) -> tuple[Destination, Connection]:
        return destination, connection

    # ------------------------------------------------------------ hooks

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        feed_key = self._feed_key(destination, connection)
        media = [
            {"url": m.url, "mime": m.mime, "bytes": m.bytes, "duration_s": m.duration_s, "width": m.width,
             "height": m.height, "alt": m.alt}
            for m in rendition.media
            if _http_url(m.url)
        ]
        if len(media) != len(rendition.media):
            raise IbxError(ErrorCode.PLATFORM_CONTENT_REJECTED, "Feed media must be public http(s) URLs.",
                           details={"reason": "media_url_not_http"})
        item = {
            "guid": marker,
            "feed_key": feed_key,
            "title": rendition.title or "",
            "description": rendition.body or "",
            "link": _http_url(rendition.link_url) or _http_url(rendition.canonical_url),
            "media": media,
            "tags": list(rendition.tags),
            "language": rendition.language or destination.language,
            "published_at": self.now().isoformat(),
        }
        meta = {k: v for k, v in {
            "title": destination.instance.get("feed_title") or destination.name,
            "description": destination.instance.get("feed_description"),
            "language": destination.instance.get("feed_language") or destination.language,
        }.items() if v}
        stored = await self._store(ctx).put_item(item, meta or None)
        return self._result(stored)

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        item = await self._store(ctx).get_item(marker)
        if item is None or item.get("feed_key") != self._feed_key(destination, connection):
            return None
        return self._result(item)

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        store = self._store(ctx)
        item = await store.get_item(platform_post_id)
        if item is None:
            return StatusResult(status="deleted", live_url=None)
        feed_key = item["feed_key"]
        # prove it the way a reader sees it: render the public feed and parse it with feedparser
        xml = render_feed(feed_key, await store.items(feed_key), self._base_url(), await store.meta(feed_key))
        guids = [entry.get("id") for entry in parse_feed(xml).entries]
        in_feed = platform_post_id in guids
        note(in_feed=in_feed, feed_items=len(guids))
        if not in_feed:
            self.log.info("mrss_item_outside_feed_window", guid=platform_post_id, feed_items=len(guids), window=FEED_MAX_ITEMS)
        return StatusResult(status="published", live_url=self._live_url(item))

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        removed = await self._store(ctx).delete_item(platform_post_id)
        mode = "deleted" if removed else "already_gone"
        note(delete_mode=mode)
        self.log.info("mrss_item_deleted", guid=platform_post_id, delete_mode=mode)
        return DeleteResult(deleted=True)

    async def _fetch_metrics(
        self,
        ctx: ConnectorContext,
        platform_post_id: str,
        destination: Destination,
        connection: Connection,
        since: datetime | None,
    ) -> MetricsResult:
        store = self._store(ctx)
        if await store.get_item(platform_post_id) is None:
            raise IbxError(ErrorCode.NOT_FOUND, "The feed item no longer exists.", details={"platform_post_id": platform_post_id})
        # A feed can not know views, likes or comments (readers fetch the whole feed); only clicks through our
        # redirect are counted. Everything else stays null, never a fake 0.
        return MetricsResult(clicks=await store.clicks(platform_post_id))

    # ------------------------------------------------------------ helpers

    def _store(self, ctx: ConnectorContext) -> FeedStore:
        return FeedStore(ctx.storage, ctx.db_session_factory)

    def _feed_key(self, destination: Destination, connection: Connection) -> str:
        return feed_key_of(self.instance_value(destination, connection))

    def _base_url(self) -> str:
        return self.ctx.settings.PUBLIC_BASE_URL.rstrip("/")

    def _live_url(self, item: dict[str, Any]) -> str:
        return f"{self._base_url()}{item_path(item['feed_key'], item['guid'])}"

    def _result(self, item: dict[str, Any]) -> PublishResult:
        return PublishResult(platform_post_id=item["guid"], live_url=self._live_url(item), status="published")
