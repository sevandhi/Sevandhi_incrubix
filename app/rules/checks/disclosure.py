"""disclosure.required: AI-made content (ai_generated true OR unknown) must carry the localised
disclosure line. Missing -> allow_with_changes with an insert_disclosure fix, or block when the
destination turned automatic insertion off."""

from app.errors import ErrorCode
from app.rules.checks.base import RuleContext, blocked, passed
from app.rules.disclosure import choose_text, has_disclosure
from app.rules.models import CheckOutcome, Fix

NAME = "disclosure"


def check(ctx: RuleContext):
    rules, rendition = ctx.ruleset, ctx.rendition
    if not rules.disclosure.required:
        return passed(NAME)
    if rendition.ai_generated is False:
        return passed(NAME, details={"ai_generated": False})
    warnings = []
    if rendition.ai_generated is None:
        warnings.append("AI_STATUS_UNKNOWN: ai_generated not set; treated as AI-generated")
    if has_disclosure(rendition, rules):
        return passed(NAME, warnings=warnings, details={"disclosure": "present"})

    text, lang, lang_warnings = choose_text(rules, rendition)
    warnings += lang_warnings
    if not rules.disclosure.auto_insert:
        return blocked(
            NAME,
            ErrorCode.DEST_RULE_DISCLOSURE,
            f"this destination requires an AI-content disclosure, the post has none, and automatic "
            f'insertion is turned off; add: "{text}"',
            warnings=warnings,
        )
    fix = Fix(kind="insert_disclosure", text=text, placement=rules.disclosure.placement, lang=lang)
    warnings.append(f"AI_DISCLOSURE_REQUIRED: '{lang}' disclosure will be added at the {fix.placement}")
    return CheckOutcome(
        check=NAME,
        outcome="fix",
        reason=f"AI-content disclosure required; add it at the {fix.placement}",
        fixes=[fix],
        warnings=warnings,
    )
