"""The network side of rules capture, no Docker: every capture request sends the service's declared
User-Agent; robots.txt is checked on EVERY hop (platform-API fetches and redirect hops included); public hosts get at
most one request per second; capture problems come back as structured warnings (robots, HTTP 403, JS-only page)."""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from app.catalogue import rules_capture as rc
from app.config import Settings

LOCAL = "http://127.0.0.1:9"
OTHER = "http://127.0.0.1:10"


def _settings() -> Settings:
    return Settings(_env_file=None, APP_ENV="test", API_TOKEN="t-123456789")


class LemmyDest:
    connector_key, url, rules_url, language = "lemmy", f"{LOCAL}/c/ibx_test", f"{LOCAL}/rules", None
    instance = {"community_url": f"{LOCAL}/c/ibx_test"}


class PageDest:
    connector_key, instance, url, rules_url, language = "webhook", {}, f"{LOCAL}/", f"{LOCAL}/rules", None


SIDEBAR = {"community_view": {"community": {"description": "**Rules**\n\n- No bots or automated posting."}}}
PAGE = "<html><body><p>Self-promotion is not allowed in this community at all.</p></body></html>"


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "MIN_INTERVAL_S", 0.0)


def _codes(gathered: rc.Gathered) -> list[str]:
    return [w["code"] for w in gathered.issues]


@respx.mock
async def test_every_capture_request_sends_the_declared_user_agent() -> None:
    respx.get(f"{LOCAL}/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    respx.get(f"{LOCAL}/api/v3/community", params={"name": "ibx_test"}).mock(return_value=httpx.Response(200, json=SIDEBAR))
    respx.get(f"{LOCAL}/rules").mock(return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    gathered = await rc.gather_sources(LemmyDest(), _settings(), None)  # type: ignore[arg-type]
    assert [d.kind for d in gathered.docs] == ["lemmy_sidebar", "rules_url"]
    agents = {call.request.headers.get("user-agent") for call in respx.calls}
    assert agents == {rc.USER_AGENT}, agents  # never python-httpx/<v>


@respx.mock
async def test_platform_api_fetch_is_checked_against_robots_txt() -> None:
    respx.get(f"{LOCAL}/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /api/\n"))
    api = respx.get(f"{LOCAL}/api/v3/community", params={"name": "ibx_test"}).mock(return_value=httpx.Response(200, json=SIDEBAR))
    respx.get(f"{LOCAL}/rules").mock(return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    gathered = await rc.gather_sources(LemmyDest(), _settings(), None)  # type: ignore[arg-type]
    assert api.call_count == 0
    assert [d.kind for d in gathered.docs] == ["rules_url"]
    assert "ROBOTS_DISALLOWED" in _codes(gathered)
    assert any("platform API" in w["message"] for w in gathered.issues)


@respx.mock
async def test_robots_agent_group_for_our_user_agent_is_honoured_on_the_platform_api() -> None:
    respx.get(f"{LOCAL}/robots.txt").mock(return_value=httpx.Response(
        200, text="User-agent: IncuBrixRulesReader\nDisallow: /\n\nUser-agent: *\nAllow: /\n"))
    api = respx.get(f"{LOCAL}/api/v3/community", params={"name": "ibx_test"}).mock(return_value=httpx.Response(200, json=SIDEBAR))
    page = respx.get(f"{LOCAL}/rules").mock(return_value=httpx.Response(200, text=PAGE))
    gathered = await rc.gather_sources(LemmyDest(), _settings(), None)  # type: ignore[arg-type]
    assert api.call_count == 0 and page.call_count == 0 and gathered.docs == []


@respx.mock
async def test_every_redirect_hop_is_checked_against_that_hosts_robots_txt() -> None:
    respx.get(f"{LOCAL}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{LOCAL}/rules").mock(return_value=httpx.Response(302, headers={"location": f"{OTHER}/private/rules"}))
    other_robots = respx.get(f"{OTHER}/robots.txt").mock(return_value=httpx.Response(
        200, text="User-agent: *\nDisallow: /private/\n"))
    private = respx.get(f"{OTHER}/private/rules").mock(return_value=httpx.Response(200, text=PAGE))
    gathered = await rc.gather_sources(PageDest(), _settings(), None)  # type: ignore[arg-type]
    assert other_robots.call_count == 1 and private.call_count == 0
    assert gathered.docs == [] and "ROBOTS_DISALLOWED" in _codes(gathered)
    assert gathered.warnings[0].startswith("ROBOTS_DISALLOWED")


@respx.mock
async def test_public_hosts_get_at_most_one_request_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "MIN_INTERVAL_S", 0.3)
    public = "http://93.184.215.14"  # a public address literal: no DNS, never a local host

    class Dest:
        connector_key, instance, url, rules_url, language = "webhook", {}, f"{public}/", f"{public}/rules", None

    respx.get(f"{public}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{public}/rules").mock(return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
    started = time.monotonic()
    gathered = await rc.gather_sources(Dest(), _settings(), None)  # type: ignore[arg-type]
    assert time.monotonic() - started >= 0.3
    assert [d.kind for d in gathered.docs] == ["rules_url"]


@respx.mock
async def test_http_403_and_js_only_pages_are_structured_warnings() -> None:
    respx.get(f"{LOCAL}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{LOCAL}/rules").mock(return_value=httpx.Response(403, text="forbidden"))
    gathered = await rc.gather_sources(PageDest(), _settings(), None)  # type: ignore[arg-type]
    issue = next(w for w in gathered.issues if w["code"] == "RULES_URL_FAILED")
    assert issue["http_status"] == 403 and "403" in issue["message"]

    respx.get(f"{LOCAL}/js").mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=(
        "<html><head><script src='/app.js'></script></head><body><div id='root'></div>"
        "<noscript>Please enable JavaScript.</noscript></body></html>")))
    gathered = await rc.gather_sources(PageDest(), _settings(), None, rules_url=f"{LOCAL}/js")  # type: ignore[arg-type]
    assert "JS_ONLY_PAGE" in _codes(gathered)


def test_field_warnings_name_quote_failures_and_defaults() -> None:
    fields = [{"field": "self_promo", "status": "quote_not_found", "proposed": "allowed", "applied": "disallowed"},
              {"field": "frequency_cap", "status": "verified", "applied": "1/P7D"},
              {"field": "automation_allowed", "status": "not_stated", "applied": False}]
    codes = {(w["code"], w["field"]) for w in rc.field_warnings(fields)}
    assert codes == {("QUOTE_NOT_FOUND", "self_promo"), ("FIELD_DEFAULTED", "automation_allowed")}
