"""__KEY__ connector (generated from app/connectors/_template).

Only the platform-specific hooks live here. Kill switch, idempotency, rate limits, the circuit
breaker, spec checks, the rules gate, error mapping and redaction come from ``BaseConnector``.

The template talks to the generic mock in ``mocks/platforms/routes/__KEY__.py``:
    POST   {base_url}/posts              {title, body, marker} -> 201 {id, url, status}
    GET    {base_url}/posts?marker=...   -> {posts: [...]}
    GET    {base_url}/posts/{id}         -> {id, url, status}
    DELETE {base_url}/posts/{id}         -> 204
    GET    {base_url}/posts/{id}/metrics -> {views, likes, comments}
Replace the paths and the response models with the real platform's.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.connectors.base import BaseConnector, ConnectorContext
from app.contract.models import (
    Connection,
    DeleteResult,
    Destination,
    MetricsResult,
    PostStatus,
    PublishResult,
    Rendition,
    StatusResult,
)

# ---------------------------------------------------------------- strict platform response models
# A renamed or missing field raises ValidationError, which the base turns into
# PLATFORM_UNAVAILABLE (details.reason=response_schema_mismatch) and certification fails.


class _Post(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int | str
    url: str
    status: str


class _PostList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    posts: list[_Post]


class _Metrics(BaseModel):
    model_config = ConfigDict(extra="ignore")
    views: int | None = None
    likes: int | None = None
    comments: int | None = None


_STATUS: dict[str, PostStatus] = {"published": "published", "draft": "draft", "deleted": "deleted", "removed": "removed_by_platform"}


def _result(post: _Post) -> PublishResult:
    return PublishResult(platform_post_id=str(post.id), live_url=post.url, status=_STATUS.get(post.status, "unknown"))


class Connector(BaseConnector):
    key = "__KEY__"

    def _base(self, destination: Destination, connection: Connection) -> str:
        return self.instance_value(destination, connection).rstrip("/")

    @staticmethod
    def _headers(connection: Connection) -> dict[str, str]:
        token = connection.secret("token") or connection.secret("api_key") or ""
        return {"Authorization": f"Bearer {token}"}

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        payload = {"title": rendition.title or "", "body": rendition.body, "marker": marker}
        response = await ctx.http.post(f"{self._base(destination, connection)}/posts", json=payload, headers=self._headers(connection))
        return _result(self.parse(response, _Post))

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        response = await ctx.http.get(
            f"{self._base(destination, connection)}/posts", params={"marker": marker}, headers=self._headers(connection)
        )
        posts = self.parse(response, _PostList).posts
        return _result(posts[0]) if posts else None

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        response = await ctx.http.get(
            f"{self._base(destination, connection)}/posts/{platform_post_id}", headers=self._headers(connection)
        )
        if response.status_code == 404:
            return StatusResult(status="deleted", live_url=None)
        post = self.parse(response, _Post)
        return StatusResult(status=_STATUS.get(post.status, "unknown"), live_url=post.url)

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        response = await ctx.http.delete(
            f"{self._base(destination, connection)}/posts/{platform_post_id}", headers=self._headers(connection)
        )
        if response.status_code == 404:
            return DeleteResult(deleted=True)  # already gone: deleting is idempotent
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
        response = await ctx.http.get(
            f"{self._base(destination, connection)}/posts/{platform_post_id}/metrics", headers=self._headers(connection)
        )
        metrics = self.parse(response, _Metrics)
        # Metrics the platform does not expose stay None (never a fake 0).
        return MetricsResult(views=metrics.views, likes=metrics.likes, comments=metrics.comments)
