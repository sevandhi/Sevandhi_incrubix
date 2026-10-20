"""Rule-safety report: run tests/rules/rule_cases.yaml and write evidence/metrics/rule_safety.csv.

    uv run python scripts/rule_safety.py [--cases PATH] [--out PATH]

Columns: case, hard_case, expected, got, outcome (caught | missed | correct_allow | false_positive |
wrong_code | stricter). Exit code 1 if any violation was missed or any case did not match exactly.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.rules.casefile import load_cases, run_case  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", default=str(ROOT / "tests/rules/rule_cases.yaml"))
    parser.add_argument("--out", default=str(ROOT / "evidence/metrics/rule_safety.csv"))
    args = parser.parse_args()

    results = [run_case(case) for case in load_cases(args.cases)]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case", "hard_case", "expected", "got", "outcome", "problems"])
        for r in results:
            writer.writerow([r.case.id, r.case.hard_case or "", r.expected, r.got, r.outcome, "; ".join(r.problems)])

    counts = Counter(r.outcome for r in results)
    violations = sum(1 for r in results if r.case.expect["action"] != "allow")
    bad = [r for r in results if not r.ok]
    print(
        f"rule safety: {len(results)} cases, {violations} expected violations, "
        f"caught {counts['caught']}, missed {counts['missed']}, "
        f"correct allows {counts['correct_allow']}, false positives {counts['false_positive']}, "
        f"other mismatches {len(bad) - counts['missed'] - counts['false_positive']} -> {out}"
    )
    for r in bad:
        print(f"  {r.outcome.upper()}: {r.case.id} expected {r.expected} got {r.got} {r.problems}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
