"""Reset-time parsing: Retry-After seconds / HTTP-date, X-RateLimit-Reset epoch vs delta, IETF header, body hints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.reliability.retry_after import fallback_reset, parse_reset, parse_retry_after

NOW = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)


def test_retry_after_seconds():
    info = parse_reset({"Retry-After": "120"}, None, NOW)
    assert info.retry_after_s == 120 and info.retry_at == NOW + timedelta(seconds=120) and info.source == "Retry-After"
    assert info.as_details()["retry_at"] == "2026-10-20T10:02:00Z"


def test_retry_after_http_date():
    info = parse_reset({"Retry-After": "Tue, 20 Oct 2026 10:05:00 GMT"}, None, NOW)
    assert info.retry_after_s == 300


def test_retry_after_in_the_past_is_zero():
    assert parse_retry_after("Tue, 20 Oct 2026 09:00:00 GMT", NOW).retry_after_s == 0


def test_fractional_seconds_round_up():
    assert parse_reset({"Retry-After": "1.2"}, None, NOW).retry_after_s == 2


@pytest.mark.parametrize("header", ["X-RateLimit-Reset", "RateLimit-Reset", "X-Rate-Limit-Reset"])
def test_reset_epoch_seconds(header):
    epoch = int((NOW + timedelta(seconds=90)).timestamp())
    info = parse_reset({header: str(epoch)}, None, NOW)
    assert info.retry_after_s == 90 and info.source == header


def test_reset_epoch_milliseconds():
    epoch_ms = int((NOW + timedelta(seconds=45)).timestamp() * 1000)
    assert parse_reset({"X-RateLimit-Reset": str(epoch_ms)}, None, NOW).retry_after_s == 45


def test_reset_small_number_is_a_delta():
    assert parse_reset({"X-RateLimit-Reset": "30"}, None, NOW).retry_after_s == 30


def test_ietf_ratelimit_header():
    assert parse_reset({"RateLimit": "limit=100, remaining=0, reset=50"}, None, NOW).retry_after_s == 50


def test_header_lookup_is_case_insensitive_for_dicts_and_httpx():
    assert parse_reset({"retry-after": "7"}, None, NOW).retry_after_s == 7
    assert parse_reset(httpx.Headers({"retry-after": "8"}), None, NOW).retry_after_s == 8


def test_retry_after_wins_over_reset_header():
    assert parse_reset({"Retry-After": "10", "X-RateLimit-Reset": "999"}, None, NOW).source == "Retry-After"


def test_discourse_body_wait_seconds():
    info = parse_reset({}, {"errors": ["too many"], "extras": {"wait_seconds": 33}}, NOW)
    assert info.retry_after_s == 33 and info.source == "body:extras.wait_seconds"


def test_body_retry_after_and_nested_error():
    assert parse_reset({}, {"retry_after": 12}, NOW).retry_after_s == 12
    assert parse_reset({}, {"error": {"retry_after": 4}}, NOW).retry_after_s == 4


def test_body_reset_at_iso():
    assert parse_reset({}, {"reset_at": "2026-10-20T10:01:00Z"}, NOW).retry_after_s == 60


def test_nothing_usable_returns_none_and_fallback_is_explicit():
    assert parse_reset({}, {"error": "rate_limit_error"}, NOW) is None
    assert parse_reset({"Retry-After": "soon"}, None, NOW) is None
    info = fallback_reset(60, NOW, "lemmy_site_config")
    assert info.retry_after_s == 60 and info.source == "lemmy_site_config"


def test_absurd_values_are_capped_at_a_week():
    assert parse_reset({"Retry-After": "99999999"}, None, NOW).retry_after_s == 7 * 24 * 3600
