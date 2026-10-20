"""Shorten text to a limit without breaking it.

truncate_words(text, limit) guarantees:
  * the result fits ``limit`` (in the spec's unit, ellipsis included);
  * it is a prefix of ``text`` (plus the ellipsis);
  * the cut is at a grapheme-cluster boundary (no half emoji, no orphan Tamil vowel sign);
  * the cut is never inside a URL, an @mention, a #hashtag, a Lemmy !community@instance, a Slack <...>
    token or a markdown [text](url) link (the whole token is dropped instead);
  * for space-separated languages the cut is at a word boundary; for scripts without spaces (Chinese,
    Japanese, Thai) it falls back to a grapheme boundary.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right

from app.rules.graphemes import boundaries, graphemes, measure

ELLIPSIS = "\u2026"

# Tokens that must never be split. Each pattern is searched on its own and overlapping matches are
# merged, so a run like "@name" glued to "https://..." is protected as one piece.
PROTECTED = [
    re.compile(r"\[[^\]\n]*\]\([^)\s]*\)"),  # markdown link [text](url)
    re.compile(r"<[^<>\s][^<>\n]*>"),  # Slack <url|text>, <@U123>, <#C123>
    re.compile(r"(?i:[a-z][a-z0-9+.-]*://|\bwww\.)\S+"),  # URLs (even when glued to a word)
    # @mention, @user@instance, !community@instance, #tag: up to the next space or punctuation (not \w, which
    # stops before Tamil/Devanagari vowel signs)
    re.compile(r"(?<![\w@])[@!#][^\s@!#,;:?()\[\]{}\"'<>]+(?:@[^\s,;:?()\[\]{}\"'<>]+)?"),
]


def no_space_script(ch: str) -> bool:
    """Scripts that are written without spaces between words."""
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0xF900 <= cp <= 0xFAFF  # Han
        or 0x3040 <= cp <= 0x30FF
        or 0x31F0 <= cp <= 0x31FF  # Hiragana, Katakana
        or 0x3000 <= cp <= 0x303F
        or 0xFF00 <= cp <= 0xFFEF  # CJK punctuation, full-width forms
        or 0x0E00 <= cp <= 0x0EFF  # Thai, Lao
        or 0x1000 <= cp <= 0x109F
        or 0x1780 <= cp <= 0x17FF  # Myanmar, Khmer
    )


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every protected token, overlapping ones merged, sorted by start.
    Edges are widened to grapheme boundaries (a regex can stop before a Tamil vowel sign)."""
    cuts = boundaries(text)

    def widen(span: tuple[int, int]) -> tuple[int, int]:
        start = cuts[bisect_right(cuts, span[0]) - 1]
        end = cuts[bisect_left(cuts, span[1])]
        return start, end

    spans = sorted(widen(m.span()) for pattern in PROTECTED for m in pattern.finditer(text))
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _fit_prefix(text: str, budget: int, unit: str) -> int:
    """Largest grapheme boundary b such that text[:b] fits the budget."""
    offset = size = 0
    for cluster in graphemes(text):  # cluster sizes add up in every unit, so count as we go
        size += measure(cluster, unit)
        if size > budget:
            break
        offset += len(cluster)
    return offset


def _cut_position(text: str, budget: int, unit: str) -> tuple[int, str]:
    """Where to cut (code-point offset) and how ("word boundary", "character boundary", ...)."""
    cut = _fit_prefix(text, budget, unit)
    how = "word boundary"
    inside_word = 0 < cut < len(text) and not text[cut].isspace() and not text[cut - 1].isspace()
    if inside_word:
        if no_space_script(text[cut - 1]) or no_space_script(text[cut]):
            how = "character boundary"
        else:
            # Last safe place before the cut: a space, or the end of a link/mention glued to this word.
            space = max(text.rfind(c, 0, cut) for c in (" ", "\n", "\t", "\N{IDEOGRAPHIC SPACE}"))
            token_end = max((end for _, end in _protected_spans(text) if end <= cut), default=-1)
            safe = max(space, token_end)
            if safe > 0 and text[:safe].strip():
                cut = safe
            else:
                how = "character boundary (single long word)"
    for start, end in _protected_spans(text):
        if start < cut < end:
            cut, how = start, "before a link/mention it would have split"
            break
    return cut, how


def truncate_with_reason(
    text: str, limit: int, *, ellipsis: str = ELLIPSIS, unit: str = "codepoints"
) -> tuple[str, str | None]:
    """Like truncate_words, and also says how the cut was made (None when nothing was cut)."""
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if measure(text, unit) <= limit:
        return text, None
    budget = limit - measure(ellipsis, unit)
    if budget <= 0:
        return "", "no room for any text"
    cut, how = _cut_position(text, budget, unit)
    kept = text[:cut].rstrip()
    while kept:
        # An ellipsis glued to a URL would change the URL, so put a space between them.
        ends_in_token = any(end == len(kept) for _, end in _protected_spans(kept))
        tail = (" " + ellipsis) if ends_in_token and ellipsis else ellipsis
        if measure(kept + tail, unit) <= limit:
            return kept + tail, how
        drop_from = max((start for start, end in _protected_spans(kept) if end == len(kept)), default=None)
        if drop_from is None:
            drop_from, _ = _cut_position(kept, measure(kept, unit) - 1, unit)
        kept = kept[:drop_from].rstrip()
    return "", "no room for any text"


def truncate_words(text: str, limit: int, *, ellipsis: str = ELLIPSIS, unit: str = "codepoints") -> str:
    """Cut ``text`` to at most ``limit`` (ellipsis included) at a safe boundary. Unchanged if it fits."""
    return truncate_with_reason(text, limit, ellipsis=ellipsis, unit=unit)[0]
