"""Ghost connector hooks against the REAL responses recorded from Ghost 6.70 (respx, no network, no DB).

Covers the Admin JWT (HS256, kid, aud /admin/, 5-minute exp), the slug marker and lookup, and every error
class: expired / forged / unknown key (401), over-long title (422), a renamed field, delete 204 / 404.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from pydantic import SecretStr, ValidationError

from app.clock import FakeClock
from app.connectors.base import CALL_META, ConnectorContext
from app.connectors.ghost import connector as ghost_module
from app.connectors.ghost.connector import admin_token, ghost_slug, lexical_markdown, split_admin_key
from app.contract.models import Connection, Destination, Rendition
from app.contract.registry import discover
from app.errors import ErrorCode, IbxError
from app.logging import get_logger

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "recorded" / "ghost"
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
SITE = "http://ghost.test"
ADMIN = f"{SITE}/ghost/api/admin"
KEY_ID, SECRET_HEX = "6acfcd9b8944510001d12d99", "0f" * 32
DEST = Destination(connector_key="ghost", surface="article", instance={"base_url": SITE})
CONN = Connection(connector_key="ghost", auth_type="api_key", credentials={"admin_api_key": SecretStr(f"{KEY_ID}:{SECRET_HEX}")})
POST_ID = "6acfce0b8944510001d130c4"
MARKER = "ibx-abcdefgh2345"


def recorded(name: str, *, json_body: Any = None) -> httpx.Response:
    data = json.loads((FIXTURES / f"{name}.json").read_text())["response"]
    if "json" not in data and json_body is None:
        return httpx.Response(data["status"], content=b"")
    body = json_body if json_body is not None else data["json"]
    return httpx.Response(data["status"], headers={"content-type": "application/json"}, content=json.dumps(body).encode())


def recorded_json(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]["json"]


@pytest.fixture
def ghost(make_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(ghost_module, "wall_clock", lambda: NOW.timestamp())
    registry = discover()
    ctx = ConnectorContext(manifest=registry.manifest("ghost"), settings=make_settings(BOOTSTRAP_OUT_DIR=str(tmp_path)),
                           http=httpx.AsyncClient(timeout=2), clock=FakeClock(NOW), log=get_logger("test.ghost"),
                           db_session_factory=None)
    return registry.classes["ghost"](ctx)


@pytest.fixture
def meta() -> dict[str, Any]:
    values: dict[str, Any] = {}
    token = CALL_META.set(values)
    yield values
    CALL_META.reset(token)


def rendition(**values: Any) -> Rendition:
    return Rendition(**{"title": "IBX fixture post 607b7337", "body": "Hello **world**", "content_type": "text", **values})


# ---------------------------------------------------------------- pure helpers


def test_admin_token_is_what_ghost_expects():
    token, expires = admin_token(KEY_ID, bytes.fromhex(SECRET_HEX), NOW.timestamp())
    assert jwt.get_unverified_header(token) == {"alg": "HS256", "typ": "JWT", "kid": KEY_ID}
    claims = jwt.decode(token, bytes.fromhex(SECRET_HEX), algorithms=["HS256"], audience="/admin/",
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["exp"] - claims["iat"] == 300 and claims["iat"] == int(NOW.timestamp())
    assert expires == datetime.fromtimestamp(claims["exp"], UTC)


@pytest.mark.parametrize("raw", [None, "", "no-colon", "id:not-hex", ":abcd", "id:"])
def test_malformed_admin_key_is_auth_expired(raw):
    with pytest.raises(IbxError) as exc:
        split_admin_key(raw)
    assert exc.value.code == ErrorCode.CONNECTION_AUTH_EXPIRED and exc.value.details["action"] == "reconnect_required"


def test_slug_carries_the_marker_and_stays_ascii():
    assert ghost_slug("Hello, World!", "ibx-abc") == "hello-world-ibx-abc"
    assert ghost_slug("Ünïcödé Tïtle", "ibx-abc") == "unicode-title-ibx-abc"
    assert ghost_slug("தமிழ் 😀", "ibx-abc") == "ibx-abc"  # nothing ASCII left: the marker alone
    long = ghost_slug("word " * 100, "ibx-abcdefgh2345")
    assert long.endswith("-ibx-abcdefgh2345") and len(long) <= 191 and "--" not in long


def test_lexical_document_is_one_markdown_card():
    doc = json.loads(lexical_markdown("# Hi"))
    assert doc["root"]["children"] == [{"type": "markdown", "version": 1, "markdown": "# Hi"}]


def test_manifest_title_limit_is_the_measured_255(ghost):
    spec = ghost.manifest.surfaces[0].spec
    assert spec["title_max_chars"] == 255
    violations = ghost.spec_violations(rendition(title="T" * 256), DEST)
    assert [(v.field, v.rule, v.limit, v.actual) for v in violations] == [("title", "title_max_chars", 255, 256)]


# ---------------------------------------------------------------- publish + lookup


@respx.mock
async def test_create_publishes_with_slug_marker_and_signed_token(ghost):
    route = respx.post(f"{ADMIN}/posts/").mock(return_value=recorded("create_post_ok"))
    result = await ghost._create(ghost.ctx, rendition(canonical_url="https://example.org/a", tags=["ai"]), DEST, CONN, MARKER)
    assert result.model_dump() == {"platform_post_id": POST_ID, "live_url": "http://ghost:2368/ibx-fixture-607b7337/",
                                   "status": "published"}
    request = route.calls[0].request
    sent = json.loads(request.content)["posts"][0]
    assert sent["slug"] == f"ibx-fixture-post-607b7337-{MARKER}" and sent["status"] == "published"
    assert "newsletter" not in sent and "newsletter" not in str(request.url)  # never emails members
    assert json.loads(sent["lexical"])["root"]["children"][0]["markdown"] == "Hello **world**"
    assert sent["canonical_url"] == "https://example.org/a" and sent["tags"] == [{"name": "ai"}]
    scheme, token = request.headers["Authorization"].split(" ")
    assert scheme == "Ghost"
    jwt.decode(token, bytes.fromhex(SECRET_HEX), algorithms=["HS256"], audience="/admin/", options={"verify_exp": False, "verify_iat": False})


@respx.mock
async def test_html_body_uses_source_html(ghost):
    route = respx.post(f"{ADMIN}/posts/").mock(return_value=recorded("create_post_ok"))
    await ghost._create(ghost.ctx, rendition(body="<p>x</p>", body_format="html"), DEST, CONN, MARKER)
    assert route.calls[0].request.url.params["source"] == "html"
    assert json.loads(route.calls[0].request.content)["posts"][0]["html"] == "<p>x</p>"


@respx.mock
async def test_lookup_filters_on_the_slug_suffix(ghost):
    route = respx.get(f"{ADMIN}/posts/").mock(return_value=recorded("get_post_by_slug_ok"))
    found = await ghost._lookup(ghost.ctx, MARKER, DEST, CONN)
    assert found is not None and found.platform_post_id == POST_ID
    assert route.calls[0].request.url.params["filter"] == f"slug:~$'{MARKER}'"
    route.mock(return_value=httpx.Response(200, json={"posts": [], "meta": {}}))
    assert await ghost._lookup(ghost.ctx, MARKER, DEST, CONN) is None


# ---------------------------------------------------------------- errors (recorded bodies)


@pytest.mark.parametrize(
    "fixture, code, platform_code",
    [
        ("expired_token", ErrorCode.CONNECTION_AUTH_EXPIRED, "INVALID_JWT"),
        ("invalid_token_bad_signature", ErrorCode.CONNECTION_AUTH_EXPIRED, "INVALID_JWT"),
        ("unknown_key_id", ErrorCode.CONNECTION_AUTH_EXPIRED, "UNKNOWN_ADMIN_API_KEY"),
        ("create_post_title_too_long", ErrorCode.PLATFORM_CONTENT_REJECTED, None),
    ],
)
@respx.mock
async def test_recorded_errors_map_to_the_right_code(ghost, fixture, code, platform_code):
    respx.post(f"{ADMIN}/posts/").mock(return_value=recorded(fixture))
    with pytest.raises(IbxError) as exc:
        await ghost._create(ghost.ctx, rendition(), DEST, CONN, MARKER)
    assert exc.value.code == code and exc.value.retryable is False
    assert exc.value.details.get("platform_code") == platform_code
    if code == ErrorCode.CONNECTION_AUTH_EXPIRED:
        assert "clock" in exc.value.message  # the clock-skew hint (an iat/exp off by minutes looks "expired")


def test_ghost_429_uses_retry_after(ghost):
    err = ghost.map_error(recorded("session_login_rate_limited"))
    assert err.code == ErrorCode.PLATFORM_RATE_LIMIT and err.retryable is True
    with_header = httpx.Response(429, headers={"Retry-After": "120"}, json=recorded_json("session_login_rate_limited"))
    assert ghost.map_error(with_header).details["retry_after_s"] == 120


@respx.mock
async def test_renamed_url_fails_loudly(ghost):
    body = recorded_json("create_post_ok")
    body["posts"][0]["permalink"] = body["posts"][0].pop("url")
    respx.post(f"{ADMIN}/posts/").mock(return_value=httpx.Response(201, json=body))
    with pytest.raises(ValidationError) as exc:
        await ghost._create(ghost.ctx, rendition(), DEST, CONN, MARKER)
    err = ghost._translate(exc.value, writing=True)
    assert err.details["reason"] == "response_schema_mismatch" and "posts.0.url" in err.details["fields"]


# ---------------------------------------------------------------- status, delete, refresh_auth


@respx.mock
async def test_status_published_then_deleted(ghost):
    route = respx.get(f"{ADMIN}/posts/{POST_ID}/")
    route.mock(return_value=recorded("get_post_ok"))
    assert (await ghost._get_status(ghost.ctx, POST_ID, DEST, CONN)).model_dump() == {
        "status": "published", "live_url": "http://ghost:2368/ibx-fixture-607b7337/"}
    route.mock(return_value=recorded("get_post_not_found_after_delete"))
    assert (await ghost._get_status(ghost.ctx, POST_ID, DEST, CONN)).model_dump() == {"status": "deleted", "live_url": None}


@respx.mock
async def test_delete_204_and_404_are_both_deleted(ghost, meta):
    route = respx.delete(f"{ADMIN}/posts/{POST_ID}/")
    route.mock(return_value=recorded("delete_post"))
    assert (await ghost._delete(ghost.ctx, POST_ID, DEST, CONN)).deleted is True
    assert meta["delete_mode"] == "permanent"
    route.mock(return_value=recorded("delete_post_not_found"))
    assert (await ghost._delete(ghost.ctx, POST_ID, DEST, CONN)).deleted is True
    assert meta["delete_mode"] == "already_gone"


async def test_bad_post_id_is_a_request_error(ghost):
    with pytest.raises(IbxError) as exc:
        await ghost._get_status(ghost.ctx, "../site", DEST, CONN)
    assert exc.value.code == ErrorCode.REQUEST_VALIDATION_FAILED


@respx.mock
async def test_refresh_auth_mints_and_proves_a_new_token(ghost):
    route = respx.get(f"{ADMIN}/posts/").mock(return_value=httpx.Response(200, json={"posts": [{"id": POST_ID}]}))
    result = await ghost._refresh_auth(ghost.ctx, CONN, DEST)
    assert result.expires_at == datetime.fromtimestamp(int(NOW.timestamp()) + 300, UTC)
    assert route.calls[0].request.headers["Authorization"].startswith("Ghost ey")


@respx.mock
async def test_refresh_auth_with_a_revoked_key_is_auth_expired(ghost):
    respx.get(f"{ADMIN}/posts/").mock(return_value=recorded("unknown_key_id"))
    with pytest.raises(IbxError) as exc:
        await ghost._refresh_auth(ghost.ctx, CONN, DEST)
    assert exc.value.code == ErrorCode.CONNECTION_AUTH_EXPIRED


@respx.mock
async def test_bootstrap_key_only_for_our_own_ghost(ghost, tmp_path):
    (tmp_path / "ghost.json").write_text(json.dumps({"base_url": SITE, "admin_api_key": f"{KEY_ID}:{SECRET_HEX}"}))
    route = respx.post(f"{ADMIN}/posts/").mock(return_value=recorded("create_post_ok"))
    await ghost._create(ghost.ctx, rendition(), DEST, Connection(connector_key="ghost"), MARKER)
    assert route.call_count == 1
    other = Destination(connector_key="ghost", instance={"base_url": "https://blog.example.org"})
    with pytest.raises(IbxError) as exc:
        await ghost._create(ghost.ctx, rendition(), other, Connection(connector_key="ghost"), MARKER)
    assert exc.value.details["reason"] == "missing_credentials"
