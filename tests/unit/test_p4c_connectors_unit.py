"""Unit tests for the Slack, PeerTube and MRSS connectors' pure parts: Slack's body-based error map (HTTP 200 ok:false), message text
and ids; PeerTube's status mapping and description marker; the MRSS feed renderer (validated by feedparser)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.connectors.mrss.connector import feed_key_of
from app.connectors.mrss.feed import parse_feed, render_feed
from app.connectors.peertube.connector import Video, description_for, marker_line, video_status
from app.connectors.slack.connector import Message, has_marker, message_text, split_post_id
from app.contract.models import Rendition
from app.contract.registry import discover
from app.errors import ErrorCode, IbxError

REGISTRY = discover()


def _connector(key: str, make_settings):
    ctx = SimpleNamespace(settings=make_settings(), log=SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
                          clock=SimpleNamespace(now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC)))
    return REGISTRY.classes[key](ctx)


# ------------------------------------------------------------------ Slack


@pytest.mark.parametrize(("status", "body", "headers", "code"), [
    (200, {"ok": False, "error": "token_revoked"}, {}, ErrorCode.CONNECTION_AUTH_EXPIRED),
    (200, {"ok": False, "error": "token_expired"}, {}, ErrorCode.CONNECTION_AUTH_EXPIRED),
    (200, {"ok": False, "error": "invalid_auth"}, {}, ErrorCode.CONNECTION_AUTH_EXPIRED),
    (200, {"ok": False, "error": "account_inactive"}, {}, ErrorCode.CONNECTION_AUTH_EXPIRED),
    (200, {"ok": False, "error": "missing_scope", "needed": "chat:write"}, {}, ErrorCode.CONNECTION_AUTH_EXPIRED),
    (429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "12"}, ErrorCode.PLATFORM_RATE_LIMIT),
    (200, {"ok": False, "error": "rate_limited"}, {}, ErrorCode.PLATFORM_RATE_LIMIT),
    (200, {"ok": False, "error": "msg_too_long"}, {}, ErrorCode.PLATFORM_CONTENT_REJECTED),
    (200, {"ok": False, "error": "msg_blocks_too_long"}, {}, ErrorCode.PLATFORM_CONTENT_REJECTED),
    (200, {"ok": False, "error": "channel_not_found"}, {}, ErrorCode.PLATFORM_CONTENT_REJECTED),
    (200, {"ok": False, "error": "internal_error"}, {}, ErrorCode.PLATFORM_UNAVAILABLE),
    (200, {"ok": False, "error": "something_new"}, {}, ErrorCode.PLATFORM_CONTENT_REJECTED),  # unmapped: flagged
])
def test_slack_error_map_reads_the_body_not_the_status(make_settings, status, body, headers, code):
    slack = _connector("slack", make_settings)
    err = slack.map_error(httpx.Response(status, json=body, headers=headers))
    assert err.code == code
    if body["error"] == "ratelimited":
        assert err.details["retry_after_s"] == 12 and err.details["reset_source"]
    if body["error"] == "something_new":
        assert err.details["unmapped"] is True


def test_slack_internal_error_on_create_is_unknown_outcome(make_settings):
    slack = _connector("slack", make_settings)
    response = httpx.Response(200, json={"ok": False, "error": "internal_error"})
    assert slack._error(response, writing=True).details["outcome"] == "unknown"
    assert "outcome" not in slack._error(response, writing=False).details


def test_slack_message_text_and_ids():
    rendition = Rendition(title="Hi", body="See https://x.example/a", link_url="https://x.example/a")
    assert message_text(rendition) == "*Hi*\nSee https://x.example/a"
    assert message_text(Rendition(body="b", link_url="https://l.example")) == "b\nhttps://l.example"
    assert split_post_id("C1:123.456", None) == ("C1", "123.456")
    assert split_post_id("123.456", "C9") == ("C9", "123.456")
    with pytest.raises(IbxError):
        split_post_id("123.456", None)
    message = Message.model_validate({"ts": "1.2", "metadata": {"event_type": "ibx_publication", "event_payload": {"marker": "ibx-a"}}})
    assert has_marker(message, "ibx-a") and not has_marker(message, "ibx-b")
    assert not has_marker(Message.model_validate({"ts": "1.2"}), "ibx-a")


# ------------------------------------------------------------------ PeerTube


def _video(**fields) -> Video:
    base = {"id": 1, "uuid": "u", "url": "http://pt/videos/watch/u", "name": "n", "state": {"id": 1}, "privacy": {"id": 1},
            "views": 0, "likes": 0, "waitTranscoding": False}
    return Video.model_validate({**base, **fields})


@pytest.mark.parametrize(("fields", "status"), [
    ({}, "published"),
    ({"state": {"id": 2}, "waitTranscoding": False}, "published"),  # measured: public while transcoding
    ({"state": {"id": 2}, "waitTranscoding": True}, "pending"),
    ({"privacy": {"id": 3}}, "draft"),
    ({"blacklisted": True}, "removed_by_platform"),
])
def test_peertube_status(fields, status):
    assert video_status(_video(**fields)) == status


def test_peertube_description_carries_an_invisible_marker():
    text = description_for(Rendition(body="Hello", link_url="https://example.com"), "ibx-abc")
    assert text == "Hello\n\nhttps://example.com\n\n" + marker_line("ibx-abc")
    assert marker_line("ibx-abc") == "[//]: # (ibx-abc)"


def test_peertube_error_map(make_settings):
    pt = _connector("peertube", make_settings)
    problem = {"type": "x", "detail": "Invalid grant: user credentials are invalid", "status": 400, "code": "invalid_grant"}
    assert pt.map_error(httpx.Response(400, json=problem)).code == ErrorCode.CONNECTION_AUTH_EXPIRED
    assert pt.map_error(httpx.Response(401, json={"code": "invalid_token"})).code == ErrorCode.CONNECTION_AUTH_EXPIRED
    assert pt.map_error(httpx.Response(400, json={"detail": "Incorrect request parameters: name"})).code == ErrorCode.PLATFORM_CONTENT_REJECTED
    limited = pt.map_error(httpx.Response(429, text="Too many requests, please try again later.", headers={"Retry-After": "10"}))
    assert limited.code == ErrorCode.PLATFORM_RATE_LIMIT and limited.details["retry_after_s"] == 10


# ------------------------------------------------------------------ MRSS


def test_feed_key_forms():
    assert feed_key_of("AI-Weekly") == "ai-weekly"
    assert feed_key_of("https://h.example/api/v1/feeds/ai_weekly/mrss.xml") == "ai_weekly"
    for bad in ("", "has space", "https://h.example/no/feed/here"):
        with pytest.raises(IbxError):
            feed_key_of(bad)


def test_rendered_feed_is_valid_media_rss_even_with_awkward_text():
    item = {"guid": "ibx-abc", "feed_key": "f", "title": "Tamil தமிழ் & <tags> \x00\x07", "description": "Body ]]> & more",
            "link": "https://example.com/?a=1&b=2", "tags": ["ai"], "language": "ta", "published_at": "2026-10-18T09:00:00+00:00",
            "media": [{"url": "https://cdn.example.com/a.jpg", "mime": "image/jpeg", "alt": "alt text"}]}
    xml = render_feed("f", [item], "http://ibx.test/", {"title": "Feed", "language": "ta"})
    parsed = parse_feed(xml)
    [entry] = parsed.entries
    assert entry.title == "Tamil தமிழ் & <tags>" and entry.id == "ibx-abc"
    assert entry.media_content[0]["medium"] == "image"
    assert entry.link == "http://ibx.test/api/v1/r/ibx-abc"
    assert parsed.feed.links[0]["href"] == "http://ibx.test/api/v1/feeds/f/mrss.xml"
    assert b'xmlns:media="http://search.yahoo.com/mrss/"' in xml


def test_parse_feed_refuses_broken_xml():
    with pytest.raises(ValueError):
        parse_feed(b"<rss><channel><title>x</title>")


def test_manifests_are_strict_and_capabilities_match():
    assert not REGISTRY.errors
    for key in ("mrss", "slack", "peertube"):
        assert REGISTRY.classes[key].implemented_capabilities() == set(REGISTRY.manifests[key].capabilities)
    assert REGISTRY.manifests["mrss"].tier == "feed"
    assert all(k.startswith("200:") or k == "429" for k in REGISTRY.manifests["slack"].error_map)
