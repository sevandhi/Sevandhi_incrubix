"""Dev.to / Forem connector (Forem API v1, ``api-key`` header). One connector, any Forem instance.

Platform calls:
    publish   POST {base}/api/articles {"article": {title, body_markdown, published, tags, canonical_url, ...}}
    lookup    GET  {base}/api/articles/me/all?page=N&per_page=100   match the marker in body_markdown
    status    GET  {base}/api/articles/{id}  (published only; 404 for drafts) then /me/all for drafts
    metrics   /me/all entry: page_views_count -> views, public_reactions_count -> likes, comments_count -> comments

The marker is an invisible markdown comment ``[//]: # (ibx-xxxx)`` appended to the body.

**No delete.** Forem API v1 has no article DELETE, so ``delete`` is not in the manifest and the contract
answers 404 METHOD_NOT_SUPPORTED. Certification calls :meth:`Connector.unpublish_to_draft` instead:
* on an instance where our account is admin/moderator (``instance.forem_admin: true``, e.g. the mock Forem)
  it calls ``PUT /api/articles/{id}/unpublish`` (removal mode ``admin_unpublish``);
* otherwise it reverts the article to a draft with ``PUT /api/articles/{id} {"published": false}`` and wipes
  title and body to a tombstone (removal mode ``unpublish_to_draft``).
Either way the public URL stops answering, but the article still exists: the helper never reports
``deleted: true``.

Draft-only mode: ``instance.draft_only: true`` (used for any run against the real dev.to) always sends
``published: false``; the result is ``status = draft`` with ``live_url = None``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.connectors.base import BaseConnector, ConnectorContext, note, platform_id, warn
from app.contract.models import (
    Connection,
    Destination,
    MetricsResult,
    PublishResult,
    Rendition,
    StatusResult,
)
from app.errors import ErrorCode, IbxError

ACCEPT = "application/vnd.forem.api-v1+json"
PER_PAGE = 100
MAX_PAGES = 10
NO_DELETE_WARNING = "PLATFORM_NO_DELETE: Forem API v1 has no article delete; the article was unpublished, not deleted"
TOMBSTONE_TITLE = "[removed] IncuBrix test article"
TOMBSTONE_BODY = "This draft was a certification test and its content was removed."


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Article(_Loose):
    id: int
    url: str
    published: bool | None = None  # GET /api/articles/{id} has no "published" (it only serves published ones)
    published_at: str | None = None


class _MeArticle(_Loose):
    id: int
    url: str
    published: bool
    body_markdown: str | None = None
    page_views_count: int | None = None
    public_reactions_count: int | None = None
    comments_count: int | None = None


def marker_comment(marker: str) -> str:
    return f"[//]: # ({marker})"


class Connector(BaseConnector):
    key = "devto"
    mock_route = "forem"

    # ------------------------------------------------------------ helpers

    def _api(self, destination: Destination, connection: Connection) -> str:
        return self.instance_value(destination, connection).rstrip("/") + "/api"

    @staticmethod
    def _headers(connection: Connection) -> dict[str, str]:
        key = connection.secret("api_key") or connection.secret("token") or ""
        return {"api-key": key, "Accept": ACCEPT}

    async def _me_all(
        self, ctx: ConnectorContext, destination: Destination, connection: Connection
    ) -> list[_MeArticle]:
        """Every article of the key's user (published and drafts), newest first, bounded pages."""
        out: list[_MeArticle] = []
        for page in range(1, MAX_PAGES + 1):
            response = await ctx.http.get(
                f"{self._api(destination, connection)}/articles/me/all",
                params={"page": page, "per_page": PER_PAGE},
                headers=self._headers(connection),
            )
            self.check(response)
            items = response.json()
            if not isinstance(items, list):
                raise IbxError(ErrorCode.PLATFORM_UNAVAILABLE, "devto: /articles/me/all did not return a list.",
                               details={"reason": "response_schema_mismatch", "fields": ["<root>"]})
            out += [_MeArticle.model_validate(item) for item in items]
            if len(items) < PER_PAGE:
                break
        return out

    async def _find_mine(
        self, ctx: ConnectorContext, article_id: str, destination: Destination, connection: Connection
    ) -> _MeArticle | None:
        return next((a for a in await self._me_all(ctx, destination, connection) if str(a.id) == str(article_id)), None)

    # ------------------------------------------------------------ hooks

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        draft_only = bool(destination.instance.get("draft_only"))
        article: dict[str, Any] = {
            "title": rendition.title or "",
            "body_markdown": _body_markdown(rendition, marker),
            "published": not draft_only,
        }
        if rendition.tags:
            article["tags"] = ", ".join(rendition.tags)
        if rendition.canonical_url:
            article["canonical_url"] = rendition.canonical_url
        image = next((m.url for m in rendition.media if (m.mime or "").startswith("image/")), None)
        if image:
            article["main_image"] = image
        for name in ("organization_id", "series"):
            if destination.instance.get(name) not in (None, ""):
                article[name] = destination.instance[name]
        if draft_only:
            note(draft_only=True)
        response = await ctx.http.post(
            f"{self._api(destination, connection)}/articles", json={"article": article}, headers=self._headers(connection)
        )
        created = self.parse(response, _Article)
        published = created.published if created.published is not None else not draft_only
        if published:
            return PublishResult(platform_post_id=str(created.id), live_url=created.url, status="published")
        return PublishResult(platform_post_id=str(created.id), live_url=None, status="draft")

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        needle = marker_comment(marker)
        for article in await self._me_all(ctx, destination, connection):
            if article.body_markdown and needle in article.body_markdown:
                if article.published:
                    return PublishResult(platform_post_id=str(article.id), live_url=article.url, status="published")
                return PublishResult(platform_post_id=str(article.id), live_url=None, status="draft")
        return None

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        response = await ctx.http.get(
            f"{self._api(destination, connection)}/articles/{platform_post_id}", headers=self._headers(connection)
        )
        if response.status_code != 404:
            article = self.parse(response, _Article)
            return StatusResult(status="published", live_url=article.url)
        mine = await self._find_mine(ctx, platform_post_id, destination, connection)
        if mine is None:
            # not public and not among the author's articles: removed in the web UI or by the platform
            return StatusResult(status="deleted", live_url=None)
        if mine.published:
            return StatusResult(status="published", live_url=mine.url)
        return StatusResult(status="draft", live_url=None)

    async def _fetch_metrics(
        self,
        ctx: ConnectorContext,
        platform_post_id: str,
        destination: Destination,
        connection: Connection,
        since: datetime | None,
    ) -> MetricsResult:
        mine = await self._find_mine(ctx, platform_post_id, destination, connection)
        if mine is None:
            raise IbxError(ErrorCode.NOT_FOUND, "devto: the article is not among this account's articles.",
                           details={"platform_post_id": platform_post_id})
        if since is not None:
            warn("METRICS_LIFETIME_TOTALS: Forem exposes lifetime totals only; 'since' is not applied")
        # shares, saves, watch time and clicks are not exposed by Forem: None, never a fake 0.
        return MetricsResult(views=mine.page_views_count, likes=mine.public_reactions_count, comments=mine.comments_count)

    # ------------------------------------------------------------ removal without delete (certification)

    async def unpublish_to_draft(
        self,
        platform_post_id: str,
        connection: Connection,
        destination: Destination,
        *,
        wipe: bool = True,
    ) -> dict[str, Any]:
        """Take a test article off the public site. Returns an honest removal record, never ``deleted: true``.

        Runs through the same pipeline as ``get_status`` (kill switch, connection, rate limits, breaker,
        error mapping). Result keys: deleted (always False), unpublished, removal_mode, wiped, warnings.
        """

        async def hook(dest: Destination, conn: Connection) -> dict[str, Any]:
            return await self._unpublish(self.ctx, platform_id(platform_post_id), dest, conn, wipe=wipe)

        return await self._simple("status", connection, destination, hook, platform_post_id=platform_post_id)

    async def _unpublish(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection, *, wipe: bool
    ) -> dict[str, Any]:
        api = self._api(destination, connection)
        headers = self._headers(connection)
        if destination.instance.get("forem_admin"):
            response = await ctx.http.put(
                f"{api}/articles/{platform_post_id}/unpublish", params={"note": "IncuBrix certification cleanup"}, headers=headers
            )
            self.check(response)
            mode = "admin_unpublish"
        else:
            mode = "unpublish_to_draft"
        if mode == "unpublish_to_draft" or wipe:
            article: dict[str, Any] = {"published": False}
            if wipe:
                article.update(title=TOMBSTONE_TITLE, body_markdown=TOMBSTONE_BODY, tags="", canonical_url=None)
            response = await ctx.http.put(f"{api}/articles/{platform_post_id}", json={"article": article}, headers=headers)
            self.check(response)
        note(removal_mode=mode)
        warn(NO_DELETE_WARNING)
        return {"deleted": False, "unpublished": True, "removal_mode": mode, "wiped": wipe, "warnings": [NO_DELETE_WARNING]}


def _body_markdown(rendition: Rendition, marker: str) -> str:
    body = rendition.body or ""
    if rendition.link_url and rendition.link_url not in body:
        body = f"{body}\n\n{rendition.link_url}" if body else rendition.link_url
    return f"{body}\n\n{marker_comment(marker)}\n"
