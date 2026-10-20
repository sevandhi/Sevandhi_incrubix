# ADR 0008: One free LLM adapter chain, strict JSON, verbatim-quote verification

Status: accepted (human direction: free LLMs only where they add value).

## Context
- Advanced asks for an AI step that proposes rules from a rules page, with a person confirming.
- LLMs invent rules, and free tiers rate-limit.
- The brief requires swappable providers, a mock adapter, and commercial licences.

## Decision
- **One adapter** (`app/providers/llm/`) speaks the OpenAI-compatible chat API.
- **The chain:** `LLM_PROVIDER=auto` tries Gemini free tier → Groq → OpenRouter `:free` → local Ollama
  (`qwen2.5:1.5b-instruct`, Apache-2.0) → the deterministic mock. A provider with no key is skipped.
  `qwen2.5:3b` is refused because its licence is non-commercial.
- **Every call:**
  - The answer must match a strict JSON schema (Pydantic). One corrective retry is allowed, then the chain falls
    through to the next provider.
  - A 429 or timeout also falls through, with a cooldown.
  - Provider, model, `fallback_used`, tokens and cost 0 are recorded in the manifest.
- **Quote verification:** a proposed value is kept only if its quote (at least 8 characters) appears **verbatim** in
  the fetched page text. Only whitespace, NFKC and curly quotes are normalised. Otherwise the conservative default is
  used and the field is flagged (`QUOTE_NOT_FOUND`, `SUSPICIOUS_QUOTE` for instructions aimed at AI readers).
- **Proposals are stored as `proposed`.** A person confirms them in `/ui/rules/{id}`. The LLM never posts.
- **The default is `mock`,** because the measured local model was worse than the deterministic extractor
  (0.50 vs 0.83 field accuracy).

## Consequences
- Tests need no keys.
- Hosted free tiers may use prompts to improve their products, so only public pages and our own content are sent
  (SOURCES.md).
- The value of a hosted model must be measured (`scripts/eval_rules_capture.py`) before `auto` becomes the default.
