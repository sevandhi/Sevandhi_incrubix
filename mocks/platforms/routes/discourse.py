"""Mock Discourse forum, mounted at /discourse on mock-platforms. Tenants are forums: /discourse/{tenant}/...

Faithful to the responses recorded from a real local Discourse 2026.10 (tests/fixtures/recorded/discourse):
    POST   /posts.json                    200 post JSON | 422 {"action":"create_post","errors":[...]}
    GET    /t/external_id/{id}.json       301 Location {base}/t/{slug}/{id}.json | 404
    GET    /t/{id}.json, /t/{slug}/{id}.json   topic JSON (admin sees soft-deleted topics, public gets 404)
    DELETE /t/{id}.json                   200 empty text/plain (soft delete: deleted_at is set); again -> 422
    GET    /t/{id}  (HTML)                301 to /t/{slug}/{id};  GET /t/{slug}/{id} -> 200 HTML (404 once deleted)
    GET    /site/basic-info.json
Auth: ``Api-Key`` + ``Api-Username``. A key starting with ``revoked``/``invalid`` (or revoked through the
control route) gets the measured 403 ``invalid_access`` body. No Api-Key header = an anonymous visitor.

Extra real endpoints used to drive metrics and hard cases:
    POST /posts.json {topic_id, raw}       a reply (comments)
    POST /post_actions.json {id, post_action_type_id: 2}   a like
    GET  /clicks/track?url=&post_id=&topic_id=             a link click (302 to url)
    PUT  /admin/site_settings/{name}.json {name: value}    min_topic_title_length, min_first_post_length,
         max_topic_title_length, rate_limit_create_topic (seconds between topics), review_every_post
    GET  /review.json, PUT /review/{id}/perform/approve_post.json   the moderation queue
    GET  /session/current.json             the key's user (200), 403 invalid_access for a dead key, 404 anonymous
Mock-only control: POST /_mock/revoke {key}; POST /_mock/restrict_category {category} (the key may not post
there: 403 invalid_access WITHOUT the "key is invalid" sentence, as Discourse::InvalidAccess renders it).

Measured on the real 2026.10 instance (beyond the recordings): deleting an already deleted or an
unknown topic answers 422 {"errors": ["There was an error deleting that topic. ..."]}, and
GET /t/external_id/{id}.json of a deleted topic is 404 even for an admin.
Assumptions not measured: a duplicate external_id answers 422; the 429
body follows Discourse's RateLimiter::LimitExceeded rendering (Retry-After + extras.wait_seconds); the
review-queue responses are simplified.
"""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mocks.platforms.tenants import store

PLATFORM = "discourse"
router = APIRouter()

AVATAR = "/letter_avatar_proxy/v4/letter/i/b4bc9f/{size}.png"
INVALID_ACCESS = {
    "errors": ["You are not permitted to view the requested resource. The API username or key is invalid."],
    "error_type": "invalid_access",
}
NOT_FOUND = {
    "errors": ["The requested URL or resource could not be found."],
    "error_type": "not_found",
    "extras": {"title": "Page Not Found", "html": "<div class=\"page-not-found\">Oops! That page doesn't exist or is private.</div>"},
}
NOT_PERMITTED = {"errors": ["You are not permitted to view the requested resource."], "error_type": "invalid_access"}
DELETE_FAILED = {"errors": ["There was an error deleting that topic. Please contact the site's administrator."]}
DEFAULT_SETTINGS: dict[str, Any] = {
    "min_topic_title_length": 15,
    "max_topic_title_length": 255,
    "min_first_post_length": 20,
    "min_post_length": 20,
    "max_post_length": 32000,
    "allow_duplicate_topic_titles": False,
    "rate_limit_create_topic": 0,
    "review_every_post": False,
}
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


# ---------------------------------------------------------------- tenant state


def _state(tenant: str) -> dict[str, Any]:
    st = store.state(PLATFORM, tenant)
    extra = st["extra"]
    if "settings" not in extra:
        extra.update(settings=dict(DEFAULT_SETTINGS), topics={}, posts={}, next_post_id=1, reviewables={},
                     next_reviewable_id=1, last_topic_at={})
    return extra


def _base(request: Request, tenant: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/{PLATFORM}/{tenant}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _auth(request: Request, tenant: str) -> tuple[str | None, JSONResponse | None]:
    """(username or None for anonymous, error response)."""
    key = request.headers.get("Api-Key")
    if key is None:
        return None, None
    revoked = store.state(PLATFORM, tenant)["revoked_tokens"]
    if not key or key.startswith(("revoked", "invalid")) or key in revoked:
        return None, JSONResponse(INVALID_ACCESS, status_code=403)
    return request.headers.get("Api-Username") or "system", None


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:80] or "topic"


async def _payload(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        data = await request.json()
        return data if isinstance(data, dict) else {}
    form = await request.form()
    return {k: v for k, v in form.items()}


def _post_json(post: dict[str, Any], topic: dict[str, Any], *, include_raw: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": post["id"],
        "name": None,
        "username": post["username"],
        "avatar_template": AVATAR,
        "created_at": post["created_at"],
        "cooked": post["cooked"],
        "post_number": post["post_number"],
        "post_type": 1,
        "posts_count": topic["posts_count"] if not topic["deleted_at"] else 0,
        "updated_at": post["created_at"],
        "reply_count": 0,
        "reply_to_post_number": None,
        "quote_count": 0,
        "incoming_link_count": 0,
        "reads": post["reads"],
        "readers_count": 0,
        "score": 0,
        "yours": True,
        "topic_id": topic["id"],
        "topic_slug": topic["slug"],
        "display_username": None,
        "primary_group_name": None,
        "badges_granted": [],
        "version": 1,
        "can_edit": True,
        "can_delete": False,
        "can_recover": bool(topic["deleted_at"]),
        "can_wiki": True,
        "user_title": None,
        "bookmarked": False,
        "actions_summary": [{"id": 2, "count": post["likes"]}] if post["likes"] else [{"id": 3, "can_act": True}],
        "moderator": False,
        "admin": True,
        "staff": True,
        "user_id": 1,
        "hidden": post["hidden"],
        "trust_level": 4,
        "deleted_at": topic["deleted_at"] if post["post_number"] == 1 else None,
        "user_deleted": False,
        "edit_reason": None,
        "can_view_edit_history": True,
        "wiki": False,
        "reviewable_id": None,
        "post_url": f"/t/{topic['slug']}/{topic['id']}/{post['post_number']}",
    }
    if post["links"]:
        data["link_counts"] = [{"url": url, "internal": False, "reflection": False, "title": None, "clicks": clicks}
                               for url, clicks in post["links"].items()]
    if include_raw:
        data["raw"] = post["raw"]
        data["draft_sequence"] = 0
    return data


def _topic_json(topic: dict[str, Any], posts: dict[int, dict[str, Any]]) -> dict[str, Any]:
    topic_posts = [posts[pid] for pid in topic["post_ids"]]
    likes = sum(p["likes"] for p in topic_posts)
    deleted = topic["deleted_at"]
    return {
        "post_stream": {"posts": [_post_json(p, topic) for p in topic_posts], "stream": topic["post_ids"]},
        "tags": [],
        "id": topic["id"],
        "title": topic["title"],
        "fancy_title": html.escape(topic["title"]),
        "posts_count": 0 if deleted else topic["posts_count"],
        "created_at": topic["created_at"],
        "views": topic["views"],
        "reply_count": max(0, topic["posts_count"] - 1),
        "like_count": likes,
        "last_posted_at": topic["created_at"],
        "visible": topic["visible"],
        "closed": False,
        "archived": False,
        "has_summary": False,
        "archetype": "regular",
        "slug": topic["slug"],
        "category_id": topic["category_id"],
        "word_count": sum(len(p["raw"].split()) for p in topic_posts),
        "deleted_at": deleted,
        "user_id": 1,
        "featured_link": None,
        "pinned_globally": False,
        "pinned_at": None,
        "pinned_until": None,
        "image_url": None,
        "slow_mode_seconds": 0,
        "draft": None,
        "draft_key": f"topic_{topic['id']}",
        "draft_sequence": 0,
        "posted": True,
        "unpinned": None,
        "pinned": False,
        "current_post_number": 1,
        "highest_post_number": len(topic_posts),
        "deleted_by": {"id": 1, "username": "system", "name": None, "avatar_template": AVATAR} if deleted else None,
        "has_deleted": bool(deleted),
        "actions_summary": [],
        "chunk_size": 20,
        "bookmarked": False,
        "bookmarks": [],
        "message_bus_last_id": 0,
        "participant_count": 1,
        "show_read_indicator": False,
        "thumbnails": None,
        "slow_mode_enabled_until": None,
        "external_id": topic["external_id"],
        "details": {"can_edit": True, "notification_level": 3, "can_delete": not deleted, "can_create_post": True,
                    "participants": [], "created_by": {"id": 1, "username": "system", "name": None, "avatar_template": AVATAR},
                    "last_poster": {"id": 1, "username": "system", "name": None, "avatar_template": AVATAR}},
    }


def _new_post(st: dict[str, Any], username: str, raw: str, post_number: int) -> dict[str, Any]:
    post_id = st["next_post_id"]
    st["next_post_id"] += 1
    post = {"id": post_id, "username": username, "raw": raw, "cooked": f"<p>{html.escape(raw)}</p>",
            "created_at": _now(), "post_number": post_number, "likes": 0, "reads": 0, "hidden": False,
            "links": {url: 0 for url in _URL_RE.findall(raw)}, "topic_id": None}
    st["posts"][post_id] = post
    return post


def _create_topic(st: dict[str, Any], tenant: str, username: str, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    topic_id = store.next_id(PLATFORM, tenant)
    post = _new_post(st, username, str(data.get("raw") or ""), 1)
    title = str(data.get("title") or "")
    category = data.get("category")
    topic = {"id": topic_id, "title": title, "slug": _slugify(title), "category_id": int(category) if str(category or "").isdigit() else 1,
             "external_id": data.get("external_id"), "created_at": post["created_at"], "deleted_at": None,
             "visible": True, "views": 0, "posts_count": 1, "post_ids": [post["id"]]}
    post["topic_id"] = topic_id
    st["topics"][topic_id] = topic
    return topic, post


def _validate_topic(st: dict[str, Any], data: dict[str, Any], base: str) -> list[str]:
    settings = st["settings"]
    title = str(data.get("title") or "")
    raw = str(data.get("raw") or "")
    errors: list[str] = []
    if not title.strip():
        errors.append("Title can't be blank")
    elif len(title) < int(settings["min_topic_title_length"]):
        errors.append(f"Title is too short (minimum is {settings['min_topic_title_length']} characters)")
    elif len(title) > int(settings["max_topic_title_length"]):
        errors.append(f"Title is too long (maximum is {settings['max_topic_title_length']} characters)")
    if len(raw.strip()) < int(settings["min_first_post_length"]):
        errors.append(f"Body is too short (minimum is {settings['min_first_post_length']} characters)")
    elif len(raw) > int(settings["max_post_length"]):
        errors.append(f"Body is too long (maximum is {settings['max_post_length']} characters)")
    live = [t for t in st["topics"].values() if not t["deleted_at"]]
    if title and not settings["allow_duplicate_topic_titles"]:
        dup = next((t for t in live if t["title"].strip().lower() == title.strip().lower()), None)
        if dup is not None:
            errors.append(f"This title has already been used by <a href='{base}/t/{dup['slug']}/{dup['id']}'>another topic</a>.")
    external_id = data.get("external_id")
    if external_id and any(t["external_id"] == external_id for t in st["topics"].values()):
        errors.append("External ID has already been taken")
    return errors


def _rate_limited(st: dict[str, Any], username: str) -> JSONResponse | None:
    every = float(st["settings"].get("rate_limit_create_topic") or 0)
    if every <= 0:
        return None
    last = st["last_topic_at"].get(username)
    now = time.monotonic()
    if last is not None and now - last < every:
        wait = int(every - (now - last)) + 1
        body = {"errors": [f"You’ve performed this action too many times. Please wait {wait} seconds before trying again."],
                "error_type": "rate_limit", "extras": {"wait_seconds": wait, "time_left": f"{wait} seconds"}}
        return JSONResponse(body, status_code=429, headers={"Retry-After": str(wait), "Discourse-Rate-Limit-Error-Code": "create_topic"})
    st["last_topic_at"][username] = now
    return None


# ---------------------------------------------------------------- routes


@router.post("/{tenant}/posts.json")
async def create_post(tenant: str, request: Request) -> Any:
    username, error = _auth(request, tenant)
    if error is not None:
        return error
    if username is None:
        return JSONResponse(INVALID_ACCESS, status_code=403)
    st = _state(tenant)
    data = await _payload(request)
    if data.get("topic_id"):  # a reply
        topic = st["topics"].get(int(data["topic_id"]))
        if topic is None or topic["deleted_at"]:
            return JSONResponse(NOT_FOUND, status_code=404)
        raw = str(data.get("raw") or "")
        if len(raw.strip()) < int(st["settings"]["min_post_length"]):
            return JSONResponse({"action": "create_post", "errors": [f"Body is too short (minimum is {st['settings']['min_post_length']} characters)"]}, status_code=422)
        topic["posts_count"] += 1
        post = _new_post(st, username, raw, topic["posts_count"])
        post["topic_id"] = topic["id"]
        topic["post_ids"].append(post["id"])
        return _post_json(post, topic, include_raw=True)
    if not str(data.get("title") or "") and not data.get("raw"):
        return JSONResponse({"errors": ["param is missing or the value is empty: raw"], "error_type": "invalid_parameters"}, status_code=400)
    if str(data.get("category") or "") in st.get("restricted_categories", set()):
        return JSONResponse(NOT_PERMITTED, status_code=403)
    errors = _validate_topic(st, data, _base(request, tenant))
    if errors:
        return JSONResponse({"action": "create_post", "errors": errors}, status_code=422)
    if (limited := _rate_limited(st, username)) is not None:
        return limited
    if st["settings"].get("review_every_post"):
        rid = st["next_reviewable_id"]
        st["next_reviewable_id"] += 1
        st["reviewables"][rid] = {"id": rid, "username": username, "payload": dict(data), "status": "pending"}
        return {"success": True, "action": "enqueued", "pending_post": {"id": rid, "raw": data.get("raw")}, "pending_count": 1}
    topic, post = _create_topic(st, tenant, username, data)
    return _post_json(post, topic, include_raw=True)


def _find(st: dict[str, Any], a: str, b: str | None) -> dict[str, Any] | None:
    raw_id = (b if b is not None else a).removesuffix(".json")
    if not raw_id.isdigit():
        return None
    return st["topics"].get(int(raw_id))


def _topic_response(request: Request, tenant: str, topic: dict[str, Any] | None, *, as_json: bool, slug_given: bool) -> Any:
    username, error = _auth(request, tenant)
    if error is not None:
        return error
    st = _state(tenant)
    if topic is None or (topic["deleted_at"] and username is None) or (not topic["visible"] and username is None and as_json):
        return JSONResponse(NOT_FOUND, status_code=404) if as_json else HTMLResponse("<h1>Page Not Found</h1>", status_code=404)
    if not as_json:
        if not slug_given:
            return RedirectResponse(f"{_base(request, tenant)}/t/{topic['slug']}/{topic['id']}", status_code=301)
        topic["views"] += 1
        first = st["posts"][topic["post_ids"][0]]
        first["reads"] += 1
        return HTMLResponse(f"<html><head><title>{html.escape(topic['title'])}</title></head>"
                            f"<body><h1>{html.escape(topic['title'])}</h1>{first['cooked']}</body></html>")
    return _topic_json(topic, st["posts"])


@router.get("/{tenant}/t/{a}")
async def get_topic_short(tenant: str, a: str, request: Request) -> Any:
    st = _state(tenant)
    return _topic_response(request, tenant, _find(st, a, None), as_json=a.endswith(".json"), slug_given=False)


@router.get("/{tenant}/t/{a}/{b}")
async def get_topic_long(tenant: str, a: str, b: str, request: Request) -> Any:
    st = _state(tenant)
    if a == "external_id":
        username, error = _auth(request, tenant)
        if error is not None:
            return error
        external_id = b.removesuffix(".json")
        topic = next((t for t in st["topics"].values() if t["external_id"] == external_id), None)
        if topic is None or topic["deleted_at"]:  # measured: 404 for a deleted topic, even for an admin
            return JSONResponse(NOT_FOUND, status_code=404)
        return Response(status_code=301, media_type="text/html",
                        headers={"Location": f"{_base(request, tenant)}/t/{topic['slug']}/{topic['id']}.json"})
    return _topic_response(request, tenant, _find(st, a, b), as_json=b.endswith(".json"), slug_given=True)


@router.delete("/{tenant}/t/{a}")
async def delete_topic(tenant: str, a: str, request: Request) -> Any:
    username, error = _auth(request, tenant)
    if error is not None:
        return error
    if username is None:
        return JSONResponse(INVALID_ACCESS, status_code=403)
    st = _state(tenant)
    topic = _find(st, a, None)
    if topic is None or topic["deleted_at"]:  # measured on 2026.10: unknown or already deleted -> 422
        return JSONResponse(DELETE_FAILED, status_code=422)
    topic["deleted_at"] = _now()
    return Response(content=b"", status_code=200, media_type="text/plain")


@router.post("/{tenant}/post_actions.json")
async def post_action(tenant: str, request: Request) -> Any:
    username, error = _auth(request, tenant)
    if error is not None:
        return error
    if username is None:
        return JSONResponse(INVALID_ACCESS, status_code=403)
    st = _state(tenant)
    data = await _payload(request)
    post = st["posts"].get(int(data.get("id") or 0))
    if post is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    if int(data.get("post_action_type_id") or 0) == 2:
        post["likes"] += 1
    return {"id": post["id"], "actions_summary": [{"id": 2, "count": post["likes"], "acted": True}]}


@router.get("/{tenant}/clicks/track")
async def track_click(tenant: str, url: str, post_id: int | None = None, topic_id: int | None = None) -> Any:
    st = _state(tenant)
    post = st["posts"].get(post_id or 0)
    if post is not None and url in post["links"]:
        post["links"][url] += 1
    return RedirectResponse(url, status_code=302)


@router.put("/{tenant}/admin/site_settings/{name}")
async def put_site_setting(tenant: str, name: str, request: Request) -> Any:
    username, error = _auth(request, tenant)
    if error is not None:
        return error
    if username is None:
        return JSONResponse(INVALID_ACCESS, status_code=403)
    setting = name.removesuffix(".json")
    st = _state(tenant)
    if setting not in st["settings"]:
        return JSONResponse({"errors": [f"No setting named '{setting}' exists"], "error_type": "not_found"}, status_code=404)
    data = await _payload(request)
    value = data.get(setting)
    default = DEFAULT_SETTINGS[setting]
    if isinstance(default, bool):
        value = str(value).lower() in ("true", "1", "yes")
    elif isinstance(default, int):
        value = int(value)
    st["settings"][setting] = value
    return Response(status_code=200)


@router.get("/{tenant}/review.json")
async def review_queue(tenant: str, request: Request) -> Any:
    username, error = _auth(request, tenant)
    if error is not None:
        return error
    st = _state(tenant)
    return {"reviewables": [{"id": r["id"], "type": "ReviewableQueuedPost", "status": 0, "payload": r["payload"]}
                            for r in st["reviewables"].values() if r["status"] == "pending"]}


@router.put("/{tenant}/review/{rid}/perform/{action}")
async def review_perform(tenant: str, rid: int, action: str, request: Request) -> Any:
    username, error = _auth(request, tenant)
    if error is not None:
        return error
    st = _state(tenant)
    item = st["reviewables"].get(rid)
    if item is None or item["status"] != "pending":
        return JSONResponse(NOT_FOUND, status_code=404)
    if action.removesuffix(".json") == "approve_post":
        topic, post = _create_topic(st, tenant, item["username"], item["payload"])
        item["status"] = "approved"
        return {"reviewable_perform_result": {"success": True, "transition_to": "approved", "created_post_id": post["id"],
                                              "created_post_topic_id": topic["id"]}}
    item["status"] = "rejected"
    return {"reviewable_perform_result": {"success": True, "transition_to": "rejected"}}


@router.get("/{tenant}/site/basic-info.json")
async def basic_info(tenant: str, request: Request) -> Any:
    base = _base(request, tenant)
    return {"logo_url": f"{base}/images/discourse-logo-sketch.png", "logo_small_url": f"{base}/images/discourse-logo-sketch-small.png",
            "apple_touch_icon_url": None, "favicon_url": None, "title": f"Mock Discourse {tenant}", "description": "",
            "header_primary_color": "333", "header_background_color": "fff", "login_required": False, "locale": "en",
            "include_in_discourse_discover": False, "mobile_logo_url": f"{base}/images/discourse-logo-sketch.png"}


@router.get("/{tenant}/session/current.json")
async def session_current(tenant: str, request: Request) -> Any:
    username, error = _auth(request, tenant)
    if error is not None:
        return error
    if username is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    return {"current_user": {"id": 1, "username": username, "admin": username == "system"}}


@router.post("/{tenant}/_mock/restrict_category")
async def mock_restrict_category(tenant: str, request: Request) -> Any:
    data = await _payload(request)
    _state(tenant).setdefault("restricted_categories", set()).add(str(data.get("category") or ""))
    return {"restricted": True}


@router.post("/{tenant}/_mock/revoke")
async def mock_revoke(tenant: str, request: Request) -> Any:
    data = await _payload(request)
    store.state(PLATFORM, tenant)["revoked_tokens"].add(str(data.get("key") or ""))
    return {"revoked": True}
