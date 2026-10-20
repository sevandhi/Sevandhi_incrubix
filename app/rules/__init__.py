"""Posting rules (Must-have 4) and destination spec checks. Pure code: no DB, no network.

from app.rules import evaluate, check_spec, RuleSet, Decision, PublishEvent
"""

from app.rules.engine import evaluate
from app.rules.fixes import apply_fixes
from app.rules.models import CheckOutcome, Decision, Disclosure, Fix, FrequencyCap, PublishEvent, RuleSet
from app.rules.promo_detector import PromoResult, detect_promo
from app.rules.spec import check_spec, effective_spec

__all__ = [
    "CheckOutcome",
    "Decision",
    "Disclosure",
    "Fix",
    "FrequencyCap",
    "PromoResult",
    "PublishEvent",
    "RuleSet",
    "apply_fixes",
    "check_spec",
    "detect_promo",
    "effective_spec",
    "evaluate",
]
