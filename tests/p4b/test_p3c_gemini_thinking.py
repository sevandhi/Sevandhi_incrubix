"""Gemini 3.x "thinking" models through the OpenAI-compatible endpoint.

Measured on 2026-10-20: ``gemini-3.8-flash`` with ``max_tokens=5`` answered HTTP 200, ``finish_reason: "length"`` and
no content - the thinking tokens used up the whole output budget. Google's OpenAI-compatibility page: thinking can not
be switched off on Gemini 3 models; ``reasoning_effort`` (minimal/low/medium/high) maps to the thinking level.

Fixed here: Gemini calls send ``reasoning_effort`` (setting ``GEMINI_REASONING_EFFORT``) and at least
``GEMINI_MIN_OUTPUT_TOKENS`` as ``max_tokens``; an empty / cut-off answer (``finish_reason: length``) is retried once
with a larger budget and then falls through to the next provider. ``LLM_HTTP_IPV4`` makes the adapter's own HTTP client
use IPv4 (hosts whose IPv6 route to Google times out). All calls are faked with respx: no key, no network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.providers.llm import LLMSettings, complete_json, reset_cooldowns
from app.providers.llm.config import GEMINI_OPENAI_BASE, GROQ_OPENAI_BASE
from app.providers.llm.openai_compat import build


class Answer(BaseModel):
    score: int
    reason: str


GOOD = json.dumps({"score": 4, "reason": "fits"})


def settings(**kw: Any) -> LLMSettings:
    values: dict[str, Any] = {"LLM_PROVIDER": "auto", "OLLAMA_URL": "http://ollama.test:11434/v1", "LLM_TIMEOUT_S": 2.0,
                              "GEMINI_API_KEY": "k-123456"}
    values.update(kw)
    return LLMSettings(_env_file=None, **values)


def reply(content: str | None, finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={"model": "gemini-3.8-flash", "choices": [
        {"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture(autouse=True)
def _fresh() -> None:
    reset_cooldowns()


@respx.mock
async def test_gemini_gets_a_thinking_budget_and_reasoning_effort() -> None:
    route = respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(return_value=reply(GOOD))
    obj, meta = await complete_json("rate", Answer, settings=settings(LLM_PROVIDER="gemini"), max_tokens=5)
    assert obj == Answer(score=4, reason="fits") and meta.provider == "gemini"
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_tokens"] >= 4096  # never 5: the thinking tokens come out of this budget
    assert sent["reasoning_effort"] == "low"


@respx.mock
async def test_reasoning_effort_and_budget_are_settings() -> None:
    route = respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(return_value=reply(GOOD))
    await complete_json("rate", Answer, settings=settings(LLM_PROVIDER="gemini", GEMINI_REASONING_EFFORT="",
                                                         GEMINI_MIN_OUTPUT_TOKENS=2000), max_tokens=900)
    sent = json.loads(route.calls[0].request.content)
    assert "reasoning_effort" not in sent and sent["max_tokens"] == 2000


@respx.mock
async def test_other_providers_do_not_get_reasoning_effort() -> None:
    route = respx.post(f"{GROQ_OPENAI_BASE}/chat/completions").mock(return_value=reply(GOOD))
    await complete_json("rate", Answer, settings=settings(LLM_PROVIDER="groq", GROQ_API_KEY="g-123456"), max_tokens=900)
    sent = json.loads(route.calls[0].request.content)
    assert "reasoning_effort" not in sent and sent["max_tokens"] == 900


@respx.mock
async def test_empty_answer_cut_off_by_length_is_retried_with_more_budget() -> None:
    route = respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(side_effect=[reply(None, "length"), reply(GOOD)])
    obj, meta = await complete_json("rate", Answer, settings=settings(LLM_PROVIDER="gemini"), max_tokens=900)
    assert obj.score == 4 and meta.provider == "gemini"
    first, second = (json.loads(c.request.content)["max_tokens"] for c in route.calls)
    assert second > first


@respx.mock
async def test_still_cut_off_falls_through_to_the_next_provider() -> None:
    respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(return_value=reply("", "length"))
    obj, meta = await complete_json("rate", Answer, settings=settings(LLM_PROVIDER="gemini"),
                                    mock=lambda p: {"score": 1, "reason": "mock"}, max_tokens=900)
    assert obj.reason == "mock" and meta.provider == "mock" and meta.fallback_used is True
    gemini = [a for a in meta.attempts if a.provider == "gemini"]
    assert gemini and gemini[-1].outcome == "truncated" and "length" in (gemini[-1].detail or "")


@respx.mock
async def test_a_refused_reasoning_effort_is_dropped_once() -> None:
    route = respx.post(f"{GEMINI_OPENAI_BASE}/chat/completions").mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "Unknown name \"reasoning_effort\""}}), reply(GOOD)])
    obj, _ = await complete_json("rate", Answer, settings=settings(LLM_PROVIDER="gemini"), max_tokens=900)
    assert obj.score == 4
    assert "reasoning_effort" in json.loads(route.calls[0].request.content)
    assert "reasoning_effort" not in json.loads(route.calls[1].request.content)


async def test_ipv4_setting_binds_the_adapters_own_client_to_ipv4() -> None:
    provider = build("gemini", settings(LLM_HTTP_IPV4=True))
    client = provider._client()
    try:
        pool = client._transport._pool  # type: ignore[attr-defined]
        assert pool._local_address == "0.0.0.0"
    finally:
        await client.aclose()
    default = build("gemini", settings())._client()
    try:
        assert default._transport._pool._local_address is None  # type: ignore[attr-defined]
    finally:
        await default.aclose()
