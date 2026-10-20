"""Connector unit tests (no Docker, no network): every error mapping of discourse, devto and webhook via respx,
replaying the responses recorded from a real local Discourse where they exist, plus pure helpers
(webhook signatures, assisted deep links / rules checklist, Dev.to marker)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.clock import FakeClock
from app.connectors.base import ConnectorContext
from app.contract.models import Connection, Destination, Rendition
from app.contract.registry import discover
from app.errors import ErrorCode, IbxError
from app.logging import get_logger

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "discourse"
REGISTRY = discover()
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def recorded(name: str) -> httpx.Response:
    data = json.loads((FIXTURES / f"{name}.json").read_text())["response"]
    headers = {k: v for k, v in data["headers"].items() if k.lower() != "content-length"}
    if "json" in data:
        return httpx.Response(data["status"], headers=headers, content=json.dumps(data["json"]).encode())
    return httpx.Response(data["status"], headers=headers, content=(data.get("text") or "").encode())


def connector(key: str, make_settings, http: httpx.AsyncClient):
    ctx = ConnectorContext(
        manifest=REGISTRY.manifest(key), settings=make_settings(), http=http, clock=FakeClock(NOW),
        log=get_logger(f"test.{key}"), db_session_factory=None,
    )
    return REGISTRY.build(key, ctx)


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


async def raises(coro) -> IbxError:
    with pytest.raises(IbxError) as info:
        await coro
    return info.value


# ------------------------------------------------------------------ discourse

D_BASE = "http://forum.test"
D_DEST = Destination(connector_key="discourse", surface="topic", instance={"base_url": D_BASE, "category_id": 5})
D_CONN = Connection(connector_key="discourse", auth_type="api_key", credentials={"api_key": "k", "api_username": "ibx_admin"})
D_REND = Rendition(title="IBX fixture topic 90fff62e recorded locally", body="Fixture topic recorded by the IncuBrix bootstrap.")


@respx.mock
async def test_discourse_create_parses_recorded_topic_and_sends_marker(make_settings, http):
    route = respx.post(f"{D_BASE}/posts.json").mock(return_value=recorded("create_topic_ok"))
    c = connector("discourse", make_settings, http)
    result = await c._create(c.ctx, D_REND, D_DEST, D_CONN, "ibx-abc")
    assert result.model_dump() == {
        "platform_post_id": "8", "status": "published",
        "live_url": f"{D_BASE}/t/ibx-fixture-topic-90fff62e-recorded-locally/8",
    }
    sent = json.loads(route.calls[0].request.content)
    assert sent["external_id"] == "ibx-abc" and sent["category"] == 5
    assert route.calls[0].request.headers["Api-Key"] == "k"
    assert route.calls[0].request.headers["Api-Username"] == "ibx_admin"


@pytest.mark.parametrize(
    "fixture, code",
    [
        ("invalid_api_key", ErrorCode.CONNECTION_AUTH_EXPIRED),
        ("create_topic_duplicate_title", ErrorCode.PLATFORM_CONTENT_REJECTED),
        ("create_topic_title_too_long", ErrorCode.PLATFORM_CONTENT_REJECTED),
        ("create_topic_title_too_short", ErrorCode.PLATFORM_CONTENT_REJECTED),
    ],
)
@respx.mock
async def test_discourse_recorded_errors_map(make_settings, http, fixture, code):
    respx.post(f"{D_BASE}/posts.json").mock(return_value=recorded(fixture))
    c = connector("discourse", make_settings, http)
    err = await raises(c._create(c.ctx, D_REND, D_DEST, D_CONN, "ibx-abc"))
    assert err.code == code
    assert err.details["platform_status"] == recorded(fixture).status_code
    if code == ErrorCode.CONNECTION_AUTH_EXPIRED:
        assert err.details["error_map_key"] == "403:invalid_access"
    else:
        assert err.details["platform_message"]  # Discourse's own words reach the caller


@respx.mock
async def test_discourse_429_uses_retry_after_and_wait_seconds(make_settings, http):
    body = {"errors": ["Please wait 42 seconds"], "error_type": "rate_limit", "extras": {"wait_seconds": 42, "time_left": "42 seconds"}}
    respx.post(f"{D_BASE}/posts.json").mock(return_value=httpx.Response(429, json=body))
    c = connector("discourse", make_settings, http)
    err = await raises(c._create(c.ctx, D_REND, D_DEST, D_CONN, "ibx-abc"))
    assert err.code == ErrorCode.PLATFORM_RATE_LIMIT
    assert err.details["retry_after_s"] == 42 and err.details["reset_source"] == "body:extras.wait_seconds"
    respx.post(f"{D_BASE}/posts.json").mock(return_value=httpx.Response(429, json=body, headers={"Retry-After": "17"}))
    err = await raises(c._create(c.ctx, D_REND, D_DEST, D_CONN, "ibx-abc"))
    assert err.details["retry_after_s"] == 17 and err.details["reset_source"] == "Retry-After"


@respx.mock
async def test_discourse_500_is_unavailable_and_enqueued_is_pending(make_settings, http):
    c = connector("discourse", make_settings, http)
    respx.post(f"{D_BASE}/posts.json").mock(return_value=httpx.Response(502, text="Bad gateway"))
    assert (await raises(c._create(c.ctx, D_REND, D_DEST, D_CONN, "ibx-abc"))).code == ErrorCode.PLATFORM_UNAVAILABLE
    enqueued = {"success": True, "action": "enqueued", "pending_post": {"id": 3, "raw": "x"}, "pending_count": 1}
    respx.post(f"{D_BASE}/posts.json").mock(return_value=httpx.Response(200, json=enqueued))
    result = await c._create(c.ctx, D_REND, D_DEST, D_CONN, "ibx-abc")
    assert result.model_dump() == {"platform_post_id": "pending:ibx-abc", "live_url": None, "status": "pending"}


@respx.mock
async def test_discourse_lookup_follows_recorded_301(make_settings, http):
    respx.get(f"{D_BASE}/t/external_id/ibx-fix90fff62e.json").mock(return_value=recorded("get_topic_by_external_id"))
    respx.get(f"{D_BASE}/t/8.json").mock(return_value=recorded("get_topic_ok"))
    c = connector("discourse", make_settings, http)
    found = await c._lookup(c.ctx, "ibx-fix90fff62e", D_DEST, D_CONN)
    assert found is not None and found.platform_post_id == "8" and found.status == "published"
    respx.get(f"{D_BASE}/t/external_id/ibx-none.json").mock(return_value=recorded("get_topic_not_found"))
    assert await c._lookup(c.ctx, "ibx-none", D_DEST, D_CONN) is None


@respx.mock
async def test_discourse_status_metrics_and_soft_delete_from_recorded(make_settings, http):
    c = connector("discourse", make_settings, http)
    respx.get(f"{D_BASE}/t/8.json").mock(return_value=recorded("get_topic_ok"))
    status = await c._get_status(c.ctx, "8", D_DEST, D_CONN)
    assert status.status == "published" and status.live_url.endswith("/t/ibx-fixture-topic-90fff62e-recorded-locally/8")
    metrics = await c._fetch_metrics(c.ctx, "8", D_DEST, D_CONN, None)
    # recorded topic: 0 views, 0 likes, 1 post (no replies), no links -> clicks not exposed (None)
    assert metrics.model_dump() == {"views": 0, "likes": 0, "comments": 0, "shares": None, "saves": None,
                                    "watch_time_s": None, "clicks": None}
    respx.delete(f"{D_BASE}/t/8.json").mock(return_value=recorded("delete_topic"))
    assert (await c._delete(c.ctx, "8", D_DEST, D_CONN)).deleted is True
    respx.get(f"{D_BASE}/t/8.json").mock(return_value=recorded("get_topic_after_delete_admin"))
    assert (await c._get_status(c.ctx, "8", D_DEST, D_CONN)).status == "deleted"
    respx.get(f"{D_BASE}/t/8.json").mock(return_value=recorded("get_topic_after_delete_public"))
    assert (await c._get_status(c.ctx, "8", D_DEST, D_CONN)).status == "deleted"
    assert c.delete_mode == "soft"


@respx.mock
async def test_discourse_second_delete_422_counts_only_when_already_deleted(make_settings, http):
    # measured on the real 2026.10 instance: a second DELETE answers 422 with this body
    failed = {"errors": ["There was an error deleting that topic. Please contact the site's administrator."]}
    c = connector("discourse", make_settings, http)
    respx.delete(f"{D_BASE}/t/8.json").mock(return_value=httpx.Response(422, json=failed))
    respx.get(f"{D_BASE}/t/8.json").mock(return_value=recorded("get_topic_after_delete_admin"))
    assert (await c._delete(c.ctx, "8", D_DEST, D_CONN)).deleted is True
    respx.get(f"{D_BASE}/t/8.json").mock(return_value=recorded("get_topic_ok"))
    err = await raises(c._delete(c.ctx, "8", D_DEST, D_CONN))
    assert err.code == ErrorCode.PLATFORM_CONTENT_REJECTED  # a live topic that cannot be deleted is a real error
    respx.delete(f"{D_BASE}/t/8.json").mock(return_value=recorded("invalid_api_key"))
    assert (await raises(c._delete(c.ctx, "8", D_DEST, D_CONN))).code == ErrorCode.CONNECTION_AUTH_EXPIRED


@respx.mock
async def test_discourse_schema_drift_raises_validation(make_settings, http):
    from pydantic import ValidationError

    good = json.loads((FIXTURES / "create_topic_ok.json").read_text())["response"]["json"]
    good["tid"] = good.pop("topic_id")
    respx.post(f"{D_BASE}/posts.json").mock(return_value=httpx.Response(200, json=good))
    c = connector("discourse", make_settings, http)
    with pytest.raises(ValidationError):
        await c._create(c.ctx, D_REND, D_DEST, D_CONN, "ibx-abc")
    assert c._translate(ValidationError.from_exception_data("x", []), writing=True).details["reason"] == "response_schema_mismatch"


# ------------------------------------------------------------------ devto

F_BASE = "http://forem.test"
F_DEST = Destination(connector_key="devto", surface="article", instance={"base_url": F_BASE})
F_CONN = Connection(connector_key="devto", auth_type="api_key", credentials={"api_key": "k"})
F_REND = Rendition(title="Hello Forem", body="Body text", tags=["python", "ai"], canonical_url="https://example.com/a")


def article(id_: int = 7, published: bool = True, **extra: Any) -> dict[str, Any]:
    return {"type_of": "article", "id": id_, "url": f"{F_BASE}/u/hello-{id_}", "published": published, **extra}


@pytest.mark.parametrize(
    "status, body, headers, code",
    [
        (401, {"error": "unauthorized", "status": 401}, {}, ErrorCode.CONNECTION_AUTH_EXPIRED),
        (422, {"error": "Title can't be blank", "status": 422}, {}, ErrorCode.PLATFORM_CONTENT_REJECTED),
        (429, {"error": "Rate limit reached, try again in 30 seconds", "status": 429}, {"Retry-After": "30"}, ErrorCode.PLATFORM_RATE_LIMIT),
        (500, {"error": "Internal Server Error", "status": 500}, {}, ErrorCode.PLATFORM_UNAVAILABLE),
    ],
)
@respx.mock
async def test_devto_errors_map(make_settings, http, status, body, headers, code):
    respx.post(f"{F_BASE}/api/articles").mock(return_value=httpx.Response(status, json=body, headers=headers))
    c = connector("devto", make_settings, http)
    err = await raises(c._create(c.ctx, F_REND, F_DEST, F_CONN, "ibx-m"))
    assert err.code == code
    if status == 429:
        assert err.details["retry_after_s"] == 30


@respx.mock
async def test_devto_429_retry_after_http_date(make_settings, http):
    when = format_datetime(datetime(2026, 10, 15, 9, 2, tzinfo=UTC), usegmt=True)  # 120 s after NOW
    respx.post(f"{F_BASE}/api/articles").mock(return_value=httpx.Response(429, json={"error": "x", "status": 429}, headers={"Retry-After": when}))
    c = connector("devto", make_settings, http)
    err = await raises(c._create(c.ctx, F_REND, F_DEST, F_CONN, "ibx-m"))
    assert err.details["retry_after_s"] == 120 and err.details["retry_at"] == "2026-10-15T09:02:00Z"


@respx.mock
async def test_devto_create_sends_marker_tags_and_draft_only(make_settings, http):
    route = respx.post(f"{F_BASE}/api/articles").mock(return_value=httpx.Response(201, json=article()))
    c = connector("devto", make_settings, http)
    result = await c._create(c.ctx, F_REND, F_DEST, F_CONN, "ibx-m")
    assert result.status == "published" and result.live_url == f"{F_BASE}/u/hello-7"
    sent = json.loads(route.calls[0].request.content)["article"]
    assert sent["published"] is True and sent["tags"] == "python, ai" and sent["canonical_url"] == "https://example.com/a"
    assert sent["body_markdown"].rstrip().endswith("[//]: # (ibx-m)")
    assert route.calls[0].request.headers["api-key"] == "k"
    draft_dest = F_DEST.model_copy(update={"instance": {"base_url": F_BASE, "draft_only": True}})
    route.mock(return_value=httpx.Response(201, json=article(published=False)))
    result = await c._create(c.ctx, F_REND, draft_dest, F_CONN, "ibx-m")
    assert json.loads(route.calls[1].request.content)["article"]["published"] is False
    assert result.model_dump() == {"platform_post_id": "7", "live_url": None, "status": "draft"}


@respx.mock
async def test_devto_status_lookup_and_metrics(make_settings, http):
    c = connector("devto", make_settings, http)
    respx.get(f"{F_BASE}/api/articles/7").mock(return_value=httpx.Response(404, json={"error": "not found", "status": 404}))
    me = respx.get(f"{F_BASE}/api/articles/me/all").mock(return_value=httpx.Response(200, json=[
        article(7, False, body_markdown="x\n\n[//]: # (ibx-m)\n", page_views_count=5, public_reactions_count=2, comments_count=1),
    ]))
    assert (await c._get_status(c.ctx, "7", F_DEST, F_CONN)).model_dump() == {"status": "draft", "live_url": None}
    assert (await c._lookup(c.ctx, "ibx-m", F_DEST, F_CONN)).status == "draft"
    assert await c._lookup(c.ctx, "ibx-other", F_DEST, F_CONN) is None
    metrics = await c._fetch_metrics(c.ctx, "7", F_DEST, F_CONN, None)
    assert (metrics.views, metrics.likes, metrics.comments, metrics.shares, metrics.clicks) == (5, 2, 1, None, None)
    me.mock(return_value=httpx.Response(200, json=[]))
    assert (await c._get_status(c.ctx, "7", F_DEST, F_CONN)).status == "deleted"
    assert me.calls[0].request.url.params["per_page"] == "100"


def test_devto_manifest_has_no_delete():
    manifest = REGISTRY.manifest("devto")
    assert "delete" not in manifest.capabilities
    assert manifest.surfaces[0].spec["tag_max"] == 4


# ------------------------------------------------------------------ webhook

W_BASE = "http://partner.test/hooks"
W_DEST = Destination(connector_key="webhook", surface="post", instance={"endpoint_url": W_BASE})
W_CONN = Connection(connector_key="webhook", auth_type="api_key", credentials={"secret": "s3cret"})
W_REND = Rendition(title="Hi", body="Partner post")


def test_signature_roundtrip_tamper_and_window():
    from app.connectors.webhook import signing

    body = b'{"a":1}'
    header = signing.sign("s3cret", body, timestamp=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert signing.verify("s3cret", header, body, now=1_700_000_100) is None
    assert signing.verify("s3cret", header, body + b" ", now=1_700_000_100) == "invalid_signature"
    assert signing.verify("other", header, body, now=1_700_000_100) == "invalid_signature"
    assert signing.verify("s3cret", header, body, now=1_700_000_301) == "timestamp_outside_tolerance"
    assert signing.verify("s3cret", None, body) == "missing_signature"
    assert signing.verify("s3cret", "v1=abc", body) == "malformed_signature"


@respx.mock
async def test_webhook_request_is_signed_over_exact_body(make_settings, http):
    from app.connectors.webhook import signing

    route = respx.post(f"{W_BASE}/posts").mock(return_value=httpx.Response(201, json={"id": 1, "url": f"{W_BASE}/p/1", "status": "published"}))
    c = connector("webhook", make_settings, http)
    result = await c._create(c.ctx, W_REND, W_DEST, W_CONN, "ibx-w")
    assert result.model_dump() == {"platform_post_id": "1", "live_url": f"{W_BASE}/p/1", "status": "published"}
    request = route.calls[0].request
    assert request.headers["Idempotency-Key"] == "ibx-w"
    assert signing.verify("s3cret", request.headers[signing.HEADER], request.content) is None
    assert json.loads(request.content)["rendition"]["title"] == "Hi"


@pytest.mark.parametrize(
    "status, body, code",
    [
        (401, {"error": "invalid_signature"}, ErrorCode.CONNECTION_AUTH_EXPIRED),
        (401, {"error": "revoked"}, ErrorCode.CONNECTION_AUTH_EXPIRED),
        (403, {"error": "forbidden"}, ErrorCode.CONNECTION_AUTH_EXPIRED),
        (400, {"error": "timestamp_outside_tolerance"}, ErrorCode.PLATFORM_UNAVAILABLE),
        (400, {"error": "invalid_body"}, ErrorCode.PLATFORM_CONTENT_REJECTED),
        (409, {"error": "idempotency_conflict"}, ErrorCode.PLATFORM_CONTENT_REJECTED),
        (422, {"error": "validation_failed"}, ErrorCode.PLATFORM_CONTENT_REJECTED),
        (429, {"error": "rate_limited"}, ErrorCode.PLATFORM_RATE_LIMIT),
        (503, {"error": "down"}, ErrorCode.PLATFORM_UNAVAILABLE),
    ],
)
@respx.mock
async def test_webhook_errors_map(make_settings, http, status, body, code):
    respx.post(f"{W_BASE}/posts").mock(return_value=httpx.Response(status, json=body, headers={"X-RateLimit-Reset": str(int(NOW.timestamp()) + 90)}))
    c = connector("webhook", make_settings, http)
    err = await raises(c._create(c.ctx, W_REND, W_DEST, W_CONN, "ibx-w"))
    assert err.code == code
    if status == 429:
        assert err.details["retry_after_s"] == 90 and err.details["reset_source"] == "X-RateLimit-Reset"


@respx.mock
async def test_webhook_status_delete_404_and_missing_secret(make_settings, http):
    c = connector("webhook", make_settings, http)
    respx.get(f"{W_BASE}/posts/9").mock(return_value=httpx.Response(410))
    assert (await c._get_status(c.ctx, "9", W_DEST, W_CONN)).status == "deleted"
    respx.delete(f"{W_BASE}/posts/9").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    assert (await c._delete(c.ctx, "9", W_DEST, W_CONN)).deleted is True
    no_secret = Connection(connector_key="webhook", auth_type="api_key")
    assert (await raises(c._get_status(c.ctx, "9", W_DEST, no_secret))).code == ErrorCode.CONNECTION_AUTH_EXPIRED


# ------------------------------------------------------------------ assisted helpers


def test_deep_link_prefills_known_sites_only():
    from app.connectors.assisted.connector import deep_link

    r = Rendition(title="A question", body="Some text", link_url="https://example.com/x")
    link, truncated = deep_link("https://www.reddit.com/r/test/submit", r, "Some text")
    assert link.startswith("https://www.reddit.com/r/test/submit?") and "title=A+question" in link and not truncated
    assert "u=https" in deep_link("https://news.ycombinator.com/submitlink", r, "x")[0]
    assert deep_link("https://example.org/contact", r, "x") == ("https://example.org/contact", False)
    assert deep_link("https://www.reddit.com/r/test/submit", r, "x", prefill=False)[0] == "https://www.reddit.com/r/test/submit"
    custom, _ = deep_link("https://site.test/post", r, "x", prefill={"title": "subject"})
    assert custom == "https://site.test/post?subject=A+question"
    long_link, truncated = deep_link("https://forum.test/new-topic", r, "y" * 5000)
    assert truncated and len(long_link) < 2200


def test_rules_in_plain_words_covers_every_rule():
    from app.connectors.assisted.connector import rules_in_plain_words
    from app.rules.models import RuleSet

    rules = RuleSet.model_validate({
        "frequency_cap": "1/P7D", "self_promo_allowed": False, "link_policy": "no_shorteners",
        "disclosure_required": True, "language": "es", "language_policy": "must_match",
        "automation_allowed": False, "blocked_terms": ["crypto"], "rules_url": "https://forum.test/rules",
    })
    text = "\n".join(rules_in_plain_words(rules))
    for expected in ("at most 1 time every 1 week", "no self-promotion", "no link shorteners", "AI-assisted content must be disclosed",
                     "write the post in the community's language: es", "a person must post this by hand", "crypto", "https://forum.test/rules"):
        assert expected in text, expected
