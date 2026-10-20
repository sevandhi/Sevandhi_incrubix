"""Relevance 1-5 against IncuBrix's audience (ADV3): a weighted multilingual lexicon, plus an optional LLM ±1.

1. **Lexicon (always).** Six audience categories (AI, video, marketing, SaaS, creators, founders) with terms in
   English, Spanish, German, French, Portuguese, Italian, Dutch, Japanese, Chinese, Russian and Korean. Each field
   of the destination that mentions a category adds its weight: name 2, the page's own title 2, category 1.5,
   audience tags 1.5, page description 1. ``best`` = the strongest category:

   | best            | score | meaning                                            |
   |-----------------|-------|----------------------------------------------------|
   | 0               | 1 (2 with an adjacent signal such as tech/dev/design) | off-audience |
   | < 1.5           | 2     | only the page text mentions it                     |
   | < 3             | 3     | one catalogue field                                |
   | < 5             | 4     | two fields                                         |
   | >= 5            | 5     | the name and the catalogue fields agree            |

   Three or more categories with at least 1.5 each add +1 (max 5). This is a weighted lexicon, not TF-IDF: there is no
   corpus to learn document frequencies from, and every point of the score can be traced to a matched term.
   ``relevance_reason`` (<= 200 characters) lists the matched terms per field.
2. **LLM (optional).** Through the one adapter chain ``app.providers.llm`` (free providers only), a rubric pass
   may move the score by -1, 0 or +1 and writes a natural-language reason. The answer is a strict JSON schema; each
   piece of ``evidence`` must appear verbatim in the destination text we sent, otherwise the adjustment is dropped.
   The mock provider, a missing adapter, a timeout or an invalid answer all mean lexicon-only (``llm.used`` false),
   and the result says why.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

REASON_MAX = 200
FIELD_WEIGHTS = {"name": 2.0, "title": 2.0, "category": 1.5, "tags": 1.5, "description": 1.0}

AUDIENCE: dict[str, list[str]] = {
    "ai": [
        "ai", "a.i.", "artificial intelligence", "machine learning", "ml", "llm", "llms", "gpt", "chatgpt", "genai",
        "generative ai", "generative", "deep learning", "neural network", "neural networks", "ai agents", "ai agent",
        "prompt engineering", "stable diffusion", "midjourney", "openai", "nlp", "computer vision", "image generation",
        "video generation", "local ai", "gemini", "claude", "copilot", "langchain", "rag", "ai tools", "ai-tools",
        # es / pt / it / fr / de / nl
        "ia", "inteligencia artificial", "inteligência artificial", "intelligenza artificiale", "intelligence artificielle",
        "künstliche intelligenz", "kuenstliche intelligenz", "ki", "kunstmatige intelligentie", "aprendizaje automático",
        "apprentissage automatique", "maschinelles lernen",
        # ja / zh / ru / ko
        "人工知能", "機械学習", "生成ai", "人工智能", "机器学习", "生成式", "искусственный интеллект", "нейросет",
        "машинное обучение", "인공지능", "머신러닝",
    ],
    "video": [
        "video", "videos", "video editing", "youtube", "youtuber", "vlog", "vlogging", "film", "filmmaking", "filmmakers",
        "cinematography", "animation", "motion graphics", "streaming", "livestream", "twitch", "peertube", "vimeo",
        "video podcasts", "shorts", "premiere pro", "davinci resolve", "after effects", "video-generation",
        "vídeo", "vídeos", "edición de vídeo", "edición de video", "vidéo", "vidéos", "montage vidéo", "videobearbeitung",
        "videoschnitt", "edição de vídeo", "montaggio video", "videobewerking",
        "動画", "動画編集", "ビデオ", "视频", "视频剪辑", "видео", "видеомонтаж", "영상", "동영상",
    ],
    "marketing": [
        "marketing", "digital marketing", "content marketing", "online marketing", "seo", "growth", "growth hacking",
        "advertising", "ads", "social media", "email marketing", "copywriting", "branding", "promotion",
        "self promotion", "self-promotion", "devrel", "newsletter marketing", "publicidad", "mercadotecnia",
        "publicité", "werbung", "marketing digitale", "marketing digital",
        "マーケティング", "デジタルマーケティング", "营销", "数字营销", "маркетинг", "цифровой маркетинг", "마케팅",
    ],
    "saas": [
        "saas", "software as a service", "b2b", "no code", "no-code", "nocode", "low code", "low-code", "micro saas",
        "software buyers", "apps", "app builder", "automation", "productos saas", "logiciels saas", "saas-produkte",
        "produtos saas", "prodotti saas", "saas-producten", "saasプロダクト", "saas产品", "saas-продукты",
    ],
    "creators": [
        "creator", "creators", "content creator", "content creators", "content creation", "contentcreation",
        "influencer", "influencers", "podcast", "podcasts", "podcasters", "podcasting", "streamers", "creator economy",
        "makers", "artists", "illustrators", "musicians",
        "creadores", "creadores de contenido", "créateurs", "créateurs de contenu", "criadores", "criadores de conteúdo",
        "creatori di contenuti", "content-creator", "contentmakers",
        "クリエイター", "创作者", "内容创作者", "авторы контента", "блогеры", "크리에이터",
    ],
    "founders": [
        "founder", "founders", "startup", "startups", "start-up", "entrepreneur", "entrepreneurs", "entrepreneurship",
        "indie hackers", "indiehackers", "indie hacker", "bootstrapped", "bootstrapping", "solopreneur", "side project",
        "side projects", "launch", "product launch", "early adopters", "small business", "venture",
        "emprendedores", "emprendedor", "emprendimiento", "fundador", "fundadores", "fundadores de startups",
        "fondateurs", "fondateurs de startups", "gründer", "gruender", "fondatori di startup", "startup-oprichters",
        "起業", "起業家", "创业", "创业者", "стартап", "стартапы", "основатели стартапов", "창업", "스타트업",
    ],
}
ADJACENT = [
    "tech", "technology", "developers", "developer", "programming", "open source", "open-source", "design", "designers",
    "productivity", "tools", "data", "writing", "writers", "bloggers", "blogging", "publishers", "3d", "art", "music",
    "business", "learners", "news",
]


_HANDLES = re.compile(r"![\w.-]+@[\w.-]+|\bhttps?://\S+|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.I)


def _norm(text: str) -> str:
    # host names and Lemmy handles ("!blender@lemmy.ml", "videos.trom.tf") are not topic words
    text = _HANDLES.sub(" ", (text or "").lower())
    text = re.sub(r"[\-_/#;,|!@()\[\]{}:\"'.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _cjk(term: str) -> bool:
    return any(ord(ch) > 0x2E80 for ch in term)


def _compile(terms: list[str]) -> list[tuple[str, re.Pattern[str] | None]]:
    out = []
    for term in terms:
        normal = _norm(term)
        if not normal:
            continue
        pattern = None if _cjk(normal) else re.compile(r"(?<![\w])" + re.escape(normal) + r"(?![\w])")
        out.append((normal, pattern))
    return out


_COMPILED = {cat: _compile(terms) for cat, terms in AUDIENCE.items()}
_ADJACENT = _compile(ADJACENT)


def _matches(text: str, compiled: list[tuple[str, re.Pattern[str] | None]]) -> list[str]:
    hits: list[str] = []
    for normal, pattern in compiled:
        found = (normal in text) if pattern is None else bool(pattern.search(text))
        if found and normal not in hits:
            hits.append(normal)
    return hits


@dataclass
class RelevanceInput:
    name: str
    category: str | None = None
    audience_tags: list[str] = field(default_factory=list)
    description: str | None = None  # the page's meta description (from the import), if any
    url: str | None = None
    language: str | None = None
    title: str | None = None  # the page's <title> (from the import), if any

    def fields(self) -> dict[str, str]:
        return {"name": self.name or "", "title": self.title or "", "category": self.category or "",
                "tags": " ; ".join(self.audience_tags or []), "description": self.description or ""}

    def source_text(self) -> str:
        return "\n".join(f"{k}: {v}" for k, v in self.fields().items() if v)


@dataclass
class RelevanceResult:
    score: int
    reason: str
    method: str  # "lexicon" | "lexicon+llm"
    lexicon_score: int
    signals: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # category -> field -> terms
    llm: dict[str, Any] = field(default_factory=dict)


def _clip(text: str, limit: int = REASON_MAX) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def lexicon_score(item: RelevanceInput) -> RelevanceResult:
    """The deterministic base score and its reason (see the module docstring for the table)."""
    fields = {k: _norm(v) for k, v in item.fields().items()}
    signals: dict[str, dict[str, list[str]]] = {}
    strength: dict[str, float] = {}
    for cat, compiled in _COMPILED.items():
        for fname, text in fields.items():
            if not text:
                continue
            hits = _matches(text, compiled)
            if hits:
                signals.setdefault(cat, {})[fname] = hits[:3]
                strength[cat] = strength.get(cat, 0.0) + FIELD_WEIGHTS[fname]
    adjacent = sorted({t for text in fields.values() if text for t in _matches(text, _ADJACENT)})
    best = max(strength.values(), default=0.0)
    breadth = sum(1 for s in strength.values() if s >= 1.5)
    if best == 0:
        score = 2 if adjacent else 1
    elif best < 1.5:
        score = 2
    elif best < 3:
        score = 3
    elif best < 5:
        score = 4
    else:
        score = 5
    if breadth >= 3 and score < 5:
        score += 1
    if signals:
        ranked = sorted(signals, key=lambda c: -strength[c])
        parts = []
        for cat in ranked:
            where = "; ".join(f"{f}: {', '.join(t)}" for f, t in signals[cat].items())
            parts.append(f"{cat} ({where})")
        reason = f"{score}/5 lexicon: " + " | ".join(parts)
    elif adjacent:
        reason = f"{score}/5 lexicon: no audience term; adjacent only ({', '.join(adjacent[:4])})"
    else:
        reason = f"{score}/5 lexicon: no AI/video/marketing/SaaS/creators/founders term found"
    return RelevanceResult(score=score, reason=_clip(reason), method="lexicon", lexicon_score=score, signals=signals)


# ------------------------------------------------------------------ the optional LLM pass


class LlmRelevance(BaseModel):
    """The strict JSON the LLM must return."""

    adjustment: Literal[-1, 0, 1]
    reason: str = Field(min_length=1, max_length=400)
    evidence: list[str] = Field(default_factory=list, max_length=6)


RUBRIC = (
    "You rate how relevant an online community or directory is as a place to share content from IncuBrix, whose "
    "audience is AI, video, marketing, SaaS, creators and founders. A deterministic lexicon already gave a base score "
    "from 1 (off-audience) to 5 (core audience). You may move it by -1, 0 or +1 only. Typical reasons: a vendor support "
    "forum or a technical niche is less useful than the base suggests (-1); a strong audience fit the lexicon missed "
    "(+1). Use only the data below. Quote 1-3 short phrases from it, copied exactly, as evidence. Answer in JSON: "
    '{"adjustment": -1|0|1, "reason": "<= 200 characters, plain English", "evidence": ["..."]}.'
)


def _llm_fn() -> Any:
    try:
        from app.providers.llm import complete_json  # the LLM adapter chain
    except Exception:  # noqa: BLE001 - not built yet or broken: lexicon only
        return None
    return complete_json


def _verified(evidence: list[str], source: str) -> list[str]:
    haystack = re.sub(r"\s+", " ", source).lower()
    return [e for e in evidence if e and re.sub(r"\s+", " ", e).strip().lower() in haystack]


async def score(item: RelevanceInput, *, use_llm: bool = False, llm: Any = None) -> RelevanceResult:
    """Lexicon score, then (when ``use_llm``) the LLM ±1 pass. Never raises because of the LLM."""
    base = lexicon_score(item)
    if not use_llm:
        return base
    fn = llm or _llm_fn()
    if fn is None:
        base.llm = {"used": False, "why": "llm adapter not available"}
        return base
    source = item.source_text()
    prompt = f"{RUBRIC}\n\nBase score: {base.lexicon_score}\nLexicon reason: {base.reason}\n\nDestination:\n{source}"
    try:
        result = fn(prompt, LlmRelevance)
        if inspect.isawaitable(result):
            result = await result
        obj, meta = result if isinstance(result, tuple) else (result, {})
        answer = obj if isinstance(obj, LlmRelevance) else LlmRelevance.model_validate(
            obj.model_dump() if isinstance(obj, BaseModel) else obj)
    except (ValidationError, ValueError, TypeError) as exc:
        base.llm = {"used": False, "why": f"invalid llm answer ({type(exc).__name__})"}
        return base
    except Exception as exc:  # noqa: BLE001 - provider errors fall back to the lexicon (the chain already tried all)
        base.llm = {"used": False, "why": f"llm call failed ({type(exc).__name__})"}
        return base
    meta = meta if isinstance(meta, dict) else (meta.model_dump() if isinstance(meta, BaseModel) else dict(getattr(meta, "__dict__", {})))
    info = {k: meta.get(k) for k in ("provider", "model", "fallback_used", "prompt_tokens", "completion_tokens", "total_tokens",
                                     "latency_ms") if k in meta}
    if str(meta.get("provider", "")).lower() == "mock":
        base.llm = {"used": False, "why": "mock provider: lexicon only", **info}
        return base
    verified = _verified(answer.evidence, source)
    adjustment = answer.adjustment if verified or answer.adjustment == 0 else 0
    final = min(5, max(1, base.lexicon_score + adjustment))
    note = "" if adjustment == answer.adjustment else " (adjustment dropped: evidence not found in the data)"
    reason = _clip(f"{final}/5 (lexicon {base.lexicon_score}, LLM {adjustment:+d}): {answer.reason}{note}")
    return RelevanceResult(score=final, reason=reason, method="lexicon+llm", lexicon_score=base.lexicon_score,
                           signals=base.signals, llm={"used": True, "adjustment": adjustment, "proposed": answer.adjustment,
                                                      "evidence_verified": verified, **info})
