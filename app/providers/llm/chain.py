"""The provider chain and the one public call, ``complete_json``.

    obj, meta = await complete_json(prompt, schema)                    # schema: a Pydantic model class or a JSON schema
    obj, meta = await complete_json(prompt, Schema, system=..., mock=my_deterministic_fn)

``LLM_PROVIDER=auto`` tries gemini -> groq -> openrouter -> ollama -> mock. A named provider is tried first and the
mock after it. For every provider:

* skipped (recorded, not called) when it has no key, or while it is cooling down after a 429 / connection failure;
* a 429 asking to wait at most ``LLM_MAX_BACKOFF_S`` is waited out once; a longer one cools the provider down;
* a timeout, connection error or HTTP error falls through to the next provider;
* the reply must be one JSON object that passes the schema (and the caller's ``validate``). A failed validation is
  retried once with the reason, then the chain falls through.

The mock is the deterministic function the caller passes (``mock=``). If every provider fails and there is no mock,
``LLMUnavailableError`` is raised (with the attempts in ``.meta``). Nothing here decides anything on its own: callers
treat the result as a proposal (rules capture: a person confirms it before it is used).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from app.providers.llm import openai_compat
from app.providers.llm.base import (
    Attempt,
    Completion,
    LLMError,
    LLMMeta,
    LLMUnavailableError,
    Provider,
    parse_json_object,
    schema_dict,
    validate_object,
)
from app.providers.llm.config import LLMSettings, get_llm_settings
from app.providers.llm.mock import MockProvider

log = structlog.get_logger("app.providers.llm")

AUTO_ORDER = ("gemini", "groq", "openrouter", "ollama")
KNOWN = (*AUTO_ORDER, "mock", "openai_compat", "auto")

# provider name -> monotonic time until which it is skipped (429 with a long wait, or unreachable)
_COOLDOWN: dict[str, float] = {}


def reset_cooldowns() -> None:
    _COOLDOWN.clear()


def chain_names(requested: str) -> list[str]:
    requested = (requested or "mock").strip().lower()
    if requested not in KNOWN:
        raise ValueError(f"LLM_PROVIDER must be one of {', '.join(KNOWN)}")
    if requested == "auto":
        return [*AUTO_ORDER, "mock"]
    if requested == "mock":
        return ["mock"]
    return ["ollama" if requested == "openai_compat" else requested, "mock"]


def build_chain(settings: LLMSettings, *, mock: Callable[[str], Any] | None = None,
                http: httpx.AsyncClient | None = None) -> list[Provider]:
    providers: list[Provider] = []
    for name in chain_names(settings.LLM_PROVIDER):
        if name == "mock":
            providers.append(MockProvider(mock))
        else:
            spelling = "openai_compat" if name == "ollama" and settings.LLM_PROVIDER == "openai_compat" else name
            providers.append(openai_compat.build(spelling, settings, http))
    return providers


def _messages(system: str | None, prompt: str) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    return messages


async def _validated(provider: Provider, completion: Completion, schema: Any,
                     validate: Callable[[Any], None] | None) -> Any:
    raw = completion.data if completion.data is not None else parse_json_object(completion.text)
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    obj = validate_object(schema, raw)
    if validate is not None:
        try:
            validate(obj)
        except ValueError as exc:
            raise LLMError("bad_output", f"check: {exc}") from None
    return obj


async def complete_json(
    prompt: str,
    schema: type[BaseModel] | dict[str, Any],
    *,
    system: str | None = None,
    mock: Callable[[str], Any] | None = None,
    validate: Callable[[Any], None] | None = None,
    max_tokens: int = 1024,
    settings: LLMSettings | None = None,
    http: httpx.AsyncClient | None = None,
    providers: list[Provider] | None = None,
) -> tuple[Any, LLMMeta]:
    """Ask the configured free providers for one JSON object that matches ``schema``.

    Returns ``(obj, meta)``: ``obj`` is a model instance when ``schema`` is a Pydantic model class, else the checked
    dict. ``meta`` records provider, model, fallback_used, every attempt, tokens and cost (0: free tiers only).
    """
    settings = settings or get_llm_settings()
    chain = providers if providers is not None else build_chain(settings, mock=mock, http=http)
    json_schema = schema.llm_json_schema() if hasattr(schema, "llm_json_schema") else schema_dict(schema)
    started = time.perf_counter()
    attempts: list[Attempt] = []
    tokens = [0, 0]
    tried_real = False

    def meta(provider: Provider, *, fallback: bool) -> LLMMeta:
        return LLMMeta(
            provider=provider.name, model=provider.model, fallback_used=fallback, attempts=attempts,
            prompt_tokens=tokens[0], completion_tokens=tokens[1], total_tokens=tokens[0] + tokens[1], cost_usd=0.0,
            latency_ms=round((time.perf_counter() - started) * 1000, 1), requested=settings.LLM_PROVIDER,
            data_terms=provider.free_tier_terms,
        )

    for provider in chain:
        ok, why = provider.available()
        if not ok:
            attempts.append(Attempt(provider=provider.name, model=provider.model, outcome="skipped", detail=why))
            continue
        until = _COOLDOWN.get(provider.name)
        if until is not None and time.monotonic() < until:
            attempts.append(Attempt(provider=provider.name, model=provider.model, outcome="skipped",
                                    detail=f"cooling down for {until - time.monotonic():.0f} s"))
            continue
        messages = _messages(system, prompt)
        retried_rate_limit = retried_output = False
        while True:
            t0 = time.perf_counter()
            completion: Completion | None = None
            try:
                completion = await provider.complete(messages, json_schema=json_schema, max_tokens=max_tokens)
                tokens[0] += completion.prompt_tokens
                tokens[1] += completion.completion_tokens
                obj = await _validated(provider, completion, schema, validate)
            except LLMError as err:
                latency = round((time.perf_counter() - t0) * 1000, 1)
                attempts.append(Attempt(provider=provider.name, model=provider.model, outcome=err.kind,
                                        detail=err.message[:300], latency_ms=latency))
                if err.kind == "rate_limited":
                    wait = err.retry_after_s
                    if not retried_rate_limit and wait is not None and wait <= settings.LLM_MAX_BACKOFF_S:
                        retried_rate_limit = True
                        await asyncio.sleep(wait)
                        continue
                    _COOLDOWN[provider.name] = time.monotonic() + max(wait or 0.0, settings.LLM_COOLDOWN_S)
                elif err.kind == "unavailable" and provider.name != "mock":
                    _COOLDOWN[provider.name] = time.monotonic() + settings.LLM_COOLDOWN_S
                elif (err.kind == "bad_output" and not retried_output and completion is not None
                      and completion.data is None and completion.text.strip()):
                    retried_output = True  # one corrective retry, then fall through
                    messages = [*messages, {"role": "assistant", "content": completion.text[:4000]},
                                {"role": "user", "content": f"That answer was invalid ({err.message[:300]}). "
                                 "Reply again with only the corrected JSON object."}]
                    continue
                log.info("llm_provider_failed", provider=provider.name, model=provider.model, kind=err.kind)
                tried_real = tried_real or provider.name != "mock"  # this provider is given up: what follows is a fallback
                break
            latency = round((time.perf_counter() - t0) * 1000, 1)
            attempts.append(Attempt(provider=provider.name, model=completion.model or provider.model, outcome="ok",
                                    latency_ms=latency))
            # fallback: an earlier provider was called and failed, or the mock answered although a real LLM was asked for
            fallback = tried_real or (provider.name == "mock" and settings.LLM_PROVIDER.strip().lower() != "mock")
            result = meta(provider, fallback=fallback)
            log.info("llm_call", provider=result.provider, model=result.model, fallback_used=result.fallback_used,
                     total_tokens=result.total_tokens, latency_ms=result.latency_ms)
            return obj, result
    unavailable = LLMMeta(provider="none", model="none", fallback_used=True, attempts=attempts,
                          prompt_tokens=tokens[0], completion_tokens=tokens[1], total_tokens=sum(tokens),
                          latency_ms=round((time.perf_counter() - started) * 1000, 1), requested=settings.LLM_PROVIDER)
    raise LLMUnavailableError("No LLM provider produced a valid answer: "
                              + json.dumps([a.model_dump(exclude_none=True) for a in attempts])[:1000], unavailable)

