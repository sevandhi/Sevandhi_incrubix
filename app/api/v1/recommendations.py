"""Recommendations (OUT1): ``POST /api/v1/recommendations {content | content_id, k}`` → 200.

Given one content item, rank the catalogue's destinations by fit (audience, rules, past results) with a reason for
each. Hard filters run first (excluded destinations, format, self-promotion, AI-content bans, language, automation
when ``automated_only``, used-up frequency caps); the destinations they remove are listed in ``excluded[]`` with a
code, a reason and, for caps, ``retry_at``. The rest are scored by
``w1·audience_fit + w2·relevance + w3·trust + w4·past_results + w5·rules_headroom`` (weights normalised; the
defaults are ``DEFAULT_WEIGHTS``, the configuration reported as ``recommender_default`` in OUT1_precision.json; send
``weights`` to use the dev-tuned ones). Ranking is deterministic code. ``phrase_with_llm`` only rewrites the
reason text through the free LLM chain (mock fallback), and a rewrite that adds numbers is dropped. Read-only:
inline content is not stored.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.catalogue import service as catalogue
from app.catalogue.models import ContentIn
from app.db.session import SessionDep
from app.errors import ErrorCode, IbxError
from app.recommender.ranker import FACTORS
from app.recommender.service import recommend

router = APIRouter(tags=["recommendations"])


class RecommendationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_id: str | None = None
    content: ContentIn | None = Field(default=None, description="Inline content (not stored).")
    k: int = Field(default=10, ge=1, le=100)
    automated_only: bool = Field(default=False, description="Exclude destinations reachable only through assisted posting.")
    publishable_only: bool = Field(default=False, description="Exclude uncertified (L0) or unconfirmed-rules destinations.")
    phrase_with_llm: bool = Field(default=False, description="Rewrite reasons through the free LLM chain (never re-ranks).")
    weights: dict[str, float] | None = Field(default=None, description=f"Optional override of {', '.join(FACTORS)}.")

    @model_validator(mode="after")
    def _check(self) -> RecommendationIn:
        if (self.content_id is None) == (self.content is None):
            raise ValueError("send exactly one of content_id or content")
        if self.weights is not None:
            unknown = sorted(set(self.weights) - set(FACTORS))
            if unknown:
                raise ValueError(f"unknown weight(s): {', '.join(unknown)}")
            if any(v < 0 for v in self.weights.values()) or sum(self.weights.values()) <= 0:
                raise ValueError("weights must be >= 0 with a positive sum")
        return self


@router.post("/api/v1/recommendations", summary="Rank destinations for one content item, with a reason for each")
async def create_recommendation(body: RecommendationIn, request: Request, session: SessionDep) -> dict[str, Any]:
    if body.content is not None:
        content: Any = body.content.model_dump()
        content_id = None
    else:
        content = await catalogue.get_content(session, body.content_id or "")
        content_id = content.id
    try:
        return await recommend(session, content, content_id=content_id, k=body.k, now=request.app.state.clock.now(),
                               weights=body.weights, automated_only=body.automated_only,
                               publishable_only=body.publishable_only, phrase_with_llm=body.phrase_with_llm)
    except ValueError as exc:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, str(exc)) from None
