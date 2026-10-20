"""self_promo: disallowed blocks any promotional post; ratio(n) allows at most 1 promotional post in n.

A post is promotional if the caller says so OR the conservative detector finds any signal
(see app/rules/promo_detector.py). The block reason names every signal that fired.
"""

from app.errors import ErrorCode
from app.rules.checks.base import RuleContext, blocked, passed
from app.rules.promo_detector import detect_promo

NAME = "self_promo"
SUGGESTION = "remove own links, tracking/affiliate parameters and calls to action, or post a discussion-first version"


def check(ctx: RuleContext):
    rules = ctx.ruleset
    if rules.self_promo == "allowed":
        return passed(NAME)
    promo = detect_promo(ctx.rendition, owner_domains=ctx.owner_domains)
    if not promo.is_promotional:
        return passed(NAME, details={"promo_signals": []})
    evidence = promo.evidence()
    details = {"rule": f"self_promo={rules.self_promo}", "suggestion": SUGGESTION}
    if rules.rules_url:
        details["rules_url"] = rules.rules_url

    if rules.self_promo == "disallowed":
        return blocked(
            NAME,
            ErrorCode.DEST_RULE_SELF_PROMO,
            f"this destination bans self-promotion and the post is promotional ({promo.summary()})",
            evidence=evidence,
            details=details,
        )

    # ratio(n): after this post, promotional posts must be at most 1/n of all posts.
    n = rules.promo_ratio
    counted = [e for e in ctx.history if e.counts_toward_cap]
    # Unknown (None) counts as promotional: conservative.
    promo_before = sum(1 for e in counted if e.is_promotional is not False)
    total_after, promo_after = len(counted) + 1, promo_before + 1
    details.update({"promotional_posts": promo_after, "total_posts": total_after, "ratio": f"1 in {n}"})
    if promo_after * n <= total_after:
        return passed(
            NAME,
            details=details,
            warnings=[f"SELF_PROMO_RATIO: {promo_after} of {total_after} posts promotional (limit 1 in {n})"],
        )
    return blocked(
        NAME,
        ErrorCode.DEST_RULE_SELF_PROMO,
        f"self-promotion limit is 1 post in {n}; this would make {promo_after} of {total_after} "
        f"promotional ({promo.summary()})",
        evidence=evidence,
        details=details,
    )
