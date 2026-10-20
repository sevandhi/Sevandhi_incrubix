"""truncate_words and grapheme segmentation: examples plus Hypothesis properties."""

import re
import unicodedata

from hypothesis import given, settings
from hypothesis import strategies as st

from app.rules.graphemes import boundaries, graphemes, measure
from app.tailoring.truncate import ELLIPSIS, no_space_script, truncate_with_reason, truncate_words

FAMILY = "\N{MAN}\N{ZERO WIDTH JOINER}\N{WOMAN}\N{ZERO WIDTH JOINER}\N{GIRL}"
FLAG_IN = "\N{REGIONAL INDICATOR SYMBOL LETTER I}\N{REGIONAL INDICATOR SYMBOL LETTER N}"
THUMBS_TONE = "\N{THUMBS UP SIGN}\N{EMOJI MODIFIER FITZPATRICK TYPE-4}"
TAMIL_KAA = "\N{TAMIL LETTER KA}\N{TAMIL VOWEL SIGN AA}"
DEVANAGARI_KSSA = "\N{DEVANAGARI LETTER KA}\N{DEVANAGARI SIGN VIRAMA}\N{DEVANAGARI LETTER SSA}"
URL = re.compile(r"https?://[^\s)\]]+")  # a URL ends at whitespace or at the ")" of a markdown link


# ---------------------------------------------------------------- graphemes


def test_grapheme_examples():
    assert graphemes(FAMILY) == [FAMILY]
    assert graphemes(FLAG_IN + FLAG_IN) == [FLAG_IN, FLAG_IN]
    assert graphemes(THUMBS_TONE + "a") == [THUMBS_TONE, "a"]
    assert graphemes(TAMIL_KAA + TAMIL_KAA) == [TAMIL_KAA, TAMIL_KAA]
    assert graphemes(DEVANAGARI_KSSA) == [DEVANAGARI_KSSA]
    assert graphemes("e\N{COMBINING ACUTE ACCENT}x") == ["e\N{COMBINING ACUTE ACCENT}", "x"]
    assert graphemes("a\r\nb") == ["a", "\r\n", "b"]
    assert graphemes("\n\N{COMBINING ACUTE ACCENT}") == ["\n", "\N{COMBINING ACUTE ACCENT}"]


@given(st.text())
def test_graphemes_rejoin_to_the_input(text):
    assert "".join(graphemes(text)) == text


# ---------------------------------------------------------------- truncate examples


def test_unchanged_when_it_fits():
    assert truncate_words("short", 10) == "short"
    assert truncate_with_reason("short", 5) == ("short", None)


def test_cuts_at_word_boundary():
    assert truncate_words("Hello world this is long", 15) == "Hello world" + ELLIPSIS


def test_never_cuts_inside_url_or_mention():
    assert truncate_words("read https://example.com/a/very/long/path today", 30) == "read" + ELLIPSIS
    assert truncate_words("thanks @someone@lemmy.example for this", 20) == "thanks" + ELLIPSIS
    assert truncate_words("see [the guide](https://example.com/guide) now", 25) == "see" + ELLIPSIS


def test_ellipsis_is_not_glued_to_a_url():
    result = truncate_words("link https://example.com/a and then much more text", 28)
    assert result == "link https://example.com/a " + ELLIPSIS


def test_cjk_falls_back_to_character_boundary():
    text, how = truncate_with_reason("動画編集のコツを紹介します動画編集のコツを紹介します", 10)
    assert text == "動画編集のコツを紹" + ELLIPSIS and how == "character boundary"


def test_tamil_and_emoji_title_never_split():
    title = ("தமிழ் வீடியோ எடிட்டிங் " + FAMILY + " ") * 12  # ~270+ code points, as in the live-session question
    result = truncate_words(title, 100)
    assert measure(result) <= 100
    kept = result[: -len(ELLIPSIS)]
    assert title.startswith(kept) and len(kept) in boundaries(title)
    assert not kept.endswith("\N{ZERO WIDTH JOINER}")


def test_long_single_word_is_cut_at_a_character():
    assert truncate_words("Supercalifragilisticexpialidocious", 10) == "Supercali" + ELLIPSIS


def test_only_a_url_gives_empty_rather_than_a_broken_url():
    assert truncate_words("https://example.com/a/very/long/path", 10) == ""


def test_grapheme_unit():
    text = FAMILY * 5
    assert truncate_words(text, 3, unit="graphemes") == FAMILY * 2 + ELLIPSIS


# ---------------------------------------------------------------- properties

WORDS = st.one_of(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyzABC", min_size=1, max_size=12),
    st.sampled_from(
        [
            "தமிழ்",
            "வீடியோ",
            "हिन्दी",
            "क्षत्रिय",
            "café",
            "e\N{COMBINING ACUTE ACCENT}t\N{COMBINING ACUTE ACCENT}",
            FAMILY,
            FLAG_IN,
            THUMBS_TONE,
            "動画編集のコツ",
            "ภาษาไทย",
            "Ünïcödé",
        ]
    ),
    st.builds(
        lambda a, b: f"https://{a}.example/{b}?q=1",
        st.text(alphabet="abcxyz", min_size=1, max_size=8),
        st.text(alphabet="abc/-_", max_size=15),
    ),
    st.builds(lambda a: f"@{a}", st.text(alphabet="abcxyz", min_size=1, max_size=8)),
    st.builds(lambda a: f"!{a}@lemmy.example", st.text(alphabet="abcxyz", min_size=1, max_size=8)),
    st.builds(lambda a: f"[{a}](https://example.com/{a})", st.text(alphabet="ab c", min_size=1, max_size=8)),
)
TEXTS = st.lists(st.tuples(WORDS, st.sampled_from([" ", "\n", "  ", ""])), max_size=40).map(
    lambda pairs: "".join(w + s for w, s in pairs)
)
UNITS = st.sampled_from(["codepoints", "graphemes", "utf16", "bytes"])


def _kept(result: str) -> str:
    if result.endswith(ELLIPSIS):
        result = result[: -len(ELLIPSIS)]
        if result.endswith(" "):
            result = result[:-1]
    return result


@settings(max_examples=400, deadline=None)
@given(TEXTS, st.integers(min_value=0, max_value=200), UNITS)
def test_truncate_properties(text, limit, unit):
    result = truncate_words(text, limit, unit=unit)
    # 1. fits
    assert measure(result, unit) <= limit
    # 2. unchanged when it already fits
    if measure(text, unit) <= limit:
        assert result == text
        return
    kept = _kept(result)
    # 3. a prefix of the original
    assert text.startswith(kept)
    # 4. at a grapheme boundary
    assert len(kept) in boundaries(text)
    # 5. no broken URL: every URL in the result is a whole URL of the original
    for match in URL.finditer(kept):
        original = URL.match(text, match.start())
        assert original is not None and original.end() == match.end(), (kept, text)
    # 6. word boundary unless the text has no spaces there (CJK, single long word) or a token was dropped
    if kept and len(kept) < len(text):
        nxt = text[len(kept)]
        assert (
            nxt.isspace()
            or unicodedata.category(nxt).startswith("P")
            or no_space_script(nxt)
            or no_space_script(kept[-1])
            or not re.search(r"\s", kept)
            or nxt in "[<@!#h"
        ), (kept, text)


@settings(max_examples=300, deadline=None)
@given(st.text(), st.integers(min_value=0, max_value=60))
def test_truncate_any_unicode_fits_and_is_a_safe_prefix(text, limit):
    result = truncate_words(text, limit)
    assert len(result) <= limit
    kept = _kept(result)
    assert text.startswith(kept)
    assert len(kept) in boundaries(text)


def test_prepend_character_joins_the_next_one():
    """Arabic number sign (a Prepend character, UAX #29 GB9b) stays with the digit after it."""
    assert graphemes("\N{ARABIC NUMBER SIGN}1a") == ["\N{ARABIC NUMBER SIGN}1", "a"]
