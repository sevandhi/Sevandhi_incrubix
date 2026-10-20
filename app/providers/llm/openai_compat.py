"""One adapter for every free provider that speaks the OpenAI-compatible chat API.

Presets (verified 2026-10-18; sources in SOURCES.md):
    gemini      Google AI Studio free tier  https://generativelanguage.googleapis.com/v1beta/openai   GEMINI_API_KEY
    groq        Groq free plan              https://api.groq.com/openai/v1                           GROQ_API_KEY
    openrouter  OpenRouter ":free" models   https://openrouter.ai/api/v1                             OPENROUTER_API_KEY
    ollama      local Ollama (profile llm)  http://ollama:11434/v1                                   (no key)

Only ``POST {base}/chat/completions`` is used, with temperature 0 and a fixed seed. JSON output is requested with
``response_format`` (json_schema where the provider supports it, else json_object); if a provider refuses that
field (HTTP 400), the call is repeated once with a weaker mode and the instructions in the prompt alone.

Thinking models: Gemini 3.x always thinks and the thinking tokens are counted in ``max_tokens``, so a small
budget answers 200 with ``finish_reason: length`` and no content. Gemini calls therefore send ``reasoning_effort``
(``GEMINI_REASONING_EFFORT``, mapped by Google to the thinking level) and at least ``GEMINI_MIN_OUTPUT_TOKENS``. A reply
cut off by the length limit is retried once with twice the budget (up to ``LLM_MAX_OUTPUT_TOKENS``) and then raises
``LLMError("truncated")``, which the chain treats as a fall-through to the next provider.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.providers.llm.base import Completion, LLMError, Provider
from app.providers.llm.config import LLMSettings


@dataclass(frozen=True)
class Preset:
    name: str
    json_mode: str  # json_schema | json_object | none
    terms: str
    needs_key: bool = True


PRESETS: dict[str, Preset] = {
    "gemini": Preset(
        "gemini", "json_schema",
        "Gemini API free tier (unpaid service): Google may use prompts and responses to improve its products, and "
        "human reviewers may read them. Only public rules pages and our own content are sent.",
    ),
    "groq": Preset(
        "groq", "json_object",
        "Groq: by default inference inputs and outputs are not retained; they may be logged for up to 30 days for "
        "troubleshooting or abuse monitoring. Only public rules pages and our own content are sent.",
    ),
    "openrouter": Preset(
        "openrouter", "json_object",
        "OpenRouter ':free' models: each upstream provider applies its own logging and training policy, and some free "
        "endpoints may train on prompts. Only public rules pages and our own content are sent.",
    ),
    "ollama": Preset("ollama", "json_schema", "Local Ollama: prompts never leave this machine.", needs_key=False),
}

_DOWNGRADE = {"json_schema": "json_object", "json_object": "none"}


class OpenAICompatProvider(Provider):
    def __init__(self, preset: Preset, *, base_url: str, model: str, api_key: str | None, timeout_s: float, seed: int,
                 http: httpx.AsyncClient | None = None, reasoning_effort: str | None = None, min_output_tokens: int = 0,
                 max_output_tokens: int = 16384, ipv4: bool = False) -> None:
        self.preset = preset
        self.name = preset.name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key
        self.timeout_s = timeout_s
        self.seed = seed
        self.http = http
        self.json_mode = preset.json_mode
        self.free_tier_terms = preset.terms
        self.reasoning_effort = reasoning_effort or None
        self.min_output_tokens = min_output_tokens
        self.max_output_tokens = max(max_output_tokens, min_output_tokens)
        self.ipv4 = ipv4

    def _client(self) -> httpx.AsyncClient:
        """The adapter's own client (when none is injected). ``LLM_HTTP_IPV4`` binds it to 0.0.0.0, i.e. IPv4 only."""
        if self.ipv4:
            return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"))
        return httpx.AsyncClient()

    def available(self) -> tuple[bool, str]:
        if self.preset.needs_key and not self._key:
            return False, "no API key configured"
        if self.name == "openrouter" and not self.model.endswith(":free"):
            return False, "only ':free' OpenRouter models are allowed (free-only rule)"
        return True, "ok"

    def _body(self, messages: list[dict[str, str]], json_schema: dict[str, Any] | None, max_tokens: int,
              mode: str) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if self.name in ("ollama", "groq", "openrouter"):
            body["seed"] = self.seed
        if mode == "json_schema" and json_schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "schema": json_schema, "strict": False}}
        elif mode in ("json_schema", "json_object"):
            body["response_format"] = {"type": "json_object"}
        return body

    async def complete(self, messages: list[dict[str, str]], *, json_schema: dict[str, Any] | None,
                       max_tokens: int) -> Completion:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        if self.name == "openrouter":
            headers["X-Title"] = "IncuBrix destination network"
        own = self.http is None
        client = self.http or self._client()
        mode = self.json_mode
        budget = max(max_tokens, self.min_output_tokens)
        grown = False
        try:
            while True:
                try:
                    response = await client.post(f"{self.base_url}/chat/completions", headers=headers,
                                                 json=self._body(messages, json_schema, budget, mode),
                                                 timeout=self.timeout_s)
                except httpx.TimeoutException:
                    raise LLMError("timeout", f"no answer within {self.timeout_s:g} s") from None
                except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError) as exc:
                    raise LLMError("unavailable", f"cannot connect ({type(exc).__name__})") from None
                if response.status_code == 400 and self.reasoning_effort and "reasoning_effort" in response.text:
                    self.reasoning_effort = None  # this model/endpoint refuses the field: ask without it
                    continue
                if response.status_code == 400 and mode != "none" and "response_format" in response.text:
                    mode = _DOWNGRADE[mode]  # this provider/model refuses that JSON mode: ask more weakly
                    self.json_mode = mode
                    continue
                completion, finish = self._parse(response)
                if finish == "length":
                    if not grown and budget < self.max_output_tokens:
                        grown, budget = True, min(budget * 2, self.max_output_tokens)
                        continue  # thinking (or the answer) used up the budget: once more with a larger one
                    raise LLMError("truncated", f"finish_reason=length at max_tokens={budget} "
                                   f"({'no content' if not completion.text.strip() else 'answer cut off'})")
                if not completion.text.strip() and completion.data is None:
                    raise LLMError("truncated", f"empty answer (finish_reason={finish or 'none'})")
                return completion
        finally:
            if own:
                await client.aclose()

    def _parse(self, response: httpx.Response) -> tuple[Completion, str | None]:
        status = response.status_code
        if status == 429:
            raise LLMError("rate_limited", "HTTP 429 (free-tier limit)", retry_after_s=_retry_after(response))
        if status in (401, 403):
            raise LLMError("unavailable", f"HTTP {status}: key refused")
        if status == 404:
            raise LLMError("unavailable", "HTTP 404: model or endpoint not found")
        if status >= 400:
            raise LLMError("http_error", f"HTTP {status}")
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("bad_output", "the response is not an OpenAI-style chat completion") from None
        usage = data.get("usage") or {}
        completion = Completion(text=text, model=str(data.get("model") or self.model),
                                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                                completion_tokens=int(usage.get("completion_tokens") or 0))
        return completion, finish if isinstance(finish, str) else None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def build(name: str, settings: LLMSettings, http: httpx.AsyncClient | None = None) -> OpenAICompatProvider:
    """A provider from its preset and the settings (``openai_compat`` = ollama with ``LLM_MODEL``)."""
    s = settings
    if name == "gemini":
        key, base, model, timeout = s.GEMINI_API_KEY, s.GEMINI_BASE_URL, s.GEMINI_MODEL, s.LLM_TIMEOUT_S
    elif name == "groq":
        key, base, model, timeout = s.GROQ_API_KEY, s.GROQ_BASE_URL, s.GROQ_MODEL, s.LLM_TIMEOUT_S
    elif name == "openrouter":
        key, base, model, timeout = s.OPENROUTER_API_KEY, s.OPENROUTER_BASE_URL, s.OPENROUTER_MODEL, s.LLM_TIMEOUT_S
    elif name in ("ollama", "openai_compat"):
        model = s.OLLAMA_MODEL
        if name == "openai_compat" and s.LLM_MODEL and not s.LLM_MODEL.startswith("mock"):
            model = s.LLM_MODEL
        key, base, timeout = None, s.OLLAMA_URL, s.OLLAMA_TIMEOUT_S
        name = "ollama"
    else:
        raise ValueError(f"unknown LLM provider preset: {name}")
    gemini = name == "gemini"
    return OpenAICompatProvider(PRESETS[name], base_url=base, model=model,
                                api_key=key.get_secret_value() if key is not None else None,
                                timeout_s=timeout, seed=s.SEED, http=http,
                                reasoning_effort=s.GEMINI_REASONING_EFFORT.strip() if gemini else None,
                                min_output_tokens=s.GEMINI_MIN_OUTPUT_TOKENS if gemini else 0,
                                max_output_tokens=s.LLM_MAX_OUTPUT_TOKENS, ipv4=s.LLM_HTTP_IPV4)
