"""LLM adapter chain: presets, fallback order, 429 back-off, timeouts, strict JSON, manifest fields.
Every provider is faked with respx; no key, no network, no Docker."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.providers.llm import LLMSettings, LLMUnavailableError, chain_names, complete_json, reset_cooldowns
from app.providers.llm.base import check_json_schema, parse_json_object
from app.providers.llm.config import GEMINI_OPENAI_BASE, GROQ_OPENAI_BASE, OPENROUTER_BASE

OLLAMA = "http://ollama.test:11434/v1"


class Answer(BaseModel):
    score: int
    reason: str


def settings(**kw: Any) -> LLMSettings:
    values: dict[str, Any] = {"LLM_PROVIDER": "auto", "OLLAMA_URL": OLLAMA, "LLM_MAX_BACKOFF_S": 1.0,
                              "LLM_TIMEOUT_S": 2.0, "OLLAMA_TIMEOUT_S": 2.0}
    values.update(kw)
    return LLMSettings(_env_file=None, **values)


def chat(content: str, *, model: str = "m", prompt_tokens: int = 11, completion_tokens: int = 7) -> httpx.Response:
    return httpx.Response(200, json={"model": model, "choices": [{"message": {"role": "assistant", "content": content}}],
                                     "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}})


GOOD = json.dumps({"score": 4, "reason": "fits"})


@pytest.fixture(autouse=True)
def _fresh() -> None:
    reset_cooldowns()


def test_chain_order_and_names() -> None:
    assert chain_names("auto") == ["gemini", "groq", "openrouter", "ollama", "mock"]
    assert chain_names("mock") == ["mock"]
    assert chain_names("gemini") == ["gemini", "mock"]
    assert chain_names("openai_compat") == ["ollama", "mock"]
    with pytest.raises(ValueError):
        chain_names("openai-paid")


@respx.mock
async def test_auto_without_keys_skips_hosted_and_falls_back_to_mock() -> None:
    respx.post(f"{OLLAMA}/chat/completions").mock(side_effect=httpx.ConnectError("no ollama"))
    obj, meta = await complete_json("rate this", Answer, mock=lambda p: {"score": 2, "reason": "lexicon"}, settings=settings())
    assert obj == Answer(score=2, reason="lexicon")
    assert (meta.provider, meta.model, meta.fallback_used, meta.cost_usd) == ("mock", "mock-heuristic-v1", True, 0.0)
    outcomes = [(a.provider, a.outcome) for a in meta.attempts]
    assert outcomes == [("gemini", "skipped"), ("groq", "skipped"), ("openrouter", "skipped"), ("ollama", "unavailable"),
                        ("mock", "ok")]
    # unreachable ollama cools down: the next call does not even try it
    _, meta2 = await complete_json("again", Answer, mock=lambda p: {"score": 2, "reason": "x"}, settings=settings())
    assert ("ollama", "skipped") in [(a.provider, a.outcome) for a in meta2.attempts]
    assert respx.calls.call_count == 1


@respx.mock
async def test_gemini_free_tier_preset_is_used_first_and_recorded() -> None:
    route = respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(return_value=chat(GOOD, model="gemini-3.8-flash"))
    obj, meta = await complete_json("p", Answer, system="sys", settings=settings(GEMINI_API_KEY="AIza-test-key-123"))
    assert obj.score == 4
    sent = json.loads(route.calls[0].request.content)
    assert route.calls[0].request.headers["Authorization"] == "Bearer AIza-test-key-123"
    assert sent["model"] == "gemini-3.8-flash" and sent["temperature"] == 0
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["response_format"]["type"] == "json_schema"
    assert (meta.provider, meta.fallback_used, meta.prompt_tokens, meta.completion_tokens, meta.total_tokens) == (
        "gemini", False, 11, 7, 18)
    entry = meta.manifest_entry("relevance")
    assert entry["cost_usd"] == 0.0 and entry["tokens"]["total"] == 18 and entry["provider"] == "gemini"
    assert "AIza-test-key-123" not in json.dumps(entry)
    assert "used to improve" in entry["data_terms"] or "improve its products" in entry["data_terms"]


@respx.mock
async def test_short_429_is_waited_out_once() -> None:
    route = respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), chat(GOOD)])
    obj, meta = await complete_json("p", Answer, settings=settings(GEMINI_API_KEY="k-123456"))
    assert route.call_count == 2 and obj.score == 4 and meta.provider == "gemini"
    assert [a.outcome for a in meta.attempts] == ["rate_limited", "ok"]


@respx.mock
async def test_long_429_falls_through_to_next_provider_and_cools_down() -> None:
    gemini = respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"}))
    groq = respx.post(f"{GROQ_OPENAI_BASE}/chat/completions").mock(return_value=chat(GOOD, model="openai/gpt-oss-20b"))
    s = settings(GEMINI_API_KEY="k-123456", GROQ_API_KEY="gsk-123456")
    obj, meta = await complete_json("p", Answer, settings=s)
    assert (meta.provider, meta.fallback_used) == ("groq", True)
    assert json.loads(groq.calls[0].request.content)["response_format"] == {"type": "json_object"}
    await complete_json("p", Answer, settings=s)
    assert gemini.call_count == 1  # cooling down, not called again


@respx.mock
async def test_timeout_falls_through() -> None:
    respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.post(f"{OLLAMA}/chat/completions").mock(return_value=chat(GOOD, model="qwen2.5:1.5b-instruct"))
    _, meta = await complete_json("p", Answer, settings=settings(GEMINI_API_KEY="k-123456"))
    assert meta.provider == "ollama" and meta.model == "qwen2.5:1.5b-instruct" and meta.fallback_used
    assert [a.outcome for a in meta.attempts if a.outcome != "skipped"] == ["timeout", "ok"]


@respx.mock
async def test_invalid_json_gets_one_corrective_retry() -> None:
    route = respx.post(f"{OLLAMA}/chat/completions").mock(side_effect=[chat("sure! score is 4"), chat(f"```json\n{GOOD}\n```")])
    obj, meta = await complete_json("p", Answer, settings=settings(LLM_PROVIDER="ollama"))
    assert obj.score == 4 and meta.provider == "ollama" and not meta.fallback_used
    retry = json.loads(route.calls[1].request.content)["messages"]
    assert retry[-1]["role"] == "user" and "invalid" in retry[-1]["content"]
    assert meta.total_tokens == 36  # both calls are counted


@respx.mock
async def test_schema_failure_twice_falls_through_to_mock() -> None:
    respx.post(f"{OLLAMA}/chat/completions").mock(return_value=chat(json.dumps({"score": "high", "reason": 3})))
    obj, meta = await complete_json("p", Answer, mock=lambda p: {"score": 1, "reason": "det"},
                                    settings=settings(LLM_PROVIDER="ollama"))
    assert obj.score == 1 and meta.provider == "mock" and meta.fallback_used
    assert [a.outcome for a in meta.attempts] == ["bad_output", "bad_output", "ok"]


@respx.mock
async def test_caller_validate_hook_counts_as_validation() -> None:
    respx.post(f"{OLLAMA}/chat/completions").mock(return_value=chat(GOOD))

    def reject(obj: Answer) -> None:
        raise ValueError("quote not in source")

    with pytest.raises(LLMUnavailableError) as info:
        await complete_json("p", Answer, validate=reject, settings=settings(LLM_PROVIDER="ollama"))
    assert info.value.meta.provider == "none"
    assert [a.outcome for a in info.value.meta.attempts] == ["bad_output", "bad_output", "skipped"]


@respx.mock
async def test_response_format_refusal_downgrades_and_retries() -> None:
    route = respx.post(f"{OPENROUTER_BASE}/chat/completions").mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "response_format is not supported by this model"}}), chat(GOOD)])
    _, meta = await complete_json("p", Answer, settings=settings(LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="sk-or-1234"))
    assert meta.provider == "openrouter"
    assert "response_format" not in json.loads(route.calls[1].request.content)


def test_openrouter_paid_model_is_never_called() -> None:
    from app.providers.llm import openai_compat

    paid = openai_compat.build("openrouter", settings(OPENROUTER_API_KEY="sk-or-1234", OPENROUTER_MODEL="openai/gpt-6"))
    ok, why = paid.available()
    assert not ok and ":free" in why


@respx.mock
async def test_auth_failure_is_unavailable_not_retried() -> None:
    route = respx.post(f"{GROQ_OPENAI_BASE}/chat/completions").mock(return_value=httpx.Response(401))
    _, meta = await complete_json("p", Answer, mock=lambda p: {"score": 3, "reason": "m"},
                                  settings=settings(LLM_PROVIDER="groq", GROQ_API_KEY="gsk-bad-1234"))
    assert route.call_count == 1 and meta.provider == "mock"


async def test_mock_only_and_no_mock_function() -> None:
    _, meta = await complete_json("p", Answer, mock=lambda p: Answer(score=5, reason="r"), settings=settings(LLM_PROVIDER="mock"))
    assert (meta.provider, meta.fallback_used, meta.total_tokens) == ("mock", False, 0)
    with pytest.raises(LLMUnavailableError):
        await complete_json("p", Answer, settings=settings(LLM_PROVIDER="mock"))


@respx.mock
async def test_dict_json_schema_is_checked_strictly() -> None:
    schema = {"type": "object", "properties": {"adjustment": {"enum": [-1, 0, 1]}, "reason": {"type": "string", "maxLength": 20}},
              "required": ["adjustment", "reason"], "additionalProperties": False}
    respx.post(f"{OLLAMA}/chat/completions").mock(side_effect=[chat(json.dumps({"adjustment": 2, "reason": "x"})),
                                                               chat(json.dumps({"adjustment": 1, "reason": "ok"}))])
    obj, _ = await complete_json("p", schema, settings=settings(LLM_PROVIDER="ollama"))
    assert obj == {"adjustment": 1, "reason": "ok"}
    assert check_json_schema(schema, {"adjustment": 0, "reason": "r", "extra": 1}) == ["$.extra: not allowed"]
    assert check_json_schema(schema, {"adjustment": True, "reason": "r"})  # a bool is not a number


def test_parse_json_object() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here: {"a": 1} done') == {"a": 1}
    with pytest.raises(Exception):  # noqa: B017
        parse_json_object("[1, 2]")


def test_keys_are_registered_for_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.providers.llm import config
    from app.reliability.redact import secret_registry

    monkeypatch.setenv("GROQ_API_KEY", "gsk-secret-zz-987654")
    config.get_llm_settings.cache_clear()
    try:
        config.get_llm_settings()
        assert "gsk-secret-zz-987654" not in secret_registry.scrub("key=gsk-secret-zz-987654")
    finally:
        config.get_llm_settings.cache_clear()
