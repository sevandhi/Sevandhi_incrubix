"""One normalised metrics record for every connector (Outstanding: "fetch_metrics ... normalised to views, likes,
comments, shares and clicks").

* ``KEYS`` is the record: the brief's five (views, likes, comments, shares, clicks) plus the contract's optional
  saves and watch_time_s (Appendix A ``fetch_metrics``).
* ``None`` means **the platform does not expose this metric** (never a fake 0). A 0 means the platform said 0.
* ``MAPPING`` is the single source of truth for which platform field becomes which key. ``docs/METRICS.md`` embeds
  ``mapping_markdown()`` (a test keeps them equal).
* ``source_fields()`` pulls exactly the mapped raw fields out of the platform responses recorded during the call, and
  ``derive()`` recomputes the normalised record from them. The metrics service stores both and flags a mismatch
  (``consistent: false``), so every stored number can be traced to the platform's own field.

    uv run python -m app.metrics.normalize          # print the mapping table (Markdown)
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

KEYS: tuple[str, ...] = ("views", "likes", "comments", "shares", "clicks", "saves", "watch_time_s")
BRIEF_KEYS: tuple[str, ...] = ("views", "likes", "comments", "shares", "clicks")


@dataclass(frozen=True)
class Source:
    """Where one normalised metric comes from on one platform (``field`` None = not exposed)."""

    field: str | None
    note: str = ""


def _na(note: str) -> Source:
    return Source(None, note)


MAPPING: dict[str, dict[str, Source]] = {
    "lemmy": {
        "views": _na("Lemmy has no view counter"),
        "likes": Source("post_view.counts.upvotes", "upvotes (not score: downvotes are kept in raw)"),
        "comments": Source("post_view.counts.comments"),
        "shares": _na("no share/cross-post counter in the API"),
        "clicks": _na("no link click counter"),
        "saves": _na("saves are private per user; no count"),
        "watch_time_s": _na("not a video platform"),
    },
    "discourse": {
        "views": Source("views", "topic views (Discourse counts one per user/IP per day)"),
        "likes": Source("like_count", "likes on all posts of the topic"),
        "comments": Source("posts_count", "posts_count - 1 (replies; the first post is ours)"),
        "shares": _na("share-button use is not counted in the API"),
        "clicks": Source("post_stream.posts[0].link_counts[].clicks", "sum over links in our post; null when it has no links"),
        "saves": _na("bookmarks are private; no count"),
        "watch_time_s": _na("not a video platform"),
    },
    "peertube": {
        "views": Source("views"),
        "likes": Source("likes"),
        "comments": Source("comments"),
        "shares": _na("no share counter"),
        "clicks": _na("no click counter"),
        "saves": _na("playlists are not counted"),
        "watch_time_s": Source("stats/overall.totalWatchTime", "seconds; null when the stats endpoint is not readable"),
    },
    "slack": {
        "views": _na("Slack does not expose message views"),
        "likes": Source("messages[].reactions[].count", "sum of all emoji reactions"),
        "comments": Source("messages[].reply_count", "thread replies; Slack omits the field when there are none (= 0)"),
        "shares": _na("no share counter"),
        "clicks": _na("no link click counter"),
        "saves": _na("saved items are private"),
        "watch_time_s": _na("not a video platform"),
    },
    "mrss": {
        "views": _na("feed readers fetch the whole feed; per-item views are unknowable"),
        "likes": _na("a feed has no reactions"),
        "comments": _na("a feed has no comments"),
        "shares": _na("not knowable from a feed"),
        "clicks": Source("feeds/_clicks/<guid>.json:clicks", "clicks through our own GET /api/v1/r/{id} redirect"),
        "saves": _na("not knowable from a feed"),
        "watch_time_s": _na("not knowable from a feed"),
    },
    "devto": {
        "views": Source("page_views_count", "author-only field of /api/articles/me/all"),
        "likes": Source("public_reactions_count"),
        "comments": Source("comments_count"),
        "shares": _na("no share counter"),
        "clicks": _na("no click counter"),
        "saves": _na("reading-list saves are not in the API"),
        "watch_time_s": _na("not a video platform"),
    },
}
# Connectors without the metrics capability (their fetch_metrics answers 404 METHOD_NOT_SUPPORTED).
NO_METRICS: dict[str, str] = {
    "ghost": "the Admin API has no per-post engagement counts we can read without members analytics",
    "webhook": "the partner protocol (docs/PARTNER_WEBHOOK.md) has no metrics call",
    "assisted": "a person posts the pack; no API to read",
}


def supported(connector_key: str) -> list[str]:
    """Keys the connector can fill (in ``KEYS`` order); [] for unknown connectors."""
    return [k for k in KEYS if (MAPPING.get(connector_key, {}).get(k) or Source(None)).field]


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def normalize(values: Any) -> dict[str, Any]:
    """Exactly ``KEYS``; counts as non-negative ints, watch time as float seconds; anything else stays None."""
    data = values.model_dump() if hasattr(values, "model_dump") else dict(values or {})
    out: dict[str, Any] = {}
    for key in KEYS:
        value = data.get(key)
        if key == "watch_time_s":
            out[key] = float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0 else None
        else:
            out[key] = _int(value)
    return out


def engagement(metrics: dict[str, Any]) -> int | None:
    """likes + comments + shares + clicks over the keys the platform exposes; None if it exposes none of them."""
    parts = [metrics.get(k) for k in ("likes", "comments", "shares", "clicks")]
    known = [p for p in parts if isinstance(p, int)]
    return sum(known) if known else None


# ------------------------------------------------------------------ raw fields from the recorded platform calls


def _calls(calls: list[dict[str, Any]], pattern: str) -> list[dict[str, Any]]:
    rx = re.compile(pattern)
    return [c for c in calls if rx.search(c.get("path") or "") and isinstance(c.get("json"), dict | list)]


def source_fields(connector_key: str, calls: list[dict[str, Any]], platform_post_id: str,
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The mapped raw fields, read from the JSON bodies the connector received (latest call wins)."""
    fields: dict[str, Any] = {}
    if connector_key == "lemmy":
        for call in _calls(calls, r"/api/v3/post$"):
            counts = ((call["json"].get("post_view") or {}).get("counts") or {}) if isinstance(call["json"], dict) else {}
            for name in ("upvotes", "downvotes", "score", "comments"):
                if name in counts:
                    fields[f"post_view.counts.{name}"] = counts[name]
    elif connector_key == "discourse":
        for call in _calls(calls, r"/t/\d+\.json$"):
            topic = call["json"]
            if not isinstance(topic, dict) or "posts_count" not in topic:
                continue
            for name in ("views", "like_count", "posts_count"):
                fields[name] = topic.get(name)
            posts = (topic.get("post_stream") or {}).get("posts") or []
            first = posts[0] if posts else {}
            links = first.get("link_counts") if isinstance(first, dict) else None
            fields["post_stream.posts[0].link_counts[].clicks"] = (
                [lc.get("clicks") for lc in links if isinstance(lc, dict)] if isinstance(links, list) else None)
    elif connector_key == "peertube":
        for call in _calls(calls, r"/api/v1/videos/[^/]+$"):
            video = call["json"]
            if isinstance(video, dict) and "views" in video:
                for name in ("views", "likes", "dislikes", "comments"):
                    if name in video:
                        fields[name] = video[name]
        for call in _calls(calls, r"/stats/overall$"):
            if isinstance(call["json"], dict) and "totalWatchTime" in call["json"]:
                fields["stats/overall.totalWatchTime"] = call["json"]["totalWatchTime"]
    elif connector_key == "slack":
        ts = platform_post_id.split(":", 1)[-1]
        for call in _calls(calls, r"conversations\.history$"):
            for message in (call["json"].get("messages") or []) if isinstance(call["json"], dict) else []:
                if isinstance(message, dict) and message.get("ts") == ts:
                    fields["messages[].reactions[]"] = [{"name": r.get("name"), "count": r.get("count")}
                                                        for r in message.get("reactions") or [] if isinstance(r, dict)]
                    fields["messages[].reply_count"] = message.get("reply_count")
    elif connector_key == "devto":
        for call in _calls(calls, r"/api/articles/me"):
            items = call["json"] if isinstance(call["json"], list) else []
            for article in items:
                if isinstance(article, dict) and str(article.get("id")) == str(platform_post_id):
                    for name in ("page_views_count", "public_reactions_count", "comments_count"):
                        fields[name] = article.get(name)
    if extra:
        fields.update(extra)
    return fields


def derive(connector_key: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """The normalised record recomputed from ``source_fields`` per ``MAPPING`` (None when there is nothing to check)."""
    if not fields:
        return None
    out: dict[str, Any] = dict.fromkeys(KEYS)
    if connector_key == "lemmy":
        out["likes"] = fields.get("post_view.counts.upvotes")
        out["comments"] = fields.get("post_view.counts.comments")
    elif connector_key == "discourse":
        out["views"] = fields.get("views")
        out["likes"] = fields.get("like_count")
        posts = fields.get("posts_count")
        out["comments"] = max(0, posts - 1) if isinstance(posts, int) else None
        clicks = fields.get("post_stream.posts[0].link_counts[].clicks")
        out["clicks"] = sum(c for c in clicks if isinstance(c, int)) if isinstance(clicks, list) else None
    elif connector_key == "peertube":
        out["views"], out["likes"], out["comments"] = fields.get("views"), fields.get("likes"), fields.get("comments")
        out["watch_time_s"] = fields.get("stats/overall.totalWatchTime")
    elif connector_key == "slack":
        reactions = fields.get("messages[].reactions[]")
        out["likes"] = sum(r.get("count") or 0 for r in reactions) if isinstance(reactions, list) else None
        reply = fields.get("messages[].reply_count")
        out["comments"] = reply if isinstance(reply, int) else (0 if "messages[].reply_count" in fields else None)
    elif connector_key == "mrss":
        out["clicks"] = fields.get("feeds/_clicks/<guid>.json:clicks")
    elif connector_key == "devto":
        out["views"] = fields.get("page_views_count")
        out["likes"] = fields.get("public_reactions_count")
        out["comments"] = fields.get("comments_count")
    else:
        return None
    return normalize(out)


def consistent(normalised: dict[str, Any], derived: dict[str, Any] | None) -> bool | None:
    """True when the connector's numbers equal the ones recomputed from the raw fields (None = not checkable)."""
    if derived is None:
        return None
    return all(normalised.get(k) == derived.get(k) for k in KEYS)


# ------------------------------------------------------------------ documentation


def mapping_markdown() -> str:
    """The mapping table embedded in docs/METRICS.md."""
    lines = ["| connector | " + " | ".join(KEYS) + " |", "|---|" + "---|" * len(KEYS)]
    for key, mapping in MAPPING.items():
        cells = []
        for metric in KEYS:
            src = mapping[metric]
            cells.append(f"`{src.field}`" if src.field else "null")
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    for key, why in NO_METRICS.items():
        lines.append(f"| {key} | " + " | ".join(["n/a"] * len(KEYS)) + f" |  <!-- {why} -->")
    notes = ["", "Notes per field:", ""]
    for key, mapping in MAPPING.items():
        for metric in KEYS:
            src = mapping[metric]
            if src.note:
                notes.append(f"- **{key}.{metric}**: {src.note}")
    for key, why in NO_METRICS.items():
        notes.append(f"- **{key}**: no metrics capability ({why}); `fetch_metrics` answers 404 `METHOD_NOT_SUPPORTED`.")
    return "\n".join(lines + notes) + "\n"


if __name__ == "__main__":  # pragma: no cover
    sys.stdout.write(mapping_markdown())
