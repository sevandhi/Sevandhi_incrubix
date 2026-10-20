"""Mock Ghost 6 site, mounted at /ghost on mock-platforms. Tenants are sites: /ghost/{tenant}/...

Faithful to the responses recorded from the real local Ghost 6.70 (tests/fixtures/recorded/ghost;
tests/integration/test_ghost_mock_parity.py checks the shapes against them):
    GET    /ghost/api/admin/site/                {"site": {...}} (no auth)
    POST   /ghost/api/admin/posts/               201 {"posts": [post]} | 422 ValidationError (title > 255, slug > 191)
    GET    /ghost/api/admin/posts/?filter=slug:~$'<suffix>'&limit=   {"posts": [...], "meta": {...}}
    GET    /ghost/api/admin/posts/{id}/          {"posts": [post]} | 404 NotFoundError
    GET    /ghost/api/admin/posts/slug/{slug}/   {"posts": [post]} | 404 NotFoundError
    DELETE /ghost/api/admin/posts/{id}/          204 | 404 NotFoundError
    GET    /{slug}/                              the public page (HTML) | 404 HTML
Auth: ``Authorization: Ghost <jwt>``. The signature cannot be checked (the mock does not know the secret),
but the rest is: a missing header, a malformed token, a wrong ``aud`` or an expired ``exp`` give 401
INVALID_JWT with Ghost's messages; a key id starting with ``unknown``/``revoked`` (or revoked through
``POST /{tenant}/_mock/revoke {"kid"}``, like deleting the integration) gives 401 UNKNOWN_ADMIN_API_KEY.
A taken slug gets ``-2`` appended, as on the real site.
"""

from __future__ import annotations

import html
import json
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from mocks.platforms.tenants import store

PLATFORM = "ghost"
VERSION = "6.70"
TITLE_MAX, SLUG_MAX = 255, 191
_SUFFIX_FILTER = re.compile(r"^slug:~\$'([^']*)'$")
router = APIRouter()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _site(request: Request, tenant: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/{PLATFORM}/{tenant}"


def _errors(status: int, message: str, type_: str, *, code: str | None = None, context: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"errors": [{"message": message, "context": context, "type": type_, "details": None, "property": None,
                     "help": None, "code": code, "id": str(uuid.uuid4()), "ghostErrorCode": None}]},
        status_code=status,
    )


def _not_found(action: str) -> JSONResponse:
    return _errors(404, f"Resource not found error, cannot {action} post.", "NotFoundError", context="Post not found.")


def _auth(tenant: str, authorization: str | None) -> JSONResponse | None:
    value = authorization or ""
    if not value.startswith("Ghost "):
        return _errors(401, 'Authorization header format is "Authorization: Ghost [token]"', "UnauthorizedError",
                       code="INVALID_AUTH_HEADER")
    token = value.removeprefix("Ghost ").strip()
    try:
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False, "verify_aud": False})
    except jwt.PyJWTError:
        return _errors(401, "Invalid token: jwt malformed", "UnauthorizedError", code="INVALID_JWT")
    kid = str(header.get("kid") or "")
    revoked = store.state(PLATFORM, tenant)["revoked_tokens"]
    if not kid or kid.startswith(("unknown", "revoked")) or kid in revoked:
        return _errors(401, "Unknown Admin API Key", "UnauthorizedError", code="UNKNOWN_ADMIN_API_KEY")
    if claims.get("aud") != "/admin/":
        return _errors(401, "Invalid token: jwt audience invalid. expected: /admin/", "UnauthorizedError", code="INVALID_JWT")
    if not isinstance(claims.get("exp"), int | float) or claims["exp"] <= time.time():
        return _errors(401, "Invalid token: jwt expired", "UnauthorizedError", code="INVALID_JWT")
    return None


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _public(post: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "uuid", "title", "slug", "mobiledoc", "lexical", "html", "comment_id", "feature_image", "featured",
            "status", "visibility", "created_at", "updated_at", "published_at", "custom_excerpt", "canonical_url",
            "tags", "authors", "url", "excerpt", "reading_time", "email", "newsletter", "count")
    return {k: post.get(k) for k in keep}


def _posts(tenant: str) -> dict[str, dict[str, Any]]:
    return store.state(PLATFORM, tenant)["posts"]


@router.get("/{tenant}/ghost/api/admin/site/")
async def site(tenant: str, request: Request) -> Any:
    return {"site": {"title": "IBX mock Ghost", "description": "Thoughts, stories and ideas.", "logo": None, "icon": None, "cover_image": None,
                     "accent_color": "#FF1A75", "locale": "en", "timezone": "Etc/UTC", "url": f"{_site(request, tenant)}/",
                     "version": VERSION, "allow_external_signup": True, "site_uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, tenant))}}


@router.post("/{tenant}/ghost/api/admin/posts/", status_code=201)
async def create_post(tenant: str, payload: dict[str, Any], request: Request, authorization: str | None = Header(None)) -> Any:
    if (error := _auth(tenant, authorization)) is not None:
        return error
    data = (payload.get("posts") or [{}])[0]
    title = str(data.get("title") or "") or "(Untitled)"
    if len(title) > TITLE_MAX:
        return _errors(422, "Validation error, cannot save post.", "ValidationError",
                       context=f"Value in [posts.title] exceeds maximum length of {TITLE_MAX} characters. posts.title")
    slug = _slugify(str(data.get("slug") or title)) or "untitled"
    if len(slug) > SLUG_MAX:
        return _errors(422, "Validation error, cannot save post.", "ValidationError",
                       context=f"Value in [posts.slug] exceeds maximum length of {SLUG_MAX} characters. posts.slug")
    taken = {p["slug"] for p in _posts(tenant).values()}
    base, n = slug, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    number = store.next_id(PLATFORM, tenant)
    post_id = f"6acf{number:020x}"
    post_uuid = str(uuid.uuid4())
    status = data.get("status") if data.get("status") in ("published", "draft", "scheduled") else "draft"
    now = _now()
    site = _site(request, tenant)
    post = {
        "id": post_id, "uuid": post_uuid, "title": title, "slug": slug, "mobiledoc": None,
        "lexical": data.get("lexical"), "html": data.get("html"), "comment_id": post_id, "feature_image": None,
        "featured": False, "status": status, "visibility": "public", "created_at": now, "updated_at": now,
        "published_at": now if status == "published" else None, "custom_excerpt": None,
        "canonical_url": data.get("canonical_url"), "tags": [{"name": t.get("name")} for t in data.get("tags") or []],
        "authors": [{"id": "6acfcd9b8944510001d12d97", "name": "IBX Owner", "slug": "ibx"}],
        "url": f"{site}/{slug}/" if status == "published" else f"{site}/p/{post_uuid}/",
        "excerpt": "", "reading_time": 0, "email": None, "newsletter": None,
        "count": {"clicks": 0, "positive_feedback": 0, "negative_feedback": 0},
    }
    _posts(tenant)[post_id] = post
    return JSONResponse({"posts": [_public(post)]}, status_code=201,
                        headers={"Location": f"{site}/ghost/api/admin/posts/{post_id}/"})


@router.get("/{tenant}/ghost/api/admin/posts/")
async def browse_posts(
    tenant: str, filter: str | None = None, limit: str = "15", authorization: str | None = Header(None)  # noqa: A002
) -> Any:
    if (error := _auth(tenant, authorization)) is not None:
        return error
    posts = sorted(_posts(tenant).values(), key=lambda p: p["id"], reverse=True)
    if filter:
        match = _SUFFIX_FILTER.match(filter)
        if match is None:
            return _errors(400, "Error parsing filter", "BadRequestError")
        posts = [p for p in posts if p["slug"].endswith(match.group(1))]
    size = len(posts) if limit == "all" else max(1, int(limit) if limit.isdigit() else 15)
    page = posts[:size]
    return {"posts": [_public(p) for p in page],
            "meta": {"pagination": {"page": 1, "limit": size, "pages": 1, "total": len(posts), "next": None, "prev": None}}}


@router.get("/{tenant}/ghost/api/admin/posts/slug/{slug}/")
async def read_post_by_slug(tenant: str, slug: str, authorization: str | None = Header(None)) -> Any:
    if (error := _auth(tenant, authorization)) is not None:
        return error
    post = next((p for p in _posts(tenant).values() if p["slug"] == slug), None)
    return {"posts": [_public(post)]} if post else _not_found("read")


@router.get("/{tenant}/ghost/api/admin/posts/{post_id}/")
async def read_post(tenant: str, post_id: str, authorization: str | None = Header(None)) -> Any:
    if (error := _auth(tenant, authorization)) is not None:
        return error
    post = _posts(tenant).get(post_id)
    return {"posts": [_public(post)]} if post else _not_found("read")


@router.delete("/{tenant}/ghost/api/admin/posts/{post_id}/", status_code=204, response_model=None)
async def delete_post(tenant: str, post_id: str, authorization: str | None = Header(None)) -> Any:
    if (error := _auth(tenant, authorization)) is not None:
        return error
    if _posts(tenant).pop(post_id, None) is None:
        return _not_found("delete")
    return Response(status_code=204)


@router.post("/{tenant}/_mock/revoke")
async def revoke_key(tenant: str, payload: dict[str, Any]) -> Any:
    """Mock-only control: forget an Admin key id, as if its integration were deleted in Ghost Admin."""
    store.state(PLATFORM, tenant)["revoked_tokens"].add(str(payload.get("kid") or ""))
    return {"revoked": True}


@router.get("/{tenant}/{slug}/", response_class=HTMLResponse)
async def public_page(tenant: str, slug: str) -> Any:
    post = next((p for p in _posts(tenant).values() if p["slug"] == slug and p["status"] == "published"), None)
    if post is None:
        return HTMLResponse("<!DOCTYPE html><html><head><title>404 — Page not found</title></head>"
                            "<body><h1>404</h1><p>Page not found</p></body></html>", status_code=404)
    # Ghost renders the title and the post's content; a Lexical markdown card is shown as its (escaped) text
    content = post.get("html") or f"<pre>{html.escape(_lexical_text(post.get('lexical')))}</pre>"
    return HTMLResponse(f"<!DOCTYPE html><html lang=\"en\"><head><title>{html.escape(post['title'])}</title></head>"
                        f"<body><article><h1>{html.escape(post['title'])}</h1>{content}</article></body></html>")


def _lexical_text(lexical: Any) -> str:
    """The markdown of every markdown card in a Lexical document (what our connector sends)."""
    try:
        doc = json.loads(lexical) if isinstance(lexical, str) else (lexical or {})
        return "\n\n".join(str(c.get("markdown", "")) for c in doc.get("root", {}).get("children", []) if isinstance(c, dict))
    except (ValueError, AttributeError):
        return ""
