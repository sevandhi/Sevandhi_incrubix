"""Small text edits used by the tailoring profiles (all pure functions on strings)."""

from __future__ import annotations

import html
import re
import unicodedata

from app.rules.links import find_urls, is_shortener, matches_domain

MD_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
HASHTAG = re.compile(r"(?<![\w&#/])#(?=[\w-]*[^\W\d_])[\w-]+")


def tidy(text: str) -> str:
    """Collapse runs of spaces and of blank lines left behind by removals."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def remove_links(text: str, keep) -> tuple[str, list[str]]:
    """Remove every URL for which ``keep(url)`` is False. Markdown links keep their text.
    Returns (new text, removed URLs)."""
    removed: list[str] = []

    def md(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if keep(url):
            return match.group()
        removed.append(url)
        return label

    text = MD_LINK.sub(md, text)
    for url in find_urls(text):
        if not keep(url):
            removed.append(url)
            text = re.sub(rf"<?{re.escape(url)}(?:\|[^>]*)?>?", "", text)
    return tidy(text), removed


def link_keeper(link_policy: str, allowlist: list[str], links_allowed: bool = True):
    """A predicate saying whether a URL may stay under a link policy (and the spec's links_allowed)."""

    def keep(url: str) -> bool:
        if not links_allowed or link_policy == "none":
            return False
        if link_policy == "no_shorteners":
            return not is_shortener(url)
        if link_policy == "allowlist":
            return any(matches_domain(url, d) for d in allowlist)
        return True

    return keep


def strip_hashtags(text: str) -> tuple[str, list[str]]:
    found = HASHTAG.findall(text)
    return (tidy(HASHTAG.sub("", text)) if found else text), found


def ends_with_question(text: str) -> bool:
    last = text.strip().splitlines()[-1].strip() if text.strip() else ""
    return last.endswith(("?", "\uff1f", "\u061f"))


def ascii_fold(text: str) -> str:
    """ "Künstliche Intelligenz" -> "Kunstliche Intelligenz" (drop accents)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def html_to_markdown(text: str) -> str:
    """Rough HTML -> markdown for profiles that need plain text (links kept as [text](url))."""
    text = re.sub(r'(?is)<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li)>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return tidy(html.unescape(text))


def markdown_to_plain(text: str) -> str:
    """Markdown -> plain text (for MRSS descriptions): links become "text (url)"."""
    text = MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1) != m.group(2) else m.group(2), text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"(\*\*|__|~~|`)", "", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+)(?<!\s)\*(?![\w*])", r"\1", text)
    return tidy(text)


def markdown_to_mrkdwn(text: str, mentions: list[str]) -> str:
    """Markdown -> Slack mrkdwn: *bold*, _italic_, ~strike~, <url|text> links, <@U123> mentions,
    and & < > escaped as Slack requires."""
    tokens: list[str] = []

    def stash(value: str) -> str:
        tokens.append(value)
        return f"\x00{len(tokens) - 1}\x00"

    text = re.sub(r"<[@#!][^<>\s]+>|<https?://[^<>\s]+>", lambda m: stash(m.group()), text)  # already Slack
    text = MD_LINK.sub(lambda m: stash(f"<{m.group(2)}|{m.group(1)}>"), text)
    for url in find_urls(text):
        if url.startswith(("http://", "https://")):
            text = text.replace(url, stash(f"<{url}>"))
    for mention in mentions:
        user = mention.lstrip("@")
        if re.fullmatch(r"[UW][A-Z0-9]{6,}", user):
            text = re.sub(rf"(?<!\w)@{re.escape(user)}(?!\w)", lambda _m, u=user: stash(f"<@{u}>"), text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", lambda m: stash(f"*{m.group(1).strip()}*"), text)
    text = re.sub(r"(\*\*|__)(.+?)\1", lambda m: stash(f"*{m.group(2)}*"), text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"_\1_", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    text = re.sub(r"(?m)^(\s*)[-*]\s+", "\\1\u2022 ", text)
    while "\x00" in text:
        text = re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], text)
    return tidy(text)
