"""Destination levels after a certification pass (brief: L0 catalogued .. L4 native + metrics).

* assisted tier (a person posts from a pack)                                 → L1
* feed / bridge tier                                                         → L2
* native / protocol with publish + status + delete *tested and confirmed*    → L3
* ... plus a passing ``fetch_metrics`` step                                  → L4
* native / protocol whose removal is not a real delete (Dev.to "unpublish to draft",
  or no delete capability)                                                   → L2: publish and status are proven,
  but "delete tested" is not, so L3 is not claimed (the evidence does not support it).
"""

from __future__ import annotations

from typing import Any

ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}
REAL_DELETE_MODES = {"deleted", "soft_deleted", "purged"}  # the public URL is gone (404) in each


def level_for(manifest: Any, *, delete_mode: str | None, metrics_ok: bool) -> tuple[str, str]:
    """(level, reason) for a passed certification run."""
    tier = manifest.tier
    caps = set(manifest.capabilities)
    if tier == "assisted" or ("assist" in caps and "publish" not in caps):
        return "L1", "assisted: the pack was built, its links worked and were then revoked"
    if tier in ("feed", "bridge"):
        return "L2", f"{tier}: publish, status and removal certified"
    if delete_mode in REAL_DELETE_MODES:
        if metrics_ok and "metrics" in caps:
            return "L4", "native: publish, live URL, status, metrics and delete certified"
        return "L3", "native: publish, live URL, status and delete certified"
    return "L2", (f"native publish and status certified, but removal was '{delete_mode or 'not supported'}', "
                  "not a delete: L3 is not claimed")


# ------------------------------------------------------------------ test-post removal outcomes (Must-have 5)

# The words the MH5 audit and the form use. Only "deleted" means the post is gone from the platform (Lemmy purge,
# Ghost delete, webhook delete); a Discourse soft delete and a Dev.to unpublish are counted separately, and a post
# that was never created is never counted as deleted.
OUTCOMES = ("deleted", "soft_deleted", "unpublished_to_draft", "pack_revoked", "not_created", "already_gone", "unconfirmed")
_MODE_OUTCOME = {
    "deleted": "deleted", "purged": "deleted", "permanent": "deleted",
    "soft_deleted": "soft_deleted", "soft": "soft_deleted",
    "unpublish_to_draft": "unpublished_to_draft", "admin_unpublish": "unpublished_to_draft",
    "pack_revoked": "pack_revoked",
    "not_created": "not_created", "not_found": "not_created",
    "already_deleted": "already_gone", "already_gone": "already_gone", "removed_by_platform": "already_gone",
}


def removal_outcome(delete_mode: str | None) -> str | None:
    """Audit outcome for a ``test_posts.delete_mode`` (None = still live). Prefixes ``cleanup_`` / ``sweeper_``
    say who removed it; anything not recognised (e.g. ``cleanup_attempted``) is ``unconfirmed``."""
    if not delete_mode:
        return None
    base = delete_mode.removeprefix("cleanup_").removeprefix("sweeper_")
    return _MODE_OUTCOME.get(base, "unconfirmed")


def delete_mode_name(platform_mode: object) -> str:
    """Name a confirmed delete precisely: a Discourse soft delete is not reported as a plain delete."""
    text = str(platform_mode or "").lower()
    if "soft" in text:
        return "soft_deleted"
    if "purge" in text:
        return "purged"
    return "deleted"
