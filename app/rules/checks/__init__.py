"""One module per rule. The engine runs them in this order (cheap, final decisions first;
frequency cap last so a post that will be blocked is never rescheduled).

To add a rule: write ``checks/<rule>.py`` with ``NAME`` and ``check(ctx) -> CheckOutcome``,
add it to ``CHECKS`` below and add a case to tests/rules/rule_cases.yaml.
"""

from app.rules.checks import automation, blocked_terms, disclosure, frequency_cap, language, links, self_promo

CHECKS = [automation, language, blocked_terms, self_promo, links, disclosure, frequency_cap]

__all__ = ["CHECKS"]
