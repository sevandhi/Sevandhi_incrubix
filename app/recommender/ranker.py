"""Deterministic ranking: hard filters first (``filters.py``), then a weighted score with a reason per result.

    score = (w1·audience_fit + w2·relevance + w3·trust + w4·past_results + w5·rules_headroom) / (w1+…+w5)

Every factor is in [0, 1]:

| factor | how |
|---|---|
| ``audience_fit`` | TF-IDF cosine between the item and the destination's text (name, category, tags, rules text, notes) plus audience pseudo-tokens from the catalogue's multilingual lexicon, × 0.2 when the community's languages do not include the item's language |
| ``relevance`` | the destination's relevance score / 5 (computed by the lexicon when the row has none), × 0.2 on a language mismatch like audience_fit: an audience that cannot read the item is not a fit |
| ``trust`` | certification level: L0 0, L1 0.25, L2 0.5, L3 0.75, L4 1 |
| ``past_results`` | Bayesian-smoothed mean engagement per post (likes + comments + shares + clicks) from ``metrics_snapshots``: (Σe + m·μ₀)/(n + m) with m = 3 pseudo-posts at the network mean μ₀, mapped to s/(s + μ₀). No data = 0.5 (neutral) |
| ``rules_headroom`` | 1, times: 0.5 when no rule is known; 0.5 restricted promotion for a promotional item (0.7 if the promo rule is unknown); 0.9 AI disclosure to add; 0.8 assisted delivery; 0.5 + 0.5·(free slots / cap) under a frequency cap |

Ties are broken by the destination's stable key, so the same input always gives the same order. An LLM never
touches the ranking: it may only re-phrase the reason text afterwards (``reasons.py``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .filters import FilterOutcome, apply_filters
from .profiles import ContentProfile, DestProfile
from .text import TfidfIndex

FACTORS = ("audience_fit", "relevance", "trust", "past_results", "rules_headroom")
# Defaults written before tuning; eval.py tunes audience_fit/relevance/rules_headroom on the dev split only.
DEFAULT_WEIGHTS: dict[str, float] = {"audience_fit": 1.0, "relevance": 0.3, "trust": 0.1, "past_results": 0.1,
                                     "rules_headroom": 0.2}
TRUST = {"L0": 0.0, "L1": 0.25, "L2": 0.5, "L3": 0.75, "L4": 1.0}
LANGUAGE_MISMATCH = 0.2
PRIOR_POSTS = 3.0


@dataclass
class PastResults:
    posts: int = 0
    engagement_sum: float = 0.0


def past_results_score(stats: PastResults | None, network_mean: float) -> float:
    mu0 = network_mean if network_mean > 0 else 1.0
    if stats is None or stats.posts == 0:
        return 0.5
    smoothed = (stats.engagement_sum + PRIOR_POSTS * mu0) / (stats.posts + PRIOR_POSTS)
    return smoothed / (smoothed + mu0)


def network_mean(stats: dict[str, PastResults]) -> float:
    posts = sum(s.posts for s in stats.values())
    return (sum(s.engagement_sum for s in stats.values()) / posts) if posts else 0.0


@dataclass
class Scored:
    dest: DestProfile
    factors: dict[str, float]
    filter: FilterOutcome
    shared_terms: list[str] = field(default_factory=list)
    shared_audiences: list[str] = field(default_factory=list)
    headroom_notes: list[str] = field(default_factory=list)
    language_note: str | None = None
    past: PastResults | None = None
    score: float = 0.0
    contributions: dict[str, float] = field(default_factory=dict)
    reason: str = ""


def normalise_weights(weights: dict[str, float] | None) -> dict[str, float]:
    merged = {**DEFAULT_WEIGHTS, **(weights or {})}
    clean = {k: max(0.0, float(merged.get(k, 0.0))) for k in FACTORS}
    if sum(clean.values()) <= 0:
        raise ValueError("at least one weight must be positive")
    return clean


def combine(factors: dict[str, float], weights: dict[str, float]) -> tuple[float, dict[str, float]]:
    total = sum(weights.values())
    contributions = {k: weights[k] * factors[k] / total for k in FACTORS}
    return sum(contributions.values()), contributions


class Ranker:
    """Holds the TF-IDF index over a fixed set of candidate destinations."""

    def __init__(self, candidates: list[DestProfile]) -> None:
        self.candidates = candidates
        self._docs = [d.tfidf_tokens() for d in candidates]
        self.index = TfidfIndex(self._docs)
        self._cats = [d.category_strengths() for d in candidates]
        self._relevance = [d.relevance() for d in candidates]

    def factors(self, content: ContentProfile, *, history: dict[str, list[Any]] | None = None, now: datetime | None = None,
                past: dict[str, PastResults] | None = None, automated_only: bool = False,
                publishable_only: bool = False) -> list[Scored]:
        """Filter outcome and the five factors for every candidate (unsorted; weights not applied)."""
        query = self.index.vector(content.tfidf_tokens())
        content_cats = content.category_strengths()
        past = past or {}
        mu0 = network_mean(past)
        out: list[Scored] = []
        for i, dest in enumerate(self.candidates):
            key = dest.destination_id or dest.ref
            hist = (history or {}).get(key, []) if history is not None else None
            flt = apply_filters(content, dest, history=hist, now=now, automated_only=automated_only,
                                publishable_only=publishable_only)
            vec = self.index.doc_vectors[i]
            fit = TfidfIndex.cosine(query, vec)
            relevance = self._relevance[i] / 5.0
            language_note = None
            if content.language and dest.languages and "multi" not in dest.languages and \
                    content.language not in dest.languages:
                fit *= LANGUAGE_MISMATCH
                relevance *= LANGUAGE_MISMATCH
                language_note = f"language mismatch (community {'/'.join(dest.languages)}, item {content.language})"
            headroom, notes = self._headroom(content, dest, flt)
            stats = past.get(key)
            factors = {
                "audience_fit": round(min(1.0, fit), 6),
                "relevance": round(relevance, 6),
                "trust": TRUST.get(dest.level, 0.0),
                "past_results": round(past_results_score(stats, mu0), 6),
                "rules_headroom": round(headroom, 6),
            }
            out.append(Scored(dest=dest, factors=factors, filter=flt, shared_terms=TfidfIndex.shared_terms(query, vec),
                              shared_audiences=sorted(set(content_cats) & set(self._cats[i]),
                                                      key=lambda c: -content_cats[c]),
                              headroom_notes=notes, language_note=language_note, past=stats))
        return out

    @staticmethod
    def _headroom(content: ContentProfile, dest: DestProfile, flt: FilterOutcome) -> tuple[float, list[str]]:
        value, notes = 1.0, []
        if not dest.rules_known:
            value *= 0.5
            notes.append("no rules known yet")
        if content.is_promotional:
            if dest.self_promo == "restricted":
                value *= 0.5
                notes.append("self-promotion restricted (special channel or ask mods)")
            elif dest.self_promo is None:
                value *= 0.7
                notes.append("self-promotion rule unknown")
        if dest.ai_content == "disclose" and content.ai_generated is not False:
            value *= 0.9
            notes.append("AI disclosure will be added")
        if flt.delivery == "assisted":
            value *= 0.8
            notes.append("assisted delivery (a person posts)")
        cap = dest.frequency_cap
        if cap is not None and flt.cap_used is not None:
            free = max(0, cap.max_posts - flt.cap_used)
            value *= 0.5 + 0.5 * free / cap.max_posts
            notes.append(f"cap {flt.cap_used}/{cap.max_posts} per {cap.window} used")
        return value, notes

    def rank(self, content: ContentProfile, *, k: int = 10, weights: dict[str, float] | None = None,
             **kwargs: Any) -> tuple[list[Scored], list[Scored]]:
        """(top k results, excluded) — results sorted by score, excluded in catalogue order."""
        w = normalise_weights(weights)
        scored = self.factors(content, **kwargs)
        kept, excluded = [], []
        for s in scored:
            if s.filter.excluded:
                excluded.append(s)
                continue
            s.score, s.contributions = combine(s.factors, w)
            kept.append(s)
        kept.sort(key=lambda s: (-s.score, s.dest.ref))
        top = kept[:k]
        from .reasons import build_reason

        for s in top:
            s.reason = build_reason(s)
        return top, excluded


def order_by(scored: list[Scored], weights: dict[str, float]) -> list[str]:
    """Refs of the non-excluded candidates in ranked order (used by the evaluation's weight search)."""
    w = normalise_weights(weights)
    keyed = [(-combine(s.factors, w)[0], s.dest.ref) for s in scored if not s.filter.excluded]
    return [ref for _, ref in sorted(keyed)]


def result_doc(s: Scored, rank: int) -> dict[str, Any]:
    d = s.dest
    return {
        "rank": rank, "destination_id": d.destination_id, "destination_ref": d.ref, "name": d.name, "url": d.url,
        "connector_key": d.connector_key, "surface": d.surface, "level": d.level, "score": round(s.score, 4),
        "factors": {k: round(v, 4) for k, v in s.factors.items()},
        "contributions": {k: round(v, 4) for k, v in s.contributions.items()},
        "delivery": s.filter.delivery, "reason": s.reason,
        "notes": s.headroom_notes + s.filter.notes + ([s.language_note] if s.language_note else []),
        "past_results": asdict(s.past) if s.past else None,
    }


def excluded_doc(s: Scored) -> dict[str, Any]:
    d = s.dest
    out = {"destination_id": d.destination_id, "destination_ref": d.ref, "name": d.name, "code": s.filter.code,
           "reason": s.filter.reason}
    if s.filter.retry_at is not None:
        out["retry_at"] = s.filter.retry_at.isoformat().replace("+00:00", "Z")
    return out
