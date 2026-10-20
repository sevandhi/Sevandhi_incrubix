"""The self-served Media RSS feeds of the ``mrss`` connector (plan §7 ADV5).

    GET /api/v1/feeds/{feed_key}/mrss.xml       RSS 2.0 + Media RSS, newest items first
    GET /api/v1/feeds/{feed_key}/items/{guid}   one item as a one-item RSS document (an item's live URL); 404 once deleted
    GET /api/v1/r/{publication_id}              302 to the item's link and +1 click (``publication_id`` = the item guid,
                                                i.e. the publication's ``platform_post_id``)

Feed readers send no bearer token, so these GET routes are exempt from the bearer gate
(``PUBLIC_GET_PREFIXES`` in ``app/api/security.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app.connectors.mrss.feed import FeedStore, check_feed_key, render_feed
from app.errors import ErrorCode, IbxError

router = APIRouter(tags=["feeds"])

RSS = "application/rss+xml; charset=utf-8"
_RSS_RESPONSE = {200: {"content": {"application/rss+xml": {}}, "description": "RSS 2.0 + Media RSS document"}, 404: {}}


def _store(request: Request) -> FeedStore:
    return FeedStore(request.app.state.storage, request.app.state.session_factory)


def _base(request: Request) -> str:
    return request.app.state.settings.PUBLIC_BASE_URL.rstrip("/")


def _not_found(what: str) -> IbxError:
    return IbxError(ErrorCode.NOT_FOUND, f"No such {what}.")


@router.get("/api/v1/feeds/{feed_key}/mrss.xml", summary="A self-served Media RSS feed", response_class=Response,
            responses=_RSS_RESPONSE)
async def feed(feed_key: str, request: Request) -> Response:
    try:
        check_feed_key(feed_key)
    except IbxError:
        raise _not_found("feed") from None
    store = _store(request)
    xml = render_feed(feed_key, await store.items(feed_key), _base(request), await store.meta(feed_key))
    return Response(xml, media_type=RSS, headers={"Cache-Control": "public, max-age=60", "X-Content-Type-Options": "nosniff"})


@router.get("/api/v1/feeds/{feed_key}/items/{guid}", summary="One feed item (its live URL)", response_class=Response,
            responses=_RSS_RESPONSE)
async def feed_item(feed_key: str, guid: str, request: Request) -> Response:
    store = _store(request)
    item = await store.get_item(guid)
    if item is None or item.get("feed_key") != feed_key:
        raise _not_found("feed item")
    xml = render_feed(feed_key, [item], _base(request), await store.meta(feed_key))
    return Response(xml, media_type=RSS, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.get("/api/v1/r/{publication_id}", summary="Click-through redirect of a feed item (counts the click)",
            status_code=302, response_class=RedirectResponse, responses={302: {"description": "to the item's link"}, 404: {}})
async def click(publication_id: str, request: Request) -> RedirectResponse:
    store = _store(request)
    item = await store.get_item(publication_id)
    if item is None or not item.get("link"):
        raise _not_found("link")
    await store.add_click(publication_id)
    # the target was stored by the publisher (never taken from this request), so this is not an open redirect
    return RedirectResponse(item["link"], status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
