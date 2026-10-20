"""Circuit breaker state machine (pure) and the token bucket, driven by an explicit clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.reliability.breaker import BreakerState, Verdict, decide, on_failure, on_success
from app.reliability.ratelimit import BucketRegistry, TokenBucket

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def _fail(st: BreakerState, now: datetime = T0) -> tuple[BreakerState, bool]:
    return on_failure(st, now, threshold=5, open_s=300)


def test_opens_after_five_consecutive_failures():
    st = BreakerState()
    for i in range(4):
        st, opened = _fail(st)
        assert not opened and st.state == "closed" and st.failures == i + 1
    st, opened = _fail(st)
    assert opened and st.state == "open" and st.open_until == T0 + timedelta(seconds=300)


def test_success_resets_the_count():
    st = BreakerState()
    for _ in range(4):
        st, _ = _fail(st)
    st = on_success(st)
    st, opened = _fail(st)
    assert not opened and st.failures == 1


def test_open_rejects_until_open_until_then_allows_one_probe():
    st = BreakerState("open", 5, T0 + timedelta(seconds=300))
    assert decide(st, T0 + timedelta(seconds=299), probe_timeout_s=30)[0] == Verdict.REJECT
    verdict, st = decide(st, T0 + timedelta(seconds=300), probe_timeout_s=30)
    assert verdict == Verdict.PROBE and st.state == "half_open"
    verdict, _ = decide(st, T0 + timedelta(seconds=301), probe_timeout_s=30)
    assert verdict == Verdict.REJECT  # only one probe at a time


def test_probe_success_closes_probe_failure_reopens():
    half = BreakerState("half_open", 5, None, T0)
    assert on_success(half) == BreakerState()
    st, opened = _fail(half, T0 + timedelta(seconds=10))
    assert opened and st.state == "open" and st.open_until == T0 + timedelta(seconds=310)


def test_stuck_probe_expires():
    half = BreakerState("half_open", 5, None, T0)
    verdict, st = decide(half, T0 + timedelta(seconds=31), probe_timeout_s=30)
    assert verdict == Verdict.PROBE and st.probe_started_at == T0 + timedelta(seconds=31)


def test_closed_always_allows():
    assert decide(BreakerState(), T0, 30)[0] == Verdict.ALLOW


# ------------------------------------------------------------------ token bucket


def test_bucket_allows_capacity_then_reports_wait():
    bucket = TokenBucket(capacity=3, per_seconds=60)
    assert [bucket.try_take(0.0) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert bucket.try_take(0.0) == pytest.approx(20.0)  # one token per 20 s


def test_bucket_refills_over_time_and_caps_at_capacity():
    bucket = TokenBucket(capacity=2, per_seconds=10)
    bucket.try_take(0.0)
    bucket.try_take(0.0)
    assert bucket.try_take(5.0) == 0.0  # 5 s = 1 token
    assert bucket.try_take(5.0) > 0
    assert bucket.try_take(1000.0) == 0.0 and bucket.tokens == pytest.approx(1.0)


def test_bucket_rejects_bad_config():
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, per_seconds=1)


def test_registry_keeps_one_bucket_per_scope():
    registry = BucketRegistry()
    assert registry.take("a", 1, 60, 0.0) == 0.0
    assert registry.take("a", 1, 60, 0.0) > 0
    assert registry.take("b", 1, 60, 0.0) == 0.0
