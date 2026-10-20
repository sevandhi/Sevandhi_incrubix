"""tailor(): every profile, the hard cases it fixes (title too long, media not accepted, disclosure),
and the promise that every change is visible in warnings and diff."""

from datetime import UTC, datetime

from app.contract.models import Rendition
from app.rules import check_spec, evaluate
from app.rules.graphemes import boundaries
from app.tailoring import choose_profile, tailor

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
LEMMY = {"connector_key": "lemmy", "platform_key": "lemmy", "surface": "post"}
DISCOURSE = {"connector_key": "discourse", "platform_key": "discourse", "surface": "topic"}
DEVTO = {"connector_key": "devto", "platform_key": "devto", "surface": "article"}
GHOST = {"connector_key": "ghost", "platform_key": "ghost", "surface": "post"}
SLACK = {"connector_key": "slack", "platform_key": "slack", "surface": "message"}
MRSS = {"connector_key": "mrss", "platform_key": "mrss", "surface": "item"}
NEWSLETTER = {"connector_key": "ghost", "platform_key": "ghost", "surface": "newsletter"}
WEBHOOK = {"connector_key": "webhook", "platform_key": "partner", "surface": "post"}
LEMMY_SPEC = {"content_types": ["text", "link"], "spec": {"title_max_chars": 200, "body_max_chars": 10000}}
DISCOURSE_SPEC = {
    "content_types": ["text", "image", "video_link"],
    "spec": {"title_max_chars": 255, "body_max_chars": 32000},
}


def rendition(**kw) -> dict:
    base = {
        "title": "How I cut my editing time in half",
        "language": "en",
        "ai_generated": False,
        "body": "I tried three workflows for a month and measured each one.",
    }
    return {**base, **kw}


def codes(result) -> list[str]:
    return [w.split(":")[0] for w in result.warnings]


def test_profile_choice():
    assert [choose_profile(_d(d)) for d in (LEMMY, DISCOURSE, DEVTO, GHOST, SLACK, MRSS, NEWSLETTER, WEBHOOK)] == [
        "forum",
        "forum",
        "article",
        "article",
        "slack",
        "mrss",
        "newsletter",
        "generic",
    ]


def _d(d):
    from app.contract.models import Destination

    return Destination.model_validate(d)


# ---------------------------------------------------------------- forum


def test_forum_lemmy_is_discussion_first():
    src = rendition(
        body="https://creator.dev/post\nI tried three workflows for a month.\nSubscribe for more!\n#ai #video",
        link_url="https://creator.dev/post",
        content_type="link",
        tags=["AI", "video"],
    )
    result = tailor(src, LEMMY, LEMMY_SPEC, {"link_policy": "any"})
    r = result.rendition
    assert r.title == "How I cut my editing time in half: what do you think?"
    lines = r.body.split("\n\n")
    assert lines[0] == "I tried three workflows for a month."  # context first
    assert lines[1].endswith("?")  # question
    assert lines[2] == "Link: https://creator.dev/post"  # link at the bottom, only once
    assert r.body.count("https://creator.dev/post") == 1
    assert "#ai" not in r.body and r.tags == [] and "Subscribe" not in r.body
    assert r.content_type == "text" and r.link_url is None
    assert {"CTA_REMOVED", "HASHTAGS_REMOVED", "TITLE_QUESTION_ADDED", "LINK_MOVED_TO_BOTTOM"} <= set(codes(result))
    assert result.ok


def test_forum_question_title_kept_and_no_extra_question():
    result = tailor(
        rendition(title="Which codec do you use?", body="Mine stutters. What do you use?"),
        DISCOURSE,
        DISCOURSE_SPEC,
        {},
    )
    assert result.rendition.title == "Which codec do you use?"
    assert result.rendition.body == "Mine stutters. What do you use?"


def test_forum_link_not_allowed_is_removed_and_said():
    result = tailor(
        rendition(body="Notes: https://creator.dev/a", link_url="https://creator.dev/b"),
        DISCOURSE,
        DISCOURSE_SPEC,
        {"link_policy": "none"},
    )
    assert "http" not in result.rendition.body and result.rendition.link_url is None
    assert any(w.startswith("LINK_REMOVED: 2 link(s)") for w in result.warnings)
    assert evaluate(result.rendition, {"link_policy": "none"}, [], NOW).action == "allow"


def test_forum_shortener_removed_but_normal_link_kept():
    result = tailor(
        rendition(body="a https://bit.ly/x and https://ffmpeg.org/doc"),
        DISCOURSE,
        DISCOURSE_SPEC,
        {"link_policy": "no_shorteners"},
    )
    assert "bit.ly" not in result.rendition.body and "https://ffmpeg.org/doc" in result.rendition.body


def test_discourse_keeps_tags_within_spec():
    result = tailor(
        rendition(tags=["Video Editing", "ai", "x" * 40]),
        DISCOURSE,
        {"spec": {"tag_max": 2, "tag_pattern": "[a-z0-9-]+", "tag_max_chars": 20}},
        {},
    )
    assert result.rendition.tags == ["videoediting", "ai"]
    assert "TAGS_CHANGED" in codes(result)


# ---------------------------------------------------------------- hard case: title too long + media not accepted


def test_title_too_long_and_webp_on_text_only_lemmy():
    src = rendition(
        title=("Editing workflow notes " * 14)[:300],
        language="en",
        media=[{"url": "https://cdn.example/frame.webp", "mime": "image/webp"}],
    )
    assert len(src["title"]) == 300
    before = check_spec(src, LEMMY_SPEC, None)
    assert [v.rule for v in before] == ["title_max_chars", "content_types"]
    result = tailor(src, LEMMY, LEMMY_SPEC, {})
    assert len(result.rendition.title) <= 200 and result.rendition.title.endswith("\N{HORIZONTAL ELLIPSIS}")
    assert any(
        w.startswith("TITLE_TRUNCATED: 300\N{RIGHTWARDS ARROW}") and "word boundary" in w for w in result.warnings
    )
    assert any(w.startswith("MEDIA_DROPPED") and "linked instead" in w for w in result.warnings)
    assert result.rendition.media == [] and "https://cdn.example/frame.webp" in result.rendition.body
    assert check_spec(result.rendition, LEMMY_SPEC, None) == [] and result.ok


def test_tamil_emoji_title_truncated_at_grapheme_boundary():
    family = "\N{MAN}\N{ZERO WIDTH JOINER}\N{WOMAN}\N{ZERO WIDTH JOINER}\N{GIRL}"
    title = ("தமிழ் வீடியோ எடிட்டிங் குறிப்புகள் " + family + " ") * 8
    result = tailor(rendition(title=title, language="ta"), DISCOURSE, {"spec": {"title_max_chars": 100}}, {})
    kept = result.rendition.title.rstrip("\N{HORIZONTAL ELLIPSIS}").rstrip()
    assert len(result.rendition.title) <= 100 and title.startswith(kept) and len(kept) in boundaries(title)


def test_media_dropped_without_link_when_links_banned():
    src = rendition(media=[{"url": "https://cdn.example/clip.mp4", "mime": "video/mp4"}], content_type="video")
    result = tailor(src, LEMMY, LEMMY_SPEC, {"link_policy": "none"})
    assert "cdn.example" not in result.rendition.body and result.rendition.content_type == "text"
    assert any("not linked because links are not allowed" in w for w in result.warnings)


def test_media_too_large_dropped():
    src = rendition(media=[{"url": "https://cdn.example/a.png", "mime": "image/png", "bytes": 5_000_000}])
    result = tailor(src, DISCOURSE, {"content_types": ["text", "image"], "spec": {"image_max_bytes": 3_000_000}}, {})
    assert result.rendition.media == [] and "MEDIA_DROPPED" in codes(result)


# ---------------------------------------------------------------- disclosure (hard case: non-English + disclosure)


def test_german_community_gets_german_disclosure_and_engine_then_allows():
    rules = {"disclosure_required": True, "language_policy": "any"}  # English post allowed in a German community
    dest = {**LEMMY, "language": "de"}
    src = rendition(ai_generated=True)
    assert evaluate(src, {**rules, "language": "de"}, [], NOW).action == "allow_with_changes"
    result = tailor(src, dest, LEMMY_SPEC, rules)
    assert "Hinweis: Dieser Beitrag wurde mit Hilfe von KI erstellt." in result.rendition.body
    assert result.rendition.body.rstrip().endswith("this post was created with the help of AI.")
    assert "AI_DISCLOSURE_APPENDED" in codes(result)
    after = evaluate(result.rendition, {**rules, "language": "de"}, [], NOW)
    assert after.action == "allow" and not after.fixes


def test_disclosure_top_placement():
    result = tailor(rendition(ai_generated=None), GHOST, {}, {"disclosure": {"required": True, "placement": "top"}})
    assert result.rendition.body.startswith("Disclosure:")
    assert "AI_STATUS_UNKNOWN" in codes(result)


def test_disclosure_not_added_when_auto_insert_off():
    result = tailor(rendition(ai_generated=True), GHOST, {}, {"disclosure": {"required": True, "auto_insert": False}})
    assert "Disclosure" not in result.rendition.body and "AI_DISCLOSURE_MISSING" in codes(result)


def test_body_truncation_keeps_disclosure_and_drops_optional_blocks_first():
    src = rendition(ai_generated=True, body="word " * 400, link_url="https://example.org/x")
    result = tailor(src, DISCOURSE, {"spec": {"body_max_chars": 300}}, {"disclosure_required": True})
    body = result.rendition.body
    assert len(body) <= 300 and body.endswith("Disclosure: this post was created with the help of AI.")
    assert "BODY_TRUNCATED" in codes(result) and result.ok


def test_disclosure_that_cannot_fit_is_reported():
    result = tailor(
        rendition(ai_generated=True), GHOST, {"spec": {"body_max_chars": 20}}, {"disclosure_required": True}
    )
    assert not result.ok and any(v.rule == "disclosure" for v in result.violations)
    assert "DISCLOSURE_UNPLACEABLE" in codes(result)


# ---------------------------------------------------------------- article


def test_devto_tags_and_canonical():
    src = rendition(
        tags=["AI", "Video Editing", "web-dev", "Künstliche", "rust", "ai"], link_url="https://creator.dev/p"
    )
    result = tailor(src, DEVTO, {"content_types": ["text"], "spec": {"title_max_chars": 128}}, {})
    assert result.rendition.tags == ["ai", "videoediting", "webdev", "kunstliche"]
    assert result.rendition.canonical_url == "https://creator.dev/p"
    assert {"TAGS_NORMALISED", "CANONICAL_SET"} <= set(codes(result))
    assert check_spec(result.rendition, {"tag_max": 4, "tag_pattern": "[a-z0-9]+"}, None) == []


def test_ghost_keeps_existing_canonical_and_tags():
    src = rendition(tags=["Video Editing"], canonical_url="https://creator.dev/orig", link_url="https://other.example")
    result = tailor(src, GHOST, {}, {})
    assert result.rendition.canonical_url == "https://creator.dev/orig" and result.rendition.tags == ["Video Editing"]


# ---------------------------------------------------------------- slack / newsletter / mrss / generic


def test_slack_mrkdwn():
    src = rendition(
        title="Release notes",
        body="**New**: see [the guide](https://example.org/g) & thanks @U0123ABCD",
        mentions=["U0123ABCD"],
        tags=["x"],
    )
    result = tailor(src, SLACK, {}, {})
    body = result.rendition.body
    assert body.startswith("*Release notes*\n*New*:")
    assert "<https://example.org/g|the guide>" in body and "&amp;" in body and "<@U0123ABCD>" in body
    assert result.rendition.title is None and result.rendition.tags == []
    assert {"TITLE_FOLDED_INTO_TEXT", "FORMAT_MRKDWN", "TAGS_DROPPED"} <= set(codes(result))


def test_newsletter_subject_derived_and_fitted():
    src = rendition(title=None, body="# A very long first line that becomes the subject of the email issue\n\nBody.")
    result = tailor(src, NEWSLETTER, {"spec": {"title_max_chars": 40}}, {})
    assert result.rendition.title.startswith("A very long first line") and len(result.rendition.title) <= 40
    assert {"TITLE_DERIVED", "TITLE_TRUNCATED"} <= set(codes(result))


def test_mrss_plain_description():
    src = rendition(body="**Bold** intro with [a link](https://example.org/a)", body_format="markdown")
    result = tailor(src, MRSS, {"spec": {"title_max_chars": 60}}, {})
    assert result.rendition.body == "Bold intro with a link (https://example.org/a)"
    assert result.rendition.body_format == "plain"


def test_generic_profile_changes_nothing_when_it_fits():
    result = tailor(rendition(), WEBHOOK, {"spec": {"title_max_chars": 100}}, {})
    assert result.diff == [] and result.warnings == [] and result.ok


def test_no_rules_is_warned():
    result = tailor(rendition(), WEBHOOK, {}, None)
    assert "NO_RULES" in codes(result)


def test_unresolvable_spec_problem_is_reported_not_hidden():
    result = tailor(rendition(title="Hi"), WEBHOOK, {"spec": {"title_min_chars": 15}}, {})
    assert not result.ok and "SPEC_UNRESOLVED" in codes(result)


def test_diff_and_input_not_mutated():
    src = Rendition.model_validate(rendition(title="t" * 300))
    result = tailor(src, DISCOURSE, DISCOURSE_SPEC, {})
    assert src.title == "t" * 300
    assert {d["field"] for d in result.diff} == {"title", "body"}
    title_change = next(d for d in result.diff if d["field"] == "title")
    assert title_change["before"] == "t" * 300 and len(title_change["after"]) <= 255


def test_every_discussion_question_is_a_question():
    from app.tailoring.i18n import DISCUSSION
    from app.tailoring.textops import ends_with_question

    for lang, words in DISCUSSION.items():
        assert ends_with_question(words["body_q"]) and ends_with_question(words["title_q"]), lang


# ---------------------------------------------------------------- regressions


def test_question_with_cta_word_is_kept():
    src = rendition(body="We are pricing our tool.\nIs a 20% discount for annual plans normal?\nSubscribe for more!")
    result = tailor(src, DISCOURSE, DISCOURSE_SPEC, {})
    assert "Is a 20% discount for annual plans normal?" in result.rendition.body
    assert "Subscribe for more!" not in result.rendition.body
    assert "CTA_REMOVED" in codes(result)


def test_long_sentence_with_cta_word_is_kept_and_flagged():
    line = "Last year I got a discount on a camera and the colour science was much better than I expected."
    result = tailor(rendition(body=line), DISCOURSE, DISCOURSE_SPEC, {})
    assert line in result.rendition.body and "CTA_KEPT" in codes(result)


def test_lemmy_mentions_get_the_instance():
    src = rendition(body="Thanks @alice and @bob@other.example for the tips.", mentions=["alice", "@bob@other.example"])
    result = tailor(src, {**LEMMY, "instance": {"community_url": "https://lemmy.example/c/video"}}, LEMMY_SPEC, {})
    assert "@alice@lemmy.example" in result.rendition.body and "@bob@other.example" in result.rendition.body
    assert result.rendition.mentions == ["@alice@lemmy.example", "@bob@other.example"]
    assert "MENTIONS_ADAPTED" in codes(result)


def test_devto_and_discourse_mentions_are_at_names():
    src = rendition(body="Thanks @alice.", mentions=["alice", "@bob"])
    for destination in (DEVTO, DISCOURSE):
        assert tailor(src, destination, {}, {}).rendition.mentions == ["@alice", "@bob"]


def test_devto_symbol_tags_are_spelled_out():
    result = tailor(rendition(tags=["C++", "C#", ".NET"]), DEVTO, {}, {})
    assert result.rendition.tags == ["cpp", "csharp", "dotnet"]
