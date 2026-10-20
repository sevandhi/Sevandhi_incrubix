"""What every check receives, plus small helpers to build its outcome."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.contract.models import Rendition
from app.errors import ErrorCode
from app.rules.models import CheckOutcome, PublishEvent, RuleSet


@dataclass(frozen=True)
class RuleContext:
    rendition: Rendition
    ruleset: RuleSet
    history: list[PublishEvent]
    now: datetime  # timezone-aware UTC
    owner_domains: tuple[str, ...] = field(default_factory=tuple)


def passed(check: str, **extra) -> CheckOutcome:
    return CheckOutcome(check=check, outcome="pass", **extra)


def blocked(check: str, code: ErrorCode, reason: str, **extra) -> CheckOutcome:
    return CheckOutcome(check=check, outcome="block", code=code, reason=f"{code.value}: {reason}", **extra)
