"""Recommendations against the database (``POST /api/v1/recommendations``).

- Candidates: every row of ``destinations`` (excluded ones too, so they appear in ``excluded[]`` with their reason),
  with their confirmed rules.
- Frequency caps: the ``publish_history`` per destination, the same history the publication gate counts.
- Past results: ``metrics_snapshots``, latest snapshot per (destination, post); engagement = likes + comments +
  shares + clicks over the keys the platform exposes. Certification test posts are left out. The table may be empty
  or not yet filled; any read error means "no past results" (neutral 0.5), never a failed request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import DestinationRow, MetricsSnapshotRow, PublicationRow, RuleSetRow, TestPostRow

from .profiles import ContentProfile, DestProfile
from .ranker import PastResults, Ranker, excluded_doc, normalise_weights, result_doc

log = structlog.get_logger("recommender")
MAX_CANDIDATES = 5000


def engagement(metrics: dict[str, Any] | None) -> int | None:
    parts = [(metrics or {}).get(k) for k in ("likes", "comments", "shares", "clicks")]
    known = [p for p in parts if isinstance(p, int) and not isinstance(p, bool) and p >= 0]
    return sum(known) if known else None


async def load_candidates(session: AsyncSession) -> list[DestProfile]:
    dests = (await session.execute(select(DestinationRow).order_by(DestinationRow.id).limit(MAX_CANDIDATES))).scalars().all()
    rules = (await session.execute(select(RuleSetRow).where(RuleSetRow.status == "confirmed"))).scalars().all()
    latest: dict[str, RuleSetRow] = {}
    for r in rules:
        if r.destination_id not in latest or r.version > latest[r.destination_id].version:
            latest[r.destination_id] = r
    return [DestProfile.from_db(d, latest[d.id].rules if d.id in latest else None) for d in dests]


async def load_past_results(session: AsyncSession) -> dict[str, PastResults]:
    try:
        async with session.begin_nested():
            snaps = (await session.execute(select(MetricsSnapshotRow).order_by(MetricsSnapshotRow.captured_at))).scalars().all()
            test_pubs = set((await session.execute(select(PublicationRow.id).where(PublicationRow.is_test.is_(True)))).scalars())
            test_posts = set((await session.execute(select(TestPostRow.platform_post_id))).scalars())
    except Exception as exc:  # noqa: BLE001 - past results are optional
        log.warning("recommender_past_results_unavailable", error=type(exc).__name__)
        return {}
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for s in snaps:  # ordered by time: the last one per post wins
        if not s.destination_id or s.publication_id in test_pubs or s.platform_post_id in test_posts:
            continue
        latest[(s.destination_id, s.platform_post_id)] = s.metrics or {}
    out: dict[str, PastResults] = {}
    for (dest_id, _post), metrics in latest.items():
        e = engagement(metrics)
        if e is None:
            continue
        stats = out.setdefault(dest_id, PastResults())
        stats.posts += 1
        stats.engagement_sum += e
    return out


async def load_history(session: AsyncSession, candidates: list[DestProfile]) -> dict[str, list[Any]]:
    from app.certification.publications import publish_history

    capped = [c.destination_id for c in candidates if c.frequency_cap is not None and c.destination_id]
    if not capped:
        return {}
    rows = (await session.execute(select(DestinationRow).where(DestinationRow.id.in_(capped)))).scalars().all()
    return {row.id: await publish_history(session, row) for row in rows}


async def recommend(session: AsyncSession, content: Any, *, content_id: str | None, k: int, now: datetime,
                    weights: dict[str, float] | None = None, automated_only: bool = False, publishable_only: bool = False,
                    phrase_with_llm: bool = False, llm: Any = None) -> dict[str, Any]:
    profile = ContentProfile.from_content(content, content_id=content_id)
    candidates = await load_candidates(session)
    w = normalise_weights(weights)
    if not candidates:
        top, excluded = [], []
    else:
        ranker = Ranker(candidates)
        top, excluded = ranker.rank(profile, k=k, weights=w, history=await load_history(session, candidates), now=now,
                                    past=await load_past_results(session), automated_only=automated_only,
                                    publishable_only=publishable_only)
    results = [result_doc(s, n) for n, s in enumerate(top, start=1)]
    phrasing: dict[str, Any] = {"used": False, "why": "not requested"}
    if phrase_with_llm:
        from .reasons import phrase_reasons

        phrasing = await phrase_reasons(results, llm=llm)
    return {
        "content_id": profile.content_id,
        "content": {"language": profile.language, "is_promotional": profile.is_promotional,
                    "promo_signals": profile.promo_signals, "ai_generated": profile.ai_generated,
                    "is_video": profile.has_video, "audiences": sorted(profile.category_strengths())},
        "k": k,
        "weights": w,
        "candidates_considered": len(candidates),
        "results": results,
        "excluded": [excluded_doc(s) for s in excluded],
        "reason_phrasing": phrasing,
        "ranking": "deterministic (hard filters, then the weighted factor score); an LLM never changes the order",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
    }
