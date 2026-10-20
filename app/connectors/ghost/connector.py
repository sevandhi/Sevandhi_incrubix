"""Ghost connector: one connector, any Ghost 6 site (``instance.base_url``), using a custom integration's Admin key.

Platform calls (Ghost Admin API, ``Authorization: Ghost <jwt>``, ``Accept-Version: v6.0``):
    publish   POST   /ghost/api/admin/posts/  {title, slug "<title words>-<marker>", status published, lexical}
    lookup    GET    /ghost/api/admin/posts/?filter=slug:~$'<marker>'   (the marker is the slug's suffix)
    status    GET    /ghost/api/admin/posts/{id}/       404 = deleted
    delete    DELETE /ghost/api/admin/posts/{id}/       204 (permanent); 404 = already gone
    refresh   mint a new token and prove it with GET /ghost/api/admin/posts/?limit=1&fields=id

The Admin key is ``"<key id>:<hex secret>"``. Each request carries a fresh JWT: HS256 signed with the
secret (hex-decoded), header ``kid`` = key id, claims ``iat``, ``exp = iat + 5 min`` and ``aud = /admin/``.
The token is signed with the real wall clock (Ghost checks it against its own clock), so a skewed clock
shows up as 401 INVALID_JWT and the error message says to check the clock.

The body is sent as a lexical document holding one markdown card (Ghost renders it; verified locally),
or as HTML (``?source=html``) when ``body_format`` is html. No ``newsletter`` parameter is ever sent,
so publishing never emails members.

Measured on the local Ghost 6.70 (tests/fixtures/recorded/ghost): title > 255 -> 422 ValidationError;
expired token -> 401 INVALID_JWT "jwt expired"; unknown key -> 401 UNKNOWN_ADMIN_API_KEY; DELETE -> 204,
then 404 on the API and on the public URL; a slug longer than 191 characters -> 422.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
from pydantic import SecretStr

from app.connectors.base import BaseConnector, ConnectorContext, note
from app.connectors.ghost.platform_models import BrowseIds, GhostPost, OnePost, PostsEnvelope
from app.contract.models import (
    Connection,
    DeleteResult,
    Destination,
    PostStatus,
    PublishResult,
    RefreshAuthResult,
    Rendition,
    StatusResult,
)
from app.errors import ErrorCode, IbxError
from app.reliability.redact import redact, secret_registry

ADMIN_PATH = "/ghost/api/admin"
ACCEPT_VERSION = "v6.0"
TOKEN_TTL_S = 300  # Ghost refuses Admin tokens that live longer than 5 minutes
SLUG_TITLE_CHARS = 60  # title words kept in the slug; the whole slug stays far below Ghost's 191
SIM_ADMIN_KEY = "5" * 24 + ":" + "ab" * 32  # a well-formed fake key for SIMULATION_MODE (mock-platforms)
STATUS: dict[str, PostStatus] = {"published": "published", "sent": "published", "draft": "draft", "scheduled": "scheduled"}

# The real clock: Ghost validates iat/exp against its own clock (tests may replace this function).
wall_clock = time.time


# ---------------------------------------------------------------- small pure helpers (unit tested)


def split_admin_key(raw: str | None) -> tuple[str, bytes]:
    """``"<id>:<hex secret>"`` -> (key id, secret bytes). A malformed key is an auth problem for the owner."""
    key_id, sep, secret = (raw or "").strip().partition(":")
    try:
        secret_bytes = bytes.fromhex(secret)
    except ValueError:
        secret_bytes = b""
    if not sep or not key_id or not secret_bytes:
        raise IbxError(
            ErrorCode.CONNECTION_AUTH_EXPIRED,
            "The Ghost Admin API key must look like '<key id>:<hex secret>' (Ghost Admin > Integrations).",
            details={"reason": "malformed_admin_key" if raw else "missing_credentials", "action": "reconnect_required"},
        )
    return key_id, secret_bytes


def admin_token(key_id: str, secret: bytes, now_s: float) -> tuple[str, datetime]:
    """A short-lived Admin API JWT and its expiry."""
    iat = int(now_s)
    exp = iat + TOKEN_TTL_S
    token = jwt.encode({"iat": iat, "exp": exp, "aud": "/admin/"}, secret, algorithm="HS256", headers={"kid": key_id})
    return token, datetime.fromtimestamp(exp, UTC)


def ghost_slug(title: str | None, marker: str) -> str:
    """``"Hello, World!"`` + ``ibx-abc`` -> ``hello-world-ibx-abc``. ASCII only, so Ghost keeps it unchanged."""
    ascii_title = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")[:SLUG_TITLE_CHARS].rstrip("-")
    return f"{words}-{marker}" if words else marker


def lexical_markdown(markdown: str) -> str:
    """A Ghost lexical document with a single markdown card."""
    card = {"type": "markdown", "version": 1, "markdown": markdown}
    root = {"children": [card], "direction": None, "format": "", "indent": 0, "type": "root", "version": 1}
    return json.dumps({"root": root})


def post_status(post: GhostPost) -> PostStatus:
    return STATUS.get(post.status, "unknown")


def _result(post: GhostPost) -> PublishResult:
    status = post_status(post)
    return PublishResult(platform_post_id=post.id, live_url=post.url if status == "published" else None, status=status)


def _host(url: str) -> str:
    parts = httpx.URL(url if "://" in url else f"http://{url}")
    return f"{parts.host.lower()}:{parts.port or (443 if parts.scheme == 'https' else 80)}"


# ---------------------------------------------------------------- the connector


class Connector(BaseConnector):
    key = "ghost"

    # ------------------------------------------------------------ simulation: a well-formed fake key

    def sim_credentials(self, connection: Connection) -> dict[str, SecretStr]:
        # a well-formed Admin key (id:hexsecret), so a JWT can be signed for the Ghost mock
        return {"admin_api_key": SecretStr(SIM_ADMIN_KEY)}

    # ------------------------------------------------------------ hooks

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        post: dict[str, Any] = {
            "title": rendition.title or "",
            "slug": ghost_slug(rendition.title, marker),
            "status": "published",  # never a `newsletter` parameter: publishing must not email members
        }
        body = rendition.body or ""
        if rendition.link_url and rendition.link_url not in body:
            body = f"{body}\n\n{rendition.link_url}" if body else rendition.link_url
        params = {}
        if rendition.body_format == "html":
            post["html"], params["source"] = body, "html"
        else:
            post["lexical"] = lexical_markdown(body)
        if rendition.canonical_url:
            post["canonical_url"] = rendition.canonical_url
        if rendition.tags:
            post["tags"] = [{"name": tag} for tag in rendition.tags]
        response = await ctx.http.post(
            f"{self._admin(destination, connection)}/posts/", params=params, json={"posts": [post]},
            headers=self._headers(destination, connection),
        )
        return _result(self.parse(response, OnePost).posts[0])

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        # Every slug we create ends with the marker; Ghost's filter language has "ends with" (~$).
        response = await ctx.http.get(
            f"{self._admin(destination, connection)}/posts/",
            params={"filter": f"slug:~$'{marker}'", "limit": 1},
            headers=self._headers(destination, connection),
        )
        posts = self.parse(response, PostsEnvelope).posts
        return _result(posts[0]) if posts else None

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        response = await ctx.http.get(
            f"{self._admin(destination, connection)}/posts/{self._post_id(platform_post_id)}/",
            headers=self._headers(destination, connection),
        )
        if response.status_code == 404:
            return StatusResult(status="deleted", live_url=None)
        result = _result(self.parse(response, OnePost).posts[0])
        return StatusResult(status=result.status, live_url=result.live_url)

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        response = await ctx.http.delete(
            f"{self._admin(destination, connection)}/posts/{self._post_id(platform_post_id)}/",
            headers=self._headers(destination, connection),
        )
        mode = "already_gone" if response.status_code == 404 else "permanent"
        if mode == "permanent":
            self.check(response)  # 204 = deleted for good (the API and the public URL then answer 404)
        note(delete_mode=mode)
        self.log.info("ghost_post_deleted", post_id=platform_post_id, delete_mode=mode)
        return DeleteResult(deleted=True)

    async def _refresh_auth(
        self, ctx: ConnectorContext, connection: Connection, destination: Destination
    ) -> RefreshAuthResult:
        """Ghost keys do not expire; the short-lived token is re-minted and proven with one cheap call."""
        token, expires_at = self._token(destination, connection)
        response = await ctx.http.get(
            f"{self._admin(destination, connection)}/posts/",
            params={"limit": 1, "fields": "id"},
            headers={"Authorization": f"Ghost {token}", "Accept-Version": ACCEPT_VERSION},
        )
        self.parse(response, BrowseIds)
        return RefreshAuthResult(expires_at=expires_at)

    # ------------------------------------------------------------ errors: say what to check on a 401

    def map_error(self, response: httpx.Response) -> IbxError:
        err = super().map_error(response)
        try:
            first = (response.json().get("errors") or [{}])[0]
        except (ValueError, AttributeError, IndexError):
            first = {}
        if isinstance(first, dict) and first.get("code"):
            err.details["platform_code"] = first["code"]  # e.g. INVALID_JWT, UNKNOWN_ADMIN_API_KEY
        context = first.get("context") if isinstance(first, dict) else None
        if isinstance(context, str) and context.strip():
            # Ghost's "message" is generic ("Validation error, cannot save post."); the reason is in "context",
            # e.g. "Value in [posts.title] exceeds maximum length of 255 characters."
            context = redact(context.strip())[:300]
            err.details["platform_context"] = context
            err.details["platform_message"] = f"{err.details.get('platform_message') or ''} {context}".strip()[:600]
            err.message += f" ({context})"
        if err.code == ErrorCode.CONNECTION_AUTH_EXPIRED:
            err.message += (
                ". Check the Admin API key, and that this server's clock is right"
                " (Ghost tokens are valid for 5 minutes from their 'iat')."
            )
        return err

    # ------------------------------------------------------------ helpers

    def _admin(self, destination: Destination, connection: Connection) -> str:
        return self.instance_value(destination, connection).rstrip("/") + ADMIN_PATH

    def _headers(self, destination: Destination, connection: Connection) -> dict[str, str]:
        token, _ = self._token(destination, connection)
        return {"Authorization": f"Ghost {token}", "Accept-Version": ACCEPT_VERSION}

    def _token(self, destination: Destination, connection: Connection) -> tuple[str, datetime]:
        credentials = {name: secret.get_secret_value() for name, secret in connection.credentials.items()}
        credentials = credentials or self._local_credentials(self.instance_value(destination, connection))
        raw = credentials.get("admin_api_key") or credentials.get("api_key") or credentials.get("key")
        key_id, secret = split_admin_key(raw)
        return admin_token(key_id, secret, wall_clock())

    def _local_credentials(self, base_url: str) -> dict[str, str]:
        """Our OWN local Ghost (compose "local" mode): the dev-only key the bootstrap wrote to ghost.json.

        Used only when the call has no credentials AND targets that exact container (or its toxiproxy).
        """
        settings = self.ctx.settings
        try:
            data = json.loads((Path(settings.BOOTSTRAP_OUT_DIR) / "ghost.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if _host(base_url) not in {_host(data.get("base_url") or ""), _host(settings.IBX_GHOST_PROXY_URL)}:
            return {}
        key = data.get("admin_api_key") or ""
        secret_registry.add_many([key, key.partition(":")[2]])
        return {"admin_api_key": key} if key else {}

    @staticmethod
    def _post_id(platform_post_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{24}", platform_post_id or ""):
            raise IbxError(
                ErrorCode.REQUEST_VALIDATION_FAILED,
                f"A Ghost platform_post_id is a 24-character hex id; got '{platform_post_id}'.",
                details={"errors": [{"loc": ["platform_post_id"], "type": "value_error", "msg": "not a Ghost post id"}]},
            )
        return platform_post_id
