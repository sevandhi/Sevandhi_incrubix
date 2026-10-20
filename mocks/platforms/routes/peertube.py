"""Mock PeerTube REST API (v1), mounted at /peertube on mock-platforms. A tenant is one instance:
``/peertube/{tenant}/api/v1/...``.

Shaped on what a real PeerTube v8.3.0 answered locally (2026-10-18, see app/connectors/peertube/manifest.yaml):
* errors are ``application/problem+json``: wrong password -> 400 ``code: invalid_grant``; expired / unknown token
  -> 401 ``code: invalid_token`` "Token is invalid"; bad fields -> 400 with ``invalid-params``; non-video -> 415;
  unknown video -> 404 "Video not found";
* the rate limit is 429 **text/html** "Too many requests, please try again later." + Retry-After + X-RateLimit-*;
* a refresh grant invalidates the old access token;
* upload answers ``{"video": {id, shortUUID, uuid}}``; with waitTranscoding=false the video is public while its
  state is 2 "To transcode";
* the watch page is the web client: 200 for any id, with og:title only while the video exists.

Accepted logins: any username whose password starts with ``sim-`` (simulation placeholders), or users added with
``POST /{tenant}/_mock/users {username, password}``. Mock-only controls: ``/_mock/users``, ``/_mock/expire_tokens``,
``/_mock/rate_limit {count, retry_after}``, ``/_mock/engage {id, views, likes, comments, watch_time}``,
``/_mock/blacklist {id}``, and ``GET /{tenant}/_static/clip.mp4`` (a stand-in media file).
"""

from __future__ import annotations

import html
import secrets
import threading
import time
import uuid as uuidlib
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from mocks.platforms.tenants import store

PLATFORM = "peertube"
TOKEN_TTL_S = 86399
REFRESH_TTL_S = 2419199
VIDEO_EXT = (".webm", ".ogv", ".ogg", ".mp4", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".mpg", ".mpeg", ".ts")
DOCS = "https://docs.joinpeertube.org/api-rest-reference.html"

router = APIRouter()
_lock = threading.Lock()


def _problem(status: int, detail: str, *, code: str | None = None, title: str | None = None, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"type": f"{DOCS}#section/Errors/{code}" if code else "about:blank", "detail": detail, "status": status}
    if title:
        body["title"] = title
    if code:
        body["code"] = code
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def _invalid(field: str, value: Any, msg: str, instance: str) -> JSONResponse:
    return _problem(400, f"Incorrect request parameters: {field}", title="Bad Request", instance=instance,
                    **{"invalid-params": {field: {"type": "field", "value": value, "msg": msg, "path": field, "location": "body"}}})


def _state(tenant: str) -> dict[str, Any]:
    extra = store.state(PLATFORM, tenant)["extra"]
    if "client" not in extra:
        extra.update({
            "client": {"client_id": secrets.token_hex(16), "client_secret": secrets.token_hex(16)},
            "users": {}, "tokens": {}, "refresh": {}, "videos": {},
            "rate_limit": {"count": 0, "retry_after": 10},
        })
    return extra


def _base(request: Request, tenant: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/{PLATFORM}/{tenant}"


def _rate_limited(st: dict[str, Any]) -> Response | None:
    with _lock:
        limit = st["rate_limit"]
        if limit["count"] <= 0:
            return None
        limit["count"] -= 1
    reset = int(time.time()) + int(limit["retry_after"])
    return HTMLResponse("Too many requests, please try again later.", status_code=429, headers={
        "Retry-After": str(limit["retry_after"]), "X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(reset)})


def _user(request: Request, st: dict[str, Any]) -> str | JSONResponse:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    entry = st["tokens"].get(token)
    if not token or entry is None or entry["expires_at"] < time.time():
        return _problem(401, "Token is invalid", code="invalid_token")
    return entry["username"]


def _find(st: dict[str, Any], video_id: str) -> dict[str, Any] | None:
    for video in st["videos"].values():
        if video_id in (str(video["id"]), video["uuid"], video["shortUUID"]):
            return video
    return None


def _video_json(request: Request, tenant: str, video: dict[str, Any], *, details: bool = True) -> dict[str, Any]:
    base = _base(request, tenant)
    desc = video["description"]
    out: dict[str, Any] = {
        "id": video["id"], "uuid": video["uuid"], "shortUUID": video["shortUUID"],
        "url": f"{base}/videos/watch/{video['uuid']}", "name": video["name"],
        "privacy": {"id": video["privacy"], "label": {1: "Public", 2: "Unlisted", 3: "Private"}.get(video["privacy"], "Internal")},
        "truncatedDescription": desc[:247] + "..." if desc and len(desc) > 250 else desc, "description": desc,
        "isLocal": True, "duration": 2, "views": video["views"], "viewers": 0, "likes": video["likes"], "dislikes": 0,
        "embedPath": f"/videos/embed/{video['shortUUID']}", "createdAt": video["createdAt"], "publishedAt": video["createdAt"],
        "isLive": False, "comments": video["comments"], "state": {"id": video["state"], "label": "To transcode" if video["state"] == 2 else "Published"},
        "waitTranscoding": video["waitTranscoding"], "blacklisted": video["blacklisted"], "tags": video["tags"],
        "account": {"name": video["owner"], "host": request.url.netloc},
        "channel": {"id": video["channelId"], "name": f"{video['owner']}_channel"},
    }
    if details:
        out["files"] = [{"id": video["id"], "resolution": {"id": 90, "label": "90p"}, "size": video["size"]}]
    return out


# ------------------------------------------------------------------ OAuth


@router.get("/{tenant}/api/v1/oauth-clients/local")
async def oauth_client(tenant: str) -> Any:
    return _state(tenant)["client"]


def _issue(st: dict[str, Any], username: str) -> dict[str, Any]:
    access, refresh = secrets.token_hex(20), secrets.token_hex(20)
    st["tokens"][access] = {"username": username, "expires_at": time.time() + TOKEN_TTL_S}
    st["refresh"][refresh] = {"username": username, "access": access, "expires_at": time.time() + REFRESH_TTL_S}
    return {"token_type": "Bearer", "access_token": access, "refresh_token": refresh, "expires_in": TOKEN_TTL_S,
            "refresh_token_expires_in": REFRESH_TTL_S}


@router.post("/{tenant}/api/v1/users/token")
async def token(tenant: str, request: Request) -> Any:
    st = _state(tenant)
    if (limited := _rate_limited(st)) is not None:
        return limited
    form = dict(await request.form())
    client = st["client"]
    if form.get("client_id") != client["client_id"] or form.get("client_secret") != client["client_secret"]:
        return _problem(400, "Invalid client: client is invalid", code="invalid_client")
    grant = form.get("grant_type")
    if grant == "password":
        username, password = str(form.get("username") or ""), str(form.get("password") or "")
        known = st["users"].get(username)
        if not username or not (password == known if known is not None else password.startswith("sim-")):
            return _problem(400, "Invalid grant: user credentials are invalid", code="invalid_grant")
        return _issue(st, username)
    if grant == "refresh_token":
        entry = st["refresh"].pop(str(form.get("refresh_token") or ""), None)
        if entry is None or entry["expires_at"] < time.time():
            return _problem(400, "Invalid grant: refresh token is invalid", code="invalid_grant")
        st["tokens"].pop(entry["access"], None)  # measured: the old access token stops working
        return _issue(st, entry["username"])
    return _problem(400, "Unsupported grant type", code="unsupported_grant_type")


@router.get("/{tenant}/api/v1/users/me")
async def me(tenant: str, request: Request) -> Any:
    st = _state(tenant)
    if (limited := _rate_limited(st)) is not None:
        return limited
    user = _user(request, st)
    if isinstance(user, JSONResponse):
        return user
    return {"id": 1, "username": user, "account": {"name": user, "id": 2},
            "videoChannels": [{"id": 1, "name": f"{user}_channel", "displayName": f"Main {user} channel"}]}


# ------------------------------------------------------------------ videos


@router.post("/{tenant}/api/v1/videos/upload")
async def upload(tenant: str, request: Request) -> Any:
    st = _state(tenant)
    if (limited := _rate_limited(st)) is not None:
        return limited
    user = _user(request, st)
    if isinstance(user, JSONResponse):
        return user
    instance = "/api/v1/videos/upload"
    form = await request.form()
    name = str(form.get("name") or "")
    if not 3 <= len(name) <= 120:
        return _invalid("name", name, "Should have a video name between 3 and 120 characters long", instance)
    description = str(form.get("description") or "")
    if len(description) > 10000:
        return _invalid("description", description, "Should have a valid description", instance)
    if str(form.get("channelId") or "") != "1":
        return _invalid("channelId", form.get("channelId"), "Should have correct video channel ID", instance)
    tags = [str(t) for t in form.getlist("tags[]")]
    if len(tags) > 5 or any(not 2 <= len(t) <= 30 for t in tags):
        return _invalid("tags", tags, "Should have correct tags", instance)
    upload_file = form.get("videofile")
    filename = getattr(upload_file, "filename", "") or ""
    if upload_file is None or not filename.lower().endswith(VIDEO_EXT):
        return _problem(415, "This file is not supported. Please, make sure it is of the following type: "
                        + ", ".join(VIDEO_EXT), title="Unsupported Media Type")
    data = await upload_file.read()
    video_id = store.next_id(PLATFORM, tenant)
    video = {
        "id": video_id, "uuid": str(uuidlib.uuid4()), "shortUUID": secrets.token_urlsafe(16)[:22], "name": name,
        "description": description or None, "privacy": int(form.get("privacy") or 1), "tags": tags, "owner": user,
        "channelId": 1, "waitTranscoding": str(form.get("waitTranscoding", "true")).lower() == "true",
        "state": 2, "views": 0, "likes": 0, "comments": 0, "watch_time": 0, "blacklisted": False, "size": len(data),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }
    st["videos"][video_id] = video
    return {"video": {"id": video["id"], "shortUUID": video["shortUUID"], "uuid": video["uuid"]}}


@router.get("/{tenant}/api/v1/users/me/videos")
async def my_videos(tenant: str, request: Request, count: int = 15, start: int = 0, sort: str = "-createdAt") -> Any:
    st = _state(tenant)
    if (limited := _rate_limited(st)) is not None:
        return limited
    user = _user(request, st)
    if isinstance(user, JSONResponse):
        return user
    mine = sorted((v for v in st["videos"].values() if v["owner"] == user), key=lambda v: v["id"], reverse=sort.startswith("-"))
    return {"total": len(mine), "data": [_video_json(request, tenant, v, details=False) for v in mine[start:start + count]]}


@router.get("/{tenant}/api/v1/videos/{video_id}")
async def get_video(tenant: str, video_id: str, request: Request) -> Any:
    st = _state(tenant)
    if (limited := _rate_limited(st)) is not None:
        return limited
    video = _find(st, video_id)
    if video is None:
        return _problem(404, "Video not found", title="Not Found")
    return _video_json(request, tenant, video)


@router.delete("/{tenant}/api/v1/videos/{video_id}", status_code=204, response_model=None)
async def delete_video(tenant: str, video_id: str, request: Request) -> Any:
    st = _state(tenant)
    if (limited := _rate_limited(st)) is not None:
        return limited
    user = _user(request, st)
    if isinstance(user, JSONResponse):
        return user
    video = _find(st, video_id)
    if video is None:
        return _problem(404, "Video not found", title="Not Found")
    if video["owner"] != user:
        return _problem(403, "Cannot remove video of another user", title="Forbidden")
    st["videos"].pop(video["id"], None)
    return Response(status_code=204)


@router.get("/{tenant}/api/v1/videos/{video_id}/stats/overall")
async def stats(tenant: str, video_id: str, request: Request) -> Any:
    st = _state(tenant)
    user = _user(request, st)
    if isinstance(user, JSONResponse):
        return user
    video = _find(st, video_id)
    if video is None:
        return _problem(404, "Video not found", title="Not Found")
    if video["owner"] != user:
        return _problem(403, "Cannot get stats of another user's video", title="Forbidden")
    return {"totalWatchTime": video["watch_time"], "averageWatchTime": 0, "totalViewers": video["views"], "viewersPeak": 0,
            "viewersPeakDate": None, "countries": [], "subdivisions": []}


# ------------------------------------------------------------------ the web client (watch page)


@router.get("/{tenant}/videos/watch/{video_id}")
@router.get("/{tenant}/w/{video_id}")
async def watch_page(tenant: str, video_id: str) -> HTMLResponse:
    video = _find(_state(tenant), video_id)
    tags = f'<meta property="og:title" content="{html.escape(video["name"])}" />' if video else ""
    # a single-page app: 200 for any id, deleted or not (measured on v8.3.0)
    return HTMLResponse(f"<!DOCTYPE html><html><head>{tags}<title>PeerTube</title></head><body><my-app></my-app></body></html>")


# ------------------------------------------------------------------ mock-only controls

# a stand-in media host for tests: 64 bytes that start like an MP4 (ftyp box); not a playable video
CLIP_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 40


@router.get("/{tenant}/_static/clip.mp4")
async def static_clip(tenant: str) -> Response:
    return Response(CLIP_BYTES, media_type="video/mp4")



@router.post("/{tenant}/_mock/users")
async def add_user(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    _state(tenant)["users"][str(payload["username"])] = str(payload["password"])
    return {"users": sorted(_state(tenant)["users"])}


@router.post("/{tenant}/_mock/expire_tokens")
async def expire_tokens(tenant: str) -> dict[str, Any]:
    tokens = _state(tenant)["tokens"]
    for entry in tokens.values():
        entry["expires_at"] = 0
    return {"expired": len(tokens)}


@router.post("/{tenant}/_mock/rate_limit")
async def rate_limit(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    _state(tenant)["rate_limit"] = {"count": int(payload.get("count", 1)), "retry_after": int(payload.get("retry_after", 10))}
    return _state(tenant)["rate_limit"]


@router.post("/{tenant}/_mock/engage")
async def engage(tenant: str, payload: dict[str, Any]) -> Any:
    video = _find(_state(tenant), str(payload.get("id")))
    if video is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    for field, key in (("views", "views"), ("likes", "likes"), ("comments", "comments"), ("watch_time", "watch_time")):
        video[key] += int(payload.get(field) or 0)
    return {k: video[k] for k in ("views", "likes", "comments", "watch_time")}


@router.post("/{tenant}/_mock/blacklist")
async def blacklist(tenant: str, payload: dict[str, Any]) -> Any:
    video = _find(_state(tenant), str(payload.get("id")))
    if video is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    video["blacklisted"] = True
    return {"blacklisted": True}
