"""Labelling CLI for the OUT1 recommender evaluation (rubric: ``docs/LABELLING_RUBRIC.md``).

    uv run python -m app.tools.label stats
    uv run python -m app.tools.label review --reviewer NAME [--item c04] [--split test] [--one-by-one] [--all]
    uv run python -m app.tools.label set c04 https://lemmy.ml/c/youtube 1 --reviewer NAME [--note "..."]
    uv run python -m app.tools.label pool [--no-tuned]      # add pooled pairs that have no row yet (label empty)

``review`` shows each content item, then its pooled candidates in pages of 10 (or one at a time) with the current
label and its note. In page mode: Enter accepts every row on the page; numbers flip rows ("2 7"); "y 3" / "n 3" set
rows; "s" skips the page; "q" saves and quits. Every accepted or flipped row becomes ``labeler=human:<NAME>`` with
``reviewed_at``; ``agent_label`` keeps the agent's draft. The file is written after every page, so stopping is safe.
After a session, ``python -m app.recommender.eval`` recomputes P@10 from the reviewed labels.

The review screen never shows which system retrieved a candidate or at what rank (that is in ``pool.csv``), so the
review stays blind to the systems being compared.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.recommender import data

PAGE = 10
Input = Callable[[str], str]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _yn(value: str) -> str:
    label = data.parse_label(value)
    return {1: "y", 0: "n"}.get(label, " ") if label is not None else " "


def _flip(value: str) -> str:
    return "0" if data.parse_label(value) == 1 else "1"


def _dest_line(row: dict[str, str], dest: Any) -> list[str]:
    if dest is None:
        return [row["destination_name"] or row["destination_ref"], f"      {row['destination_ref']}"]
    rules = []
    for name, value in (("promo", dest.self_promo), ("bots", dest.automation), ("ai", dest.ai_content)):
        rules.append(f"{name}={value or '?'}")
    head = (f"{dest.name} — {dest.connector_key}/{dest.surface} · {dest.category or '-'} · "
            f"{';'.join(dest.audience_tags) or '-'} · {'/'.join(dest.languages) or '?'} · {' '.join(rules)}")
    lines = [head, f"      {dest.url}"]
    extra = " ".join(p for p in (dest.rules_text, dest.notes) if p)
    if extra:
        lines.append("      rules/notes: " + textwrap.shorten(extra, 220, placeholder="…"))
    return lines


def _item_header(item: dict[str, Any]) -> str:
    flags = [item.get("language") or "?", item.get("content_type") or "text",
             "PROMOTIONAL" if item.get("is_promotional") else "not promotional",
             "AI-generated" if item.get("ai_generated") else "not AI-generated"]
    body = textwrap.shorten(" ".join(str(item.get("body") or "").split()), 420, placeholder="…")
    return (f"\n=== {item['id']} [{item.get('split')}, {item.get('group')}] ({', '.join(flags)})\n"
            f"    {item.get('title')}\n" + textwrap.indent(textwrap.fill(body, 110), "    "))


def select_rows(rows: list[dict[str, str]], *, item: str | None = None, split: str | None = None,
                include_reviewed: bool = False, items: dict[str, dict[str, Any]] | None = None) -> list[dict[str, str]]:
    out = []
    for r in rows:
        if item and r["item_id"] != item:
            continue
        if split and items and items.get(r["item_id"], {}).get("split") != split:
            continue
        if not include_reviewed and r["labeler"].startswith("human"):
            continue
        out.append(r)
    return out


def _mark(row: dict[str, str], label: str, reviewer: str) -> None:
    if not row["agent_label"] and row["labeler"] == data.AGENT:
        row["agent_label"] = row["label"]
    row["label"] = label
    row["labeler"] = f"human:{reviewer}"
    row["reviewed_at"] = _now()


def review(rows: list[dict[str, str]], todo: list[dict[str, str]], *, reviewer: str, items: dict[str, dict[str, Any]],
           dests: dict[str, Any], save: Callable[[], None], ask: Input = input, out: Callable[[str], None] = print,
           one_by_one: bool = False) -> dict[str, int]:
    """Interactive review loop (``ask``/``out`` are injectable for tests). Returns counts."""
    counts = {"accepted": 0, "flipped": 0, "set": 0, "skipped": 0}
    by_item: dict[str, list[dict[str, str]]] = {}
    for r in todo:
        by_item.setdefault(r["item_id"], []).append(r)
    for item_id in sorted(by_item):
        item = items.get(item_id, {"id": item_id})
        out(_item_header(item))
        group = by_item[item_id]
        pages = [group[i:i + (1 if one_by_one else PAGE)] for i in range(0, len(group), 1 if one_by_one else PAGE)]
        for page_no, page in enumerate(pages, start=1):
            out(f"\n  -- {item_id} page {page_no}/{len(pages)} ({len(group)} rows to review) --")
            for n, r in enumerate(page, start=1):
                lines = _dest_line(r, dests.get(r["destination_ref"]))
                out(f"  {n:>2}. [{_yn(r['label'])}] {lines[0]}")
                for line in lines[1:]:
                    out(line)
                if r["note"]:
                    out(f"      draft note: {r['note']}")
            pending = {n: r["label"] for n, r in enumerate(page, start=1)}
            while True:
                hint = ("Enter=accept · f=flip · y/n=set · s=skip · q=quit" if one_by_one else
                        'Enter=accept page · "2 7"=flip rows · "y 3"/"n 3"=set rows · s=skip · q=save+quit')
                answer = ask(f"  {hint} > ").strip().lower()
                if answer == "q":
                    save()
                    return counts
                if answer == "s":
                    counts["skipped"] += len(page)
                    break
                if one_by_one and answer in ("f", "y", "n"):
                    answer = {"f": "1", "y": "y 1", "n": "n 1"}[answer]
                if answer == "":
                    unlabelled = [n for n, v in pending.items() if data.parse_label(v) is None]
                    if unlabelled:
                        out(f"  rows {', '.join(map(str, unlabelled))} have no label yet: use y/n first")
                        continue
                    for n, r in enumerate(page, start=1):
                        new = pending[n]
                        if data.parse_label(new) == data.parse_label(r["label"]) and r["label"]:
                            counts["accepted"] += 1
                        elif r["label"] and data.parse_label(r["label"]) is not None:
                            counts["flipped"] += 1
                        else:
                            counts["set"] += 1
                        _mark(r, str(data.parse_label(new)), reviewer)
                    save()
                    break
                parts = answer.split()
                setting = parts[0] if parts and parts[0] in ("y", "n") else None
                numbers = parts[1:] if setting else parts
                try:
                    picked = [int(p) for p in numbers]
                except ValueError:
                    out("  not understood")
                    continue
                if not picked or any(p not in pending for p in picked):
                    out(f"  row numbers must be 1..{len(page)}")
                    continue
                for p in picked:
                    pending[p] = {"y": "1", "n": "0"}[setting] if setting else _flip(pending[p] or "0")
                out("  now: " + " ".join(f"{n}:{_yn(v)}" for n, v in pending.items()) + "  (Enter to confirm)")
    save()
    return counts


def pool(*, labels_path: Path = data.LABELS, pool_path: Path = data.POOL, use_tuned: bool = True,
         ctx: Any = None) -> dict[str, int]:
    from app.recommender import eval as ev

    ctx = ctx or ev.build_context()
    rows = data.read_labels(labels_path)
    judg = data.judgments(rows)
    tuned = ev.tune(ctx, judg)["weights"] if (use_tuned and judg) else None
    wanted = ev.pool_candidates(ctx, tuned)
    names = {d.ref: d.name for d in ctx.catalogue}
    have = {(r["item_id"], r["destination_ref"]) for r in rows}
    pool_rows = {(r["item_id"], r["destination_ref"]): r for r in data.read_pool(pool_path)}
    added = 0
    for (item_id, ref), info in sorted(wanted.items()):
        if (item_id, ref) not in have:
            rows.append({"item_id": item_id, "destination_ref": ref, "destination_name": names.get(ref, ""), "label": "",
                         "labeler": "", "agent_label": "", "note": "", "reviewed_at": "", "review_note": ""})
            added += 1
        existing = pool_rows.get((item_id, ref))
        systems = dict(sorted(info["systems"].items()))
        if existing is None:
            pool_rows[(item_id, ref)] = {"item_id": item_id, "destination_ref": ref, "round": str(info["round"]),
                                         "systems": ";".join(systems), "ranks": ";".join(f"{k}={v}" for k, v in systems.items())}
        else:  # keep the first round; add systems that retrieve it now
            old = dict(p.split("=", 1) for p in existing["ranks"].split(";") if "=" in p)
            for k, v in systems.items():
                old.setdefault(k, str(v))
            existing["systems"] = ";".join(sorted(old))
            existing["ranks"] = ";".join(f"{k}={v}" for k, v in sorted(old.items()))
    data.write_labels(rows, labels_path)
    data.write_pool(list(pool_rows.values()), pool_path)
    return {"added": added, "pool_pairs": len(pool_rows), "label_rows": len(rows), "tuned_used": int(tuned is not None)}


def stats(rows: list[dict[str, str]], items: dict[str, dict[str, Any]]) -> str:
    status = data.label_status(rows)
    lines = [f"labels: {status['status']}",
             f"rows {status['rows']} · judged {status['judged']} ({status['positives']} relevant) · "
             f"unjudged {status['unjudged_in_pool']} · agent-draft {status['by_labeler']['agent-draft']} · "
             f"human {status['by_labeler']['human']} ({status['human_reviewed_fraction']:.0%} reviewed)"]
    if status["agent_human_agreement"]:
        a = status["agent_human_agreement"]
        lines.append(f"agent vs human on {a['rows']} reviewed rows: {a['agree']} agree, {a['flipped']} flipped "
                     f"({a['rate']:.0%} agreement)")
    lines.append(f"\n{'item':<6}{'split':<6}{'group':<13}{'rows':>5}{'rel':>5}{'human':>7}{'todo':>6}")
    for item_id in sorted({r["item_id"] for r in rows}):
        mine = [r for r in rows if r["item_id"] == item_id]
        rel = sum(1 for r in mine if data.parse_label(r["label"]) == 1)
        human = sum(1 for r in mine if r["labeler"].startswith("human"))
        item = items.get(item_id, {})
        lines.append(f"{item_id:<6}{item.get('split', ''):<6}{item.get('group', ''):<13}{len(mine):>5}{rel:>5}{human:>7}"
                     f"{len(mine) - human:>6}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, ask: Input = input, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tools.label", description="Label (item, destination) pairs for OUT1.")
    parser.add_argument("--labels", type=Path, default=data.LABELS)
    parser.add_argument("--pool-file", type=Path, default=data.POOL)
    parser.add_argument("--items", type=Path, default=data.ITEMS)
    parser.add_argument("--catalogue", type=Path, default=data.CATALOGUE)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help="progress and agent/human agreement")
    rv = sub.add_parser("review", help="review rows page by page (accept / flip)")
    rv.add_argument("--reviewer", required=True, help="your name; rows become labeler=human:<name>")
    rv.add_argument("--item")
    rv.add_argument("--split", choices=["dev", "test"])
    rv.add_argument("--all", action="store_true", help="also show rows already reviewed by a person")
    rv.add_argument("--one-by-one", action="store_true")
    st = sub.add_parser("set", help="set one label")
    st.add_argument("item_id")
    st.add_argument("destination_ref")
    st.add_argument("label", choices=["1", "0", "y", "n"])
    st.add_argument("--reviewer", required=True)
    st.add_argument("--note", default="")
    pl = sub.add_parser("pool", help="add pooled pairs with no row yet (label empty)")
    pl.add_argument("--no-tuned", action="store_true", help="round 1 only (do not add the tuned recommender's top 10)")
    args = parser.parse_args(argv)

    items = {i["id"]: i for i in data.load_items(args.items)}
    if args.cmd == "pool":
        result = pool(labels_path=args.labels, pool_path=args.pool_file, use_tuned=not args.no_tuned)
        out(f"pool: {result['added']} new pair(s) added; {result['pool_pairs']} pooled pairs; "
            f"{result['label_rows']} label rows")
        return 0
    rows = data.read_labels(args.labels)
    if args.cmd == "stats":
        out(stats(rows, items))
        return 0
    if args.cmd == "set":
        reviewer = args.reviewer.strip()
        match = [r for r in rows if r["item_id"] == args.item_id and r["destination_ref"] == args.destination_ref]
        if not match:
            out("no such row (run `pool` first, or check the destination_ref)")
            return 2
        _mark(match[0], str(data.parse_label(args.label)), reviewer)
        if args.note:
            match[0]["review_note"] = args.note
        data.write_labels(rows, args.labels)
        out(f"{args.item_id} {args.destination_ref} -> {match[0]['label']} (human:{reviewer})")
        return 0
    reviewer = args.reviewer.strip()
    if not reviewer or ":" in reviewer:
        out("--reviewer must be a plain name")
        return 2
    dests = {d.ref: d for d in data.load_catalogue(args.catalogue)}
    todo = select_rows(rows, item=args.item, split=args.split, include_reviewed=args.all, items=items)
    if not todo:
        out("nothing to review")
        return 0
    counts = review(rows, todo, reviewer=reviewer, items=items, dests=dests,
                    save=lambda: data.write_labels(rows, args.labels), ask=ask, out=out, one_by_one=args.one_by_one)
    out(f"\nsaved: {counts['accepted']} accepted, {counts['flipped']} flipped, {counts['set']} set, "
        f"{counts['skipped']} skipped. Recompute P@10 with: uv run python -m app.recommender.eval --print")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nstopped (pages confirmed so far are saved)", file=sys.stderr)
        raise SystemExit(130) from None
