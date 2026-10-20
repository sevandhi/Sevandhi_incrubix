"""Deterministic tailoring profiles, one per kind of destination (plan ADV4).

| profile    | platforms                    | what it does                                                       |
|------------|------------------------------|--------------------------------------------------------------------|
| forum      | discourse, lemmy             | discussion-first: CTA lines out, context first, question at the    |
|            |                              | end, link at the bottom, question-style title; no hashtags on Lemmy |
| article    | devto/forem, ghost, peertube | canonical_url = the original; Dev.to tags: <=4 lowercase alnum     |
| slack      | slack                        | Slack mrkdwn, <url|text> links, <@U...> mentions, title folded in  |
| newsletter | surface newsletter/email     | subject (title) required and within limits                         |
| mrss       | mrss                         | media:title required; plain-text description                       |
| generic    | anything else                | only rules and spec fitting                                        |
"""

from __future__ import annotations

import re
import unicodedata

from app.contract.models import Destination
from app.rules.graphemes import measure
from app.rules.links import find_urls
from app.rules.promo_detector import find_cta
from app.tailoring.draft import Block, Draft
from app.tailoring.i18n import discussion
from app.tailoring.textops import (
    ascii_fold,
    ends_with_question,
    html_to_markdown,
    markdown_to_mrkdwn,
    markdown_to_plain,
    strip_hashtags,
    tidy,
)

FORUM = {"discourse", "lemmy"}
ARTICLE = {"devto", "forem", "ghost", "peertube", "hashnode", "writefreely"}
NEWSLETTER_SURFACES = {"newsletter", "email"}

# Platform facts that hold even when a manifest forgets them (merged under the manifest spec).
PLATFORM_SPEC_DEFAULTS = {
    "devto": {"tag_max": 4, "tag_pattern": "[a-z0-9]+"},
    "forem": {"tag_max": 4, "tag_pattern": "[a-z0-9]+"},
    "lemmy": {"tag_max": 0},  # Lemmy posts have no tags
}


def platform_of(destination: Destination) -> str:
    return (destination.platform_key or destination.connector_key or "").lower()


def choose_profile(destination: Destination) -> str:
    platform = platform_of(destination)
    connector = (destination.connector_key or "").lower()
    if (destination.surface or "").lower() in NEWSLETTER_SURFACES or connector == "newsletter":
        return "newsletter"
    if platform in FORUM or connector in FORUM:
        return "forum"
    if platform in ARTICLE or connector in ARTICLE:
        return "article"
    if "slack" in (platform, connector):
        return "slack"
    if "mrss" in (platform, connector):
        return "mrss"
    return "generic"


# ------------------------------------------------------------------ forum


def forum(draft: Draft, platform: str, spec: dict) -> None:
    r = draft.rendition
    words = discussion(draft.lang)
    lines = r.body.splitlines()

    # 1. Discussion first: drop short lines that ARE a call to action ("Subscribe for more!").
    #    Real content that merely contains a CTA word ("Is a 20% discount normal?") is kept and flagged.
    kept = [line for line in lines if not is_pure_cta_line(line)]
    if len(kept) < len(lines):
        draft.warn(
            "CTA_REMOVED", f"{len(lines) - len(kept)} call-to-action line(s) removed for a discussion-first post"
        )
    flagged = [line for line in kept if find_cta(line)]
    if flagged:
        draft.warn(
            "CTA_KEPT",
            f"{len(flagged)} line(s) contain call-to-action words and were kept; a no-promotion destination may block them",
        )

    # 2. Context first: a line that is only a link moves to the bottom.
    links_moved = [line.strip() for line in kept if _is_link_line(line)]
    kept = [line for line in kept if not _is_link_line(line)]
    r.body = tidy("\n".join(kept))

    # 3. No hashtags on Lemmy.
    if platform == "lemmy":
        r.body, found = strip_hashtags(r.body)
        if found or r.tags:
            draft.warn("HASHTAGS_REMOVED", "Lemmy has no hashtags; removed " + ", ".join(found + r.tags))
        r.tags = []

    # 4. Question at the end, then the link at the bottom.
    if not ends_with_question(r.body):
        draft.tail.append(Block(words["body_q"], "question"))
    if r.link_url:
        links_moved = [line for line in links_moved if r.link_url not in find_urls(line)]  # no duplicate
        if r.link_url not in find_urls(r.body):
            links_moved.append(f"{words['link']}: {r.link_url}")
        if r.content_type in ("link", "video_link"):
            draft.warn(
                "LINK_MOVED_TO_BOTTOM", f"{r.content_type} post turned into a discussion with the link at the bottom"
            )
            r.content_type = "text"
        r.link_url = None
    for line in links_moved:
        draft.tail.append(Block(line, "link"))

    # 5. Question-style title, if it fits.
    if r.title and not ends_with_question(r.title):
        question_title = f"{r.title.strip().rstrip('.!:;')}: {words['title_q']}"
        limit = spec.get("title_max_chars")
        if limit is None or measure(question_title, spec.get("count_unit", "codepoints")) <= int(limit):
            r.title = question_title
            draft.warn("TITLE_QUESTION_ADDED", "title turned into a question for a discussion-first post")
        else:
            draft.warn("TITLE_NOT_QUESTION", "no room within the title limit to add a question")


CTA_LINE_MAX_WORDS = 12


def is_pure_cta_line(line: str) -> bool:
    """A short, non-question line that starts with a call to action ("Subscribe for more!", "Use code X")."""
    text = line.strip().lstrip("-*>#\u2022 ").strip()
    if not text or ends_with_question(text) or len(text.split()) > CTA_LINE_MAX_WORDS:
        return False
    start = re.sub(r"^[^\w]+", "", unicodedata.normalize("NFKC", text).casefold())
    return any(start.startswith(unicodedata.normalize("NFKC", hit).casefold()) for hit in find_cta(text))


# ------------------------------------------------------------------ mentions

MENTION_PLATFORMS = {"lemmy", "discourse", "devto", "forem"}
_BARE_MENTION = re.compile(r"(?<![\w@/.])@([A-Za-z0-9_]{1,64})\b(?!@)")


def adapt_mentions(draft: Draft, platform: str) -> None:
    """Mention style per platform: Lemmy needs @user@instance (a bare @user gets this community's
    instance); Discourse and Dev.to use @user. The ``mentions`` list is rewritten the same way."""
    r = draft.rendition
    before = (r.body, list(r.mentions))
    names = [m.strip() for m in r.mentions if m.strip()]
    if platform == "lemmy":
        if draft.instance_host is None:
            if names or _BARE_MENTION.search(r.body):
                draft.warn("MENTION_INSTANCE_UNKNOWN", "could not tell the Lemmy instance; mentions left as written")
            r.mentions = ["@" + n.lstrip("@") for n in names]
            return
        host = draft.instance_host
        r.body = _BARE_MENTION.sub(lambda m: f"@{m.group(1)}@{host}", r.body)
        r.mentions = ["@" + n.lstrip("@") + ("" if "@" in n.lstrip("@") else f"@{host}") for n in names]
    else:
        r.mentions = ["@" + n.lstrip("@") for n in names]
    if (r.body, r.mentions) != before:
        draft.warn("MENTIONS_ADAPTED", f"mentions written in {platform} style: {', '.join(r.mentions) or 'body only'}")


def _is_link_line(line: str) -> bool:
    text = line.strip()
    urls = find_urls(text)
    return bool(urls) and text.strip("<>()[]") in urls


# ------------------------------------------------------------------ article


# Tags whose symbols carry the meaning ("c++" must not become "c").
SYMBOL_TAGS = {"c++": "cpp", "c#": "csharp", "f#": "fsharp", ".net": "dotnet", "node.js": "node", "vue.js": "vue"}


def article(draft: Draft, platform: str, spec: dict) -> None:
    r = draft.rendition
    if not r.canonical_url and r.link_url:
        r.canonical_url = r.link_url
        draft.warn("CANONICAL_SET", f"canonical_url set to the original {r.link_url}")
    if platform in ("devto", "forem"):
        tags = []
        for tag in r.tags:
            lowered = tag.strip().lstrip("#").lower()
            clean = SYMBOL_TAGS.get(lowered) or "".join(c for c in ascii_fold(lowered) if c.isascii() and c.isalnum())
            if clean and clean not in tags:
                tags.append(clean)
        tags, dropped = tags[:4], tags[4:]
        if tags != r.tags:
            note = f"; dropped {', '.join(dropped)}" if dropped else ""
            draft.warn("TAGS_NORMALISED", f"Dev.to tags: {', '.join(r.tags)} -> {', '.join(tags)}{note}")
        r.tags = tags


# ------------------------------------------------------------------ slack


def slack(draft: Draft, platform: str, spec: dict) -> None:
    r = draft.rendition
    main = html_to_markdown(r.body) if r.body_format == "html" else r.body
    if r.title:
        main = f"**{r.title.strip()}**\n{main}"
        r.title = None
        draft.warn("TITLE_FOLDED_INTO_TEXT", "Slack messages have no title; it is the first (bold) line")
    if r.tags:
        draft.warn("TAGS_DROPPED", "Slack messages have no tags; dropped " + ", ".join(r.tags))
        r.tags = []
    r.body = markdown_to_mrkdwn(main, r.mentions)
    r.body_format = "markdown"
    if r.link_url and r.link_url not in r.body:
        draft.tail.append(Block(f"<{r.link_url}>", "link"))
    draft.warn("FORMAT_MRKDWN", "converted to Slack mrkdwn")


# ------------------------------------------------------------------ newsletter / mrss / generic


def newsletter(draft: Draft, platform: str, spec: dict) -> None:
    draft.rendition.title = draft.rendition.title or None  # subject is derived/fitted in fit_title


def mrss(draft: Draft, platform: str, spec: dict) -> None:
    r = draft.rendition
    if r.body_format == "html":
        r.body = html_to_markdown(r.body)
    if r.body_format != "plain":
        r.body = markdown_to_plain(r.body)
        r.body_format = "plain"
        draft.warn("FORMAT_PLAIN", "MRSS description converted to plain text")


def generic(draft: Draft, platform: str, spec: dict) -> None:
    return None


PROFILES = {
    "forum": forum,
    "article": article,
    "slack": slack,
    "newsletter": newsletter,
    "mrss": mrss,
    "generic": generic,
}
TITLE_NEEDED = {"forum", "article", "newsletter", "mrss"}
