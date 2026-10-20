"""Mock "submit page" for assisted destinations, mounted at /assist. Tenants are sites: /assist/{tenant}/...

    GET  /submit?title=&url=&text=     200 HTML form, prefilled from the query (what deep_link opens)
    POST /submit (form: title, text, url)   a person submits -> 303 to /p/{id}
    GET  /p/{id}                       the public post page (200; 404 after DELETE /p/{id})
    GET  /media/{name}                 a tiny sample PNG / text file to use as rendition media in tests
"""

from __future__ import annotations

import base64
import html
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from mocks.platforms.tenants import store

PLATFORM = "assist"
router = APIRouter()

# 1x1 transparent PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@router.get("/{tenant}/submit")
async def submit_form(tenant: str, title: str = "", url: str = "", text: str = "") -> Any:
    def field(name: str, value: str) -> str:
        return f'<label>{name}<input name="{name}" value="{html.escape(value, quote=True)}"></label>'

    return HTMLResponse(
        f"<html><head><title>Submit to {html.escape(tenant)}</title></head><body>"
        f'<form method="post">{field("title", title)}{field("url", url)}'
        f'<textarea name="text">{html.escape(text)}</textarea><button>Post</button></form></body></html>'
    )


@router.post("/{tenant}/submit")
async def submit(tenant: str, request: Request) -> Any:
    form = await request.form()
    post_id = store.next_id(PLATFORM, tenant)
    store.state(PLATFORM, tenant)["posts"][post_id] = {
        "id": post_id, "title": str(form.get("title") or ""), "text": str(form.get("text") or ""), "url": str(form.get("url") or ""),
    }
    return RedirectResponse(f"{str(request.base_url).rstrip('/')}/{PLATFORM}/{tenant}/p/{post_id}", status_code=303)


@router.get("/{tenant}/p/{post_id}")
async def post_page(tenant: str, post_id: int) -> Any:
    post = store.state(PLATFORM, tenant)["posts"].get(post_id)
    if post is None:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    return HTMLResponse(f"<html><body><h1>{html.escape(post['title'])}</h1><p>{html.escape(post['text'])}</p></body></html>")


@router.delete("/{tenant}/p/{post_id}", response_model=None)
async def delete_post(tenant: str, post_id: int) -> Any:
    if store.state(PLATFORM, tenant)["posts"].pop(post_id, None) is None:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    return Response(status_code=204)


@router.get("/{tenant}/media/{name}")
async def media(tenant: str, name: str) -> Any:
    if name.endswith(".png"):
        return Response(PNG, media_type="image/png")
    return Response(f"sample media {name}".encode(), media_type="text/plain")
