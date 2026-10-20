"""Mock Lemmy 0.19 instance, mounted at /lemmy on mock-platforms. Tenants are instances: /lemmy/{tenant}/...

Faithful to the responses recorded from the real local Lemmy 0.19.20 (tests/fixtures/recorded/lemmy;
tests/integration/test_lemmy_mock_parity.py checks the shapes against them):
    POST /api/v3/user/login            {"jwt", ...} | 401 {"error": "incorrect_login"}
    POST /api/v3/user/logout           {"success": true} (the token is revoked: later calls get 401)
    GET  /api/v3/site                  version, site_view.local_site_rate_limit, my_user (with a valid token)
    PUT  /api/v3/site                  rate_limit_post / rate_limit_post_per_second (admin)
    GET  /api/v3/community?name=       community_view | 400 couldnt_find_community
    POST /api/v3/post                  post_view | 400 invalid_post_title | invalid_body_field | rate_limit_error
    GET  /api/v3/post?id=              post_view (admins also see soft-deleted posts) | 400 couldnt_find_post
    POST /api/v3/post/delete           post_view with deleted=true
    POST /api/v3/admin/purge/post      {"success": true} | 400 not_an_admin
    GET  /api/v3/user?username=&sort=New&limit=   the person's newest posts
    GET  /post/{id}                    the ActivityPub page (the live URL)
Measured rules reproduced here: titles are 3..200 characters after trimming with no newline; bodies are at
most 50000 UTF-16 units; the post rate limit is a token bucket per instance (default = the raised limits our
bootstrap sets, 1000 per 60 s; a test can lower it with PUT /api/v3/site); no Retry-After header is sent.

Auth: ``Authorization: Bearer <token>``. Any token works except a logged-out one or one starting with
``revoked``/``invalid`` (401 incorrect_login). No token on a write = 400 not_logged_in. A token containing
``nonadmin`` belongs to a normal user (purge is refused). Communities are created on first use, so
"add a community" needs no setup; a name starting with ``missing`` does not exist.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from mocks.platforms.tenants import store

PLATFORM = "lemmy"
VERSION = "0.19.20"
TITLE_MIN, TITLE_MAX, BODY_MAX_UTF16 = 3, 200, 50000
DEFAULT_ADMIN = "ibx_admin"
router = APIRouter()


# ---------------------------------------------------------------- state and helpers


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _state(tenant: str) -> dict[str, Any]:
    extra = store.state(PLATFORM, tenant)["extra"]
    extra.setdefault("communities", {})  # name -> community dict
    extra.setdefault("tokens", {})  # token -> username
    extra.setdefault("rate_limit", {"post": 1000, "post_per_second": 60})
    extra.setdefault("bucket", None)  # [tokens, last_monotonic]
    return extra


def _base(request: Request, tenant: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/{PLATFORM}/{tenant}"


def _error(token: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": token}, status_code=status)


def _token(authorization: str | None) -> str | None:
    value = (authorization or "").strip()
    return value[7:].strip() if value[:7].lower() == "bearer " else None


def _auth(tenant: str, authorization: str | None, *, required: bool = True) -> tuple[str | None, JSONResponse | None]:
    """(username, error). Optional auth returns (None, None) without a token."""
    token = _token(authorization)
    if not token:
        return None, (_error("not_logged_in") if required else None)
    revoked = store.state(PLATFORM, tenant)["revoked_tokens"]
    if token in revoked or token.startswith(("revoked", "invalid")):
        return None, _error("incorrect_login", 401)
    return _state(tenant)["tokens"].get(token, DEFAULT_ADMIN), None


def _is_admin(authorization: str | None) -> bool:
    return "nonadmin" not in (_token(authorization) or "")


def _person(request: Request, tenant: str, name: str) -> dict[str, Any]:
    return {"id": 2, "name": name, "banned": False, "published": "2026-10-14T18:44:51.982079Z",
            "actor_id": f"{_base(request, tenant)}/u/{name}", "local": True, "deleted": False,
            "bot_account": False, "instance_id": 1}


def _community(request: Request, tenant: str, name: str) -> dict[str, Any] | None:
    communities = _state(tenant)["communities"]
    if name.startswith("missing"):
        return None
    if name not in communities:
        communities[name] = {
            "id": len(communities) + 2, "name": name, "title": f"IBX test {name}",
            "description": "Mock community (IncuBrix).", "removed": False, "published": _now(), "deleted": False,
            "nsfw": False, "actor_id": f"{_base(request, tenant)}/c/{name}", "local": True, "hidden": False,
            "posting_restricted_to_mods": False, "instance_id": 1, "visibility": "Public",
        }
    return communities[name]


def _community_by_id(tenant: str, community_id: Any) -> dict[str, Any] | None:
    return next((c for c in _state(tenant)["communities"].values() if c["id"] == community_id), None)


def _post_view(request: Request, tenant: str, post: dict[str, Any]) -> dict[str, Any]:
    community = _community_by_id(tenant, post["community_id"]) or {}
    public = {k: post[k] for k in ("id", "name", "creator_id", "community_id", "removed", "locked", "published",
                                   "deleted", "nsfw", "ap_id", "local", "language_id", "featured_community",
                                   "featured_local")}
    if post.get("body") is not None:
        public["body"] = post["body"]
    if post.get("url"):
        public["url"] = post["url"]
    return {
        "post": public,
        "creator": _person(request, tenant, post["creator_name"]),
        "community": community,
        "creator_banned_from_community": False, "banned_from_community": False,
        "creator_is_moderator": True, "creator_is_admin": True,
        "counts": {"post_id": post["id"], "comments": post["comments"], "score": post["upvotes"] - post["downvotes"],
                   "upvotes": post["upvotes"], "downvotes": post["downvotes"], "published": post["published"],
                   "newest_comment_time": post["published"]},
        "subscribed": "Subscribed", "saved": False, "read": True, "hidden": False, "creator_blocked": False,
        "my_vote": 1, "unread_comments": 0,
    }


def _take_post_slot(tenant: str) -> bool:
    """Lemmy's token bucket: ``post`` slots, refilled at post/post_per_second per second."""
    st = _state(tenant)
    capacity, seconds = st["rate_limit"]["post"], st["rate_limit"]["post_per_second"]
    now = time.monotonic()
    tokens, last = st["bucket"] or [float(capacity), now]
    tokens = min(float(capacity), tokens + (now - last) * capacity / max(seconds, 1))
    if tokens < 1:
        st["bucket"] = [tokens, now]
        return False
    st["bucket"] = [tokens - 1, now]
    return True


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------- routes


@router.post("/{tenant}/api/v3/user/login")
async def login(tenant: str, payload: dict[str, Any]) -> Any:
    username = str(payload.get("username_or_email") or "")
    password = str(payload.get("password") or "")
    if not username or not password or password.startswith("wrong"):
        return _error("incorrect_login", 401)
    st = _state(tenant)
    token = f"mock-jwt-{username}-{len(st['tokens']) + 1}"
    st["tokens"][token] = username
    return {"jwt": token, "registration_created": False, "verify_email_sent": False}


@router.post("/{tenant}/api/v3/user/logout")
async def logout(tenant: str, authorization: str | None = Header(None)) -> Any:
    _, error = _auth(tenant, authorization)
    if error is not None:
        return error
    store.state(PLATFORM, tenant)["revoked_tokens"].add(_token(authorization))
    return {"success": True}


@router.get("/{tenant}/api/v3/site")
async def get_site(tenant: str, request: Request, authorization: str | None = Header(None)) -> Any:
    username, _ = _auth(tenant, authorization, required=False)
    st = _state(tenant)
    limit = st["rate_limit"]
    body: dict[str, Any] = {
        "version": VERSION,
        "site_view": {
            "site": {"id": 1, "name": "IBX mock Lemmy", "actor_id": f"{_base(request, tenant)}/", "instance_id": 1},
            "local_site": {"id": 1, "site_id": 1, "federation_enabled": False, "registration_mode": "Closed"},
            "local_site_rate_limit": {
                "local_site_id": 1, "message": 1000, "message_per_second": 60,
                "post": limit["post"], "post_per_second": limit["post_per_second"],
                "register": 100, "register_per_second": 3600, "image": 1000, "image_per_second": 60,
                "comment": 1000, "comment_per_second": 60, "search": 1000, "search_per_second": 60,
                "published": "2026-10-14T18:44:52.563732Z", "import_user_settings": 1,
                "import_user_settings_per_second": 86400,
            },
            "counts": {"site_id": 1, "users": 1, "posts": len(store.state(PLATFORM, tenant)["posts"])},
        },
        "admins": [], "all_languages": [], "discussion_languages": [], "taglines": [], "custom_emojis": [],
        "blocked_urls": [],
    }
    if username is not None:
        body["my_user"] = {
            "local_user_view": {"local_user": {"id": 1, "person_id": 2, "admin": _is_admin(authorization)},
                                "person": _person(request, tenant, username), "counts": {"person_id": 2}},
            "follows": [], "moderates": [], "community_blocks": [], "instance_blocks": [], "person_blocks": [],
            "discussion_languages": [],
        }
    return body


@router.put("/{tenant}/api/v3/site")
async def edit_site(tenant: str, payload: dict[str, Any], authorization: str | None = Header(None)) -> Any:
    _, error = _auth(tenant, authorization)
    if error is not None:
        return error
    if not _is_admin(authorization):
        return _error("not_an_admin")
    st = _state(tenant)
    if "rate_limit_post" in payload:
        st["rate_limit"]["post"] = int(payload["rate_limit_post"])
    if "rate_limit_post_per_second" in payload:
        st["rate_limit"]["post_per_second"] = int(payload["rate_limit_post_per_second"])
    st["bucket"] = None  # a new config starts with a full bucket
    return {"site_view": {"local_site_rate_limit": dict(st["rate_limit"])}}


@router.get("/{tenant}/api/v3/community")
async def get_community(tenant: str, request: Request, name: str = "", authorization: str | None = Header(None)) -> Any:
    _, error = _auth(tenant, authorization, required=False)
    if error is not None:
        return error
    community = _community(request, tenant, name) if name else None
    if community is None:
        return _error("couldnt_find_community")
    return {"community_view": {"community": community, "subscribed": "Subscribed", "blocked": False,
                               "counts": {"community_id": community["id"], "subscribers": 1}, "banned_from_community": False},
            "site": None, "moderators": [], "discussion_languages": []}


@router.post("/{tenant}/api/v3/post")
async def create_post(tenant: str, payload: dict[str, Any], request: Request, authorization: str | None = Header(None)) -> Any:
    username, error = _auth(tenant, authorization)
    if error is not None:
        return error
    title = str(payload.get("name") or "")
    body = payload.get("body")
    if not (TITLE_MIN <= len(title.strip()) <= TITLE_MAX) or "\n" in title:
        return _error("invalid_post_title")
    if body is not None and _utf16_len(str(body)) > BODY_MAX_UTF16:
        return _error("invalid_body_field")
    if _community_by_id(tenant, payload.get("community_id")) is None:
        return _error("couldnt_find_community")
    if not _take_post_slot(tenant):
        return _error("rate_limit_error")  # measured: HTTP 400 and no Retry-After / X-RateLimit headers
    post_id = store.next_id(PLATFORM, tenant)
    post = {
        "id": post_id, "name": title, "body": body, "url": payload.get("url"), "creator_id": 2,
        "creator_name": username, "community_id": payload["community_id"], "removed": False, "locked": False,
        "published": _now(), "deleted": False, "nsfw": False, "ap_id": f"{_base(request, tenant)}/post/{post_id}",
        "local": True, "language_id": 0, "featured_community": False, "featured_local": False,
        "comments": 0, "upvotes": 1, "downvotes": 0,
    }
    store.state(PLATFORM, tenant)["posts"][post_id] = post
    return {"post_view": _post_view(request, tenant, post)}


@router.get("/{tenant}/api/v3/post")
async def get_post(tenant: str, request: Request, id: int = 0, authorization: str | None = Header(None)) -> Any:  # noqa: A002
    _, error = _auth(tenant, authorization, required=False)
    if error is not None:
        return error
    post = store.state(PLATFORM, tenant)["posts"].get(id)
    if post is None:
        return _error("couldnt_find_post")
    view = _post_view(request, tenant, post)
    community = view["community"]
    return {"post_view": view,
            "community_view": {"community": community, "subscribed": "Subscribed", "blocked": False,
                               "counts": {"community_id": community.get("id"), "subscribers": 1}, "banned_from_community": False},
            "moderators": [], "cross_posts": []}


@router.post("/{tenant}/api/v3/post/delete")
async def delete_post(tenant: str, payload: dict[str, Any], request: Request, authorization: str | None = Header(None)) -> Any:
    _, error = _auth(tenant, authorization)
    if error is not None:
        return error
    post = store.state(PLATFORM, tenant)["posts"].get(payload.get("post_id"))
    if post is None:
        return _error("couldnt_find_post")
    post["deleted"] = bool(payload.get("deleted", True))
    return {"post_view": _post_view(request, tenant, post)}


@router.post("/{tenant}/api/v3/admin/purge/post")
async def purge_post(tenant: str, payload: dict[str, Any], authorization: str | None = Header(None)) -> Any:
    _, error = _auth(tenant, authorization)
    if error is not None:
        return error
    if not _is_admin(authorization):
        return _error("not_an_admin")
    if store.state(PLATFORM, tenant)["posts"].pop(payload.get("post_id"), None) is None:
        return _error("couldnt_find_post")
    return {"success": True}


@router.get("/{tenant}/api/v3/user")
async def get_person(
    tenant: str, request: Request, username: str = DEFAULT_ADMIN, limit: int = 10, authorization: str | None = Header(None)
) -> Any:
    _, error = _auth(tenant, authorization, required=False)
    if error is not None:
        return error
    posts = [p for p in store.state(PLATFORM, tenant)["posts"].values() if p["creator_name"] == username]
    posts.sort(key=lambda p: p["id"], reverse=True)  # sort=New
    return {
        "person_view": {"person": _person(request, tenant, username),
                        "counts": {"person_id": 2, "post_count": len(posts), "comment_count": 0}, "is_admin": True},
        "site": None, "moderates": [], "comments": [],
        "posts": [_post_view(request, tenant, p) for p in posts[: max(1, min(limit, 50))]],
    }


@router.get("/{tenant}/post/{post_id}")
async def live_page(tenant: str, post_id: int, request: Request) -> Any:
    post = store.state(PLATFORM, tenant)["posts"].get(post_id)
    if post is None or post["deleted"] or post["removed"]:
        return JSONResponse({"error": "couldnt_find_post"}, status_code=404)
    base = _base(request, tenant)
    community = _community_by_id(tenant, post["community_id"]) or {}
    return JSONResponse(
        {"@context": ["https://join-lemmy.org/context.json", "https://www.w3.org/ns/activitystreams"],
         "type": "Page", "id": post["ap_id"], "attributedTo": f"{base}/u/{post['creator_name']}",
         "to": [community.get("actor_id"), "https://www.w3.org/ns/activitystreams#Public"], "name": post["name"],
         "cc": [], "mediaType": "text/html", "source": {"content": post.get("body") or "", "mediaType": "text/markdown"},
         "attachment": [], "sensitive": False, "published": post["published"], "audience": community.get("actor_id")},
        media_type="application/activity+json",
    )
