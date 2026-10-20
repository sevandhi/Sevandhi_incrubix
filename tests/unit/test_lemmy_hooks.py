"""Lemmy connector hooks against the REAL responses recorded from Lemmy 0.19.20 (respx, no network, no DB).

Every error class the connector maps is driven by a recorded fixture: rate limit (400 rate_limit_error,
reset from the site config), expired/revoked token (401 incorrect_login), rejected content (400
invalid_post_title / invalid_body_field), missing community, a renamed field, and the delete modes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from app.clock import FakeClock
from app.connectors.base import CALL_META, ConnectorContext
from app.connectors.lemmy.connector import (
    MARKER_OVERHEAD,
    Connector,
    body_with_marker,
    marker_line,
    parse_community,
)
from app.contract.models import Connection, Destination, Rendition
from app.contract.registry import discover
from app.errors import ErrorCode, IbxError
from app.logging import get_logger

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "recorded" / "lemmy"
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
API = "http://lemmy.test"
MARKER = "ibx-fixfb3f5429"  # the marker inside the recorded post bodies
DEST = Destination(connector_key="lemmy", surface="post", instance={"community_url": f"{API}/c/ibx_test", "community_id": 2})
CONN = Connection(connector_key="lemmy", auth_type="api_key", credentials={"jwt": SecretStr("test-jwt-123456")})


def recorded(name: str, **override: Any) -> httpx.Response:
    data = json.loads((FIXTURES / f"{name}.json").read_text())["response"]
    body = override.get("json", data.get("json"))
    return httpx.Response(data["status"], headers={"content-type": "application/json"}, content=json.dumps(body).encode())


def recorded_json(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]["json"]


@pytest.fixture
def lemmy(make_settings, tmp_path) -> Connector:
    registry = discover()
    ctx = ConnectorContext(
        manifest=registry.manifest("lemmy"),
        settings=make_settings(BOOTSTRAP_OUT_DIR=str(tmp_path), LEMMY_RL_FALLBACK_S=77),
        http=httpx.AsyncClient(timeout=2),
        clock=FakeClock(NOW),
        log=get_logger("test.lemmy"),
        db_session_factory=None,
    )
    return registry.classes["lemmy"](ctx)


@pytest.fixture
def meta() -> dict[str, Any]:
    values: dict[str, Any] = {}
    token = CALL_META.set(values)
    yield values
    CALL_META.reset(token)


def rendition(title: str = "IBX fixture post fb3f5429", body: str = "Hello") -> Rendition:
    return Rendition(title=title, body=body, content_type="text")


# ---------------------------------------------------------------- pure helpers and manifest


def test_parse_community_url_forms():
    assert parse_community("http://lemmy:8536/c/ibx_test") == parse_community("http://lemmy:8536/c/ibx_test/")
    ref = parse_community("http://mock:8080/lemmy/tenant-1/c/ibx_test")
    assert (ref.api, ref.name) == ("http://mock:8080/lemmy/tenant-1", "ibx_test")
    bang = parse_community("!technology@lemmy.world")
    assert (bang.api, bang.name) == ("https://lemmy.world", "technology")
    for bad in ("http://lemmy:8536/u/someone", "lemmy:8536/c/x", "", "!bad"):
        with pytest.raises(IbxError) as exc:
            parse_community(bad)
        assert exc.value.code == ErrorCode.REQUEST_VALIDATION_FAILED


def test_marker_is_an_invisible_markdown_comment_at_the_end():
    body = body_with_marker("Hello\n\n", "ibx-abc")
    assert body == "Hello\n\n[//]: # (ibx-abc)\n"
    assert body_with_marker("", "ibx-abc") == "[//]: # (ibx-abc)\n"
    assert marker_line(MARKER) in recorded_json("create_post_ok")["post_view"]["post"]["body"]  # same format as recorded


def test_manifest_body_limit_leaves_room_for_the_marker():
    """Measured: Lemmy accepts 50000 UTF-16 units of body; we add a 29-character marker line."""
    spec = discover().manifest("lemmy").surfaces[0].spec
    assert MARKER_OVERHEAD == 29
    assert spec["body_max_chars"] + MARKER_OVERHEAD == 50000
    assert spec["count_unit"] == "utf16"
    assert (spec["title_min_chars"], spec["title_max_chars"]) == (3, 200)


def test_spec_check_refuses_long_title_and_long_emoji_body_before_any_call(lemmy):
    violations = lemmy.spec_violations(rendition(title="T" * 201, body="😀" * 25000), DEST)
    assert {(v.field, v.rule) for v in violations} == {("title", "title_max_chars"), ("body", "body_max_chars")}


# ---------------------------------------------------------------- publish + lookup


@respx.mock
async def test_create_sends_marker_and_returns_the_recorded_post(lemmy):
    route = respx.post(f"{API}/api/v3/post").mock(return_value=recorded("create_post_ok"))
    result = await lemmy._create(lemmy.ctx, rendition(body="Fixture post recorded by the IncuBrix bootstrap."), DEST, CONN, MARKER)
    assert result.model_dump() == {"platform_post_id": "1", "live_url": "http://lemmy:8536/post/1", "status": "published"}
    sent = json.loads(route.calls[0].request.content)
    assert sent["community_id"] == 2 and sent["body"].endswith(f"{marker_line(MARKER)}\n")
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-jwt-123456"


@respx.mock
async def test_create_resolves_and_caches_the_community_id(lemmy):
    dest = Destination(connector_key="lemmy", instance={"community_url": f"{API}/c/ibx_test"})
    community = respx.get(f"{API}/api/v3/community").mock(
        return_value=httpx.Response(200, json={"community_view": {"community": {"id": 2, "name": "ibx_test"}}})
    )
    respx.post(f"{API}/api/v3/post").mock(return_value=recorded("create_post_ok"))
    await lemmy._create(lemmy.ctx, rendition(), dest, CONN, MARKER)
    await lemmy._create(lemmy.ctx, rendition(), dest, CONN, MARKER)
    assert community.call_count == 1 and community.calls[0].request.url.params["name"] == "ibx_test"


@respx.mock
async def test_lookup_finds_the_post_by_its_body_marker(lemmy):
    respx.get(f"{API}/api/v3/site").mock(return_value=httpx.Response(200, json={
        "version": "0.19.20", "site_view": {"local_site_rate_limit": {"post": 6, "post_per_second": 600}},
        "my_user": {"local_user_view": {"person": {"id": 2, "name": "ibx_admin"}}}}))
    user = respx.get(f"{API}/api/v3/user").mock(return_value=recorded("creator_posts_lookup"))
    found = await lemmy._lookup(lemmy.ctx, MARKER, DEST, CONN)
    assert found is not None and found.platform_post_id == "1"
    assert user.calls[0].request.url.params["username"] == "ibx_admin"
    assert user.calls[0].request.url.params["sort"] == "New"
    assert await lemmy._lookup(lemmy.ctx, "ibx-notthereatall", DEST, CONN) is None


@respx.mock
async def test_password_login_is_used_once_and_cached(lemmy):
    conn = Connection(connector_key="lemmy", credentials={"username": SecretStr("ibx_admin"), "password": SecretStr("pw-123456")})
    login = respx.post(f"{API}/api/v3/user/login").mock(
        return_value=httpx.Response(200, json={"jwt": "fresh-jwt-abcdef", "registration_created": False, "verify_email_sent": False})
    )
    create = respx.post(f"{API}/api/v3/post").mock(return_value=recorded("create_post_ok"))
    await lemmy._create(lemmy.ctx, rendition(), DEST, conn, MARKER)
    await lemmy._create(lemmy.ctx, rendition(), DEST, conn, MARKER)
    assert login.call_count == 1
    assert create.calls[1].request.headers["Authorization"] == "Bearer fresh-jwt-abcdef"


@respx.mock
async def test_wrong_password_is_auth_expired(lemmy):
    conn = Connection(connector_key="lemmy", credentials={"username": SecretStr("ibx_admin"), "password": SecretStr("wrong-pw")})
    respx.post(f"{API}/api/v3/user/login").mock(return_value=recorded("login_incorrect"))
    with pytest.raises(IbxError) as exc:
        await lemmy._create(lemmy.ctx, rendition(), DEST, conn, MARKER)
    assert exc.value.code == ErrorCode.CONNECTION_AUTH_EXPIRED


async def test_no_credentials_is_auth_expired_without_any_call(lemmy):
    with pytest.raises(IbxError) as exc:
        await lemmy._create(lemmy.ctx, rendition(), DEST, Connection(connector_key="lemmy"), MARKER)
    assert exc.value.code == ErrorCode.CONNECTION_AUTH_EXPIRED
    assert exc.value.details["reason"] == "missing_credentials"


# ---------------------------------------------------------------- error mapping (recorded bodies)


@pytest.mark.parametrize(
    "fixture, code, retryable",
    [
        ("create_post_invalid_token", ErrorCode.CONNECTION_AUTH_EXPIRED, False),
        ("create_post_revoked_token", ErrorCode.CONNECTION_AUTH_EXPIRED, False),
        ("create_post_title_too_long", ErrorCode.PLATFORM_CONTENT_REJECTED, False),
        ("create_post_community_not_found", ErrorCode.PLATFORM_CONTENT_REJECTED, False),
    ],
)
@respx.mock
async def test_recorded_errors_map_to_the_right_code(lemmy, fixture, code, retryable):
    respx.post(f"{API}/api/v3/post").mock(return_value=recorded(fixture))
    with pytest.raises(IbxError) as exc:
        await lemmy._create(lemmy.ctx, rendition(), DEST, CONN, MARKER)
    assert exc.value.code == code and exc.value.retryable is retryable
    assert exc.value.details["platform_status"] == recorded(fixture).status_code


@pytest.mark.parametrize(
    "token, code",
    [
        ("not_logged_in", ErrorCode.CONNECTION_AUTH_EXPIRED),
        ("invalid_body_field", ErrorCode.PLATFORM_CONTENT_REJECTED),
        ("slurs", ErrorCode.PLATFORM_CONTENT_REJECTED),
        ("couldnt_create_post", ErrorCode.PLATFORM_UNAVAILABLE),
        ("something_new_in_lemmy", ErrorCode.PLATFORM_CONTENT_REJECTED),
    ],
)
def test_other_lemmy_tokens(lemmy, token, code):
    err = lemmy.map_error(httpx.Response(400, json={"error": token}))
    assert err.code == code


def test_5xx_and_proxy_429_are_retryable(lemmy):
    assert lemmy.map_error(httpx.Response(502, text="bad gateway")).code == ErrorCode.PLATFORM_UNAVAILABLE
    err = lemmy.map_error(httpx.Response(429, headers={"Retry-After": "30"}))
    assert err.code == ErrorCode.PLATFORM_RATE_LIMIT and err.details["retry_after_s"] == 30


@respx.mock
async def test_rate_limit_reset_comes_from_the_site_config(lemmy):
    """Recorded: 400 rate_limit_error, no header; the site allowed 2 posts per 600 s -> one slot in 300 s."""
    respx.post(f"{API}/api/v3/post").mock(return_value=recorded("create_post_rate_limited"))
    site = respx.get(f"{API}/api/v3/site").mock(return_value=recorded("site_rate_limit_config"))
    with pytest.raises(IbxError) as exc:
        await lemmy._create(lemmy.ctx, rendition(), DEST, CONN, MARKER)
    err = exc.value
    assert err.code == ErrorCode.PLATFORM_RATE_LIMIT and err.retryable is True
    assert err.details["reset_source"] == "lemmy_site_config"
    assert err.details["retry_after_s"] == 300
    assert err.details["retry_at"] == (NOW + timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    assert site.call_count == 1


@respx.mock
async def test_rate_limit_falls_back_when_the_site_config_is_unreadable(lemmy):
    respx.post(f"{API}/api/v3/post").mock(return_value=recorded("create_post_rate_limited"))
    respx.get(f"{API}/api/v3/site").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(IbxError) as exc:
        await lemmy._create(lemmy.ctx, rendition(), DEST, CONN, MARKER)
    assert exc.value.details["reset_source"] == "lemmy_fallback" and exc.value.details["retry_after_s"] == 77


@respx.mock
async def test_renamed_field_fails_loudly(lemmy):
    body = recorded_json("create_post_ok")
    body["post_view"]["post"]["post_id"] = body["post_view"]["post"].pop("id")
    respx.post(f"{API}/api/v3/post").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(ValidationError) as exc:
        await lemmy._create(lemmy.ctx, rendition(), DEST, CONN, MARKER)
    err = lemmy._translate(exc.value, writing=True)
    assert err.code == ErrorCode.PLATFORM_UNAVAILABLE
    assert err.details["reason"] == "response_schema_mismatch" and "post_view.post.id" in err.details["fields"]
    assert err.details["outcome"] == "unknown"  # the post may exist: the retry reconciles by marker


# ---------------------------------------------------------------- status, delete, metrics


@respx.mock
async def test_status_published_soft_deleted_and_purged(lemmy):
    route = respx.get(f"{API}/api/v3/post")
    route.mock(return_value=recorded("get_post_ok"))
    assert (await lemmy._get_status(lemmy.ctx, "1", DEST, CONN)).model_dump() == {
        "status": "published", "live_url": "http://lemmy:8536/post/1"}
    route.mock(return_value=recorded("get_post_soft_deleted"))
    assert (await lemmy._get_status(lemmy.ctx, "1", DEST, CONN)).model_dump() == {"status": "deleted", "live_url": None}
    route.mock(return_value=recorded("get_post_not_found_after_purge"))
    assert (await lemmy._get_status(lemmy.ctx, "1", DEST, CONN)).model_dump() == {"status": "deleted", "live_url": None}


@respx.mock
async def test_status_of_a_removed_post(lemmy):
    body = recorded_json("get_post_ok")
    body["post_view"]["post"]["removed"] = True
    respx.get(f"{API}/api/v3/post").mock(return_value=httpx.Response(200, json=body))
    assert (await lemmy._get_status(lemmy.ctx, "1", DEST, CONN)).status == "removed_by_platform"


@respx.mock
async def test_delete_soft_then_admin_purge(lemmy, meta):
    soft = respx.post(f"{API}/api/v3/post/delete").mock(return_value=recorded("delete_post_soft"))
    purge = respx.post(f"{API}/api/v3/admin/purge/post").mock(return_value=recorded("purge_post"))
    assert (await lemmy._delete(lemmy.ctx, "1", DEST, CONN)).deleted is True
    assert json.loads(soft.calls[0].request.content) == {"post_id": 1, "deleted": True}
    assert purge.call_count == 1 and meta["delete_mode"] == "purged"


@respx.mock
async def test_delete_without_admin_rights_keeps_the_soft_delete(lemmy, meta):
    respx.post(f"{API}/api/v3/post/delete").mock(return_value=recorded("delete_post_soft"))
    respx.post(f"{API}/api/v3/admin/purge/post").mock(return_value=httpx.Response(400, json={"error": "not_an_admin"}))
    assert (await lemmy._delete(lemmy.ctx, "1", DEST, CONN)).deleted is True
    assert meta["delete_mode"] == "soft" and "warnings" not in meta


@respx.mock
async def test_delete_of_a_purged_post_is_idempotent(lemmy, meta):
    respx.post(f"{API}/api/v3/post/delete").mock(return_value=recorded("get_post_not_found_after_purge"))
    assert (await lemmy._delete(lemmy.ctx, "1", DEST, CONN)).deleted is True
    assert meta["delete_mode"] == "already_gone"


async def test_non_numeric_post_id_is_a_request_error(lemmy):
    with pytest.raises(IbxError) as exc:
        await lemmy._delete(lemmy.ctx, "abc", DEST, CONN)
    assert exc.value.code == ErrorCode.REQUEST_VALIDATION_FAILED


@respx.mock
async def test_metrics_upvotes_and_comments_others_null(lemmy):
    respx.get(f"{API}/api/v3/post").mock(return_value=recorded("get_post_ok"))
    metrics = await lemmy._fetch_metrics(lemmy.ctx, "1", DEST, CONN, None)
    assert metrics.model_dump() == {"views": None, "likes": 1, "comments": 0, "shares": None, "saves": None,
                                    "watch_time_s": None, "clicks": None}


# ---------------------------------------------------------------- local bootstrap credentials


@respx.mock
async def test_bootstrap_credentials_only_for_our_own_instance(lemmy, tmp_path):
    (tmp_path / "lemmy.json").write_text(json.dumps(
        {"base_url": API, "jwt": "bootstrap-jwt-xyz", "admin_username": "ibx_admin", "admin_password": "pw-local-1"}))
    route = respx.post(f"{API}/api/v3/post").mock(return_value=recorded("create_post_ok"))
    await lemmy._create(lemmy.ctx, rendition(), DEST, Connection(connector_key="lemmy"), MARKER)
    assert route.calls[0].request.headers["Authorization"] == "Bearer bootstrap-jwt-xyz"

    other = Destination(connector_key="lemmy", instance={"community_url": "https://lemmy.world/c/technology", "community_id": 1})
    with pytest.raises(IbxError) as exc:
        await lemmy._create(lemmy.ctx, rendition(), other, Connection(connector_key="lemmy"), MARKER)
    assert exc.value.details["reason"] == "missing_credentials"  # never the local key for another host


def test_simulation_keeps_the_community_name(make_settings):
    registry = discover()
    ctx = ConnectorContext(manifest=registry.manifest("lemmy"), settings=make_settings(MOCK_PLATFORM_URL="http://mock:8080"),
                           http=None, clock=FakeClock(NOW), log=get_logger("t"), db_session_factory=None, simulation=True)  # type: ignore[arg-type]
    connector = registry.classes["lemmy"](ctx)
    dest = Destination(connector_key="lemmy", instance={"community_url": "http://lemmy:8536/c/ibx_test"})
    sim_dest, sim_conn = connector.simulate(dest, CONN)
    assert sim_dest.instance["community_url"] == "http://mock:8080/lemmy/lemmy-8536-c-ibx-test/c/ibx_test"
    assert sim_conn.secret("jwt") == "sim-jwt"
    again, _ = connector.simulate(sim_dest, sim_conn)
    assert again.instance == sim_dest.instance
