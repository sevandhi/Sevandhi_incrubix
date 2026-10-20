"""frequency_cap: at most max_posts in any rolling window. Over the cap -> reschedule.

retry_at is the moment enough old posts leave the window. In the usual case (exactly max_posts in the
window) that is oldest_in_window + window. If there are already more (k > max), the slot opens when
the (k - max + 1)-th oldest post leaves, so we use that one.
"""

from app.errors import ErrorCode
from app.rules.checks.base import RuleContext, passed
from app.rules.models import CheckOutcome

NAME = "frequency_cap"


def _iso(moment) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def check(ctx: RuleContext):
    cap = ctx.ruleset.frequency_cap
    if cap is None:
        return passed(NAME)
    window = cap.window_td
    window_start = ctx.now - window
    # Counted: posts and reserved slots after window_start. A scheduled post counts if it is less than one
    # window ahead (it would share a window with a post made now); one further ahead cannot.
    window_end = ctx.now + window
    times = sorted(
        e.published_at for e in ctx.history if e.counts_toward_cap and window_start < e.published_at < window_end
    )
    details = {"cap": cap.max_posts, "window": cap.window, "count_in_window": len(times)}
    if len(times) < cap.max_posts:
        return passed(NAME, details=details)
    frees_slot = times[len(times) - cap.max_posts]
    retry_at = frees_slot + window
    details["retry_at"] = _iso(retry_at)
    reason = (
        f"{ErrorCode.DEST_RULE_FREQUENCY_CAP.value}: cap of {cap.max_posts} post(s) per {cap.window} reached "
        f"({len(times)} in the window, oldest {_iso(times[0])}); next slot {_iso(retry_at)}"
    )
    return CheckOutcome(
        check=NAME,
        outcome="reschedule",
        code=ErrorCode.DEST_RULE_FREQUENCY_CAP,
        reason=reason,
        retry_at=retry_at,
        details=details,
        evidence=[{"type": "post_in_window", "value": _iso(t), "field": "history"} for t in times],
    )
