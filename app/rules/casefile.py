"""Table-driven rule cases (tests/rules/rule_cases.yaml), shared by the pytest suite and
scripts/rule_safety.py so both judge the engine the same way.

A case gives rules, history, a rendition and the expected decision:

    - id: cap_second_post_inside_week
      hard_case: second post inside a weekly cap
      rules: {frequency_cap: 1/P7D}          # null = no confirmed rules; "conservative_default" also works
      history: [{ago: P2D}]                  # or {at: 2026-10-18T09:00:00Z, status: published, is_promotional: false}
      rendition: {body: "..."}               # merged over the file's defaults.rendition
      owner_domains: [creator.dev]
      expect: {action: reschedule, code: DEST_RULE_FREQUENCY_CAP, retry_at: 2026-10-25T09:00:00Z}

Optional expect keys: reason_contains, message_contains (the contract error message), evidence_types, warnings_contain, fix_lang, fix_text_contains, no_fix.

Outcome per case: caught | missed | correct_allow | false_positive | wrong_code | stricter.
"missed" means the engine was more lenient than expected (a violation would have gone out).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.errors import ErrorCode
from app.rules.durations import parse_duration
from app.rules.engine import evaluate
from app.rules.models import Decision, RuleSet, as_utc

SEVERITY = {"allow": 0, "allow_with_changes": 1, "reschedule": 2, "block": 3}


@dataclass
class RuleCase:
    id: str
    expect: dict[str, Any]
    rules: Any = None
    history: list[dict[str, Any]] = field(default_factory=list)
    rendition: dict[str, Any] = field(default_factory=dict)
    owner_domains: list[str] = field(default_factory=list)
    now: datetime | None = None
    hard_case: str | None = None
    description: str | None = None


@dataclass
class CaseResult:
    case: RuleCase
    decision: Decision
    outcome: str
    problems: list[str]

    @property
    def ok(self) -> bool:
        return self.outcome in ("caught", "correct_allow") and not self.problems

    @property
    def expected(self) -> str:
        return _label(self.case.expect.get("action"), self.case.expect.get("code"))

    @property
    def got(self) -> str:
        return _label(self.decision.action, self.decision.code.value if self.decision.code else None)


def _label(action: str | None, code: str | None) -> str:
    return f"{action}:{code}" if code else str(action)


def _parse_time(value: Any) -> datetime:
    return as_utc(value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def load_cases(path: str | Path) -> list[RuleCase]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    default_now = _parse_time(defaults.get("now", "2026-10-20T09:00:00Z"))
    cases = []
    for raw in data["cases"]:
        raw = dict(raw)
        raw["rendition"] = {**defaults.get("rendition", {}), **(raw.get("rendition") or {})}
        raw["now"] = _parse_time(raw["now"]) if raw.get("now") else default_now
        cases.append(RuleCase(**raw))
    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate case ids: {sorted(duplicates)}")
    return cases


def _history(case: RuleCase) -> list[dict[str, Any]]:
    events = []
    for item in case.history:
        item = dict(item)
        if "ago" in item:
            item["published_at"] = case.now - parse_duration(item.pop("ago"))
        elif "in" in item:
            item["published_at"] = case.now + parse_duration(item.pop("in"))
        repeat = int(item.pop("repeat", 1))
        events += [dict(item) for _ in range(repeat)]
    return events


def _ruleset(case: RuleCase) -> RuleSet | None:
    if case.rules is None:
        return None
    if isinstance(case.rules, str) and case.rules == "conservative_default":
        return RuleSet.conservative_default()
    return RuleSet.model_validate(case.rules)


def run_case(case: RuleCase) -> CaseResult:
    try:
        ruleset = _ruleset(case)
    except ValidationError as exc:  # invalid rules are refused, as resolve_ruleset() does (422)
        ruleset, decision = (
            None,
            Decision(
                action="block", code=ErrorCode.REQUEST_VALIDATION_FAILED, reasons=[e["msg"] for e in exc.errors()]
            ),
        )
    if ruleset is not None or case.rules is None:
        decision = evaluate(case.rendition, ruleset, _history(case), case.now, owner_domains=case.owner_domains)
    expect = case.expect
    want_action, want_code = expect["action"], expect.get("code")
    got_code = decision.code.value if decision.code else None

    want, got = SEVERITY[want_action], SEVERITY[decision.action]
    if got < want:
        outcome = "missed" if want > 0 else "correct_allow"
    elif got > want:
        outcome = "false_positive" if want == 0 else "stricter"
    elif want_code is not None and got_code != want_code:
        outcome = "wrong_code"
    else:
        outcome = "caught" if want > 0 else "correct_allow"

    problems: list[str] = []
    if want_code is None and want >= SEVERITY["reschedule"]:
        problems.append("case must name the expected code")
    if "retry_at" in expect:
        want_retry = _parse_time(expect["retry_at"]) if expect["retry_at"] else None
        if decision.retry_at != want_retry:
            problems.append(f"retry_at {decision.retry_at} != expected {want_retry}")
    reasons = " | ".join(decision.reasons)
    for text in _as_list(expect.get("reason_contains")):
        if text.casefold() not in reasons.casefold():
            problems.append(f"reasons do not mention {text!r}: {reasons}")
    seen_types = {e.get("type") for e in decision.evidence}
    for kind in _as_list(expect.get("evidence_types")):
        if kind not in seen_types:
            problems.append(f"evidence type {kind!r} missing (got {sorted(seen_types)})")
    warnings = " | ".join(decision.warnings)
    for text in _as_list(expect.get("warnings_contain")):
        if text not in warnings:
            problems.append(f"warnings do not contain {text!r}: {warnings}")
    fix = decision.fixes[0] if decision.fixes else None
    if "fix_lang" in expect and (fix is None or fix.lang != expect["fix_lang"]):
        problems.append(f"fix language {fix.lang if fix else None} != {expect['fix_lang']}")
    for text in _as_list(expect.get("fix_text_contains")):
        if fix is None or text not in fix.text:
            problems.append(f"fix text does not contain {text!r}: {fix.text if fix else None}")
    error = decision.to_error()
    for text in _as_list(expect.get("message_contains")):
        if error is None or text not in error.message:
            problems.append(f"error message does not contain {text!r}: {error.message if error else None}")
    if expect.get("no_fix") and decision.fixes:
        problems.append(f"unexpected fixes: {decision.fixes}")
    return CaseResult(case, decision, outcome, problems)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
