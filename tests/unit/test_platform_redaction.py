"""Redaction: secret keys, live secret values, token patterns, nested data, exceptions and log output."""

from __future__ import annotations

import json
import logging

import pytest
import structlog
from pydantic import SecretStr

from app.logging import configure_logging, get_logger
from app.reliability.redact import MASK, redact, redact_text, secret_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    secret_registry.clear()
    yield
    secret_registry.clear()


def test_secret_keys_are_masked_at_any_depth():
    data = {"connection": {"auth": {"api_key": "abc123xyz", "Password": "hunter22"}, "credentials": {"x": "y"}},
            "items": [{"token": "t0k3n-value"}]}
    out = redact(data)
    assert out["connection"]["auth"] == {"api_key": MASK, "Password": MASK}
    assert out["connection"]["credentials"] == MASK  # a whole secret-named object is masked
    assert out["items"][0]["token"] == MASK


def test_safe_keys_and_booleans_are_kept():
    out = redact({"idempotency_key": "k-1", "secret_set": True, "has_token": False, "title": "hello"})
    assert out == {"idempotency_key": "k-1", "secret_set": True, "has_token": False, "title": "hello"}


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("Authorization: Bearer abcdef123456", "abcdef123456"),
        ("Ghost 64fa9c0e3b:2f1a9e7c", "64fa9c0e3b:2f1a9e7c"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJl here", "eyJhbGciOiJIUzI1NiJ9"),
        ("slack xoxb-1234567890-abcdefghij", "xoxb-1234567890"),
        ("key sk-abcdefghijklmnop", "sk-abcdefghijklmnop"),
        ("GET /x?api_key=supersecret&page=2", "supersecret"),
        ("postgresql://ibx:pa55word@db/ibx", "pa55word"),
    ],
)
def test_token_patterns_are_scrubbed(text, leaked):
    assert leaked not in redact_text(text)


def test_registered_live_values_are_scrubbed_anywhere():
    secret_registry.add("CANARY-7f3a-value")
    out = redact({"message": "platform said CANARY-7f3a-value is wrong", "list": ["x CANARY-7f3a-value y"]})
    assert "CANARY-7f3a-value" not in json.dumps(out)


def test_exceptions_and_secretstr_are_redacted():
    secret_registry.add("leaky-secret-99")
    assert "leaky-secret-99" not in redact(RuntimeError("failed with leaky-secret-99"))
    assert redact(SecretStr("x")) == MASK


def test_short_values_are_not_registered():
    secret_registry.add("ab")
    assert redact_text("about") == "about"


def test_log_lines_are_redacted_json(capsys):
    configure_logging("INFO", json_logs=True, secrets=["CANARY-LOG-123456"])
    log = get_logger("test")
    structlog.contextvars.bind_contextvars(correlation_id="corr-1", job_id="job_1")
    try:
        log.info("calling", headers={"Authorization": "Bearer abcdefghijkl"}, note="value CANARY-LOG-123456")
        logging.getLogger("uvicorn.error").warning("stdlib Bearer zzzzzzzzzzzz CANARY-LOG-123456")
        raise ValueError("boom CANARY-LOG-123456")
    except ValueError:
        log.exception("failed")
    finally:
        structlog.contextvars.clear_contextvars()
    out = capsys.readouterr().out
    assert "CANARY-LOG-123456" not in out and "abcdefghijkl" not in out and "zzzzzzzzzzzz" not in out
    first = json.loads(out.splitlines()[0])
    assert first["event"] == "calling" and first["correlation_id"] == "corr-1" and first["job_id"] == "job_1"
    assert {"timestamp", "level"} <= set(first)


def test_loggers_created_before_configuration_still_redact(capsys):
    early = get_logger("early.module")  # e.g. a module-level logger imported before start-up
    configure_logging("INFO", json_logs=True, secrets=["CANARY-EARLY-998877"])
    early.info("early", note="CANARY-EARLY-998877", headers={"Authorization": "Bearer qqqqqqqqqqqq"})
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["logger"] == "early.module" and line["event"] == "early"
    assert "CANARY-EARLY-998877" not in json.dumps(line) and "qqqqqqqqqqqq" not in json.dumps(line)
