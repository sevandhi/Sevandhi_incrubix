"""Mock partner implementing docs/PARTNER_WEBHOOK.md, mounted at /partner. Tenants are partners: /partner/{tenant}/...

Written the way a partner would write it (it does not import the connector's signing code):
* every /posts request must carry ``X-IncuBrix-Signature: t=<unix>,v1=<hex>``, the HMAC-SHA256 of
  ``"<t>." + raw body`` with the partner's secret. Bad or missing -> 401 ``invalid_signature``;
  ``|now - t| > 300 s`` -> 400 ``timestamp_outside_tolerance`` (replay window);
* POST /posts needs ``Idempotency-Key``: the same key + same body -> 200 with the same post
  (``Idempotent-Replayed: true``); the same key + a different body -> 409 ``idempotency_conflict``;
* GET /posts?idempotency_key=, GET /posts/{id}, DELETE /posts/{id} (204; 404 afterwards);
* GET /p/{id} is the public page (no signature): 200, or 404 once deleted.
Control routes (tests and demos only, unsigned):
    POST /_partner/revoke             every request now gets 401 {"error": "revoked"}
    POST /_partner/restore
    POST /_partner/rotate {"secret"}  the partner rotates its secret (old signatures fail: 401)
    POST /_partner/rate_limit {"limit", "window_s"}   429 with X-RateLimit-Reset (epoch) and Retry-After
The default secret is ``sim-secret`` (what SIMULATION_MODE sends).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from mocks.platforms.tenants import store

PLATFORM = "partner"
DEFAULT_SECRET = os.environ.get("SIM_SECRET", "sim-secret")  # the same setting the API signs with in simulation
WINDOW_S = 300
TITLE_MAX = 300
router = APIRouter()


def _state(tenant: str) -> dict[str, Any]:
    extra = store.state(PLATFORM, tenant)["extra"]
    if "secret" not in extra:
        extra.update(secret=DEFAULT_SECRET, revoked=False, keys={}, rate={"limit": 0, "window_s": 60.0, "hits": []})
    return extra


def _error(code: str, message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=headers)


def _check_signature(st: dict[str, Any], header: str | None, body: bytes) -> JSONResponse | None:
    if st["revoked"]:
        return _error("revoked", "This partner revoked IncuBrix's access.", 401)
    if not header:
        return _error("invalid_signature", "Missing X-IncuBrix-Signature.", 401)
    fields = dict(part.strip().split("=", 1) for part in header.split(",") if "=" in part)
    try:
        timestamp = int(fields.get("t", ""))
    except ValueError:
        return _error("invalid_signature", "Malformed signature header.", 401)
    if abs(time.time() - timestamp) > WINDOW_S:
        return _error("timestamp_outside_tolerance", f"Signature timestamp is more than {WINDOW_S}s from our clock.", 400)
    expected = hmac.new(st["secret"].encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, fields.get("v1", "")):
        return _error("invalid_signature", "Signature does not match.", 401)
    return None


def _rate_limited(st: dict[str, Any]) -> JSONResponse | None:
    rate = st["rate"]
    if not rate["limit"]:
        return None
    now = time.time()
    rate["hits"] = [t for t in rate["hits"] if now - t < rate["window_s"]]
    if len(rate["hits"]) >= rate["limit"]:
        reset = int(rate["hits"][0] + rate["window_s"]) + 1
        return _error("rate_limited", "Too many requests.", 429,
                      {"X-RateLimit-Reset": str(reset), "Retry-After": str(max(1, reset - int(now)))})
    rate["hits"].append(now)
    return None


def _public(request: Request, tenant: str, post: dict[str, Any]) -> dict[str, Any]:
    url = f"{str(request.base_url).rstrip('/')}/{PLATFORM}/{tenant}/p/{post['id']}"
    return {"id": post["id"], "url": url, "status": post["status"], "idempotency_key": post["idempotency_key"],
            "created_at": post["created_at"]}


@router.post("/{tenant}/posts")
async def create_post(tenant: str, request: Request) -> Any:
    st = _state(tenant)
    body = await request.body()
    if (error := _check_signature(st, request.headers.get("X-IncuBrix-Signature"), body)) is not None:
        return error
    key = request.headers.get("Idempotency-Key")
    if not key:
        return _error("idempotency_key_required", "Send an Idempotency-Key header.", 400)
    digest = hashlib.sha256(body).hexdigest()
    if key in st["keys"]:
        post_id, seen_digest = st["keys"][key]
        if seen_digest != digest:
            return _error("idempotency_conflict", "This Idempotency-Key was used with a different body.", 409)
        post = store.state(PLATFORM, tenant)["posts"][post_id]
        return JSONResponse(_public(request, tenant, post), status_code=200, headers={"Idempotent-Replayed": "true"})
    if (limited := _rate_limited(st)) is not None:
        return limited
    try:
        payload = json.loads(body)
        rendition = payload["rendition"]
    except (ValueError, KeyError, TypeError):
        return _error("invalid_body", "Body must be JSON with a 'rendition' object.", 400)
    title = str(rendition.get("title") or "")
    if len(title) > TITLE_MAX:
        return JSONResponse({"error": "validation_failed", "message": f"title is longer than {TITLE_MAX} characters",
                             "fields": [{"field": "title", "limit": TITLE_MAX, "actual": len(title)}]}, status_code=422)
    post_id = store.next_id(PLATFORM, tenant)
    post = {"id": post_id, "status": "published", "idempotency_key": key, "title": title,
            "body": str(rendition.get("body") or ""), "created_at": int(time.time())}
    store.state(PLATFORM, tenant)["posts"][post_id] = post
    st["keys"][key] = (post_id, digest)
    return JSONResponse(_public(request, tenant, post), status_code=201)


@router.get("/{tenant}/posts")
async def list_posts(tenant: str, request: Request, idempotency_key: str | None = None) -> Any:
    st = _state(tenant)
    if (error := _check_signature(st, request.headers.get("X-IncuBrix-Signature"), await request.body())) is not None:
        return error
    posts = store.state(PLATFORM, tenant)["posts"].values()
    return {"posts": [_public(request, tenant, p) for p in posts if idempotency_key in (None, p["idempotency_key"])]}


@router.get("/{tenant}/posts/{post_id}")
async def get_post(tenant: str, post_id: int, request: Request) -> Any:
    st = _state(tenant)
    if (error := _check_signature(st, request.headers.get("X-IncuBrix-Signature"), await request.body())) is not None:
        return error
    post = store.state(PLATFORM, tenant)["posts"].get(post_id)
    if post is None:
        return _error("not_found", "No such post.", 404)
    return _public(request, tenant, post)


@router.delete("/{tenant}/posts/{post_id}", response_model=None)
async def delete_post(tenant: str, post_id: int, request: Request) -> Any:
    st = _state(tenant)
    if (error := _check_signature(st, request.headers.get("X-IncuBrix-Signature"), await request.body())) is not None:
        return error
    if store.state(PLATFORM, tenant)["posts"].pop(post_id, None) is None:
        return _error("not_found", "No such post.", 404)
    return Response(status_code=204)


@router.get("/{tenant}/p/{post_id}")
async def public_page(tenant: str, post_id: int) -> Any:
    post = store.state(PLATFORM, tenant)["posts"].get(post_id)
    if post is None:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    return HTMLResponse(f"<html><body><h1>{html.escape(post['title'])}</h1><p>{html.escape(post['body'])}</p></body></html>")


@router.post("/{tenant}/_partner/revoke")
async def revoke(tenant: str) -> Any:
    _state(tenant)["revoked"] = True
    return {"revoked": True}


@router.post("/{tenant}/_partner/restore")
async def restore(tenant: str) -> Any:
    _state(tenant)["revoked"] = False
    return {"revoked": False}


@router.post("/{tenant}/_partner/rotate")
async def rotate(tenant: str, request: Request) -> Any:
    data = await request.json()
    _state(tenant)["secret"] = str(data["secret"])
    return {"rotated": True}


@router.post("/{tenant}/_partner/rate_limit")
async def rate_limit(tenant: str, request: Request) -> Any:
    data = await request.json()
    _state(tenant)["rate"].update(limit=int(data.get("limit") or 0), window_s=float(data.get("window_s") or 60), hits=[])
    return {"ok": True}
