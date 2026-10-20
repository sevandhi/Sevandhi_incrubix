"""Settings of the LLM adapter chain (free providers only).

They are read from the environment / ``.env`` like ``app.config.Settings`` but live here so the adapter owns
them. ``LLM_PROVIDER``, ``LLM_MODEL`` and ``OLLAMA_URL`` are the same variables ``app.config`` already lists.

    LLM_PROVIDER = auto | gemini | groq | openrouter | ollama | mock | openai_compat
        auto           try gemini -> groq -> openrouter -> ollama, then the deterministic mock
        <name>         try that provider, then the mock
        openai_compat  (older spelling) = ollama at OLLAMA_URL with LLM_MODEL
        mock           deterministic extractor only (tests, zero-key runs)

Every key is optional. A provider without its key is skipped, never called.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GROQ_OPENAI_BASE = "https://api.groq.com/openai/v1"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=True)

    LLM_PROVIDER: str = "mock"
    LLM_MODEL: str = "mock-heuristic-v1"  # model for LLM_PROVIDER=openai_compat (older spelling of ollama)
    LLM_TIMEOUT_S: float = 30.0  # per call to a hosted provider
    LLM_MAX_BACKOFF_S: float = 5.0  # a 429 asking to wait longer than this falls through to the next provider
    LLM_MAX_INPUT_CHARS: int = 12000  # page text sent to a provider (quotes are verified against the full text)
    LLM_COOLDOWN_S: float = 60.0  # skip a provider this long after it was unreachable or rate-limited
    SEED: int = 42
    LLM_HTTP_IPV4: bool = False  # bind the adapter's own HTTP client to IPv4 (hosts whose IPv6 route times out)
    LLM_MAX_OUTPUT_TOKENS: int = 16384  # ceiling when a cut-off answer (finish_reason=length) is retried with more

    # Google AI Studio (Gemini API) free tier, OpenAI-compatible endpoint. Use a key from a project WITHOUT billing.
    GEMINI_API_KEY: SecretStr | None = None
    GEMINI_BASE_URL: str = GEMINI_OPENAI_BASE
    GEMINI_MODEL: str = "gemini-3.8-flash"
    # Gemini 3.x models always "think", and the thinking tokens come out of max_tokens: a small budget gives HTTP 200
    # with finish_reason=length and no content. reasoning_effort (minimal|low|medium|high; empty = not sent) sets the
    # thinking level through the OpenAI-compatible endpoint; max_tokens is raised to at least GEMINI_MIN_OUTPUT_TOKENS.
    GEMINI_REASONING_EFFORT: str = "low"
    GEMINI_MIN_OUTPUT_TOKENS: int = 4096

    # Groq free plan.
    GROQ_API_KEY: SecretStr | None = None
    GROQ_BASE_URL: str = GROQ_OPENAI_BASE
    GROQ_MODEL: str = "openai/gpt-oss-20b"

    # OpenRouter: only ":free" model variants are accepted.
    OPENROUTER_API_KEY: SecretStr | None = None
    OPENROUTER_BASE_URL: str = OPENROUTER_BASE
    OPENROUTER_MODEL: str = "google/gemma-4-31b-it:free"

    # Local Ollama (compose profile "llm"); no key. Apache-2.0 model by default.
    OLLAMA_URL: str = "http://ollama:11434/v1"
    OLLAMA_MODEL: str = "qwen2.5:1.5b-instruct"
    OLLAMA_TIMEOUT_S: float = 240.0  # CPU inference is slow

    def secret_values(self) -> list[str]:
        keys = (self.GEMINI_API_KEY, self.GROQ_API_KEY, self.OPENROUTER_API_KEY)
        return [k.get_secret_value() for k in keys if k is not None and k.get_secret_value()]


@lru_cache(maxsize=1)
def get_llm_settings() -> LLMSettings:
    settings = LLMSettings()
    from app.reliability.redact import secret_registry

    secret_registry.add_many(settings.secret_values())  # keys never reach a log line
    return settings
