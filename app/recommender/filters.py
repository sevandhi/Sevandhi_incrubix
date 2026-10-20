"""Hard filters: a destination that fails one is never ranked; it goes to ``excluded[]`` with a code and a reason.

Order (the first failing filter is reported):

| # | code | when |
|---|---|---|
| 1 | ``DESTINATION_EXCLUDED`` | the destination is excluded (e.g. demoted after a failed certification) |
| 2 | ``FORMAT_NOT_AN_AUDIENCE`` | a feed-ingest or ping endpoint: it takes a whole feed or site, not one item |
| 3 | ``FORMAT_NEEDS_VIDEO`` | a video platform (PeerTube instance, video site) and the item is not a video |
| 4 | ``FORMAT_NEEDS_PRODUCT`` | a launch site, directory, curated list, marketplace or tool newsletter, and the item is not a product announcement (declared promotional, or links the owner's own site) |
| 5 | ``DEST_RULE_SELF_PROMO`` | the item is promotional (flag or the rules engine's promo detector) and the destination says no self-promotion |
| 6 | ``DEST_RULE_AI_CONTENT`` | the item is AI-generated and the destination bans AI-generated content |
| 7 | ``DEST_RULE_LANGUAGE`` | the community's language policy is ``must_match`` (stated, or implied by a non-English community) and the item's language is not one of its languages; an unknown item language is excluded only under a *stated* ``must_match`` |
| 8 | ``DEST_RULE_AUTOMATION_NOT_ALLOWED`` | only with ``automated_only``: the destination forbids bots (or needs moderator approval) or is assisted-only |
| 9 | ``DEST_RULE_FREQUENCY_CAP`` | the cap is used up by the destination's publish history; ``retry_at`` says when a slot opens |
| 10 | ``DESTINATION_NOT_CERTIFIED`` / ``DEST_RULES_UNCONFIRMED`` | only with ``publishable_only``: level L0, or no confirmed rules (publications would refuse both today) |

Without ``automated_only``, a destination that forbids bots stays in the ranking with ``delivery: assisted``: a person
posts the assisted pack, which is how such destinations are reached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .profiles import ContentProfile, DestProfile


@dataclass
class FilterOutcome:
    excluded: bool
    code: str | None = None
    reason: str | None = None
    retry_at: datetime | None = None
    delivery: str = "automated"  # automated | assisted
    cap_used: int | None = None  # posts counted in the current cap window
    notes: list[str] = field(default_factory=list)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def cap_usage(dest: DestProfile, history: list[Any], now: datetime) -> tuple[int, datetime | None]:
    """(posts counted in the window, retry_at if the cap is reached) using the rules engine's frequency-cap check."""
    from app.rules.checks.base import RuleContext
    from app.rules.checks.frequency_cap import check
    from app.rules.models import RuleSet

    if dest.frequency_cap is None:
        return 0, None
    ruleset = RuleSet(frequency_cap=dest.frequency_cap)
    ctx = RuleContext(rendition=None, ruleset=ruleset, history=history, now=now)
    outcome = check(ctx)
    used = int((outcome.details or {}).get("count_in_window", 0))
    return used, (outcome.retry_at if outcome.outcome == "reschedule" else None)


def apply_filters(content: ContentProfile, dest: DestProfile, *, history: list[Any] | None = None,
                  now: datetime | None = None, automated_only: bool = False,
                  publishable_only: bool = False) -> FilterOutcome:
    out = FilterOutcome(excluded=False)
    assisted_only = dest.connector_key == "assisted" or dest.automation in ("no", "approval")
    out.delivery = "assisted" if assisted_only else "automated"

    def fail(code: str, reason: str, retry_at: datetime | None = None) -> FilterOutcome:
        out.excluded, out.code, out.reason, out.retry_at = True, code, reason, retry_at
        return out

    if dest.status == "excluded":
        return fail("DESTINATION_EXCLUDED", f"destination is excluded: {dest.excluded_reason or 'no reason recorded'}")
    kind = dest.kind
    if kind == "feed":
        return fail("FORMAT_NOT_AN_AUDIENCE",
                    f"'{dest.surface or dest.connector_key}' endpoint takes a whole feed or site, not one item")
    if kind == "video" and not content.has_video:
        return fail("FORMAT_NEEDS_VIDEO", "video platform: the item is not a video")
    if kind == "listing" and not content.is_product_announcement:
        return fail("FORMAT_NEEDS_PRODUCT", f"'{dest.surface}' sites list products; the item is not a product announcement")
    if content.is_promotional and dest.self_promo == "disallowed":
        why = f" (signals: {', '.join(content.promo_signals[:3])})" if content.promo_signals else ""
        return fail("DEST_RULE_SELF_PROMO", f"item is promotional{why} and the destination does not allow self-promotion")
    if content.ai_generated is True and dest.ai_content == "prohibited":
        return fail("DEST_RULE_AI_CONTENT", "item is AI-generated and the destination bans AI-generated content")
    if dest.effective_language_policy == "must_match" and dest.languages and "multi" not in dest.languages:
        if content.language and content.language not in dest.languages:
            return fail("DEST_RULE_LANGUAGE", f"community language {'/'.join(dest.languages)} does not match the item "
                                              f"language {content.language}")
        if not content.language and dest.language_policy == "must_match":
            return fail("DEST_RULE_LANGUAGE", "item language unknown and the destination requires a matching language")
    if automated_only and assisted_only:
        why = "assisted-only destination" if dest.connector_key == "assisted" else (
            "bots need moderator approval" if dest.automation == "approval" else "automation not allowed")
        return fail("DEST_RULE_AUTOMATION_NOT_ALLOWED", f"{why}; reachable only through the assisted route")
    if dest.frequency_cap is not None and history is not None and now is not None:
        used, retry_at = cap_usage(dest, history, now)
        out.cap_used = used
        if retry_at is not None:
            cap = dest.frequency_cap
            return fail("DEST_RULE_FREQUENCY_CAP",
                        f"cap of {cap.max_posts} post(s) per {cap.window} used up ({used} in the window); "
                        f"next slot {_iso(retry_at)}", retry_at)
    if publishable_only:
        if dest.level == "L0":
            return fail("DESTINATION_NOT_CERTIFIED", "not certified yet (L0): publications are refused until it passes")
        if not dest.rules_confirmed:
            return fail("DEST_RULES_UNCONFIRMED", "no confirmed rules: publications are refused until a person confirms them")
    if dest.automation == "approval":
        out.notes.append("bots need moderator approval")
    return out
