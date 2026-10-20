"""The reason for each recommendation.

``build_reason`` is deterministic: it names the two factors that contributed most to the score, with the evidence
behind them (shared terms and audiences, the relevance score, the level, past results), then the caveats that matter
before posting (delivery route, rules unknown or restricted, disclosure, cap, language).

``phrase_reasons`` is optional. It asks the free LLM chain (``app.providers.llm.complete_json``) to rewrite those
reasons as one plain sentence each. The LLM sees only the factor facts we computed, never the ranking decision. A
rewrite is kept only if it passes ``_faithful``: same destination, no number that is not in the facts, ≤ 300
characters. The mock (the final fallback, and the provider in tests) returns the deterministic reasons unchanged.
The order and scores never change.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

REASON_MAX = 300

_LABELS = {
    "audience_fit": "audience fit",
    "relevance": "relevance",
    "trust": "trust",
    "past_results": "past results",
    "rules_headroom": "rules headroom",
}


def _factor_text(s: Any, name: str) -> str:
    f = s.factors[name]
    if name == "audience_fit":
        bits = []
        if s.shared_audiences:
            bits.append("audiences " + "/".join(s.shared_audiences[:3]))
        if s.shared_terms:
            bits.append("terms " + ", ".join(s.shared_terms[:4]))
        return f"audience fit {f:.2f}" + (f" ({'; '.join(bits)})" if bits else "")
    if name == "relevance":
        return f"relevance {round(f * 5)}/5 for IncuBrix's audience"
    if name == "trust":
        return f"certified {s.dest.level}"
    if name == "past_results":
        if s.past and s.past.posts:
            return f"past results {f:.2f} ({s.past.posts} post(s), {s.past.engagement_sum / s.past.posts:.1f} engagement/post)"
        return "no past results yet (neutral)"
    return f"rules headroom {f:.2f}"


def _clip(text: str, limit: int = REASON_MAX) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_reason(s: Any) -> str:
    top = sorted((n for n in s.contributions if s.contributions[n] > 0), key=lambda n: -s.contributions[n])[:2]
    head = "; ".join(_factor_text(s, n) for n in top) or "no positive factor"
    caveats: list[str] = []
    if s.language_note:
        caveats.append(s.language_note)
    caveats.extend(s.headroom_notes)
    caveats.extend(s.filter.notes)
    if s.dest.level == "L0" and "trust" not in top:
        caveats.append("not certified yet")
    text = head[0].upper() + head[1:]
    if caveats:
        text += ". Before posting: " + "; ".join(dict.fromkeys(caveats))
    return _clip(text + ".")


# ------------------------------------------------------------------ optional LLM phrasing


class PhrasedReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_ref: str = Field(min_length=1, max_length=600)
    reason: str = Field(min_length=1, max_length=REASON_MAX)


class PhrasedReasons(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reasons: list[PhrasedReason] = Field(max_length=50)


SYSTEM = (
    "You rewrite short machine-generated explanations of why a destination suits a piece of content. Rewrite each "
    "explanation as one plain English sentence (max 300 characters). Use only the facts given. Do not add numbers, "
    "claims or advice that are not in the facts. Do not change which destination it is about. Answer in JSON: "
    '{"reasons": [{"destination_ref": "...", "reason": "..."}]}.'
)


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _faithful(original: str, rewrite: str) -> bool:
    return len(rewrite) <= REASON_MAX and _numbers(rewrite) <= _numbers(original)


async def phrase_reasons(results: list[dict[str, Any]], *, llm: Any = None) -> dict[str, Any]:
    """Rewrite ``results[*]["reason"]`` in place when the LLM's rewrite is faithful. Returns the LLM record."""
    if not results:
        return {"used": False, "why": "no results"}
    facts = {r["destination_ref"]: r["reason"] for r in results}
    prompt = "Explanations to rewrite (destination_ref: explanation):\n" + "\n".join(
        f"- {ref}: {reason}" for ref, reason in facts.items())

    def mock(_prompt: str) -> dict[str, Any]:
        return {"reasons": [{"destination_ref": ref, "reason": reason} for ref, reason in facts.items()]}

    if llm is None:
        try:
            from app.providers.llm import complete_json as llm
        except Exception:  # noqa: BLE001 - adapter missing: keep the deterministic reasons
            return {"used": False, "why": "llm adapter not available"}
    try:
        obj, meta = await llm(prompt, PhrasedReasons, system=SYSTEM, mock=mock, max_tokens=1500)
    except Exception as exc:  # noqa: BLE001 - every provider failed: keep the deterministic reasons
        return {"used": False, "why": f"llm call failed ({type(exc).__name__})"}
    info = meta.model_dump() if hasattr(meta, "model_dump") else dict(getattr(meta, "__dict__", {}) or {})
    record = {k: info.get(k) for k in ("provider", "model", "fallback_used", "prompt_tokens", "completion_tokens",
                                       "total_tokens", "latency_ms") if k in info}
    answer = obj if isinstance(obj, PhrasedReasons) else PhrasedReasons.model_validate(obj)
    kept = rejected = 0
    by_ref = {r.destination_ref: r.reason for r in answer.reasons}
    for r in results:
        rewrite = by_ref.get(r["destination_ref"])
        if rewrite is None or rewrite == r["reason"]:
            continue
        if _faithful(r["reason"], rewrite):
            r["reason_facts"] = r["reason"]
            r["reason"] = _clip(rewrite)
            kept += 1
        else:
            rejected += 1
    return {"used": str(record.get("provider", "")).lower() != "mock", "rewritten": kept, "rejected_unfaithful": rejected,
            **record}
