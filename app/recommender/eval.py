"""OUT1 evaluation: precision@10 of the recommender on a labelled set, with TREC-style pooling.

    uv run python -m app.recommender.eval            # recompute from the current labels (a few seconds)
    uv run python -m app.recommender.eval --print    # also print a summary table

Protocol (plan §7 OUT1, ``docs/LABELLING_RUBRIC.md``):

1. **Items:** 20 content items in ``catalogue/labels/content_items.yaml``, split 10 dev / 10 test before labelling.
2. **Candidates:** the real seed catalogue (175 destinations).
3. **Pool:** per item, the union of the top 20 from the recommender (default weights), BM25 only and relevance only
   (round 1), plus the tuned recommender's top 10 (round 2 and later). ``python -m app.tools.label pool`` adds missing
   pairs. Pairs outside the pool are unjudged and count as not relevant.
4. **Weights** are tuned on the **dev** items only: a grid over the audience_fit, relevance and rules_headroom
   weights. ``trust`` and ``past_results`` are constant in this offline set (no destination is certified, and no
   metrics exist for the seed rows), so they cannot change the order and stay at their defaults. Ties between grid
   points go to the point closest to the defaults.
5. **P@10 is reported once on the test items** with the tuned weights, next to the default weights, BM25 only,
   relevance only and the expected value of a random ranking.

Labels are read fresh on every run, so a person's review changes the numbers immediately. Until every judged row is
human-reviewed, the report says "agent-drafted labels, pending human review".
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import data
from .profiles import ContentProfile, DestProfile
from .ranker import DEFAULT_WEIGHTS, Ranker, Scored, order_by
from .text import Bm25Index

K = 10
POOL_DEPTH = 20
GRID = {
    "audience_fit": [0.5, 1.0, 1.5, 2.0, 3.0],
    "relevance": [0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0],
    "rules_headroom": [0.0, 0.1, 0.2, 0.3, 0.5],
}
SYSTEMS = ("recommender_tuned", "recommender_default", "bm25_only", "relevance_only")


@dataclass
class EvalContext:
    items: list[dict[str, Any]]
    catalogue: list[DestProfile]
    contents: dict[str, ContentProfile]
    scored: dict[str, list[Scored]]
    bm25: dict[str, list[str]]
    relevance: list[str]

    @property
    def n(self) -> int:
        return len(self.catalogue)

    def ids(self, split: str | None = None) -> list[str]:
        return [i["id"] for i in self.items if split is None or i.get("split") == split]

    def item(self, item_id: str) -> dict[str, Any]:
        return next(i for i in self.items if i["id"] == item_id)


def build_context(items: list[dict[str, Any]] | None = None, catalogue: list[DestProfile] | None = None) -> EvalContext:
    items = items if items is not None else data.load_items()
    catalogue = catalogue if catalogue is not None else data.load_catalogue()
    ranker = Ranker(catalogue)
    bm25 = Bm25Index([d.doc_tokens() for d in catalogue])
    contents, scored, bm25_rank = {}, {}, {}
    for item in items:
        content = ContentProfile.from_content(item, content_id=item["id"])
        contents[item["id"]] = content
        scored[item["id"]] = ranker.factors(content)
        scores = bm25.scores(content.tokens())
        ranked = sorted(((-s, d.ref) for s, d in zip(scores, catalogue, strict=True) if s > 0))
        bm25_rank[item["id"]] = [ref for _, ref in ranked]
    rel = sorted(catalogue, key=lambda d: (-d.relevance(), -(d.est_audience or 0), d.ref))
    return EvalContext(items, catalogue, contents, scored, bm25_rank, [d.ref for d in rel])


def ranking(ctx: EvalContext, system: str, item_id: str, weights: dict[str, float] | None = None) -> list[str]:
    if system in ("recommender", "recommender_tuned", "recommender_default"):
        return order_by(ctx.scored[item_id], weights or DEFAULT_WEIGHTS)
    if system == "bm25_only":
        return ctx.bm25[item_id]
    if system == "relevance_only":
        return ctx.relevance
    raise ValueError(system)


def precision(refs: list[str], item_id: str, judg: dict[tuple[str, str], int], k: int = K) -> tuple[float, float]:
    """(P@k, judged@k); unjudged and missing slots count as not relevant."""
    top = refs[:k]
    rel = sum(judg.get((item_id, r), 0) for r in top)
    judged = sum(1 for r in top if (item_id, r) in judg)
    return rel / k, judged / k


def mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def tune(ctx: EvalContext, judg: dict[tuple[str, str], int]) -> dict[str, Any]:
    dev = ctx.ids("dev")
    best: tuple[Any, ...] | None = None
    grid_points = list(itertools.product(*GRID.values()))
    for point in grid_points:
        weights = {**DEFAULT_WEIGHTS, **dict(zip(GRID.keys(), point, strict=True))}
        p = mean([precision(ranking(ctx, "recommender", i, weights), i, judg)[0] for i in dev])
        distance = sum(abs(weights[k] - DEFAULT_WEIGHTS[k]) for k in GRID)
        key = (-p, round(distance, 6), point)
        if best is None or key < best[0]:
            best = (key, weights, p)
    assert best is not None
    ties = sum(1 for point in grid_points if mean([precision(ranking(
        ctx, "recommender", i, {**DEFAULT_WEIGHTS, **dict(zip(GRID.keys(), point, strict=True))}), i, judg)[0]
        for i in dev]) == best[2])
    return {"weights": best[1], "dev_p_at_10": best[2], "grid": GRID, "grid_size": len(grid_points),
            "grid_points_with_best_dev_score": ties}


def pool_candidates(ctx: EvalContext, tuned: dict[str, float] | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """Pairs each system puts in its pooled depth: round 1 = top 20 of three systems; round 2 = tuned top 10."""
    out: dict[tuple[str, str], dict[str, Any]] = {}

    def add(item_id: str, refs: list[str], system: str, round_no: int) -> None:
        for rank, ref in enumerate(refs, start=1):
            entry = out.setdefault((item_id, ref), {"round": round_no, "systems": {}})
            entry["systems"][system] = rank
            entry["round"] = min(entry["round"], round_no)

    for item_id in ctx.ids():
        add(item_id, ranking(ctx, "recommender_default", item_id)[:POOL_DEPTH], "recommender_default", 1)
        add(item_id, ranking(ctx, "bm25_only", item_id)[:POOL_DEPTH], "bm25_only", 1)
        add(item_id, ranking(ctx, "relevance_only", item_id)[:POOL_DEPTH], "relevance_only", 1)
        if tuned is not None:
            add(item_id, ranking(ctx, "recommender_tuned", item_id, tuned)[:K], "recommender_tuned", 2)
    return out


def _system_block(ctx: EvalContext, ids: list[str], judg: dict[tuple[str, str], int],
                  weights: dict[str, dict[str, float]]) -> dict[str, Any]:
    block: dict[str, Any] = {}
    for system in SYSTEMS:
        ps, js = [], []
        for i in ids:
            p, j = precision(ranking(ctx, system, i, weights.get(system)), i, judg)
            ps.append(p)
            js.append(j)
        block[system] = {"p_at_10": mean(ps), "judged_at_10": mean(js)}
    rel_counts = [sum(v for (it, _), v in judg.items() if it == i) for i in ids]
    block["random_expected"] = {"p_at_10": mean([r / ctx.n for r in rel_counts]),
                                "judged_at_10": mean([sum(1 for (it, _) in judg if it == i) / ctx.n for i in ids]),
                                "how": "expected P@10 of a uniformly random order of all candidates = judged-relevant / candidates"}
    return block


def evaluate(*, items_path: Path = data.ITEMS, catalogue_path: Path = data.CATALOGUE, labels_path: Path = data.LABELS,
             pool_path: Path = data.POOL, ctx: EvalContext | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = ctx or build_context(data.load_items(items_path), data.load_catalogue(catalogue_path))
    rows = data.read_labels(labels_path)
    judg = data.judgments(rows)
    status = data.label_status(rows)
    tuned = tune(ctx, judg)
    weights = {"recommender_tuned": tuned["weights"], "recommender_default": DEFAULT_WEIGHTS}
    test_ids, dev_ids = ctx.ids("test"), ctx.ids("dev")
    test = _system_block(ctx, test_ids, judg, weights)
    dev = _system_block(ctx, dev_ids, judg, weights)
    groups: dict[str, Any] = {}
    for group in sorted({ctx.item(i).get("group", "") for i in test_ids}):
        ids = [i for i in test_ids if ctx.item(i).get("group") == group]
        groups[group] = {"items": ids, **{s: v["p_at_10"] for s, v in _system_block(ctx, ids, judg, weights).items()}}

    names = {d.ref: d.name for d in ctx.catalogue}
    per_item = []
    for i in ctx.ids():
        item = ctx.item(i)
        entry: dict[str, Any] = {
            "item_id": i, "split": item.get("split"), "group": item.get("group"), "title": item.get("title"),
            "language": ctx.contents[i].language, "judged": sum(1 for (it, _) in judg if it == i),
            "judged_relevant": sum(v for (it, _), v in judg.items() if it == i), "systems": {},
        }
        for system in SYSTEMS:
            refs = ranking(ctx, system, i, weights.get(system))
            p, j = precision(refs, i, judg)
            entry["systems"][system] = {"p_at_10": p, "judged_at_10": j}
        top = ranking(ctx, "recommender_tuned", i, tuned["weights"])[:K]
        entry["recommender_tuned_top10"] = [{"rank": n, "name": names[r], "destination_ref": r, "label": judg.get((i, r))}
                                            for n, r in enumerate(top, start=1)]
        excluded = [s for s in ctx.scored[i] if s.filter.excluded]
        entry["hard_filtered"] = len(excluded)
        per_item.append(entry)

    pool_rows = data.read_pool(pool_path)
    rounds: dict[str, int] = {}
    for r in pool_rows:
        rounds[r["round"]] = rounds.get(r["round"], 0) + 1
    missing_tuned = sum(1 for i in ctx.ids() for r in ranking(ctx, "recommender_tuned", i, tuned["weights"])[:K]
                        if (i, r) not in judg)
    t = test["recommender_tuned"]["p_at_10"]
    report = {
        "metric": "precision@10 (P@10), test split",
        "label_status": status["status"],
        "claim": f"P@10 = {t:.2f} on the 10 test items ({status['status']})",
        "test": test,
        "test_by_group": groups,
        "dev": dev,
        "weights": {"default": DEFAULT_WEIGHTS, "tuned": tuned["weights"], "tuned_on": "dev split only",
                    "dev_p_at_10_tuned": tuned["dev_p_at_10"], "dev_p_at_10_default": dev["recommender_default"]["p_at_10"],
                    "grid": tuned["grid"], "grid_size": tuned["grid_size"],
                    "grid_points_with_best_dev_score": tuned["grid_points_with_best_dev_score"],
                    "fixed": {"trust": "constant here (every seed row is L0)", "past_results": "constant here (no metrics for seed rows)"}},
        "labels": status,
        "pooling": {
            "method": f"TREC-style: union of the top {POOL_DEPTH} from recommender_default, bm25_only and relevance_only "
                      f"(round 1), plus the tuned recommender's top {K} (round 2+)",
            "pool_pairs": len(pool_rows), "pool_pairs_by_round": rounds,
            "pool_per_item_mean": round(len(pool_rows) / max(1, len(ctx.ids())), 1),
            "tuned_top10_unjudged_pairs": missing_tuned,
            "bias_disclosure": (
                "Unjudged pairs count as not relevant. At depth 10 the pooled systems are judged to "
                f"{min(test[s]['judged_at_10'] for s in SYSTEMS):.0%} (lowest judged@10 on test), so their P@10 is what "
                "full judgement of their top 10 gives. The random "
                "baseline was not pooled: most of its picks are unjudged and score 0, so its value is a lower bound and "
                "the gap to it is overstated. A destination no pooled system ranked in its top 20 was never judged; if "
                "it is relevant, recall is lower than the pool suggests, but P@10 of the pooled systems is unaffected. "
                "The rubric, the content items, the recommender and the draft labels were all written by the same "
                "agent; the format and rules tests in the rubric are also hard filters in the recommender, which favours "
                "it over the baselines. A human review of the labels is needed before this number is claimed."),
        },
        "candidates": {"source": str(catalogue_path.relative_to(data.ROOT)) if catalogue_path.is_relative_to(data.ROOT)
                       else str(catalogue_path), "count": ctx.n},
        "items": {"count": len(ctx.items), "dev": dev_ids, "test": test_ids},
        "per_item": per_item,
        "inputs_sha256": {"labels.csv": data.sha256_file(labels_path), "content_items.yaml": data.sha256_file(items_path),
                          "catalogue": data.sha256_file(catalogue_path), "pool.csv": data.sha256_file(pool_path)},
        "command": "uv run python -m app.recommender.eval",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "runtime_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    return report


def summary(report: dict[str, Any]) -> str:
    lines = [f"OUT1 {report['metric']}: {report['claim']}", "",
             f"{'system':<22}{'test P@10':>10}{'judged@10':>11}{'dev P@10':>10}"]
    for system in (*SYSTEMS, "random_expected"):
        t, d = report["test"][system], report["dev"][system]
        lines.append(f"{system:<22}{t['p_at_10']:>10.3f}{t['judged_at_10']:>11.2f}{d['p_at_10']:>10.3f}")
    w = report["weights"]["tuned"]
    lines += ["", "tuned weights (dev only): " + ", ".join(f"{k}={v}" for k, v in w.items()),
              f"labels: {report['labels']['judged']} judged ({report['labels']['positives']} relevant); "
              f"{report['label_status']}",
              f"tuned top-10 pairs still unjudged: {report['pooling']['tuned_top10_unjudged_pairs']}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.recommender.eval", description=__doc__.split("\n")[0])
    parser.add_argument("--labels", type=Path, default=data.LABELS)
    parser.add_argument("--items", type=Path, default=data.ITEMS)
    parser.add_argument("--catalogue", type=Path, default=data.CATALOGUE)
    parser.add_argument("--pool", type=Path, default=data.POOL)
    parser.add_argument("--out", type=Path, default=data.OUT)
    parser.add_argument("--no-write", action="store_true", help="do not write the evidence JSON")
    parser.add_argument("--print", dest="show", action="store_true", help="print the summary table")
    args = parser.parse_args(argv)
    report = evaluate(items_path=args.items, catalogue_path=args.catalogue, labels_path=args.labels, pool_path=args.pool)
    if not args.no_write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(summary(report) if args.show else report["claim"])
    if not args.no_write:
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
