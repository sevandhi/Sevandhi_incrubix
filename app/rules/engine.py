"""The posting-rules engine: one pure function, no database, no network, no clock of its own.

    evaluate(rendition, ruleset, history, now, owner_domains=()) -> Decision

Every check in app/rules/checks/ runs (so the Decision lists every problem), in this order:
    automation -> language -> blocked terms -> self-promo -> links -> disclosure -> frequency cap
The action is the most serious outcome: block > reschedule > allow_with_changes > allow, and
``code`` is the code of the first check (in that order) that produced it. Because the frequency cap
is checked last and "block" beats "reschedule", a post that will be blocked is never rescheduled.

``history`` must be the earlier posts on THIS destination (the caller filters by destination_key).
``ruleset=None`` means nobody confirmed the destination's rules: block DEST_RULES_UNCONFIRMED.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.contract.models import Rendition
from app.errors import ErrorCode
from app.rules.checks import CHECKS
from app.rules.checks.base import RuleContext
from app.rules.models import CheckOutcome, Decision, PublishEvent, RuleSet, as_utc

_SEVERITY = {"pass": 0, "fix": 1, "reschedule": 2, "block": 3}
_ACTION = {0: "allow", 1: "allow_with_changes", 2: "reschedule", 3: "block"}


def evaluate(
    rendition: Rendition | dict[str, Any],
    ruleset: RuleSet | dict[str, Any] | None,
    history: Iterable[PublishEvent | dict[str, Any]] | None,
    now: datetime,
    *,
    owner_domains: Iterable[str] = (),
) -> Decision:
    if ruleset is None:
        return Decision(
            action="block",
            code=ErrorCode.DEST_RULES_UNCONFIRMED,
            reasons=[
                f"{ErrorCode.DEST_RULES_UNCONFIRMED.value}: this destination has no confirmed posting rules; "
                "confirm them (or use RuleSet.conservative_default()) before posting"
            ],
        )
    ctx = RuleContext(
        rendition=rendition if isinstance(rendition, Rendition) else Rendition.model_validate(rendition),
        ruleset=ruleset if isinstance(ruleset, RuleSet) else RuleSet.model_validate(ruleset),
        history=[e if isinstance(e, PublishEvent) else PublishEvent.model_validate(e) for e in history or []],
        now=as_utc(now),
        owner_domains=tuple(d for d in owner_domains if d),
    )
    outcomes = [check.check(ctx) for check in CHECKS]
    return combine(outcomes, ctx.ruleset)


def combine(outcomes: list[CheckOutcome], ruleset: RuleSet) -> Decision:
    """Merge the per-check outcomes into one Decision."""
    worst = max((_SEVERITY[o.outcome] for o in outcomes), default=0)
    action = _ACTION[worst]
    deciding = next((o for o in outcomes if _SEVERITY[o.outcome] == worst), None) if worst else None

    warnings = _ruleset_warnings(ruleset)
    for outcome in outcomes:
        warnings += [w for w in outcome.warnings if w not in warnings]
    # Details come from the deciding check only (a self-promo block must not carry the cap's retry_at).
    details: dict[str, Any] = dict(deciding.details) if deciding is not None else {}
    details["failed_checks"] = [o.check for o in outcomes if o.outcome != "pass"]

    return Decision(
        action=action,
        code=deciding.code if deciding is not None and worst >= _SEVERITY["reschedule"] else None,
        message=deciding.reason if deciding is not None else None,
        reasons=[o.reason for o in outcomes if o.outcome != "pass" and o.reason],
        retry_at=deciding.retry_at if action == "reschedule" else None,
        fixes=[fix for o in outcomes for fix in o.fixes],
        warnings=warnings,
        evidence=[e for o in outcomes if o.outcome != "pass" for e in o.evidence],
        details=details,
        checks=outcomes,
    )


def _ruleset_warnings(ruleset: RuleSet) -> list[str]:
    warnings = []
    if ruleset.source == "default_conservative":
        warnings.append("RULES_DEFAULTED: conservative default rules in use; confirm the real rules")
    return warnings
