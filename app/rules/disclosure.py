"""AI-content disclosure: which text, which language, is it already there, and how to insert it.

Language choice: the community's language first (rules.languages, from the destination), then the
content's language, then English. If the content is in another supported language, that line is
added after a " / " so both the community and the post's readers can read it.
"""

from __future__ import annotations

import html
import re
import unicodedata
from functools import cache
from pathlib import Path

import yaml

from app.rules.lang import detect_language, normalize_lang

_YAML = Path(__file__).with_name("disclosure.yaml")


@cache
def builtin_texts() -> dict[str, str]:
    with _YAML.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return {normalize_lang(k) or k: str(v) for k, v in data.items()}


def all_texts(ruleset) -> dict[str, str]:
    """Built-in texts overlaid with the destination's own ``text_by_lang``."""
    texts = dict(builtin_texts())
    if ruleset is not None:
        texts.update(ruleset.disclosure.text_by_lang)
    return texts


def content_language(rendition) -> str | None:
    """The declared language of the rendition, or a guess from its text (None when unsure)."""
    declared = normalize_lang(rendition.language)
    if declared:
        return declared
    return detect_language(f"{rendition.title or ''}\n{rendition.body or ''}")


def choose_text(ruleset, rendition) -> tuple[str, str, list[str]]:
    """Return (text, language used, warnings)."""
    texts = all_texts(ruleset)
    community = ruleset.languages[0] if ruleset is not None and ruleset.languages else None
    content = content_language(rendition)
    wanted = community or content or "en"
    warnings: list[str] = []
    if wanted in texts:
        lang = wanted
    else:
        lang = "en"
        warnings.append(f"DISCLOSURE_LANG_FALLBACK: no disclosure text for '{wanted}'; used English")
    text = texts[lang]
    if content and content != lang and content in texts:
        text = f"{text} / {texts[content]}"
    return text, lang, warnings


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).casefold().strip()


def has_disclosure(rendition, ruleset) -> bool:
    """True if one of the known disclosure lines (any language) is already in the title or body.
    Only exact known lines count: a vague "made with AI" might not satisfy a community, and adding
    the line twice is harmless while missing it is a rule violation."""
    haystack = _squash(html.unescape(f"{rendition.title or ''}\n{rendition.body or ''}"))
    return any(_squash(text) in haystack for text in all_texts(ruleset).values())


def insert_disclosure(body: str, text: str, placement: str, body_format: str = "markdown") -> str:
    """Put the disclosure line at the top or bottom of the body, as its own paragraph."""
    block = f"<p>{html.escape(text)}</p>" if body_format == "html" else text
    body = (body or "").strip("\n")
    if not body.strip():
        return block
    separator = "\n" if body_format == "html" else "\n\n"
    return f"{block}{separator}{body}" if placement == "top" else f"{body.rstrip()}{separator}{block}"
