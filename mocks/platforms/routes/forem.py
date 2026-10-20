"""Mock Forem instance (the software behind dev.to), mounted at /forem. Tenants are instances: /forem/{tenant}/...

Follows Forem API v1 (``api-key`` header, ``Accept: application/vnd.forem.api-v1+json``):
    POST /api/articles {"article": {...}}       201 article | 422 {"error": "...", "status": 422}
    GET  /api/articles/{id}                     published articles only (404 for drafts / unpublished)
    PUT  /api/articles/{id} {"article": {...}}  update by the author (``published: false`` reverts to draft)
    PUT  /api/articles/{id}/unpublish           admin/moderator only, 204 (others 401)
    GET  /api/articles/me/all | me/published | me/unpublished   ?page=&per_page=
    GET  /{username}/{slug}                     public page: 200 when published, 404 otherwise
There is deliberately **no** article DELETE (like the real API): ``DELETE /api/articles/{id}`` is 404.

Keys: any non-empty key works; ``revoked*``/``invalid*`` keys get 401. A key starting with ``user-`` is a
normal author; every other key is the instance admin ``ibx_admin`` (we are admin of our own Forem).
Mock-only controls (not part of Forem):
    POST /_mock/rate_limit {"limit": n, "window_s": s, "retry_after": "seconds"|"http-date"}
    POST /_mock/articles/{id}/engage {"views": n, "reactions": n, "comments": n}
    POST /_mock/revoke {"key": "..."}
"""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from mocks.platforms.tenants import store

PLATFORM = "forem"
router = APIRouter()

TAG_RE = re.compile(r"^[a-z0-9]+$")
TITLE_MAX = 128


def _state(tenant: str) -> dict[str, Any]:
    extra = store.state(PLATFORM, tenant)["extra"]
    if "articles" not in extra:
        extra.update(articles={}, rate={"limit": 0, "window_s": 30.0, "retry_after": "seconds", "hits": []})
    return extra


def _err(message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "status": status}, status_code=status, headers=headers)


def _user(request: Request, tenant: str) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    key = request.headers.get("api-key") or ""
    revoked = store.state(PLATFORM, tenant)["revoked_tokens"]
    if not key or key.startswith(("revoked", "invalid")) or key in revoked:
        return None, _err("unauthorized", 401)
    if key.startswith("user-"):
        return {"username": "ibx_author", "name": "IBX Author", "admin": False}, None
    return {"username": "ibx_admin", "name": "IBX Admin", "admin": True}, None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z") if moment else None


def _base(request: Request, tenant: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/{PLATFORM}/{tenant}"


def _slug(title: str, article_id: int, published: bool) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "article"
    return f"{stem}-{article_id:04x}" if published else f"{stem}-temp-slug-{article_id * 7919 % 1000003}"


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(",")
    return [str(t).strip() for t in items if str(t).strip()]


def _validate(article: dict[str, Any]) -> str | None:
    title = str(article.get("title") or "")
    if not title.strip():
        return "Title can't be blank"
    if len(title) > TITLE_MAX:
        return f"Title is too long (maximum is {TITLE_MAX} characters)"
    tags = _tags(article.get("tags"))
    if len(tags) > 4:
        return "Tag list exceed the maximum of 4 tags"
    for tag in tags:
        if len(tag) > 30:
            return f'Tag "{tag}" is too long (maximum is 30 characters)'
        if not TAG_RE.match(tag.lower()):
            return f'Tag "{tag}" contains non-alphanumeric or prohibited unicode characters'
    return None


def _rate_limited(st: dict[str, Any]) -> JSONResponse | None:
    rate = st["rate"]
    if not rate["limit"]:
        return None
    now = time.monotonic()
    rate["hits"] = [t for t in rate["hits"] if now - t < rate["window_s"]]
    if len(rate["hits"]) >= rate["limit"]:
        wait = int(rate["window_s"] - (now - rate["hits"][0])) + 1
        if rate["retry_after"] == "http-date":
            retry_after = format_datetime(_now() + timedelta(seconds=wait), usegmt=True)
        else:
            retry_after = str(wait)
        return _err(f"Rate limit reached, try again in {wait} seconds", 429, {"Retry-After": retry_after})
    rate["hits"].append(now)
    return None


def _url(base: str, article: dict[str, Any]) -> str:
    return f"{base}/{article['username']}/{article['slug']}"


def _full(base: str, a: dict[str, Any]) -> dict[str, Any]:
    """The article as POST /api/articles and GET /api/articles/{id} return it."""
    return {
        "type_of": "article",
        "id": a["id"],
        "title": a["title"],
        "description": a["description"],
        "readable_publish_date": a["published_at"].strftime("%b %-d") if a["published_at"] else None,
        "slug": a["slug"],
        "path": f"/{a['username']}/{a['slug']}",
        "url": _url(base, a),
        "comments_count": a["comments"],
        "public_reactions_count": a["reactions"],
        "collection_id": None,
        "published_timestamp": _iso(a["published_at"]) or "",
        "positive_reactions_count": a["reactions"],
        "cover_image": a["main_image"],
        "social_image": a["main_image"] or f"{base}/social.png",
        "canonical_url": a["canonical_url"] or _url(base, a),
        "created_at": _iso(a["created_at"]),
        "edited_at": _iso(a["edited_at"]),
        "crossposted_at": None,
        "published_at": _iso(a["published_at"]),
        "last_comment_at": _iso(a["published_at"] or a["created_at"]),
        "reading_time_minutes": max(1, len(a["body_markdown"].split()) // 200),
        "tag_list": ", ".join(a["tags"]),
        "tags": list(a["tags"]),
        "body_html": f"<p>{html.escape(a['body_markdown'])}</p>",
        "body_markdown": a["body_markdown"],
        "user": {"name": a["name"], "username": a["username"], "twitter_username": None, "github_username": None,
                 "user_id": 1, "website_url": None, "profile_image": f"{base}/avatar.png",
                 "profile_image_90": f"{base}/avatar90.png"},
    }


def _me_item(base: str, a: dict[str, Any]) -> dict[str, Any]:
    """The article as GET /api/articles/me/* returns it (adds published, page_views_count; tag_list is a list)."""
    return {
        "type_of": "article",
        "id": a["id"],
        "title": a["title"],
        "description": a["description"],
        "published": a["published"],
        "published_at": _iso(a["published_at"]),
        "slug": a["slug"],
        "path": f"/{a['username']}/{a['slug']}",
        "url": _url(base, a),
        "comments_count": a["comments"],
        "public_reactions_count": a["reactions"],
        "page_views_count": a["views"],
        "published_timestamp": _iso(a["published_at"]) or "",
        "body_markdown": a["body_markdown"],
        "positive_reactions_count": a["reactions"],
        "cover_image": a["main_image"],
        "tag_list": list(a["tags"]),
        "canonical_url": a["canonical_url"] or _url(base, a),
        "reading_time_minutes": max(1, len(a["body_markdown"].split()) // 200),
        "user": {"name": a["name"], "username": a["username"], "user_id": 1},
    }


def _apply(a: dict[str, Any], data: dict[str, Any]) -> None:
    for field in ("title", "body_markdown", "canonical_url", "description", "main_image"):
        if field in data:
            a[field] = data[field] if data[field] is not None else ("" if field in ("title", "body_markdown") else None)
    if "tags" in data:
        a["tags"] = _tags(data["tags"])
    if "published" in data:
        published = bool(data["published"])
        if published and not a["published"]:
            a["published_at"] = a["published_at"] or _now()
        a["published"] = published
    a["slug"] = _slug(a["title"], a["id"], a["published"])
    a["edited_at"] = _now()


async def _article_body(request: Request) -> dict[str, Any] | None:
    try:
        data = await request.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("article"), dict):
        return None
    return data["article"]


@router.post("/{tenant}/api/articles", status_code=201)
async def create_article(tenant: str, request: Request) -> Any:
    user, error = _user(request, tenant)
    if error is not None:
        return error
    st = _state(tenant)
    data = await _article_body(request)
    if data is None:
        return _err("param is missing or the value is empty: article", 400)
    problem = _validate(data)
    if problem:
        return _err(problem, 422)
    if (limited := _rate_limited(st)) is not None:
        return limited
    article_id = store.next_id(PLATFORM, tenant)
    published = bool(data.get("published", False))
    now = _now()
    article = {"id": article_id, "title": "", "body_markdown": "", "description": str(data.get("description") or ""),
               "canonical_url": None, "main_image": None, "tags": [], "published": published,
               "published_at": now if published else None, "created_at": now, "edited_at": None,
               "username": user["username"], "name": user["name"], "views": 0, "reactions": 0, "comments": 0,
               "unpublished_by_admin": False}
    _apply(article, {k: v for k, v in data.items() if k != "published"})
    article["edited_at"] = None
    st["articles"][article_id] = article
    if not article["description"]:
        article["description"] = article["body_markdown"][:140].split("\n")[0]
    return _full(_base(request, tenant), article)


def _me_list(request: Request, tenant: str, user: dict[str, Any], published: bool | None, page: int, per_page: int) -> list[dict[str, Any]]:
    st = _state(tenant)
    mine = [a for a in st["articles"].values() if a["username"] == user["username"]]
    if published is not None:
        mine = [a for a in mine if a["published"] == published]
    mine.sort(key=lambda a: a["id"], reverse=True)
    per_page = max(1, min(per_page, 1000))
    start = (max(1, page) - 1) * per_page
    base = _base(request, tenant)
    return [_me_item(base, a) for a in mine[start:start + per_page]]


@router.get("/{tenant}/api/articles/me/{which}")
async def me_articles(tenant: str, which: str, request: Request, page: int = 1, per_page: int = 30) -> Any:
    user, error = _user(request, tenant)
    if error is not None:
        return error
    flags = {"all": None, "published": True, "unpublished": False}
    if which not in flags:
        return _err("not found", 404)
    return _me_list(request, tenant, user, flags[which], page, per_page)


@router.get("/{tenant}/api/articles/me")
async def me_default(tenant: str, request: Request, page: int = 1, per_page: int = 30) -> Any:
    user, error = _user(request, tenant)
    if error is not None:
        return error
    return _me_list(request, tenant, user, True, page, per_page)


@router.get("/{tenant}/api/articles/{article_id}")
async def get_article(tenant: str, article_id: str, request: Request) -> Any:
    st = _state(tenant)
    article = st["articles"].get(int(article_id)) if article_id.isdigit() else None
    if article is None or not article["published"]:
        return _err("not found", 404)
    return _full(_base(request, tenant), article)


@router.put("/{tenant}/api/articles/{article_id}")
async def update_article(tenant: str, article_id: int, request: Request) -> Any:
    user, error = _user(request, tenant)
    if error is not None:
        return error
    st = _state(tenant)
    article = st["articles"].get(article_id)
    if article is None:
        return _err("not found", 404)
    if article["username"] != user["username"]:
        return _err("unauthorized", 401)
    data = await _article_body(request)
    if data is None:
        return _err("param is missing or the value is empty: article", 400)
    merged = {"title": article["title"], "tags": article["tags"], **data}
    problem = _validate(merged)
    if problem:
        return _err(problem, 422)
    _apply(article, data)
    return _full(_base(request, tenant), article)


@router.put("/{tenant}/api/articles/{article_id}/unpublish")
async def unpublish_article(tenant: str, article_id: int, request: Request, note: str | None = None) -> Any:
    user, error = _user(request, tenant)
    if error is not None:
        return error
    if not user["admin"]:
        return _err("unauthorized", 401)
    st = _state(tenant)
    article = st["articles"].get(article_id)
    if article is None:
        return _err("not found", 404)
    article["published"] = False
    article["unpublished_by_admin"] = True
    article["slug"] = _slug(article["title"], article["id"], False)
    return Response(status_code=204)


@router.delete("/{tenant}/api/articles/{article_id}")
async def no_delete(tenant: str, article_id: str) -> Any:
    return _err("not found", 404)  # Forem API v1 has no article delete


@router.get("/{tenant}/{username}/{slug}")
async def public_page(tenant: str, username: str, slug: str) -> Any:
    st = _state(tenant)
    article = next((a for a in st["articles"].values() if a["username"] == username and a["slug"] == slug), None)
    if article is None or not article["published"]:
        return HTMLResponse("<h1>This page does not exist</h1>", status_code=404)
    article["views"] += 1
    return HTMLResponse(f"<html><head><title>{html.escape(article['title'])}</title></head>"
                        f"<body><h1>{html.escape(article['title'])}</h1><p>{html.escape(article['body_markdown'])}</p></body></html>")


@router.post("/{tenant}/_mock/rate_limit")
async def mock_rate_limit(tenant: str, request: Request) -> Any:
    data = await request.json()
    rate = _state(tenant)["rate"]
    rate.update(limit=int(data.get("limit") or 0), window_s=float(data.get("window_s") or 30),
                retry_after=str(data.get("retry_after") or "seconds"), hits=[])
    return {"rate": {k: v for k, v in rate.items() if k != "hits"}}


@router.post("/{tenant}/_mock/articles/{article_id}/engage")
async def mock_engage(tenant: str, article_id: int, request: Request) -> Any:
    data = await request.json()
    article = _state(tenant)["articles"].get(article_id)
    if article is None:
        return _err("not found", 404)
    article["views"] += int(data.get("views") or 0)
    article["reactions"] += int(data.get("reactions") or 0)
    article["comments"] += int(data.get("comments") or 0)
    return {"id": article_id, "views": article["views"], "reactions": article["reactions"], "comments": article["comments"]}


@router.post("/{tenant}/_mock/revoke")
async def mock_revoke(tenant: str, request: Request) -> Any:
    data = await request.json()
    store.state(PLATFORM, tenant)["revoked_tokens"].add(str(data.get("key") or ""))
    return {"revoked": True}
