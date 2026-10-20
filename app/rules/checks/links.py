"""link_policy: none (no links at all) | no_shorteners (no bit.ly etc.) | allowlist (only listed domains)."""

from app.errors import ErrorCode
from app.rules.checks.base import RuleContext, blocked, passed
from app.rules.links import extract_links, is_shortener, matches_domain

NAME = "links"


def check(ctx: RuleContext):
    rules = ctx.ruleset
    if rules.link_policy == "any":
        return passed(NAME)
    links = extract_links(ctx.rendition)
    if rules.link_policy == "none":
        bad, why = links, "this destination allows no links"
    elif rules.link_policy == "no_shorteners":
        bad, why = [link for link in links if is_shortener(link.url)], "this destination does not allow link shorteners"
    else:  # allowlist
        allowed = rules.link_allowlist
        bad = [link for link in links if not any(matches_domain(link.url, d) for d in allowed)]
        why = f"this destination only allows links to {', '.join(allowed) or '(nothing)'}"
    if not bad:
        return passed(NAME)
    evidence = [{"type": "link", "value": link.url, "field": link.field} for link in bad]
    listed = ", ".join(f"{link.url} in {link.field}" for link in bad)
    return blocked(
        NAME,
        ErrorCode.DEST_RULE_LINK_POLICY,
        f"{why}; found {listed}",
        evidence=evidence,
        details={
            "rule": f"link_policy={rules.link_policy}",
            "suggestion": "remove the link(s); tailoring can do this and says so in warnings",
        },
    )
