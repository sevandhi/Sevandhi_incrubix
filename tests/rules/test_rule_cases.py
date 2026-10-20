"""Runs every case in rule_cases.yaml through the rules engine (table-driven, Must-have 4)."""

from pathlib import Path

import pytest

from app.rules.casefile import load_cases, run_case

CASES = load_cases(Path(__file__).with_name("rule_cases.yaml"))


def test_case_file_is_big_enough_and_covers_hard_cases():
    assert len(CASES) >= 60
    hard = {c.hard_case for c in CASES if c.hard_case}
    for needed in (
        "second post inside a weekly cap",
        "promotional post to a no-promotion community",
        "non-English community",
        "destination that requires AI-content disclosure",
        "non-English community that requires AI-content disclosure",
    ):
        assert needed in hard


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_rule_case(case):
    result = run_case(case)
    assert result.outcome != "missed", f"MISSED violation: expected {result.expected}, got {result.got}"
    assert result.ok, f"{result.outcome}: expected {result.expected}, got {result.got}; {result.problems}"
