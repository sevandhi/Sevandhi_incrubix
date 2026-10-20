"""Mock Slack Web API, mounted at /slack on mock-platforms. A tenant is one workspace: ``/slack/{tenant}/api/<method>``.

Faithful to the documented Web API (docs.slack.dev, read 2026-10-18), which is what makes Slack special:
* every answer is **HTTP 200**; failures are ``{"ok": false, "error": "<code>"}`` (token_revoked, invalid_auth,
  not_authed, token_expired, channel_not_found, msg_too_long, no_text, invalid_metadata_format, message_not_found ...);
* rate limits are **HTTP 429** with ``Retry-After`` and ``{"ok": false, "error": "ratelimited"}``, or (chat.postMessage)
  HTTP 200 ``{"ok": false, "error": "rate_limited"}`` when an app posts too much;
* message metadata is only returned by conversations.history with ``include_all_metadata=true``.

Methods: chat.postMessage, chat.delete, chat.getPermalink, conversations.history, reactions.get, auth.test.
Arguments may come as JSON, form fields or the query string (Slack accepts all three).
Tokens: ``xoxb-``/``xoxp-`` tokens are accepted; ``xoxb-expired...`` answers token_expired; anything else
invalid_auth; no token not_authed; a revoked token token_revoked.
Channel ids must look like Slack's (``C``/``G``/``D`` + upper-case letters and digits), else channel_not_found.

Mock-only controls (not Slack API): ``POST /{tenant}/_mock/revoke {token}``, ``/_mock/react {channel, ts, name, count}``,
``/_mock/reply {channel, ts}``, ``/_mock/rate_limit {count, retry_after, mode: http_429|rate_limited}``,
``/_mock/archive {channel}``. The permalink page ``/{tenant}/archives/{channel}/p<ts digits>`` shows the
message (200) and answers 404 once it is deleted.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from mocks.platforms.tenants import store

PLATFORM = "slack"
TEXT_MAX = 40_000  # above this the message is refused (msg_too_long)
CHANNEL_RE = re.compile(r"^[CGD][A-Z0-9]{2,}$")
BOT_USER, BOT_ID, APP_ID, TEAM = "U0IBXBOT1", "B0IBXBOT1", "A0IBXAPP1", "T0IBXTEAM"

router = APIRouter()
_lock = threading.Lock()


def _ok(**fields: Any) -> JSONResponse:
    return JSONResponse({"ok": True, **fields})


def _fail(error: str, *, status: int = 200, headers: dict[str, str] | None = None, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error": error, **extra}
    return JSONResponse(body, status_code=status, headers=headers)


def _state(tenant: str) -> dict[str, Any]:
    extra = store.state(PLATFORM, tenant)["extra"]
    extra.setdefault("channels", {})  # channel -> {ts: message}
    extra.setdefault("archived", set())
    extra.setdefault("rate_limit", {"count": 0, "retry_after": 30, "mode": "http_429"})
    extra.setdefault("seq", 0)
    return extra


async def _args(request: Request) -> dict[str, Any]:
    """Slack accepts JSON, form-encoded and query arguments."""
    args: dict[str, Any] = dict(request.query_params)
    content_type = request.headers.get("content-type", "")
    if request.method == "POST":
        if "application/json" in content_type:
            try:
                body = await request.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return {"__error__": "invalid_json"}
            args.update(body)
        elif "form" in content_type:
            args.update(dict(await request.form()))
    return args


def _auth(request: Request, args: dict[str, Any], tenant: str) -> JSONResponse | None:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else str(args.get("token") or "")
    if not token:
        return _fail("not_authed")
    if token in store.state(PLATFORM, tenant)["revoked_tokens"]:
        return _fail("token_revoked")
    if token.startswith("xoxb-expired"):
        return _fail("token_expired")
    if not token.startswith(("xoxb-", "xoxp-")):
        return _fail("invalid_auth")
    return None


def _flag(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "yes")


def _public(message: dict[str, Any], *, with_metadata: bool) -> dict[str, Any]:
    out = {k: v for k, v in message.items() if not k.startswith("_") and (with_metadata or k != "metadata")}
    if not out.get("reactions"):
        out.pop("reactions", None)
    return out


def _permalink(request: Request, tenant: str, channel: str, ts: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/{PLATFORM}/{tenant}/archives/{channel}/p{ts.replace('.', '')}"


def _channel(st: dict[str, Any], args: dict[str, Any]) -> tuple[str, JSONResponse | None]:
    channel = str(args.get("channel") or "")
    if not CHANNEL_RE.match(channel):
        return channel, _fail("channel_not_found")
    st["channels"].setdefault(channel, {})
    return channel, None


@router.api_route("/{tenant}/api/{method}", methods=["GET", "POST"])
async def web_api(tenant: str, method: str, request: Request) -> Any:
    args = await _args(request)
    if args.get("__error__"):
        return _fail("invalid_json")
    if (denied := _auth(request, args, tenant)) is not None:
        return denied
    st = _state(tenant)
    handler = METHODS.get(method)
    if handler is None:
        return _fail("unknown_method", status=404)
    return handler(request, tenant, st, args)


def _post_message(request: Request, tenant: str, st: dict[str, Any], args: dict[str, Any]) -> JSONResponse:
    with _lock:
        limit = st["rate_limit"]
        if limit["count"] > 0:
            limit["count"] -= 1
            if limit["mode"] == "rate_limited":
                return _fail("rate_limited")
            return _fail("ratelimited", status=429, headers={"Retry-After": str(limit["retry_after"])})
    channel, error = _channel(st, args)
    if error is not None:
        return error
    if channel in st["archived"]:
        return _fail("is_archived")
    text = str(args.get("text") or "")
    if not text.strip() and not args.get("blocks"):
        return _fail("no_text")
    if len(text) > TEXT_MAX:
        return _fail("msg_too_long")
    metadata = args.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return _fail("invalid_metadata_format")
    if metadata is not None and (not isinstance(metadata, dict) or not isinstance(metadata.get("event_type"), str)
                                 or not isinstance(metadata.get("event_payload", {}), dict)):
        return _fail("invalid_metadata_format")
    with _lock:
        st["seq"] += 1
        ts = f"{int(time.time())}.{st['seq']:06d}"
    message: dict[str, Any] = {"type": "message", "user": BOT_USER, "bot_id": BOT_ID, "app_id": APP_ID, "team": TEAM,
                               "text": text, "ts": ts, "reactions": []}
    if metadata is not None:
        message["metadata"] = {"event_type": metadata["event_type"], "event_payload": metadata.get("event_payload", {})}
    st["channels"][channel][ts] = message
    return _ok(channel=channel, ts=ts, message=_public(message, with_metadata=False))


def _delete(request: Request, tenant: str, st: dict[str, Any], args: dict[str, Any]) -> JSONResponse:
    channel, error = _channel(st, args)
    if error is not None:
        return error
    ts = str(args.get("ts") or "")
    if st["channels"][channel].pop(ts, None) is None:
        return _fail("message_not_found")
    return _ok(channel=channel, ts=ts)


def _get_permalink(request: Request, tenant: str, st: dict[str, Any], args: dict[str, Any]) -> JSONResponse:
    channel, error = _channel(st, args)
    if error is not None:
        return error
    ts = str(args.get("message_ts") or "")
    if ts not in st["channels"][channel]:
        return _fail("message_not_found")
    return _ok(channel=channel, permalink=_permalink(request, tenant, channel, ts))


def _history(request: Request, tenant: str, st: dict[str, Any], args: dict[str, Any]) -> JSONResponse:
    channel, error = _channel(st, args)
    if error is not None:
        return error
    inclusive = _flag(args.get("inclusive"))
    latest = float(args["latest"]) if args.get("latest") else None
    oldest = float(args["oldest"]) if args.get("oldest") else 0.0
    try:
        limit = max(1, min(999, int(args.get("limit") or 100)))
    except ValueError:
        return _fail("invalid_arguments")
    messages = sorted(st["channels"][channel].values(), key=lambda m: float(m["ts"]), reverse=True)

    def keep(message: dict[str, Any]) -> bool:
        ts = float(message["ts"])
        if latest is not None and (ts > latest or (ts == latest and not inclusive)):
            return False
        return not (ts < oldest or (ts == oldest and oldest and not inclusive))

    selected = [m for m in messages if keep(m)]
    page = [_public(m, with_metadata=_flag(args.get("include_all_metadata"))) for m in selected[:limit]]
    return _ok(messages=page, has_more=len(selected) > limit, pin_count=0,
               response_metadata={"next_cursor": ""})


def _reactions_get(request: Request, tenant: str, st: dict[str, Any], args: dict[str, Any]) -> JSONResponse:
    channel, error = _channel(st, args)
    if error is not None:
        return error
    message = st["channels"][channel].get(str(args.get("timestamp") or ""))
    if message is None:
        return _fail("message_not_found")
    return _ok(type="message", channel=channel, message=_public(message, with_metadata=False))


def _auth_test(request: Request, tenant: str, st: dict[str, Any], args: dict[str, Any]) -> JSONResponse:
    return _ok(url=f"https://{tenant}.slack.invalid/", team=tenant, user="ibx_bot", team_id=TEAM, user_id=BOT_USER,
               bot_id=BOT_ID)


METHODS = {
    "chat.postMessage": _post_message,
    "chat.delete": _delete,
    "chat.getPermalink": _get_permalink,
    "conversations.history": _history,
    "reactions.get": _reactions_get,
    "auth.test": _auth_test,
}


# ------------------------------------------------------------------ the message page (what a permalink opens)


@router.get("/{tenant}/archives/{channel}/p{digits}")
async def message_page(tenant: str, channel: str, digits: str) -> Any:
    ts = f"{digits[:-6]}.{digits[-6:]}" if len(digits) > 6 else digits
    message = _state(tenant)["channels"].get(channel, {}).get(ts)
    if message is None:
        return HTMLResponse("<h1>This message was deleted or never existed.</h1>", status_code=404)
    return HTMLResponse(f"<html><body><article data-ts='{ts}'>{html.escape(message['text'])}</article></body></html>")


# ------------------------------------------------------------------ mock-only controls


@router.post("/{tenant}/_mock/revoke")
async def revoke(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    store.state(PLATFORM, tenant)["revoked_tokens"].add(str(payload.get("token") or ""))
    return {"revoked": True}


@router.post("/{tenant}/_mock/react")
async def react(tenant: str, payload: dict[str, Any]) -> Any:
    message = _state(tenant)["channels"].get(str(payload.get("channel")), {}).get(str(payload.get("ts")))
    if message is None:
        return JSONResponse({"error": "message_not_found"}, status_code=404)
    name, count = str(payload.get("name") or "thumbsup"), int(payload.get("count") or 1)
    found = next((r for r in message["reactions"] if r["name"] == name), None)
    if found is None:
        message["reactions"].append({"name": name, "users": [f"U{i:08d}" for i in range(count)], "count": count})
    else:
        found["count"] += count
    return {"reactions": message["reactions"]}


@router.post("/{tenant}/_mock/reply")
async def reply(tenant: str, payload: dict[str, Any]) -> Any:
    message = _state(tenant)["channels"].get(str(payload.get("channel")), {}).get(str(payload.get("ts")))
    if message is None:
        return JSONResponse({"error": "message_not_found"}, status_code=404)
    message["reply_count"] = int(message.get("reply_count") or 0) + 1
    message["thread_ts"] = message["ts"]
    return {"reply_count": message["reply_count"]}


@router.post("/{tenant}/_mock/rate_limit")
async def rate_limit(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    _state(tenant)["rate_limit"] = {"count": int(payload.get("count", 1)), "retry_after": int(payload.get("retry_after", 30)),
                                    "mode": str(payload.get("mode") or "http_429")}
    return {"rate_limit": _state(tenant)["rate_limit"]}


@router.post("/{tenant}/_mock/archive")
async def archive(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    _state(tenant)["archived"].add(str(payload.get("channel")))
    return {"archived": sorted(_state(tenant)["archived"])}
