"""Files of the OUT1 evaluation: the candidate catalogue, the content items, the labels and the pool.

``catalogue/labels/labels.csv`` (one row per judged (item, destination) pair):

| column | meaning |
|---|---|
| item_id | content item id from ``content_items.yaml`` |
| destination_ref | the destination's normalised URL (stable across catalogue versions) |
| destination_name | for reading only |
| label | ``1`` relevant, ``0`` not relevant, empty = not judged yet |
| labeler | ``agent-draft`` (AI-drafted, NOT a human label) or ``human:<name>`` |
| agent_label | the agent's draft, kept after a human review so agreement can be measured |
| note | the agent's short justification |
| reviewed_at | when a person accepted or flipped the row (UTC) |
| review_note | optional note from the person |

``catalogue/labels/pool.csv`` (provenance, kept apart so that labelling is blind to it): item_id, destination_ref,
round, systems (which systems retrieved it), ranks.
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Any

import yaml

from .profiles import DestProfile

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "catalogue" / "seeds" / "seed_v1.csv"
ITEMS = ROOT / "catalogue" / "labels" / "content_items.yaml"
LABELS = ROOT / "catalogue" / "labels" / "labels.csv"
POOL = ROOT / "catalogue" / "labels" / "pool.csv"
OUT = ROOT / "evidence" / "levels" / "out" / "OUT1_precision.json"

LABEL_FIELDS = ["item_id", "destination_ref", "destination_name", "label", "labeler", "agent_label", "note",
                "reviewed_at", "review_note"]
POOL_FIELDS = ["item_id", "destination_ref", "round", "systems", "ranks"]
AGENT = "agent-draft"


def sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def load_catalogue(path: Path = CATALOGUE) -> list[DestProfile]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    profiles = [DestProfile.from_seed_row(r) for r in rows if (r.get("url") or "").strip()]
    seen: set[str] = set()
    unique = []
    for p in profiles:  # the evaluation needs one profile per key
        if p.ref not in seen:
            seen.add(p.ref)
            unique.append(p)
    return unique


def load_items(path: Path = ITEMS) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = data["items"] if isinstance(data, dict) else data
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate item ids in content_items.yaml")
    return items


def read_csv(path: Path, fields: list[str]) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return [{f: (row.get(f) or "") for f in fields} for row in csv.DictReader(fh)]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    """Atomic write (temp file + rename), sorted by item then destination name for stable diffs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["item_id"], str(r.get("destination_name", "")).casefold(), r["destination_ref"]))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({f: row.get(f, "") for f in fields})
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    tmp.replace(path)


def read_labels(path: Path = LABELS) -> list[dict[str, str]]:
    return read_csv(path, LABEL_FIELDS)


def write_labels(rows: list[dict[str, Any]], path: Path = LABELS) -> None:
    write_csv(path, rows, LABEL_FIELDS)


def read_pool(path: Path = POOL) -> list[dict[str, str]]:
    return read_csv(path, POOL_FIELDS)


def write_pool(rows: list[dict[str, Any]], path: Path = POOL) -> None:
    # the pool has no destination_name column; sort by ref
    write_csv(path, [{**r, "destination_name": r["destination_ref"]} for r in rows], POOL_FIELDS)


def parse_label(value: str) -> int | None:
    v = (value or "").strip().lower()
    if v in ("1", "y", "yes", "true"):
        return 1
    if v in ("0", "n", "no", "false"):
        return 0
    return None


def judgments(rows: list[dict[str, str]]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for r in rows:
        label = parse_label(r["label"])
        if label is not None:
            out[(r["item_id"], r["destination_ref"])] = label
    return out


def label_status(rows: list[dict[str, str]]) -> dict[str, Any]:
    judged = [r for r in rows if parse_label(r["label"]) is not None]
    human = [r for r in judged if r["labeler"].startswith("human")]
    agent = [r for r in judged if r["labeler"] == AGENT]
    other = len(judged) - len(human) - len(agent)
    both = [r for r in human if parse_label(r["agent_label"]) is not None]
    agree = sum(1 for r in both if parse_label(r["agent_label"]) == parse_label(r["label"]))
    if not judged:
        status = "no labels"
    elif len(human) == len(judged):
        status = "human-reviewed labels"
    elif human:
        status = f"partially human-reviewed: {len(human)}/{len(judged)} rows reviewed; the rest are agent-drafted labels, pending human review"
    else:
        status = "agent-drafted labels, pending human review"
    return {
        "status": status, "rows": len(rows), "judged": len(judged), "unjudged_in_pool": len(rows) - len(judged),
        "by_labeler": {"agent-draft": len(agent), "human": len(human), "other": other},
        "human_reviewed_fraction": round(len(human) / len(judged), 4) if judged else 0.0,
        "agent_human_agreement": ({"rows": len(both), "agree": agree, "flipped": len(both) - agree,
                                   "rate": round(agree / len(both), 4)} if both else None),
        "positives": sum(1 for r in judged if parse_label(r["label"]) == 1),
    }
