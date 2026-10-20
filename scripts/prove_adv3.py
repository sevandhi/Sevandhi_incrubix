"""ADV3 proof: relevance 1-5 agreement with hand-checked labels, lexicon-only vs lexicon + LLM ±1.

    uv run python scripts/prove_adv3.py                       # lexicon only (+ LLM column "not run")
    LLM_PROVIDER=ollama OLLAMA_URL=http://127.0.0.1:11439/v1 uv run python scripts/prove_adv3.py --llm

Inputs: ``tests/fixtures/relevance/hand_labels_v1.csv`` (seed rows with a label 1-5 and the labeller) and
``catalogue/seeds/seed_v1.csv`` (name, category, audience tags of each row). Nothing is fetched from the web; the
LLM, when asked for, goes through the one adapter chain ``app.providers.llm`` (free providers only).

Writes ``evidence/levels/adv/ADV3_relevance.csv`` (one line per labelled row) and
``evidence/levels/adv/ADV3_relevance_summary.csv`` (exact agreement, within ±1, MAE, Spearman per method).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalogue import relevance as rel  # noqa: E402

LABELS = ROOT / "tests" / "fixtures" / "relevance" / "hand_labels_v1.csv"
SEED = ROOT / "catalogue" / "seeds" / "seed_v1.csv"
OUT = ROOT / "evidence" / "levels" / "adv"


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(cov / (va * vb), 3) if va and vb else None


def metrics(labels: list[int], scores: list[int | None]) -> dict[str, object]:
    pairs = [(lab, s) for lab, s in zip(labels, scores, strict=True) if s is not None]
    if not pairs:
        return {"n": 0, "exact": None, "within_1": None, "mae": None, "spearman": None}
    n = len(pairs)
    return {
        "n": n,
        "exact": round(sum(lab == s for lab, s in pairs) / n, 3),
        "within_1": round(sum(abs(lab - s) <= 1 for lab, s in pairs) / n, 3),
        "mae": round(sum(abs(lab - s) for lab, s in pairs) / n, 3),
        "spearman": spearman([p[0] for p in pairs], [p[1] for p in pairs]),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--llm", action="store_true", help="also run the LLM ±1 pass through app.providers.llm")
    args = parser.parse_args()
    seed = list(csv.DictReader(SEED.open(encoding="utf-8-sig")))
    labelled = list(csv.DictReader(LABELS.open(encoding="utf-8")))
    rows, labels, lex_scores, llm_scores = [], [], [], []
    providers: set[str] = set()
    for lab in labelled:
        src = seed[int(lab["seed_row_no"]) - 1]
        assert src["url"] == lab["url"], f"seed row {lab['seed_row_no']} moved"
        item = rel.RelevanceInput(name=src["name"], category=src["category"] or None,
                                  audience_tags=[t for t in src["audience_tags"].split(";") if t])
        lexicon = rel.lexicon_score(item)
        llm_result = await rel.score(item, use_llm=True) if args.llm else None
        llm_used = bool(llm_result and llm_result.llm.get("used"))
        if llm_used:
            providers.add(f"{llm_result.llm.get('provider')}:{llm_result.llm.get('model')}")
        label = int(lab["label"])
        labels.append(label)
        lex_scores.append(lexicon.score)
        llm_scores.append(llm_result.score if llm_used else None)
        rows.append({
            "seed_row_no": lab["seed_row_no"], "name": src["name"], "url": src["url"], "hand_label": label,
            "labeler": lab["labeler"], "lexicon_score": lexicon.score, "lexicon_reason": lexicon.reason,
            "lexicon_abs_err": abs(lexicon.score - label),
            "llm_score": llm_result.score if llm_used else "",
            "llm_adjustment": llm_result.llm.get("adjustment", "") if llm_used else "",
            "llm_reason": llm_result.reason if llm_used else "",
            "llm_abs_err": abs(llm_result.score - label) if llm_used else "",
            "llm_status": ("used" if llm_used else (llm_result.llm.get("why") or "lexicon only") if llm_result else "not run"),
        })
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "ADV3_relevance.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    generated = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    summary = [
        {"method": "lexicon", **metrics(labels, lex_scores), "provider": "deterministic lexicon"},
        {"method": "lexicon+llm", **metrics(labels, llm_scores),
         "provider": ", ".join(sorted(providers)) or ("not run" if not args.llm else "no provider answered")},
    ]
    # the same rows as the LLM method, scored by the lexicon, so the comparison is like for like
    both = [(lab, lx) for lab, lx, ll in zip(labels, lex_scores, llm_scores, strict=True) if ll is not None]
    if both and len(both) != len(labels):
        summary.append({"method": "lexicon (rows the LLM answered)", **metrics([b[0] for b in both], [b[1] for b in both]),
                        "provider": "deterministic lexicon"})
    with (OUT / "ADV3_relevance_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["generated_at", "labels_file", "labeler", "method", "n", "exact", "within_1",
                                                "mae", "spearman", "provider"], lineterminator="\n")
        writer.writeheader()
        for line in summary:
            writer.writerow({"generated_at": generated, "labels_file": str(LABELS.relative_to(ROOT)),
                             "labeler": labelled[0]["labeler"], **line})
    for line in summary:
        print(line)
    print(f"LLM_PROVIDER={os.environ.get('LLM_PROVIDER', '(settings default)')}; wrote {OUT / 'ADV3_relevance.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
