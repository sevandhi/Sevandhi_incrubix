"""Test-only native connector for the in-process harness tests (talks to tests/p2d_fakes/mock_routes.py)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.connectors.base import BaseConnector
from app.contract.models import DeleteResult, MetricsResult, PublishResult, StatusResult


class _Post(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    url: str
    status: str


class _Posts(BaseModel):
    posts: list[_Post]


class _Metrics(BaseModel):
    views: int
    likes: int


class Connector(BaseConnector):
    key = "fakeforum"

    def _base(self, d, c):  # noqa: ANN001, ANN202
        return self.instance_value(d, c).rstrip("/")

    @staticmethod
    def _h(c):  # noqa: ANN001, ANN205
        return {"Authorization": f"Bearer {c.secret('token') or ''}"}

    async def _create(self, ctx, rendition, destination, connection, marker):  # noqa: ANN001, ANN201
        r = await ctx.http.post(f"{self._base(destination, connection)}/posts", headers=self._h(connection),
                                json={"title": rendition.title, "body": rendition.body, "marker": marker})
        p = self.parse(r, _Post)
        return PublishResult(platform_post_id=str(p.id), live_url=p.url, status="published")

    async def _lookup(self, ctx, marker, destination, connection):  # noqa: ANN001, ANN201
        r = await ctx.http.get(f"{self._base(destination, connection)}/posts", params={"marker": marker}, headers=self._h(connection))
        posts = self.parse(r, _Posts).posts
        return PublishResult(platform_post_id=str(posts[0].id), live_url=posts[0].url, status="published") if posts else None

    async def _get_status(self, ctx, platform_post_id, destination, connection):  # noqa: ANN001, ANN201
        r = await ctx.http.get(f"{self._base(destination, connection)}/posts/{platform_post_id}", headers=self._h(connection))
        if r.status_code == 404:
            return StatusResult(status="deleted", live_url=None)
        p = self.parse(r, _Post)
        return StatusResult(status="draft" if p.status == "draft" else "published", live_url=p.url)

    async def _delete(self, ctx, platform_post_id, destination, connection):  # noqa: ANN001, ANN201
        r = await ctx.http.delete(f"{self._base(destination, connection)}/posts/{platform_post_id}", headers=self._h(connection))
        if r.status_code == 404:
            return DeleteResult(deleted=True)
        self.check(r)
        return DeleteResult(deleted=r.json().get("deleted", True))

    async def _fetch_metrics(self, ctx, platform_post_id, destination, connection, since: datetime | None):  # noqa: ANN001, ANN201
        r = await ctx.http.get(f"{self._base(destination, connection)}/posts/{platform_post_id}/metrics", headers=self._h(connection))
        m = self.parse(r, _Metrics)
        return MetricsResult(views=m.views, likes=m.likes)
