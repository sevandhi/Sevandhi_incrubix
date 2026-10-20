"""Lemmy connector: one connector, any community on any Lemmy 0.19 instance (``instance.community_url``).

``community_url`` is ``http://lemmy:8536/c/ibx_test`` (or ``!ibx_test@lemmy.example``). The part before
``/c/`` is the API base, the part after it is the community name.

Platform calls (Lemmy HTTP API v3, ``Authorization: Bearer <jwt>``):
    publish   GET  /api/v3/community?name=<name>        community id (cached)
              POST /api/v3/post {name, community_id, body + marker line, url?}
    lookup    GET  /api/v3/user?username=<me>&sort=New&limit=50   find the marker in my newest posts
    status    GET  /api/v3/post?id=<id>                 deleted / removed flags, ap_id = live URL
    delete    POST /api/v3/post/delete {deleted: true}  soft delete (any author), then
              POST /api/v3/admin/purge/post             permanent purge when the account is an admin
    metrics   GET  /api/v3/post?id=<id>                 counts.upvotes -> likes, counts.comments -> comments

Facts measured on the local Lemmy 0.19.20 (tests/fixtures/recorded/lemmy) that shape this file:
* Errors are HTTP 400 ``{"error": "<token>"}``; the manifest's ``"400:<token>"`` keys map them.
* The rate limit is 400 ``rate_limit_error`` with NO reset header. The reset is computed from the site's
  ``local_site_rate_limit`` (``post`` posts per ``post_per_second`` s, a token bucket): one post slot comes
  back after ``post_per_second / post`` seconds. If the site config cannot be read: ``LEMMY_RL_FALLBACK_S``.
* A bad or logged-out token is 401 ``incorrect_login``; tokens never expire on their own.
* Nothing can be searched by an external id, so the idempotency marker is an invisible markdown comment
  ``[//]: # (ibx-xxxx)`` at the end of the body, and ``_lookup`` scans the creator's newest posts.
* A soft delete keeps the post readable for admins; only an admin purge removes it (then 400
  ``couldnt_find_post``). The delete mode used is reported in ``_meta.delete_mode`` and the log.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.connectors.base import BaseConnector, ConnectorContext, note, warn
from app.connectors.lemmy.local import aux_call, local_credentials
from app.connectors.lemmy.platform_models import (
    GetCommunityResponse,
    GetPostResponse,
    LoginResponse,
    PersonDetails,
    Post,
    PostResponse,
    SiteResponse,
    SuccessResponse,
)
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
from app.errors import ErrorCode, IbxError
from app.reliability.redact import secret_registry
from app.reliability.retry_after import ResetInfo, fallback_reset

LOOKUP_LIMIT = 50  # how many of the creator's newest posts _lookup scans (Lemmy's page maximum)
NOT_FOUND_TOKENS = {"couldnt_find_post"}
NOT_ADMIN_TOKENS = {"not_an_admin", "not_a_mod_or_admin", "not_higher_admin"}
PURGE_REASON = "IncuBrix: deleted by the content owner"

_BANG_FORM = re.compile(r"^!(?P<name>[A-Za-z0-9_]+)@(?P<host>[A-Za-z0-9.\-]+(?::\d+)?)$")
_COMMUNITY_PATH = re.compile(r"^(?P<prefix>.*?)/c/(?P<name>[A-Za-z0-9_]+)/?$")


# ---------------------------------------------------------------- small pure helpers (unit tested)


@dataclass(frozen=True)
class CommunityRef:
    api: str  # API base, e.g. http://lemmy:8536 (a mock tenant adds a path prefix)
    name: str  # community name, e.g. ibx_test


def parse_community(value: str) -> CommunityRef:
    """``http://host/c/name`` or ``!name@host`` -> CommunityRef; anything else is a request error."""
    value = (value or "").strip()
    bang = _BANG_FORM.match(value)
    if bang:
        return CommunityRef(api=f"https://{bang['host']}", name=bang["name"])
    scheme, _, rest = value.partition("://")
    netloc, slash, path = rest.partition("/")
    match = _COMMUNITY_PATH.match(slash + path)
    if scheme not in ("http", "https") or not netloc or match is None:
        raise IbxError(
            ErrorCode.REQUEST_VALIDATION_FAILED,
            "destination.instance.community_url must look like https://<instance>/c/<community> or !<community>@<instance>.",
            details={"errors": [{"loc": ["destination", "instance", "community_url"], "type": "value_error",
                                 "msg": "not a Lemmy community URL"}]},
        )
    return CommunityRef(api=f"{scheme}://{netloc}{match['prefix']}", name=match["name"])


def marker_line(marker: str) -> str:
    """An invisible markdown comment: Lemmy stores it in the body but does not render it."""
    return f"[//]: # ({marker})"


def body_with_marker(body: str | None, marker: str) -> str:
    text = (body or "").rstrip()
    return f"{text}\n\n{marker_line(marker)}\n" if text else f"{marker_line(marker)}\n"


MARKER_OVERHEAD = len(body_with_marker("x", "ibx-" + "a" * 12)) - 1  # 29 characters added to every body


def post_status(post: Post) -> PostStatus:
    if post.removed:
        return "removed_by_platform"
    if post.deleted:
        return "deleted"
    return "published"


def error_token(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else None


def is_post_gone(response: httpx.Response) -> bool:
    """Lemmy answers a missing (or purged) post with 400 couldnt_find_post; a proxy may say 404."""
    return response.status_code == 404 or (response.status_code == 400 and error_token(response) in NOT_FOUND_TOKENS)


def post_id_of(platform_post_id: str) -> int:
    try:
        return int(platform_post_id)
    except (TypeError, ValueError):
        raise IbxError(
            ErrorCode.REQUEST_VALIDATION_FAILED,
            f"A Lemmy platform_post_id is a number; got '{platform_post_id}'.",
            details={"errors": [{"loc": ["platform_post_id"], "type": "int_parsing", "msg": "not a number"}]},
        ) from None


def _result(post: Post) -> PublishResult:
    status = post_status(post)
    return PublishResult(platform_post_id=str(post.id), live_url=post.ap_id if status == "published" else None, status=status)


# ---------------------------------------------------------------- the connector


class Connector(BaseConnector):
    key = "lemmy"

    def __init__(self, ctx: ConnectorContext) -> None:
        super().__init__(ctx)
        self._community_ids: dict[str, int] = {}  # "<api>|<name>" -> community id (ids never change)
        self._logins: dict[str, str] = {}  # "<api>|<username>" -> jwt from a password login
        self._usernames: dict[str, str] = {}  # sha256(jwt)[:16] -> the account's username

    # ------------------------------------------------------------ simulation: keep the /c/<name> part

    def simulate(self, destination: Destination, connection: Connection) -> tuple[Destination, Connection]:
        sim_dest, sim_conn = super().simulate(destination, connection)
        if sim_dest is destination:  # not simulating, or already pointing at mock-platforms
            return sim_dest, sim_conn
        name = parse_community(self.instance_value(destination, connection)).name
        param = self.manifest.instance_param
        instance = {**sim_dest.instance, param: f"{sim_dest.instance[param]}/c/{name}"}
        return sim_dest.model_copy(update={"instance": instance}), sim_conn

    # ------------------------------------------------------------ hooks

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        ref = self._ref(destination, connection)
        headers = await self._auth_headers(ctx, ref, connection)
        community_id = await self._community_id(ctx, ref, destination, headers)
        payload: dict[str, Any] = {
            "name": rendition.title or "",
            "community_id": community_id,
            "body": body_with_marker(rendition.body, marker),
        }
        if rendition.link_url:
            payload["url"] = rendition.link_url
        response = await ctx.http.post(f"{ref.api}/api/v3/post", json=payload, headers=headers)
        created = await self._read(ctx, ref, response, PostResponse)
        return _result(created.post_view.post)

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        ref = self._ref(destination, connection)
        headers = await self._auth_headers(ctx, ref, connection)
        username = await self._username(ctx, ref, connection, headers)
        response = await ctx.http.get(
            f"{ref.api}/api/v3/user",
            params={"username": username, "sort": "New", "limit": LOOKUP_LIMIT},
            headers=headers,
        )
        details = await self._read(ctx, ref, response, PersonDetails)
        line = marker_line(marker)
        for view in details.posts:
            if line in (view.post.body or ""):
                return _result(view.post)
        return None

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        ref = self._ref(destination, connection)
        response = await self._get_post(ctx, ref, connection, platform_post_id)
        if is_post_gone(response):
            return StatusResult(status="deleted", live_url=None)  # purged: gone for everyone
        post = (await self._read(ctx, ref, response, GetPostResponse)).post_view.post
        result = _result(post)
        return StatusResult(status=result.status, live_url=result.live_url)

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        ref = self._ref(destination, connection)
        post_id = post_id_of(platform_post_id)
        headers = await self._auth_headers(ctx, ref, connection)
        # 1) soft delete: allowed for the author; reversible, admins can still read the post
        response = await ctx.http.post(
            f"{ref.api}/api/v3/post/delete", json={"post_id": post_id, "deleted": True}, headers=headers
        )
        if is_post_gone(response):
            return self._deleted(post_id, "already_gone")
        await self._read(ctx, ref, response, PostResponse)
        # 2) admin purge: permanent. A normal account is refused (not_an_admin) and keeps the soft delete.
        mode = "soft"
        try:
            purge = await ctx.http.post(
                f"{ref.api}/api/v3/admin/purge/post", json={"post_id": post_id, "reason": PURGE_REASON}, headers=headers
            )
            if purge.is_success:
                SuccessResponse.model_validate(purge.json())
                mode = "purged"
            elif error_token(purge) not in NOT_ADMIN_TOKENS:
                warn(f"LEMMY_PURGE_FAILED: HTTP {purge.status_code}; the post stays soft-deleted")
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            warn(f"LEMMY_PURGE_FAILED: {type(exc).__name__}; the post stays soft-deleted")
        return self._deleted(post_id, mode)

    async def _fetch_metrics(
        self,
        ctx: ConnectorContext,
        platform_post_id: str,
        destination: Destination,
        connection: Connection,
        since: datetime | None,
    ) -> MetricsResult:
        ref = self._ref(destination, connection)
        response = await self._get_post(ctx, ref, connection, platform_post_id)
        if is_post_gone(response):
            raise IbxError(ErrorCode.NOT_FOUND, f"Lemmy post {platform_post_id} no longer exists.",
                           details={"platform_status": response.status_code, "platform_message": "couldnt_find_post"})
        counts = (await self._read(ctx, ref, response, GetPostResponse)).post_view.counts
        # Lemmy has no view counter: views (and shares, saves, ...) stay None, never a fake 0.
        return MetricsResult(likes=counts.upvotes, comments=counts.comments)

    # ------------------------------------------------------------ platform helpers

    def _ref(self, destination: Destination, connection: Connection) -> CommunityRef:
        return parse_community(self.instance_value(destination, connection))

    async def _read(self, ctx: ConnectorContext, ref: CommunityRef, response: httpx.Response, model: type[BaseModel]) -> Any:
        """Raise the mapped error for an error response, else parse the body strictly."""
        await self._raise_for_error(ctx, ref, response)
        return model.model_validate(response.json())

    async def _raise_for_error(self, ctx: ConnectorContext, ref: CommunityRef, response: httpx.Response) -> None:
        if not response.is_error:
            return
        err = self.map_error(response)
        if err.code == ErrorCode.PLATFORM_RATE_LIMIT and err.details.get("reset_source") == "default":
            reset = await self._rate_limit_reset(ctx, ref)  # Lemmy sent no reset header: use its site config
            err.details.update(reset.as_details())
            err.message += f" (rate limited; next attempt after {err.details['retry_at']})"
        if err.code == ErrorCode.CONNECTION_AUTH_EXPIRED:
            self._logins = {k: v for k, v in self._logins.items() if not k.startswith(f"{ref.api}|")}
        raise err

    async def _rate_limit_reset(self, ctx: ConnectorContext, ref: CommunityRef) -> ResetInfo:
        """When one post slot is free again, from the instance's ``local_site_rate_limit``."""
        now = self.now()
        try:
            with aux_call():
                response = await ctx.http.get(f"{ref.api}/api/v3/site")
            response.raise_for_status()
            limit = SiteResponse.model_validate(response.json()).site_view.local_site_rate_limit
        except (httpx.HTTPError, ValidationError, ValueError):
            return fallback_reset(ctx.settings.LEMMY_RL_FALLBACK_S, now, "lemmy_fallback")
        seconds = math.ceil(limit.post_per_second / max(limit.post, 1))
        return fallback_reset(seconds, now, "lemmy_site_config")

    def _credentials(self, ref: CommunityRef, connection: Connection) -> dict[str, str]:
        given = {name: secret.get_secret_value() for name, secret in connection.credentials.items()}
        return given or local_credentials(self.ctx.settings, ref.api)

    async def _auth_headers(self, ctx: ConnectorContext, ref: CommunityRef, connection: Connection) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token(ctx, ref, connection)}"}

    async def _token(self, ctx: ConnectorContext, ref: CommunityRef, connection: Connection) -> str:
        """``credentials.jwt`` (or ``token``) as is; otherwise log in once with username + password."""
        credentials = self._credentials(ref, connection)
        token = credentials.get("jwt") or credentials.get("token")
        if token:
            return token
        username, password = credentials.get("username"), credentials.get("password")
        if not (username and password):
            raise IbxError(
                ErrorCode.CONNECTION_AUTH_EXPIRED,
                "No Lemmy credentials: send credentials.jwt, or credentials.username and credentials.password.",
                details={"reason": "missing_credentials", "action": "reconnect_required"},
            )
        cache_key = f"{ref.api}|{username}"
        if cache_key not in self._logins:
            with aux_call():
                response = await ctx.http.post(
                    f"{ref.api}/api/v3/user/login", json={"username_or_email": username, "password": password}
                )
            jwt = (await self._read(ctx, ref, response, LoginResponse)).jwt
            if not jwt:
                raise IbxError(
                    ErrorCode.CONNECTION_AUTH_EXPIRED,
                    "Lemmy accepted the password but issued no token (email check or sign-up application pending).",
                    details={"reason": "login_without_token", "action": "reconnect_required"},
                )
            secret_registry.add_many([jwt])
            self._logins[cache_key] = jwt
        return self._logins[cache_key]

    async def _username(
        self, ctx: ConnectorContext, ref: CommunityRef, connection: Connection, headers: dict[str, str]
    ) -> str:
        """The account's username: from the credentials, else asked from Lemmy once per token."""
        given = self._credentials(ref, connection).get("username")
        if given:
            return given
        token_ref = hashlib.sha256(headers["Authorization"].encode()).hexdigest()[:16]
        if token_ref not in self._usernames:
            with aux_call():
                response = await ctx.http.get(f"{ref.api}/api/v3/site", headers=headers)
            site = await self._read(ctx, ref, response, SiteResponse)
            if site.my_user is None:  # Lemmy ignores a token it does not accept on this public endpoint
                raise IbxError(
                    ErrorCode.CONNECTION_AUTH_EXPIRED,
                    "Lemmy did not accept the token (no logged-in user); the owner must reconnect.",
                    details={"platform_status": response.status_code, "action": "reconnect_required"},
                )
            self._usernames[token_ref] = site.my_user.local_user_view.person.name
        return self._usernames[token_ref]

    async def _community_id(
        self, ctx: ConnectorContext, ref: CommunityRef, destination: Destination, headers: dict[str, str]
    ) -> int:
        """``instance.community_id`` if the destination gives it, else looked up by name once and cached."""
        given = destination.instance.get("community_id")
        if isinstance(given, int) or (isinstance(given, str) and given.isdigit()):
            return int(given)
        cache_key = f"{ref.api}|{ref.name}"
        if cache_key not in self._community_ids:
            with aux_call():
                response = await ctx.http.get(f"{ref.api}/api/v3/community", params={"name": ref.name}, headers=headers)
            found = await self._read(ctx, ref, response, GetCommunityResponse)
            self._community_ids[cache_key] = found.community_view.community.id
        return self._community_ids[cache_key]

    async def _get_post(
        self, ctx: ConnectorContext, ref: CommunityRef, connection: Connection, platform_post_id: str
    ) -> httpx.Response:
        post_id = post_id_of(platform_post_id)
        headers = await self._auth_headers(ctx, ref, connection)
        return await ctx.http.get(f"{ref.api}/api/v3/post", params={"id": post_id}, headers=headers)

    def _deleted(self, post_id: int, mode: str) -> DeleteResult:
        note(delete_mode=mode)
        self.log.info("lemmy_post_deleted", post_id=post_id, delete_mode=mode)
        return DeleteResult(deleted=True)
