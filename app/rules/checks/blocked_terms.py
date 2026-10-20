"""blocked_terms: words the destination does not allow (whole-word, case-insensitive)."""

import re
import unicodedata

from app.errors import ErrorCode
from app.rules.checks.base import RuleContext, blocked, passed

NAME = "blocked_terms"
# Scripts written without spaces between words: a whole-word test cannot work, so use a substring test.
_NO_SPACE_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA", "THAI", "LAO", "KHMER", "MYANMAR")


def _pattern(term: str) -> re.Pattern[str]:
    norm = unicodedata.normalize("NFKC", term).casefold().strip()
    if any(any(s in unicodedata.name(ch, "") for s in _NO_SPACE_SCRIPTS) for ch in norm):
        return re.compile(re.escape(norm))
    return re.compile(rf"(?<!\w){re.escape(norm)}(?!\w)")


def check(ctx: RuleContext):
    r = ctx.rendition
    fields = {"title": r.title or "", "body": r.body or "", "tags": " ".join(r.tags), "link_url": r.link_url or ""}
    hits = []
    for term in ctx.ruleset.blocked_terms:
        if not term.strip():
            continue
        regex = _pattern(term)
        for name, text in fields.items():
            if regex.search(unicodedata.normalize("NFKC", text).casefold()):
                hits.append({"type": "blocked_term", "value": term, "field": name})
    if not hits:
        return passed(NAME)
    listed = ", ".join(f"'{h['value']}' in {h['field']}" for h in hits)
    return blocked(NAME, ErrorCode.DEST_RULE_BLOCKED_TERM, f"content contains blocked term(s): {listed}", evidence=hits)
