"""Language codes and a small, explainable language guesser (no model, no network).

``normalize_lang`` turns "en-US", "EN", "eng" or "English" into "en".
``detect_language`` is only used when the caller did not say which language the content is in:
  1. non-Latin scripts are decided by script (Devanagari -> hi, Tamil -> ta, kana -> ja, ...);
  2. Latin text is decided by counting common function words ("the", "und", "les", ...).
It returns ``None`` when it is not confident, and the rules engine treats ``None`` conservatively.
"""

from __future__ import annotations

import re
import unicodedata

_ALIASES = {
    "eng": "en",
    "english": "en",
    "spa": "es",
    "spanish": "es",
    "español": "es",
    "espanol": "es",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "deutsch": "de",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "français": "fr",
    "francais": "fr",
    "por": "pt",
    "portuguese": "pt",
    "português": "pt",
    "portugues": "pt",
    "hin": "hi",
    "hindi": "hi",
    "jpn": "ja",
    "japanese": "ja",
    "日本語": "ja",
    "tam": "ta",
    "tamil": "ta",
    "தமிழ்": "ta",
    "ita": "it",
    "italian": "it",
    "nld": "nl",
    "dut": "nl",
    "dutch": "nl",
    "zho": "zh",
    "chi": "zh",
    "chinese": "zh",
    "kor": "ko",
    "korean": "ko",
    "rus": "ru",
    "russian": "ru",
    "ara": "ar",
    "arabic": "ar",
}


def normalize_lang(code: str | None) -> str | None:
    """Return the lower-case primary language subtag, or None when empty/unknown ("und", "mul", "*")."""
    if code is None:
        return None
    text = str(code).strip().lower().replace("_", "-")
    if not text or text in ("und", "mul", "zxx", "*", "any", "unknown"):
        return None
    text = _ALIASES.get(text, text)
    primary = text.split("-")[0]
    return _ALIASES.get(primary, primary) or None


def normalize_langs(codes) -> list[str]:
    """Normalise a list (or a "en;de" / "en,de" string) of language codes, dropping unknowns and duplicates."""
    if codes is None:
        return []
    if isinstance(codes, str):
        codes = re.split(r"[;,|/\s]+", codes)
    out: list[str] = []
    for code in codes:
        lang = normalize_lang(code)
        if lang and lang not in out:
            out.append(lang)
    return out


# ------------------------------------------------------------------ detection

# (first code point, last code point, language) for scripts that identify a language well enough.
_SCRIPTS = [
    (0x0900, 0x097F, "hi"),  # Devanagari (also Marathi/Nepali; hi is the best single guess)
    (0x0B80, 0x0BFF, "ta"),  # Tamil
    (0x3040, 0x30FF, "ja"),  # Hiragana + Katakana
    (0xAC00, 0xD7A3, "ko"),  # Hangul syllables
    (0x0400, 0x04FF, "ru"),  # Cyrillic
    (0x0600, 0x06FF, "ar"),  # Arabic
    (0x0E00, 0x0E7F, "th"),  # Thai
    (0x0980, 0x09FF, "bn"),  # Bengali
    (0x0C00, 0x0C7F, "te"),  # Telugu
    (0x0370, 0x03FF, "el"),  # Greek
    (0x0590, 0x05FF, "he"),  # Hebrew
]
_HAN = [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF)]

_STOPWORDS = {
    "en": "the and is are was this that with for you have not but what how from they will your can about it's i'm".split(),
    "de": "der die das und ist nicht ein eine ich mit für auf sie wir den dem auch wie aber oder wenn sich habe".split(),
    "es": "el la los las que es en un una por para con del al pero como más muy esto está son mi".split(),
    "fr": "le la les des est et une un pour que dans avec pas sur ce cette qui vous nous mais je".split(),
    "pt": "o a os as que é em um uma para com não do da dos das mas como mais muito você isso está".split(),
    "it": "il lo gli che è un una per con non della delle sono questo come più anche ma".split(),
    "nl": "de het een en is niet van voor met op dat die zijn ook maar wat hoe wij".split(),
}
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")


def _script_language(text: str) -> str | None:
    counts: dict[str, int] = {}
    han = letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _HAN):
            han += 1
            continue
        for lo, hi, lang in _SCRIPTS:
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if letters == 0:
        return None
    if counts.get("ja"):  # any kana means Japanese, even with many kanji
        return "ja"
    best = max(counts, key=counts.get, default=None)
    if best and counts[best] / letters >= 0.3:
        return best
    if han / letters >= 0.3:
        return "zh"
    return None


def detect_language(text: str) -> str | None:
    """Best-effort guess of the language of ``text``; None when unsure."""
    text = unicodedata.normalize("NFKC", text or "")
    by_script = _script_language(text)
    if by_script:
        return by_script
    words = [w.casefold() for w in _WORD.findall(text)]
    if not words:
        return None
    scores = {lang: sum(1 for w in words if w in stops) for lang, stops in _STOPWORDS.items()}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    # Confident only with at least 2 hits and a clear lead over the runner-up.
    if top >= 2 and top >= 2 * second:
        return best
    return None
