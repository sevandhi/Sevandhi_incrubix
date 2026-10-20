"""Mock routes for the test-only connectors: /fakeforum/{tenant}/... and /fakeassist/{tenant}/submit."""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from mocks.platforms.tenants import store

forum = APIRouter()
assist = APIRouter()


def _authed(authorization: str | None) -> bool:
    return bool((authorization or "").removeprefix("Bearer ").strip())


def _pub(p: dict[str, Any]) -> dict[str, Any]:
    return {"id": p["id"], "url": p["url"], "status": p["status"], "title": p["title"], "marker": p["marker"]}


@forum.post("/{tenant}/posts", status_code=201)
async def create(tenant: str, payload: dict[str, Any], request: Request, authorization: str | None = Header(None)) -> Any:
    if not _authed(authorization):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pid = store.next_id("fakeforum", tenant)
    url = f"{str(request.base_url).rstrip('/')}/fakeforum/{tenant}/p/{pid}"
    post = {"id": pid, "url": url, "status": "published", "title": payload.get("title") or "", "body": payload.get("body", ""),
            "marker": payload.get("marker")}
    store.state("fakeforum", tenant)["posts"][pid] = post
    return _pub(post)


@forum.get("/{tenant}/posts")
async def find(tenant: str, marker: str | None = None, authorization: str | None = Header(None)) -> Any:
    posts = store.state("fakeforum", tenant)["posts"].values()
    return {"posts": [_pub(p) for p in posts if marker is None or p["marker"] == marker]}


@forum.get("/{tenant}/posts/{pid}")
async def get(tenant: str, pid: int) -> Any:
    post = store.state("fakeforum", tenant)["posts"].get(pid)
    return _pub(post) if post else JSONResponse({"error": "not_found"}, status_code=404)


@forum.get("/{tenant}/posts/{pid}/metrics")
async def metrics(tenant: str, pid: int) -> Any:
    return {"views": 3, "likes": 1} if pid in store.state("fakeforum", tenant)["posts"] else JSONResponse({}, status_code=404)


@forum.delete("/{tenant}/posts/{pid}")
async def delete(tenant: str, pid: int, authorization: str | None = Header(None)) -> Any:
    st = store.state("fakeforum", tenant)
    mode = st["extra"].get("delete_mode")
    if mode == "draft":  # behaves like Dev.to: no delete, the article goes back to draft
        st["posts"][pid]["status"] = "draft"
        return {"deleted": False}
    if st["posts"].pop(pid, None) is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {"deleted": True}


@forum.get("/{tenant}/p/{pid}")
async def page(tenant: str, pid: int) -> Any:
    post = store.state("fakeforum", tenant)["posts"].get(pid)
    if post is None or post["status"] != "published":
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    return HTMLResponse(f"<html><h1>{html.escape(post['title'])}</h1><p>{html.escape(post['body'])}</p></html>")


@forum.post("/{tenant}/_mode")
async def set_mode(tenant: str, payload: dict[str, Any]) -> Any:
    store.state("fakeforum", tenant)["extra"].update(payload)
    return {"ok": True}


@assist.get("/{tenant}/submit")
async def submit_page(tenant: str) -> Any:
    return HTMLResponse("<form>submit</form>")
