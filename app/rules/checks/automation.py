"""automation_allowed=false: this destination only takes posts made by a person (assisted connector)."""

from app.errors import ErrorCode
from app.rules.checks.base import RuleContext, blocked, passed

NAME = "automation"


def check(ctx: RuleContext):
    if ctx.ruleset.automation_allowed:
        return passed(NAME)
    return blocked(
        NAME,
        ErrorCode.DEST_RULE_AUTOMATION_NOT_ALLOWED,
        "this destination does not allow automated posting; build an assist pack (assisted connector) "
        "so a person can post it",
        details={"suggestion": "use build_assist_pack"},
    )
