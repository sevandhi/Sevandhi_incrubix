"""The self-served Media RSS feed: item store, XML rendering (lxml) and validation (feedparser).

Storage layout (the service's own object store, so the api and the worker share it):
    feeds/_items/<guid>.json         one item (guid = the publish marker, unique across feeds)
    feeds/<feed_key>/index.json      the feed's guids, newest first
    feeds/<feed_key>/meta.json       channel title / description / language (from the destination)
    feeds/_clicks/<guid>.json        the click counter behind GET /api/v1/r/<guid>

Every read-modify-write runs under a Postgres advisory lock (per feed for the index, per item for clicks),
so concurrent publishes from the api and the worker never lose an item.

Public URLs (all under ``PUBLIC_BASE_URL``):
    /api/v1/feeds/<feed_key>/mrss.xml        the feed (newest FEED_MAX_ITEMS items)
    /api/v1/feeds/<feed_key>/items/<guid>    one item as a one-item RSS document (the item's live URL; 404 once deleted)
    /api/v1/r/<guid>                         302 to the item's link, counting the click
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import feedparser
from lxml import etree
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import ErrorCode, IbxError
from app.reliability.idempotency import advisory_lock

MEDIA_NS = "http://search.yahoo.com/mrss/"
ATOM_NS = "http://www.w3.org/2005/Atom"
NSMAP = {"media": MEDIA_NS, "atom": ATOM_NS}
FEED_MAX_ITEMS = 100
INDEX_MAX = 1000  # guids kept in a feed's index (older items stay reachable by their own URL until deleted)
FEED_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
GUID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
LOCK_WAIT_S = 15.0
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")  # not allowed in XML 1.0


def clean(text: Any) -> str:
    return _CONTROL.sub("", str(text or ""))


def feed_path(feed_key: str) -> str:
    return f"/api/v1/feeds/{feed_key}/mrss.xml"


def item_path(feed_key: str, guid: str) -> str:
    return f"/api/v1/feeds/{feed_key}/items/{guid}"


def click_path(guid: str) -> str:
    return f"/api/v1/r/{guid}"


def check_feed_key(value: str) -> str:
    if not FEED_KEY_RE.match(value or ""):
        raise IbxError(
            ErrorCode.REQUEST_VALIDATION_FAILED,
            "feed_key must be 1-64 lower-case letters, digits, '-' or '_' (or a URL ending in /feeds/<feed_key>/mrss.xml).",
            details={"errors": [{"loc": ["destination", "instance", "feed_key"], "type": "value_error", "msg": "bad feed_key"}]},
        )
    return value


# ---------------------------------------------------------------- the store


class FeedStore:
    def __init__(self, storage: Any, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        if storage is None or session_factory is None:
            raise IbxError(ErrorCode.DEPENDENCY_UNAVAILABLE, "The feed store needs object storage and the database.")
        self.storage = storage
        self.factory = session_factory

    async def _read(self, key: str) -> Any:
        if await self.storage.stat(key) is None:
            return None
        try:
            return json.loads(await self.storage.get_bytes(key))
        except IbxError as err:
            if err.code == ErrorCode.NOT_FOUND:
                return None  # deleted between stat and get
            raise

    async def _write(self, key: str, value: Any) -> None:
        await self.storage.put_bytes(key, json.dumps(value, sort_keys=True).encode(), "application/json")

    async def get_item(self, guid: str) -> dict[str, Any] | None:
        if not GUID_RE.match(guid or ""):
            return None
        return await self._read(f"feeds/_items/{guid}.json")

    async def meta(self, feed_key: str) -> dict[str, Any]:
        return await self._read(f"feeds/{feed_key}/meta.json") or {}

    async def put_item(self, item: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Add the item (idempotent per guid: an existing item with this guid is returned unchanged)."""
        feed_key, guid = item["feed_key"], item["guid"]
        async with advisory_lock(self.factory, f"mrss-feed:{feed_key}", LOCK_WAIT_S):
            existing = await self.get_item(guid)
            if existing is not None:
                return existing
            await self._write(f"feeds/_items/{guid}.json", item)
            index = await self._read(f"feeds/{feed_key}/index.json") or []
            index = [guid, *[g for g in index if g != guid]][:INDEX_MAX]
            await self._write(f"feeds/{feed_key}/index.json", index)
            if meta:
                await self._write(f"feeds/{feed_key}/meta.json", meta)
        return item

    async def delete_item(self, guid: str) -> bool:
        """Remove the item from its feed and delete it. False if it did not exist."""
        item = await self.get_item(guid)
        if item is None:
            return False
        feed_key = item["feed_key"]
        async with advisory_lock(self.factory, f"mrss-feed:{feed_key}", LOCK_WAIT_S):
            index = await self._read(f"feeds/{feed_key}/index.json") or []
            await self._write(f"feeds/{feed_key}/index.json", [g for g in index if g != guid])
            await self.storage.delete(f"feeds/_items/{guid}.json")
        return True

    async def items(self, feed_key: str, limit: int = FEED_MAX_ITEMS) -> list[dict[str, Any]]:
        index = await self._read(f"feeds/{feed_key}/index.json") or []
        out: list[dict[str, Any]] = []
        for guid in index:
            if len(out) >= limit:
                break
            item = await self.get_item(guid)
            if item is not None and item.get("feed_key") == feed_key:
                out.append(item)
        return out

    async def add_click(self, guid: str) -> int:
        async with advisory_lock(self.factory, f"mrss-click:{guid}", LOCK_WAIT_S):
            count = int((await self._read(f"feeds/_clicks/{guid}.json") or {}).get("clicks", 0)) + 1
            await self._write(f"feeds/_clicks/{guid}.json", {"clicks": count})
        return count

    async def clicks(self, guid: str) -> int:
        return int((await self._read(f"feeds/_clicks/{guid}.json") or {}).get("clicks", 0))


# ---------------------------------------------------------------- rendering


def _sub(parent: etree._Element, tag: str, text: Any = None, **attrs: Any) -> etree._Element:
    element = etree.SubElement(parent, tag, {k: str(v) for k, v in attrs.items() if v is not None})
    if text is not None:
        element.text = clean(text)
    return element


def _rfc822(value: str | None) -> str:
    when = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(UTC)
    return format_datetime(when.astimezone(UTC), usegmt=True)


def _medium(mime: str | None) -> str | None:
    kind = (mime or "").split("/", 1)[0]
    return kind if kind in ("image", "video", "audio") else None


def _item_element(channel: etree._Element, item: dict[str, Any], base_url: str) -> None:
    node = _sub(channel, "item")
    _sub(node, "title", item.get("title") or item["guid"])
    target = item.get("link")
    _sub(node, "link", f"{base_url}{click_path(item['guid'])}" if target else f"{base_url}{item_path(item['feed_key'], item['guid'])}")
    _sub(node, "description", item.get("description") or "")
    _sub(node, "guid", item["guid"], isPermaLink="false")
    _sub(node, "pubDate", _rfc822(item.get("published_at")))
    for tag in item.get("tags") or []:
        _sub(node, "category", tag)
    media = item.get("media") or []
    first = media[0] if media else None
    if first and first.get("bytes") and first.get("mime"):  # RSS 2.0 enclosure needs a length
        _sub(node, "enclosure", url=first["url"], length=first["bytes"], type=first["mime"])
    for entry in media:
        content = _sub(node, f"{{{MEDIA_NS}}}content", url=entry["url"], type=entry.get("mime"), medium=_medium(entry.get("mime")),
                       fileSize=entry.get("bytes"), duration=int(entry["duration_s"]) if entry.get("duration_s") else None,
                       width=entry.get("width"), height=entry.get("height"), lang=item.get("language"))
        if entry.get("alt"):
            _sub(content, f"{{{MEDIA_NS}}}description", entry["alt"], type="plain")
    _sub(node, f"{{{MEDIA_NS}}}title", item.get("title") or item["guid"], type="plain")
    _sub(node, f"{{{MEDIA_NS}}}description", item.get("description") or "", type="plain")


def render_feed(feed_key: str, items: list[dict[str, Any]], base_url: str, meta: dict[str, Any] | None = None,
                *, self_url: str | None = None) -> bytes:
    """An RSS 2.0 + Media RSS document for ``items`` (newest first)."""
    meta = meta or {}
    base_url = base_url.rstrip("/")
    feed_url = f"{base_url}{feed_path(feed_key)}"
    rss = etree.Element("rss", nsmap=NSMAP, version="2.0")
    channel = _sub(rss, "channel")
    _sub(channel, "title", meta.get("title") or f"IncuBrix feed {feed_key}")
    _sub(channel, "link", meta.get("link") or feed_url)
    _sub(channel, "description", meta.get("description") or f"Media RSS feed '{feed_key}' published by the IncuBrix destination network.")
    if meta.get("language"):
        _sub(channel, "language", meta["language"])
    _sub(channel, "generator", "IncuBrix destination network (mrss connector)")
    _sub(channel, "lastBuildDate", _rfc822(items[0].get("published_at") if items else None))
    _sub(channel, "ttl", 15)
    _sub(channel, f"{{{ATOM_NS}}}link", href=self_url or feed_url, rel="self", type="application/rss+xml")
    for item in items:
        _item_element(channel, item, base_url)
    return etree.tostring(rss, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def parse_feed(xml: bytes) -> Any:
    """feedparser's view of a feed; raises if the XML is not well-formed RSS (feedparser's ``bozo`` flag)."""
    parsed = feedparser.parse(xml)
    if parsed.bozo:
        raise ValueError(f"feed is not valid RSS: {parsed.bozo_exception}")
    if parsed.version != "rss20":
        raise ValueError(f"feed is not RSS 2.0 (feedparser says {parsed.version!r})")
    return parsed
