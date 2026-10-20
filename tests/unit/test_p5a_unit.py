"""Metrics and recertification unit tests (no Docker): metrics normalisation + mapping, jitter, failure streak, alert sinks (respx)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.config import Settings
from app.contract.models import MetricsResult
from app.jobs.callbacks import verify
from app.metrics import normalize as nm
from app.providers.alerts.base import OPENED, RESOLVED, AlertEvent
from app.providers.alerts.github_issue import GitHubIssueSink
from app.providers.alerts.webhook import WebhookSink
from app.recert.health import failure_streak
from app.recert.scheduler import jitter_s

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


def _settings(**kw: object) -> Settings:
    base = {"APP_ENV": "test", "API_TOKEN": "t" * 24, "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/x"}
    return Settings(_env_file=None, **{**base, **kw})


def test_normalize_keeps_null_and_never_invents_zero():
    out = nm.normalize(MetricsResult(likes=3, comments=0))
    assert out == {"views": None, "likes": 3, "comments": 0, "shares": None, "clicks": None, "saves": None, "watch_time_s": None}
    assert nm.normalize({"views": True, "likes": 2.0, "comments": "7", "watch_time_s": 12}) == {
        "views": None, "likes": 2, "comments": 7, "shares": None, "clicks": None, "saves": None, "watch_time_s": 12.0}
    assert nm.engagement(out) == 3 and nm.engagement(nm.normalize({})) is None


def test_every_connector_maps_the_five_brief_keys_and_shares_stay_null():
    for key, mapping in nm.MAPPING.items():
        assert set(mapping) == set(nm.KEYS), key
        assert mapping["shares"].field is None  # no connector of ours exposes shares: null, never 0
    assert nm.supported("lemmy") == ["likes", "comments"]
    assert nm.supported("discourse") == ["views", "likes", "comments", "clicks"]
    assert nm.supported("ghost") == []


def test_docs_metrics_md_embeds_the_generated_mapping():
    assert nm.mapping_markdown() in (ROOT / "docs" / "METRICS.md").read_text()


CALLS = {
    "lemmy": ([{"path": "/api/v3/post", "json": {"post_view": {"counts": {"upvotes": 2, "downvotes": 1, "score": 1, "comments": 3}}}}],
              "7", MetricsResult(likes=2, comments=3)),
    "discourse": ([{"path": "/t/12.json", "json": {"views": 9, "like_count": 2, "posts_count": 4,
                                                    "post_stream": {"posts": [{"link_counts": [{"url": "u", "clicks": 3}, {"clicks": 1}]}]}}}],
                  "12", MetricsResult(views=9, likes=2, comments=3, clicks=4)),
    "peertube": ([{"path": "/api/v1/videos/abc", "json": {"views": 5, "likes": 1, "dislikes": 0, "comments": 2}},
                  {"path": "/api/v1/videos/abc/stats/overall", "json": {"totalWatchTime": 40}}],
                 "abc", MetricsResult(views=5, likes=1, comments=2, watch_time_s=40.0)),
    "slack": ([{"path": "/api/conversations.history", "json": {"ok": True, "messages": [
        {"ts": "1.2", "reactions": [{"name": "+1", "count": 2}, {"name": "tada", "count": 1}]}]}}],
              "C1:1.2", MetricsResult(likes=3, comments=0)),
    "devto": ([{"path": "/api/articles/me/all", "json": [{"id": 5, "page_views_count": 10, "public_reactions_count": 2,
                                                          "comments_count": 1}]}], "5", MetricsResult(views=10, likes=2, comments=1)),
}


@pytest.mark.parametrize("key", sorted(CALLS))
def test_raw_fields_rederive_the_connector_numbers(key):
    calls, post_id, result = CALLS[key]
    fields = nm.source_fields(key, calls, post_id)
    derived = nm.derive(key, fields)
    assert nm.consistent(nm.normalize(result), derived) is True
    tampered = nm.normalize(result) | {"likes": 99}
    assert nm.consistent(tampered, derived) is False


def test_discourse_without_links_has_null_clicks_and_mrss_uses_own_counter():
    fields = nm.source_fields("discourse", [{"path": "/t/1.json", "json": {"views": 1, "like_count": 0, "posts_count": 1,
                                                                            "post_stream": {"posts": [{}]}}}], "1")
    assert nm.derive("discourse", fields)["clicks"] is None
    mrss = nm.derive("mrss", nm.source_fields("mrss", [], "g", {"feeds/_clicks/<guid>.json:clicks": 4}))
    assert mrss["clicks"] == 4 and mrss["views"] is None
    assert nm.derive("unknown", {"x": 1}) is None and nm.consistent({}, None) is None


def test_jitter_is_stable_and_bounded():
    values = {jitter_s(f"dest_{i}", NOW, 600) for i in range(200)}
    assert all(0 <= v <= 600 for v in values) and len(values) > 50
    assert jitter_s("dest_1", NOW, 600) == jitter_s("dest_1", NOW, 600)
    assert jitter_s("dest_1", NOW, 0) == 0


def test_failure_streak_skips_neutral_runs_and_stops_at_a_pass():
    assert failure_streak(["DEMOTED", "DEMOTED", "PASSED", "DEMOTED"]) == 2
    assert failure_streak(["PASSED", "DEMOTED"]) == 0 and failure_streak([]) == 0


def test_recert_after_days_must_stay_below_thirty():
    assert _settings(RECERT_AFTER_DAYS=21).RECERT_AFTER_DAYS == 21
    with pytest.raises(ValidationError):
        _settings(RECERT_AFTER_DAYS=30)


def _event(name: str, delivered: list[str]) -> AlertEvent:
    return AlertEvent(name, "alert_1", "certification_failed", {"kind": "certification_failed", "summary": "s",
                                                                 "destination": {"name": "Forum"}}, delivered)


@respx.mock
async def test_webhook_sink_signs_the_raw_body(monkeypatch):
    import app.providers.alerts.webhook as wh

    async def _ok(url: str, s: Settings) -> None:
        return None

    monkeypatch.setattr(wh, "check_url", _ok)  # no DNS in unit tests
    route = respx.post("https://hooks.example.com/ibx").mock(return_value=httpx.Response(204))
    settings = _settings(ALERT_WEBHOOK_URL="https://hooks.example.com/ibx", ALERT_WEBHOOK_SECRET="whsec-test-123")
    sink = WebhookSink()
    assert sink.enabled(settings) and not sink.enabled(_settings())
    async with httpx.AsyncClient() as http:
        result = await sink.send(_event(OPENED, ["log"]), settings, http, NOW)
    assert result.ok and result.marker == "webhook"
    request = route.calls.last.request
    assert request.headers["X-IncuBrix-Event"] == "alert.opened"
    assert request.headers["X-IncuBrix-Delivery"] == "alert_1:alert.opened"
    assert verify(b"whsec-test-123", request.content, request.headers["X-IncuBrix-Signature"])
    assert not verify(b"wrong-secret", request.content, request.headers["X-IncuBrix-Signature"])
    assert json.loads(request.content)["kind"] == "certification_failed"
    assert sink.done(_event(OPENED, ["webhook"])) and not sink.done(_event(RESOLVED, ["webhook"]))


@respx.mock
async def test_github_sink_opens_then_closes_one_issue(monkeypatch):
    import app.providers.alerts.github_issue as gh

    async def _ok(url: str, s: Settings) -> None:
        return None

    monkeypatch.setattr(gh, "check_url", _ok)
    created = respx.post("https://api.github.com/repos/me/ibx/issues").mock(return_value=httpx.Response(201, json={"number": 7}))
    comment = respx.post("https://api.github.com/repos/me/ibx/issues/7/comments").mock(return_value=httpx.Response(201, json={}))
    closed = respx.patch("https://api.github.com/repos/me/ibx/issues/7").mock(return_value=httpx.Response(200, json={}))
    sink = GitHubIssueSink()
    assert not sink.enabled(_settings(GITHUB_REPO="me/ibx"))  # no token: off
    settings = _settings(GITHUB_TOKEN="ghp_unit_test_token_000", GITHUB_REPO="me/ibx")
    async with httpx.AsyncClient() as http:
        opened = await sink.send(_event(OPENED, []), settings, http, NOW)
        assert opened.ok and opened.marker == "github_issue#7"
        assert sink.done(_event(OPENED, ["github_issue#7"])) and not sink.done(_event(RESOLVED, ["github_issue#7"]))
        resolved = await sink.send(_event(RESOLVED, ["github_issue#7"]), settings, http, NOW)
    assert resolved.ok and resolved.marker == "github_issue#7:closed"
    assert created.calls.last.request.headers["Authorization"] == "Bearer ghp_unit_test_token_000"
    assert json.loads(closed.calls.last.request.content)["state"] == "closed" and comment.called
