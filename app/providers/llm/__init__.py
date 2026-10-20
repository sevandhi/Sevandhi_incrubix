"""LLM adapters (free providers only) behind one call.

    from app.providers.llm import complete_json
    obj, meta = await complete_json(prompt, schema)          # schema: Pydantic model class or JSON-schema dict
    ctx.manifest_extra.setdefault("providers", []).append(meta.manifest_entry("relevance"))

Chain (``LLM_PROVIDER=auto``): gemini -> groq -> openrouter -> ollama -> mock. See ``chain.py`` for the rules
(strict JSON, one corrective retry, 429 back-off, fallback) and ``config.py`` for the settings.
"""

from app.providers.llm.base import LLMError, LLMMeta, LLMUnavailableError
from app.providers.llm.chain import build_chain, chain_names, complete_json, reset_cooldowns
from app.providers.llm.config import LLMSettings, get_llm_settings

__all__ = [
    "LLMError",
    "LLMMeta",
    "LLMSettings",
    "LLMUnavailableError",
    "build_chain",
    "chain_names",
    "complete_json",
    "get_llm_settings",
    "reset_cooldowns",
]
