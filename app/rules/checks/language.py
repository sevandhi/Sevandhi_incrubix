"""language_policy: with must_match, a post whose language is not one of the community's languages
is BLOCKED (DEST_RULE_LANGUAGE). With "any", a mismatch is only a warning.

If no policy is stated, a non-English community language implies must_match (a German community
does not get English posts by default). With that implied policy only a definite mismatch blocks;
content whose language cannot be determined passes with a LANGUAGE_UNKNOWN warning.
"""

from app.errors import ErrorCode
from app.rules.checks.base import RuleContext, blocked, passed
from app.rules.lang import detect_language, normalize_lang

NAME = "language"


def check(ctx: RuleContext):
    rules, rendition = ctx.ruleset, ctx.rendition
    declared = normalize_lang(rendition.language)
    lang = declared or detect_language(f"{rendition.title or ''}\n{rendition.body or ''}")
    how = "declared" if declared else "detected"
    community = rules.languages
    policy = rules.effective_language_policy
    implied = rules.language_policy_implied
    details = {
        "content_language": lang,
        "language_source": how if lang else None,
        "destination_languages": community,
        "language_policy": policy + (" (implied by the community language)" if implied else ""),
    }

    if policy == "any":
        warnings = []
        if community and lang and lang not in community:
            warnings.append(
                f"LANGUAGE_MISMATCH: content is '{lang}' ({how}) but the community uses {', '.join(community)}"
            )
        return passed(NAME, warnings=warnings, details=details)

    # must_match (stated, or implied by a non-English community language)
    if not community:
        return blocked(
            NAME,
            ErrorCode.DEST_RULE_LANGUAGE,
            "the destination requires a matching language but its language is not known; "
            "set destination.language or rules.languages",
            details=details,
        )
    if lang is None:
        if implied:
            return passed(
                NAME,
                details=details,
                warnings=[
                    f"LANGUAGE_UNKNOWN: could not determine the content language; the community uses "
                    f"{', '.join(community)} (set rendition.language)"
                ],
            )
        return blocked(
            NAME,
            ErrorCode.DEST_RULE_LANGUAGE,
            f"the community requires {', '.join(community)} and the content language could not be "
            "determined; set rendition.language",
            details=details,
        )
    if lang not in community:
        hint = "; set language_policy: any if this community accepts other languages" if implied else ""
        return blocked(
            NAME,
            ErrorCode.DEST_RULE_LANGUAGE,
            f"content is in '{lang}' ({how}) but the community requires {', '.join(community)}; "
            f"translate the post or choose another destination{hint}",
            details=details,
            evidence=[{"type": "language", "value": lang, "field": "language"}],
        )
    return passed(NAME, details=details)
