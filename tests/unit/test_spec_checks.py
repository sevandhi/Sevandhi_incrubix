"""check_spec: field-level DEST_SPEC_VIOLATION reasons (hard case: title too long, media not accepted)."""

import pytest

from app.rules.spec import check_spec, effective_spec

DISCOURSE_TOPIC = {
    "key": "topic",
    "content_types": ["text", "image", "video_link"],
    "spec": {"title_max_chars": 255, "body_max_chars": 32000, "links_allowed": True},
}
LEMMY_TEXT = {"key": "post", "content_types": ["text", "link"], "spec": {"title_max_chars": 200}}


def rules_of(violations):
    return [(v.field, v.rule) for v in violations]


def test_fitting_post_has_no_violations():
    assert check_spec({"title": "Short title", "body": "Hello"}, DISCOURSE_TOPIC, None) == []


def test_title_too_long_reports_limit_and_actual():
    [v] = check_spec({"title": "x" * 300, "body": "b"}, DISCOURSE_TOPIC, None)
    assert (v.field, v.rule, v.limit, v.actual) == ("title", "title_max_chars", 255, 300)


def test_title_at_limit_is_ok():
    assert check_spec({"title": "x" * 255}, DISCOURSE_TOPIC, None) == []


def test_all_violations_listed_not_just_first():
    rendition = {
        "title": "t" * 300,
        "body": "b",
        "media": [{"url": "https://cdn.example/a.webp", "mime": "image/webp"}],
    }
    assert rules_of(check_spec(rendition, LEMMY_TEXT, {"title_max_chars": 255})) == [
        ("title", "title_max_chars"),
        ("media[0]", "content_types"),
    ]


def test_overrides_win_over_manifest():
    spec = effective_spec(DISCOURSE_TOPIC, {"title_max_chars": 100})
    assert spec["title_max_chars"] == 100 and spec["content_types"] == ["text", "image", "video_link"]
    assert rules_of(check_spec({"title": "x" * 150}, DISCOURSE_TOPIC, {"title_max_chars": 100})) == [
        ("title", "title_max_chars")
    ]


def test_flat_spec_dict_is_accepted():
    assert rules_of(check_spec({"body": "x" * 11}, {"body_max_chars": 10}, None)) == [("body", "body_max_chars")]


def test_content_type_not_accepted():
    assert rules_of(check_spec({"content_type": "video", "body": "b"}, LEMMY_TEXT, None)) == [
        ("content_type", "content_types")
    ]


def test_mime_from_extension_when_missing():
    assert rules_of(check_spec({"media": [{"url": "https://cdn.example/clip.mp4?x=1"}]}, LEMMY_TEXT, None)) == [
        ("media[0]", "content_types")
    ]


def test_unknown_media_type_is_a_violation_when_types_are_restricted():
    [v] = check_spec({"media": [{"url": "https://cdn.example/blob"}]}, LEMMY_TEXT, None)
    assert v.rule == "mime_types" and "unknown" in v.message


def test_mime_wildcards():
    spec = {"content_types": ["text", "image"], "mime_types": ["image/png", "image/jpe*"]}
    ok = {"media": [{"url": "a", "mime": "image/jpeg"}]}
    bad = {"media": [{"url": "a", "mime": "image/webp"}]}
    assert check_spec(ok, spec, None) == []
    assert rules_of(check_spec(bad, spec, None)) == [("media[0]", "mime_types")]


def test_media_size_duration_and_count():
    spec = {"media_max_bytes": 1000, "video_max_bytes": 5000, "media_max_duration_s": 60, "media_max_count": 1}
    rendition = {"media": [{"url": "a.png", "bytes": 1001}, {"url": "b.mp4", "bytes": 4000, "duration_s": 61}]}
    assert rules_of(check_spec(rendition, spec, None)) == [
        ("media", "media_max_count"),
        ("media[0].bytes", "media_max_bytes"),
        ("media[1].duration_s", "media_max_duration_s"),
    ]


def test_unknown_size_is_not_guessed():
    assert check_spec({"media": [{"url": "a.png"}]}, {"media_max_bytes": 10}, None) == []


def test_devto_tags():
    spec = {"tag_max": 4, "tag_pattern": "[a-z0-9]+"}
    rendition = {"tags": ["ai", "Video", "web-dev", "go", "rust"]}
    assert rules_of(check_spec(rendition, spec, None)) == [
        ("tags", "tag_max"),
        ("tags[1]", "tag_pattern"),
        ("tags[2]", "tag_pattern"),
    ]


def test_links_not_allowed():
    rendition = {"body": "see https://example.org", "link_url": "https://example.org/x"}
    assert rules_of(check_spec(rendition, {"links_allowed": False}, None)) == [
        ("body", "links_allowed"),
        ("link_url", "links_allowed"),
    ]


def test_title_required_and_min_length():
    spec = {"title_required": True, "title_min_chars": 15, "body_min_chars": 20}
    assert rules_of(check_spec({"title": "", "body": "short"}, spec, None)) == [
        ("title", "title_required"),
        ("title", "title_min_chars"),
        ("body", "body_min_chars"),
    ]


@pytest.mark.parametrize("unit,expected", [("codepoints", 6), ("graphemes", 3), ("utf16", 8), ("bytes", 17)])
def test_count_units(unit, expected):
    # Tamil syllable KA+AA (2 code points), thumbs-up + skin tone (2), e + combining accent (2): 3 graphemes
    title = "கா" + "\N{THUMBS UP SIGN}\N{EMOJI MODIFIER FITZPATRICK TYPE-4}" + "e\N{COMBINING ACUTE ACCENT}"
    [v] = check_spec({"title": title}, {"title_max_chars": 1, "count_unit": unit}, None)
    assert v.actual == expected


def test_tamil_title_counted_in_characters_not_bytes():
    title = "தமிழ்" * 60  # 300 code points, 900 UTF-8 bytes
    [v] = check_spec({"title": title}, DISCOURSE_TOPIC, None)
    assert v.actual == 300
