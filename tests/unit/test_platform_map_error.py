"""BaseConnector.map_error against REAL recorded platform responses (tests/fixtures/recorded).

Status-only maps get Lemmy wrong (rate limit and bad input are both 400); "status:body_token" keys fix it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.clock import FakeClock
from app.connectors.base import BaseConnector, ConnectorContext
from app.contract.manifest import Manifest
from app.errors import ErrorCode
from app.logging import get_logger

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "recorded"
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

LEMMY_LIKE_MAP = {
    "400:rate_limit_error": "PLATFORM_RATE_LIMIT",
    "400:invalid_post_title": "PLATFORM_CONTENT_REJECTED",
    "400:couldnt_find_community": "PLATFORM_CONTENT_REJECTED",
    "400:not_logged_in": "CONNECTION_AUTH_EXPIRED",
    "401:incorrect_login": "CONNECTION_AUTH_EXPIRED",
    "401": "CONNECTION_AUTH_EXPIRED",
    "422": "PLATFORM_CONTENT_REJECTED",
    "429": "PLATFORM_RATE_LIMIT",
}


def _connector(make_settings, error_map: dict[str, str]) -> BaseConnector:
    manifest = Manifest.model_validate(
        {"key": "probe", "version": "1.0.0", "tier": "native", "auth": "api_key",
         "surfaces": [{"key": "post", "content_types": ["text"]}], "capabilities": [],
         "error_map": error_map, "instance_param": "base_url"}
    )
    ctx = ConnectorContext(manifest=manifest, settings=make_settings(RATE_LIMIT_DEFAULT_RETRY_S=60), http=None,  # type: ignore[arg-type]
                           clock=FakeClock(NOW), log=get_logger("test"), db_session_factory=None)

    class Probe(BaseConnector):
        key = "probe"

    Probe.manifest = manifest
    return Probe(ctx)


def _recorded(name: str) -> httpx.Response:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture {name} not recorded")
    data = json.loads(path.read_text())["response"]
    body = data.get("json")
    content = json.dumps(body).encode() if body is not None else (data.get("text") or "").encode()
    return httpx.Response(data["status"], headers=data.get("headers") or {}, content=content)


@pytest.mark.parametrize(
    "fixture, code",
    [
        ("lemmy/create_post_rate_limited.json", ErrorCode.PLATFORM_RATE_LIMIT),
        ("lemmy/create_post_invalid_token.json", ErrorCode.CONNECTION_AUTH_EXPIRED),
        ("lemmy/create_post_community_not_found.json", ErrorCode.PLATFORM_CONTENT_REJECTED),
        ("ghost/create_post_title_too_long.json", ErrorCode.PLATFORM_CONTENT_REJECTED),
        ("ghost/expired_token.json", ErrorCode.CONNECTION_AUTH_EXPIRED),
    ],
)
def test_recorded_errors_map_to_the_right_code(make_settings, fixture, code):
    err = _connector(make_settings, LEMMY_LIKE_MAP).map_error(_recorded(fixture))
    assert err.code == code, err.to_body()
    assert err.details["platform_status"] == _recorded(fixture).status_code


def test_lemmy_rate_limit_without_header_uses_default_reset(make_settings):
    err = _connector(make_settings, LEMMY_LIKE_MAP).map_error(_recorded("lemmy/create_post_rate_limited.json"))
    assert err.details["error_map_key"] == "400:rate_limit_error"
    assert err.details["retry_after_s"] == 60 and err.details["reset_source"] == "default"
    assert err.retryable is True


def test_exact_json_token_beats_substring(make_settings):
    connector = _connector(make_settings, {"400:login": "PLATFORM_CONTENT_REJECTED", "400:not_logged_in": "CONNECTION_AUTH_EXPIRED"})
    err = connector.map_error(httpx.Response(400, json={"error": "not_logged_in"}))
    assert err.code == ErrorCode.CONNECTION_AUTH_EXPIRED


def test_slack_style_200_error(make_settings):
    connector = _connector(make_settings, {"200:token_revoked": "CONNECTION_AUTH_EXPIRED"})
    assert connector.map_error(httpx.Response(200, json={"ok": False, "error": "token_revoked"})).code == ErrorCode.CONNECTION_AUTH_EXPIRED


def test_fallbacks(make_settings):
    connector = _connector(make_settings, {})
    assert connector.map_error(httpx.Response(503)).code == ErrorCode.PLATFORM_UNAVAILABLE
    limited = connector.map_error(httpx.Response(429, headers={"Retry-After": "9"}))
    assert limited.code == ErrorCode.PLATFORM_RATE_LIMIT and limited.details["retry_after_s"] == 9
    unknown = connector.map_error(httpx.Response(418, json={"message": "teapot"}))
    # an unmapped 4xx is the platform refusing this request, not an outage
    assert unknown.code == ErrorCode.PLATFORM_CONTENT_REJECTED and unknown.retryable is False
    assert unknown.details["unmapped"] is True
    assert unknown.details["platform_message"] == "teapot"
