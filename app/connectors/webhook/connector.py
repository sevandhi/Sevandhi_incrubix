"""HMAC partner-webhook connector (protocol tier). The protocol is docs/PARTNER_WEBHOOK.md.

    publish  POST   {endpoint}/posts                    signed JSON, Idempotency-Key = marker -> 201/200 {id, url, status}
    lookup   GET    {endpoint}/posts?idempotency_key=K  -> {posts: [...]}
    status   GET    {endpoint}/posts/{id}               -> {id, url, status}; 404/410 = deleted
    delete   DELETE {endpoint}/posts/{id}               -> 204/200; 404/410 = already deleted

Every request (GET and DELETE too, over an empty body) carries
``X-IncuBrix-Signature: t=<unix>,v1=<hmac_sha256(secret, "<t>.<body>")>``. The partner rejects a
timestamp outside a 5-minute window and dedups POSTs by ``Idempotency-Key``. A 401/403 means the
partner revoked us: CONNECTION_AUTH_EXPIRED, and a stored connection is paused by the base.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, SecretStr

from app.connectors.base import BaseConnector, ConnectorContext
from app.connectors.webhook import signing
from app.contract.models import (
    Connection,
    DeleteResult,
    Destination,
    PostStatus,
    PublishResult,
    Rendition,
    StatusResult,
)
from app.errors import ErrorCode, IbxError

PROTOCOL_VERSION = "2026-10"


class _Post(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int | str
    url: str | None = None
    status: str


class _PostList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    posts: list[_Post]


_STATUS: dict[str, PostStatus] = {
    "published": "published",
    "live": "published",
    "pending": "pending",
    "in_review": "pending",
    "scheduled": "scheduled",
    "draft": "draft",
    "deleted": "deleted",
    "removed": "removed_by_platform",
    "rejected": "removed_by_platform",
}


def _result(post: _Post) -> PublishResult:
    status = _STATUS.get(post.status, "unknown")
    live_url = post.url if status not in ("deleted", "removed_by_platform") else None
    return PublishResult(platform_post_id=str(post.id), live_url=live_url, status=status)


class Connector(BaseConnector):
    key = "webhook"
    mock_route = "partner"

    def _base(self, destination: Destination, connection: Connection) -> str:
        return self.instance_value(destination, connection).rstrip("/")

    def sim_credentials(self, connection: Connection) -> dict[str, SecretStr]:
        # the partner mock verifies signatures with the same SIM_SECRET setting
        return {"secret": SecretStr(self.ctx.settings.SIM_SECRET)}

    @staticmethod
    def _secret(connection: Connection) -> str:
        secret = connection.secret("secret") or connection.secret("signing_secret") or connection.secret("token")
        if not secret:
            raise IbxError(
                ErrorCode.CONNECTION_AUTH_EXPIRED,
                "webhook: the connection has no signing secret (credentials.secret).",
                details={"action": "reconnect_required"},
            )
        return secret

    def _signed(self, connection: Connection, body: bytes, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            signing.HEADER: signing.sign(self._secret(connection), body),
            "X-IncuBrix-Protocol": PROTOCOL_VERSION,
            "Accept": "application/json",
        }
        if body:
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        return headers

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        payload = {
            "event": "post.create",
            "idempotency_key": marker,
            "rendition": rendition.model_dump(
                mode="json",
                include={"rendition_id", "content_id", "surface", "content_type", "title", "body", "body_format",
                         "canonical_url", "link_url", "media", "tags", "mentions", "language", "ai_generated"},
            ),
            "destination": {k: v for k, v in {"destination_id": destination.destination_id, "name": destination.name,
                                              "surface": destination.surface}.items() if v is not None},
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        response = await ctx.http.post(
            f"{self._base(destination, connection)}/posts",
            content=body,
            headers=self._signed(connection, body, {"Idempotency-Key": marker}),
        )
        return _result(self.parse(response, _Post))

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        response = await ctx.http.get(
            f"{self._base(destination, connection)}/posts",
            params={"idempotency_key": marker},
            headers=self._signed(connection, b""),
        )
        posts = self.parse(response, _PostList).posts
        live = [p for p in posts if _STATUS.get(p.status) != "deleted"]
        return _result(live[0]) if live else None

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        response = await ctx.http.get(
            f"{self._base(destination, connection)}/posts/{platform_post_id}", headers=self._signed(connection, b"")
        )
        if response.status_code in (404, 410):
            return StatusResult(status="deleted", live_url=None)
        result = _result(self.parse(response, _Post))
        return StatusResult(status=result.status, live_url=result.live_url)

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        response = await ctx.http.delete(
            f"{self._base(destination, connection)}/posts/{platform_post_id}", headers=self._signed(connection, b"")
        )
        if response.status_code in (404, 410):
            return DeleteResult(deleted=True)  # already gone: delete is idempotent
        self.check(response)
        return DeleteResult(deleted=True)
