"""User-perceived characters (grapheme clusters) and length measuring.

A grapheme cluster is what a reader sees as one character: "é" written as e + combining accent,
the Tamil syllable "கா" (two code points), the emoji family 👨‍👩‍👧 (five code points joined by ZWJ)
or a flag 🇮🇳 (two regional indicators). Cutting text inside a cluster leaves a broken character,
so truncation only ever cuts at the boundaries returned here.

This is a compact version of the Unicode UAX #29 "extended grapheme cluster" rules, covering what
real posts contain: combining marks, spacing marks, ZWJ emoji sequences, variation selectors, skin
tones, flags, Hangul jamo, Indic conjuncts (virama + consonant), Prepend characters and CR LF. It needs no dependency.

Length units (``measure``): platforms count differently, so specs may say which unit to use.
  codepoints (default)  Python len(); what Ruby, Rust .chars() and Postgres varchar count
  graphemes             user-perceived characters
  utf16                 JavaScript string.length
  bytes                 UTF-8 bytes
"""

from __future__ import annotations

import unicodedata

ZWJ = "\u200d"
UNITS = ("codepoints", "graphemes", "utf16", "bytes")

# Viramas that join two consonants into one conjunct (Unicode 15.1 rule GB9c). Tamil's pulli is not one:
# it is shown visibly, so a Tamil consonant + pulli + consonant is two clusters.
_LINKERS = {"\u094d", "\u09cd", "\u0acd", "\u0b4d", "\u0c4d", "\u0d4d"}
_REGIONAL_INDICATORS = "".join(chr(cp) for cp in range(0x1F1E6, 0x1F200))  # flag letters A-Z


def _is_extend(ch: str) -> bool:
    """Characters that never start a new cluster: marks, joiners, variation selectors, skin tones, tags."""
    cp = ord(ch)
    return (
        unicodedata.category(ch) in ("Mn", "Me", "Mc")
        or ch in ("\u200c", ZWJ)
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
        or 0xE0100 <= cp <= 0xE01EF
    )


# Prepend characters (rule GB9b): they attach to the character AFTER them, e.g. the Arabic number sign.
_PREPEND_RANGES = [
    (0x0600, 0x0605),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x0D4E, 0x0D4E),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x111C2, 0x111C3),
    (0x1193F, 0x1193F),
    (0x11941, 0x11941),
    (0x11A3A, 0x11A3A),
    (0x11A84, 0x11A89),
    (0x11D46, 0x11D46),
    (0x11F02, 0x11F02),
]


def _is_prepend(ch: str) -> bool:
    return any(lo <= ord(ch) <= hi for lo, hi in _PREPEND_RANGES)


def _is_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_control(ch: str) -> bool:
    return unicodedata.category(ch) in ("Cc", "Zl", "Zp") and ch != ZWJ


def _is_pictographic(ch: str) -> bool:
    cp = ord(ch)
    return unicodedata.category(ch) == "So" or 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF


def _hangul_type(ch: str) -> str | None:
    cp = ord(ch)
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97F:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    return None


def _joins(cluster: str, ch: str) -> bool:
    """Should ``ch`` stay in the same cluster as the text in ``cluster``?"""
    prev = cluster[-1]
    if prev == "\r" and ch == "\n":
        return True
    if _is_control(prev) or _is_control(ch):
        return False
    if _is_prepend(prev):
        return True
    if _is_extend(ch):
        return True
    if prev == ZWJ and any(_is_pictographic(c) for c in cluster):
        return True  # emoji ZWJ sequence: 👩 ZWJ 💻
    if _is_regional_indicator(prev) and _is_regional_indicator(ch):
        trailing = len(cluster) - len(cluster.rstrip(_REGIONAL_INDICATORS))
        return trailing % 2 == 1  # flags are pairs: join the 2nd indicator, not the 3rd
    hp, hc = _hangul_type(prev), _hangul_type(ch)
    if hp and hc:
        return (
            (hp == "L" and hc in ("L", "V", "LV", "LVT"))
            or (hp in ("LV", "V") and hc in ("V", "T"))
            or (hp in ("LVT", "T") and hc == "T")
        )
    stripped = cluster.rstrip(ZWJ)
    if stripped and stripped[-1] in _LINKERS and unicodedata.category(ch) == "Lo":
        return unicodedata.name(stripped[-1], "").split()[0] == unicodedata.name(ch, "").split()[0]
    return False


def graphemes(text: str) -> list[str]:
    """Split text into grapheme clusters. ``"".join(graphemes(t)) == t`` always holds."""
    clusters: list[str] = []
    for ch in text:
        if clusters and _joins(clusters[-1], ch):
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def boundaries(text: str) -> list[int]:
    """Code-point offsets where a cut is safe, including 0 and len(text)."""
    offsets = [0]
    for cluster in graphemes(text):
        offsets.append(offsets[-1] + len(cluster))
    return offsets


def measure(text: str | None, unit: str = "codepoints") -> int:
    if not text:
        return 0
    if unit == "graphemes":
        return len(graphemes(text))
    if unit == "utf16":
        return len(text.encode("utf-16-le")) // 2
    if unit == "bytes":
        return len(text.encode("utf-8"))
    if unit == "codepoints":
        return len(text)
    raise ValueError(f"unknown length unit {unit!r}; use one of {UNITS}")
