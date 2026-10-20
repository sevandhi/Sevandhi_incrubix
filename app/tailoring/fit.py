"""Steps shared by every profile: link policy, media, tags, disclosure, title and body length."""

from __future__ import annotations

import re

from app.contract.models import Rendition
from app.rules.disclosure import choose_text, has_disclosure
from app.rules.graphemes import measure
from app.rules.models import RuleSet
from app.rules.spec import media_mime, media_violations
from app.tailoring.draft import Block, Draft
from app.tailoring.textops import ascii_fold, link_keeper, remove_links
from app.tailoring.truncate import truncate_with_reason

MIN_MAIN = 100  # keep at least this much of the creator's text before dropping optional blocks


def apply_link_policy(draft: Draft, rules: RuleSet | None, spec: dict) -> None:
    """Remove links the destination does not allow (the rules engine would block them)."""
    policy = rules.link_policy if rules else "any"
    allowlist = rules.link_allowlist if rules else []
    links_allowed = spec.get("links_allowed") is not False
    if policy == "any" and links_allowed:
        return
    keep = link_keeper(policy, allowlist, links_allowed)
    r = draft.rendition
    removed: list[str] = []
    r.body, gone = remove_links(r.body, keep)
    removed += gone
    if r.title:
        r.title, gone = remove_links(r.title, keep)
        removed += gone
    if r.link_url and not keep(r.link_url):
        removed.append(r.link_url)
        r.link_url = None
        if r.content_type in ("link", "video_link"):
            r.content_type = "text"
    if removed:
        why = f"link_policy={policy}" if policy != "any" else "links not allowed by the destination spec"
        draft.warn("LINK_REMOVED", f"{len(removed)} link(s) removed ({why}): {', '.join(removed)}")


def fit_media(draft: Draft, spec: dict, keep_links) -> None:
    """Drop media the destination does not accept; link it instead when links are allowed."""
    r = draft.rendition
    content_types = spec.get("content_types")
    kept = []
    for media in r.media:
        problems = media_violations(Rendition(media=[media]), spec, content_types)
        if problems:
            _drop_media(draft, media, problems[0].message, keep_links)  # e.g. "image/webp media is not accepted here"
        else:
            kept.append(media)
    max_count = spec.get("media_max_count")
    if max_count is not None and len(kept) > int(max_count):
        for media in kept[int(max_count) :]:
            _drop_media(draft, media, f"{media_mime(media) or 'media'} over the {max_count}-item limit", keep_links)
        kept = kept[: int(max_count)]
    r.media = kept

    # The post type must be one the destination accepts.
    if content_types and r.content_type not in content_types:
        kinds = {(media_mime(m) or "").split("/")[0] for m in r.media}
        if r.content_type in ("image", "video") and r.content_type not in kinds or "text" in content_types:
            if "text" in content_types:
                if r.content_type in ("link", "video_link") and r.link_url:
                    if keep_links(r.link_url):
                        draft.tail.append(Block(r.link_url, "link"))
                    r.link_url = None
                draft.warn("CONTENT_TYPE_CHANGED", f"{r.content_type} -> text (accepted: {', '.join(content_types)})")
                r.content_type = "text"


def _drop_media(draft: Draft, media, why: str, keep_links) -> None:
    if keep_links(media.url):
        draft.tail.append(Block(media.url, "media_link"))
        draft.warn("MEDIA_DROPPED", f"{why}; linked instead")
    else:
        draft.warn("MEDIA_DROPPED", f"{why}; not linked because links are not allowed")


def fit_tags(draft: Draft, spec: dict) -> None:
    r = draft.rendition
    pattern = spec.get("tag_pattern")
    tag_max = spec.get("tag_max")
    tag_max_chars = spec.get("tag_max_chars")
    if pattern is None and tag_max is None and tag_max_chars is None:
        return
    regex = re.compile(pattern) if pattern else None
    tags: list[str] = []
    for tag in r.tags:
        candidate = tag.strip().lstrip("#")
        if regex and not regex.fullmatch(candidate):
            candidate = re.sub(r"[^a-z0-9]", "", ascii_fold(candidate).lower())
            if not regex.fullmatch(candidate):
                continue
        if tag_max_chars is not None and len(candidate) > int(tag_max_chars):
            continue
        if candidate and candidate not in tags:
            tags.append(candidate)
    if tag_max is not None:
        tags = tags[: int(tag_max)]
    if tags != r.tags:
        draft.warn(
            "TAGS_CHANGED", f"{', '.join(r.tags) or '(none)'} -> {', '.join(tags) or '(none)'} to fit the destination"
        )
        r.tags = tags


def add_disclosure(draft: Draft, rules: RuleSet | None) -> None:
    r = draft.rendition
    if rules is None or not rules.disclosure.required or r.ai_generated is False:
        return
    if has_disclosure(draft.build(), rules):
        return
    if r.ai_generated is None:
        draft.warn("AI_STATUS_UNKNOWN", "ai_generated not set; treated as AI-generated")
    text, lang, warnings = choose_text(rules, r)
    draft.warnings += [w for w in warnings if w not in draft.warnings]
    if not rules.disclosure.auto_insert:
        draft.warn("AI_DISCLOSURE_MISSING", "disclosure required but automatic insertion is off for this destination")
        return
    block = Block(text, "disclosure", required=True)
    if rules.disclosure.placement == "top":
        draft.head.insert(0, block)
    else:
        draft.tail.append(block)
    draft.warn("AI_DISCLOSURE_APPENDED", f"'{lang}' disclosure added at the {rules.disclosure.placement}")


def fit_title(draft: Draft, spec: dict, title_needed: bool) -> None:
    r = draft.rendition
    unit = spec.get("count_unit", "codepoints")
    needed = title_needed or spec.get("title_required") or spec.get("title_min_chars")
    if not (r.title or "").strip() and needed and r.body.strip():
        first = next(line for line in r.body.splitlines() if line.strip())
        r.title = re.sub(r"^#+\s*", "", first).strip()
        draft.warn("TITLE_DERIVED", "no title given; used the first line of the text")
    limit = spec.get("title_max_chars")
    if r.title and limit is not None:
        before = measure(r.title, unit)
        r.title, how = truncate_with_reason(r.title, int(limit), unit=unit)
        if how:
            draft.warn("TITLE_TRUNCATED", f"{before}→{measure(r.title, unit)} chars at {how}")


def fit_body(draft: Draft, spec: dict) -> bool:
    """Fit the body limit by shortening the creator's text, then by dropping optional blocks.
    Returns False if a required block (the disclosure) cannot be placed within the limit."""
    limit = spec.get("body_max_chars")
    unit = spec.get("count_unit", "codepoints")
    if limit is None or measure(draft.body(), unit) <= int(limit):
        return True
    limit = int(limit)
    before = measure(draft.body(), unit)
    main = draft.rendition.body

    def budget() -> int:
        return limit - (measure(draft.body(main="x"), unit) - 1)

    while budget() < min(measure(main, unit), MIN_MAIN) and draft.optional_blocks():
        block = draft.optional_blocks()[-1]
        draft.drop(block)
        draft.warn("BLOCK_DROPPED", f"{block.kind} line dropped to fit the {limit}-character body limit")
    room = budget()
    new_main, how = truncate_with_reason(main, room, unit=unit) if room > 0 else ("", "no room for any text")
    if how:
        draft.rendition.body = new_main
        draft.warn("BODY_TRUNCATED", f"{before}→{measure(draft.body(), unit)} chars at {how}")
    if measure(draft.body(), unit) > limit:
        if any(b.kind == "disclosure" for b in draft.head + draft.tail):
            draft.warn(
                "DISCLOSURE_UNPLACEABLE", f"the required disclosure does not fit the {limit}-character body limit"
            )
            return False
    return True
