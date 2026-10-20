"""PeerTube connector: one connector, any PeerTube instance (``instance.base_url``), one account per connection.

Platform calls (PeerTube REST API v1, measured on v8.3.0; see manifest.yaml):
    auth      GET  /api/v1/oauth-clients/local, POST /api/v1/users/token (password grant, then refresh grant)
    publish   POST /api/v1/videos/upload  multipart {videofile, channelId, name, description, privacy=1 public,
              waitTranscoding=false, commentsPolicy=1}  -> {video: {id, uuid, shortUUID}}; then GET the video
    lookup    GET  /api/v1/users/me/videos?sort=-createdAt&count=25   (the marker is in the description)
    status    GET  /api/v1/videos/{uuid}       404 = deleted; blacklisted = removed_by_platform
    delete    DELETE /api/v1/videos/{uuid}     204; 404 = already gone
    metrics   GET  /api/v1/videos/{uuid} (views, likes, comments) + /stats/overall (totalWatchTime, owner only)
    refresh   a new token (refresh grant if we hold a refresh token, else the password grant)

* The marker is an invisible markdown comment line ``[//]: # (ibx-...)`` at the end of the description
  (PeerTube renders descriptions as markdown; the line does not show).
* The video file is ``rendition.media[0]`` (fetched through the SSRF guard ``safe_fetch``, size-capped). A
  ``text`` rendition is published over the bundled 2-second test card (``assets/ibx_test_card.mp4``,
  ffmpeg ``testsrc``: our own content); this is what certification posts.
* Tokens are cached in memory per (instance, username). A 401 on a cached token re-authenticates once and
  repeats the call (nothing was created by a 401, so this is safe for the upload too).
* ``live_url`` is the video's public page (``video.url``). That page is a single-page app answering 200 even
  after deletion, so a deletion is proven through the API (see ``live_check_by_api``).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from app.connectors.base import BaseConnector, ConnectorContext, note, warn
from app.contract.models import (
    Connection,
    DeleteResult,
    Destination,
    MetricsResult,
    PostStatus,
    PublishResult,
    RefreshAuthResult,
    Rendition,
    StatusResult,
)
from app.errors import ErrorCode, IbxError
from app.reliability.redact import secret_registry
from app.reliability.safe_fetch import read_media_url

API = "/api/v1"
TEST_CARD = Path(__file__).with_name("assets") / "ibx_test_card.mp4"
LOOKUP_COUNT = 25
PRIVACY_PUBLIC = 1
STATE_PUBLISHED, STATE_TO_TRANSCODE = 1, 2
SIM_CREDENTIALS = {"username": "sim-username", "password": "sim-password"}  # what our PeerTube mock accepts

wall_clock = time.time  # token expiry is the platform's real clock (tests may replace this)


# ---------------------------------------------------------------- strict platform models


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OAuthClient(_Strict):
    client_id: str
    client_secret: str


class Token(_Strict):
    access_token: str
    refresh_token: str
    expires_in: int


class Channel(_Strict):
    id: int


class Me(_Strict):
    username: str
    videoChannels: list[Channel]  # noqa: N815 - PeerTube's field name


class Created(_Strict):
    class _Ref(_Strict):
        id: int
        uuid: str
        shortUUID: str  # noqa: N815

    video: _Ref


class Labelled(_Strict):
    id: int | None = None


class Video(_Strict):
    id: int
    uuid: str
    url: str
    name: str
    state: Labelled
    privacy: Labelled
    views: int
    likes: int
    waitTranscoding: bool | None = None  # noqa: N815
    blacklisted: bool = False
    comments: int | None = None
    description: str | None = None
    truncatedDescription: str | None = None  # noqa: N815


class VideoList(_Strict):
    total: int
    data: list[Video]


class Stats(_Strict):
    totalWatchTime: float  # noqa: N815


# ---------------------------------------------------------------- pure helpers (unit tested)


def marker_line(marker: str) -> str:
    return f"[//]: # ({marker})"


def description_for(rendition: Rendition, marker: str) -> str:
    parts = [rendition.body.strip()] if rendition.body and rendition.body.strip() else []
    if rendition.link_url and rendition.link_url not in (rendition.body or ""):
        parts.append(rendition.link_url)
    parts.append(marker_line(marker))
    return "\n\n".join(parts)


def video_status(video: Video) -> PostStatus:
    if video.blacklisted:
        return "removed_by_platform"
    if video.privacy.id != PRIVACY_PUBLIC:
        return "draft"  # unlisted / private / internal: not public
    if video.state.id == STATE_PUBLISHED:
        return "published"
    if video.state.id == STATE_TO_TRANSCODE and video.waitTranscoding is False:
        return "published"  # measured: public at once while the transcode runs
    return "pending"


@contextmanager
def aux_call() -> Iterator[None]:
    """A helper request (token, channel, read-back) runs without the caller's simulated fault, so a fault sent
    with publish hits the upload itself."""
    from app.contract.router import SIM_FAULT  # imported here: the router imports the connectors' base

    token = SIM_FAULT.set(None)
    try:
        yield
    finally:
        SIM_FAULT.reset(token)


class _Session:
    def __init__(self, access: str, refresh: str | None, expires_at: float) -> None:
        self.access, self.refresh, self.expires_at = access, refresh, expires_at


class Connector(BaseConnector):
    key = "peertube"
    live_check_by_api: ClassVar[str] = (
        "PeerTube's web page is a single-page app that answers 200 even for a deleted video (measured on v8.3.0); "
        "the API (GET /api/v1/videos/<uuid>: 200, then 404) confirms the post and its deletion"
    )

    def __init__(self, ctx: ConnectorContext) -> None:
        super().__init__(ctx)
        self._clients: dict[str, OAuthClient] = {}
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._channels: dict[tuple[str, str], int] = {}

    def sim_credentials(self, connection: Connection) -> dict[str, SecretStr]:
        return {k: SecretStr(v) for k, v in SIM_CREDENTIALS.items()}

    # ------------------------------------------------------------ hooks

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        base = self._base(destination, connection)
        with aux_call():
            channel_id = await self._channel_id(ctx, base, destination, connection)
            filename, data, mime = await self._video_file(ctx, rendition)
        fields: dict[str, str | list[str]] = {
            "channelId": str(channel_id),
            "name": rendition.title or "",
            "description": description_for(rendition, marker),
            "privacy": str(PRIVACY_PUBLIC),
            "waitTranscoding": "false",
            "commentsPolicy": "1",
        }
        if rendition.tags:
            fields["tags[]"] = list(rendition.tags)
        if rendition.language and len(rendition.language) == 2:
            fields["language"] = rendition.language.lower()
        response = await self._request(ctx, "POST", base, "/videos/upload", connection, data=fields,
                                       files={"videofile": (filename, data, mime)})
        created = self.parse(response, Created).video
        with aux_call():
            video = await self._video(ctx, base, created.uuid, connection)
        if video is None:  # deleted between the upload and the read-back
            raise IbxError(ErrorCode.PLATFORM_UNAVAILABLE, "peertube: the uploaded video vanished before it could be read back.")
        return self._result(video)

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        base = self._base(destination, connection)
        response = await self._request(ctx, "GET", base, "/users/me/videos", connection,
                                       params={"sort": "-createdAt", "count": LOOKUP_COUNT})
        line = marker_line(marker)
        for item in self.parse(response, VideoList).data:
            text = item.description
            if text is None:  # a list without full descriptions: read the video itself
                full = await self._video(ctx, base, item.uuid, connection)
                text = (full.description if full else "") or ""
            if line in text:
                return self._result(item)
        return None

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        video = await self._video(ctx, self._base(destination, connection), platform_post_id, connection)
        if video is None:
            return StatusResult(status="deleted", live_url=None)
        status = video_status(video)
        return StatusResult(status=status, live_url=video.url if status == "published" else None)

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        response = await self._request(ctx, "DELETE", self._base(destination, connection), f"/videos/{platform_post_id}", connection)
        mode = "already_gone" if response.status_code == 404 else "deleted"
        if mode == "deleted":
            self.check(response)  # 204: the video and its files are removed
        note(delete_mode=mode)
        self.log.info("peertube_video_deleted", post_id=platform_post_id, delete_mode=mode)
        return DeleteResult(deleted=True)

    async def _fetch_metrics(
        self,
        ctx: ConnectorContext,
        platform_post_id: str,
        destination: Destination,
        connection: Connection,
        since: datetime | None,
    ) -> MetricsResult:
        base = self._base(destination, connection)
        video = await self._video(ctx, base, platform_post_id, connection)
        if video is None:
            raise IbxError(ErrorCode.NOT_FOUND, "The PeerTube video no longer exists.", details={"platform_post_id": platform_post_id})
        watch_time: float | None = None
        stats = await self._request(ctx, "GET", base, f"/videos/{platform_post_id}/stats/overall", connection)
        if stats.status_code == 200:
            watch_time = float(self.parse(stats, Stats).totalWatchTime)
        else:
            warn(f"peertube: watch time not available (stats answered HTTP {stats.status_code})")
        # PeerTube has no shares, saves or click counts: those stay null (never a fake 0).
        return MetricsResult(views=video.views, likes=video.likes, comments=video.comments, watch_time_s=watch_time)

    async def _refresh_auth(
        self, ctx: ConnectorContext, connection: Connection, destination: Destination
    ) -> RefreshAuthResult:
        base = self._base(destination, connection)
        if self._static_token(connection):
            response = await self._request(ctx, "GET", base, "/users/me", connection)
            self.parse(response, Me)  # proves the token; its expiry is not known to us
            return RefreshAuthResult(expires_at=None)
        session = await self._login(ctx, base, connection, force=True)
        return RefreshAuthResult(expires_at=datetime.fromtimestamp(session.expires_at, UTC))

    # ------------------------------------------------------------ PeerTube plumbing

    def _base(self, destination: Destination, connection: Connection) -> str:
        return self.instance_value(destination, connection).rstrip("/")

    def _result(self, video: Video) -> PublishResult:
        status = video_status(video)
        return PublishResult(platform_post_id=video.uuid, live_url=video.url if status == "published" else None, status=status)

    async def _video(self, ctx: ConnectorContext, base: str, video_id: str, connection: Connection) -> Video | None:
        response = await self._request(ctx, "GET", base, f"/videos/{video_id}", connection)
        if response.status_code == 404:
            return None
        return self.parse(response, Video)

    async def _channel_id(self, ctx: ConnectorContext, base: str, destination: Destination, connection: Connection) -> int:
        configured = destination.instance.get("channel_id") or (connection.model_extra or {}).get("channel_id")
        if configured:
            return int(configured)
        key = self._account_key(base, connection)
        if key not in self._channels:
            me = self.parse(await self._request(ctx, "GET", base, "/users/me", connection), Me)
            if not me.videoChannels:
                raise IbxError(ErrorCode.PLATFORM_CONTENT_REJECTED, "This PeerTube account has no video channel to upload to.")
            self._channels[key] = me.videoChannels[0].id
        return self._channels[key]

    async def _video_file(self, ctx: ConnectorContext, rendition: Rendition) -> tuple[str, bytes, str]:
        videos = [m for m in rendition.media if (m.mime or "").startswith("video/") or m.url.split("?")[0].lower().endswith(
            (".mp4", ".webm", ".mov", ".mkv", ".m4v", ".ogv"))]
        if videos:
            settings = ctx.settings
            fetched = await read_media_url(videos[0].url, settings, ctx.storage, max_bytes=settings.MEDIA_MAX_BYTES,
                                           client=ctx.http)
            mime = (fetched.mime or videos[0].mime or "video/mp4").lower()
            if not mime.startswith("video/"):
                raise IbxError(ErrorCode.PLATFORM_CONTENT_REJECTED, f"PeerTube needs a video file (got {mime}).")
            name = videos[0].url.split("?")[0].rsplit("/", 1)[-1] or "video.mp4"
            return name, fetched.data, mime
        if rendition.content_type == "text":
            note(media_source="bundled_test_card")
            warn("PEERTUBE_TEST_CARD: a text rendition was published over the bundled 2-second test card video")
            return TEST_CARD.name, TEST_CARD.read_bytes(), "video/mp4"
        raise IbxError(ErrorCode.DEST_SPEC_VIOLATION, "A PeerTube video post needs rendition.media[0] to be a video file.",
                       details={"violations": [{"field": "media", "rule": "video_required", "message": "no video file"}]})

    # ------------------------------------------------------------ auth

    @staticmethod
    def _static_token(connection: Connection) -> str | None:
        return connection.secret("access_token") or connection.secret("token")

    @staticmethod
    def _username(connection: Connection) -> str | None:
        return connection.secret("username")

    @staticmethod
    def _account_key(base: str, connection: Connection) -> tuple[str, str]:
        """Cache key for a login: instance + username + a hash of the password, so a cached token is never
        reused for a caller who sent different (e.g. wrong) credentials."""
        secret = f"{connection.secret('username') or ''}:{connection.secret('password') or connection.secret('access_token') or ''}"
        return base, hashlib.sha256(secret.encode()).hexdigest()[:24]

    async def _client(self, ctx: ConnectorContext, base: str) -> OAuthClient:
        if base not in self._clients:
            response = await ctx.http.get(f"{base}{API}/oauth-clients/local")
            self._clients[base] = self.parse(response, OAuthClient)
        return self._clients[base]

    async def _login(self, ctx: ConnectorContext, base: str, connection: Connection, *, force: bool = False) -> _Session:
        username, password = self._username(connection), connection.secret("password")
        if not username or not password:
            raise IbxError(ErrorCode.CONNECTION_AUTH_EXPIRED,
                           "PeerTube needs credentials {username, password} (or an access_token).",
                           details={"reason": "missing_credentials", "action": "reconnect_required"})
        key = self._account_key(base, connection)
        session = self._sessions.get(key)
        if session is not None and not force and session.expires_at - 60 > wall_clock():
            return session
        with aux_call():
            client = await self._client(ctx, base)
            form = {"client_id": client.client_id, "client_secret": client.client_secret}
            response = None
            if session is not None and session.refresh:
                response = await ctx.http.post(f"{base}{API}/users/token",
                                               data={**form, "grant_type": "refresh_token", "refresh_token": session.refresh})
                if response.status_code != 200:
                    response = None  # an expired refresh token: fall back to the password grant
            if response is None:
                response = await ctx.http.post(f"{base}{API}/users/token",
                                               data={**form, "grant_type": "password", "username": username, "password": password})
            token = self.parse(response, Token)
        secret_registry.add_many([token.access_token, token.refresh_token])
        session = _Session(token.access_token, token.refresh_token, wall_clock() + token.expires_in)
        self._sessions[key] = session
        return session

    async def _request(
        self, ctx: ConnectorContext, method: str, base: str, path: str, connection: Connection, **kwargs: Any
    ) -> httpx.Response:
        """An authenticated call. A 401 on a cached password-grant token re-authenticates once and repeats."""
        static = self._static_token(connection)
        for attempt in (1, 2):
            token = static or (await self._login(ctx, base, connection, force=attempt == 2)).access
            response = await ctx.http.request(method, f"{base}{API}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
            if response.status_code != 401 or static or attempt == 2:
                return response
            self.log.info("peertube_token_rejected_reauth", base=base)
            self._sessions.pop(self._account_key(base, connection), None)
        return response  # pragma: no cover - the loop always returns
