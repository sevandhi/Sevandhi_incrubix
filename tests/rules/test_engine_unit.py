"""Unit tests for the rules engine pieces that the YAML table does not show directly."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.contract.models import Rendition
from app.errors import ErrorCode
from app.rules import Decision, PublishEvent, RuleSet, apply_fixes, detect_promo, evaluate
from app.rules.durations import parse_duration
from app.rules.lang import detect_language, normalize_lang

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def r(**kw) -> Rendition:
    return Rendition(**{"title": "Editing notes", "body": "Plain discussion text.", "language": "en", **kw})


# ---------------------------------------------------------------- RuleSet parsing


def test_catalogue_row_spelling_is_understood():
    rules = RuleSet.model_validate(
        {
            "frequency_cap": "1/P7D",
            "self_promo_allowed": "false",
            "link_policy": "allow",
            "disclosure_required": "true",
            "automation_allowed": "true",
            "language": "de-DE",
        }
    )
    assert rules.frequency_cap.max_posts == 1 and rules.frequency_cap.window == "P7D"
    assert rules.self_promo == "disallowed"
    assert rules.link_policy == "any"
    assert rules.disclosure.required is True
    assert rules.languages == ["de"]


@pytest.mark.parametrize(
    "bad",
    [
        {"frequency_cap": "often"},
        {"self_promo": "sometimes"},
        {"link_policy": "maybe"},
        {"frequency_cap": "1/P0D"},
        {"frequency_cap": {"max_posts": 0}},
    ],
)
def test_invalid_rules_raise_instead_of_passing_silently(bad):
    with pytest.raises(ValidationError):
        RuleSet.model_validate(bad)


def test_misspelt_rule_key_is_rejected_with_a_suggestion():
    """A typo must not switch a rule off silently."""
    with pytest.raises(ValidationError) as info:
        RuleSet.model_validate({"disclosure_requried": True})
    message = str(info.value)
    assert "disclosure_requried" in message and "disclosure_required" in message


def test_metadata_keys_are_kept_not_rejected():
    rules = RuleSet.model_validate({"self_promo": "disallowed", "notes": "from the sidebar", "confirmed_by": "ops"})
    assert rules.metadata == {"notes": "from the sidebar", "confirmed_by": "ops"}


def test_conservative_default_is_strict():
    rules = RuleSet.conservative_default(languages=["en"])
    assert rules.automation_allowed is False and rules.self_promo == "disallowed"
    assert rules.disclosure.required and rules.language_policy == "must_match"
    assert rules.frequency_cap.max_posts == 1 and rules.source == "default_conservative"


def test_for_destination_fills_language():
    rules = RuleSet.for_destination({"rules": {"disclosure_required": True}, "language": "ta"})
    assert rules.languages == ["ta"]
    assert RuleSet.for_destination({"rules": None}) is None


def test_durations():
    assert parse_duration("P7D") == timedelta(days=7)
    assert parse_duration("PT1H30M") == timedelta(hours=1, minutes=30)
    assert parse_duration("P1W") == timedelta(days=7)
    assert parse_duration("week") == timedelta(days=7)
    with pytest.raises(ValueError):
        parse_duration("7 days")


# ---------------------------------------------------------------- engine behaviour


def test_none_ruleset_blocks_as_unconfirmed():
    decision = evaluate(r(), None, [], NOW)
    assert decision.action == "block" and decision.code == ErrorCode.DEST_RULES_UNCONFIRMED


def test_all_failing_checks_are_listed_and_first_one_decides():
    rules = {"automation_allowed": False, "self_promo": "disallowed", "link_policy": "none", "frequency_cap": "1/P7D"}
    decision = evaluate(
        r(body="subscribe https://x.example.com", is_promotional=True), rules, [{"at": NOW - timedelta(hours=1)}], NOW
    )
    assert decision.code == ErrorCode.DEST_RULE_AUTOMATION_NOT_ALLOWED
    assert decision.details["failed_checks"] == ["automation", "self_promo", "links", "frequency_cap"]
    assert decision.retry_at is None  # blocked posts are never rescheduled


def test_reschedule_to_error_has_retry_at_and_409():
    decision = evaluate(r(), {"frequency_cap": "1/P7D"}, [PublishEvent(published_at=NOW - timedelta(days=2))], NOW)
    error = decision.to_error()
    assert error.code == ErrorCode.DEST_RULE_FREQUENCY_CAP and error.http_status == 409 and error.retryable
    assert error.details["retry_at"] == "2026-10-25T09:00:00Z"
    assert error.details["count_in_window"] == 1 and error.details["cap"] == 1


def test_self_promo_error_names_signals_and_is_not_retryable():
    decision = evaluate(
        r(body="subscribe for more https://youtube.com/@creator"),
        {"self_promo": "disallowed"},
        [],
        NOW,
        owner_domains=["youtube.com/@creator"],
    )
    error = decision.to_error()
    assert error.code == ErrorCode.DEST_RULE_SELF_PROMO and not error.retryable
    types = {e["type"] for e in error.details["evidence"]}
    assert {"own_link", "cta"} <= types
    assert "retry_at" not in error.details


def test_allowed_decision_has_no_error():
    assert evaluate(r(), {}, [], NOW).to_error() is None


def test_naive_now_is_treated_as_utc():
    decision = evaluate(r(), {"frequency_cap": "1/P1D"}, [{"at": "2026-10-20T08:00:00Z"}], datetime(2026, 10, 20, 9))
    assert decision.retry_at == datetime(2026, 10, 21, 8, tzinfo=UTC)


def test_disclosure_fix_round_trip():
    rules = {"disclosure_required": True, "languages": ["de"], "language_policy": "any"}
    first = evaluate(r(ai_generated=True), rules, [], NOW)
    assert first.action == "allow_with_changes" and first.fixes[0].lang == "de"
    fixed = apply_fixes(r(ai_generated=True), first.fixes)
    assert fixed.body.endswith(first.fixes[0].text)
    assert evaluate(fixed, rules, [], NOW).action == "allow"


def test_disclosure_top_placement_html():
    rules = {"disclosure": {"required": True, "placement": "top"}}
    decision = evaluate(r(ai_generated=True, body_format="html", body="<p>Hello</p>"), rules, [], NOW)
    fixed = apply_fixes(r(ai_generated=True, body_format="html", body="<p>Hello</p>"), decision.fixes)
    assert fixed.body.startswith("<p>Disclosure:") and fixed.body.endswith("<p>Hello</p>")


def test_decision_is_json_serialisable():
    decision = evaluate(r(), {"frequency_cap": "1/P7D"}, [{"at": NOW}], NOW)
    assert Decision.model_validate_json(decision.model_dump_json()).action == "reschedule"


# ---------------------------------------------------------------- promo detector + language


def test_detector_quiet_on_plain_discussion():
    assert not detect_promo(r(body="What codec settings do you use for 4K exports? Mine stutter.")).is_promotional


def test_detector_signals_are_reported_once_per_phrase():
    result = detect_promo(r(body="Subscribe for more!"))
    assert [s.value for s in result.signals if s.type == "cta"] == ["subscribe for more"]


@pytest.mark.parametrize(
    "code,expected",
    [
        ("en-US", "en"),
        ("EN", "en"),
        ("deu", "de"),
        ("English", "en"),
        ("pt_BR", "pt"),
        ("und", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_lang(code, expected):
    assert normalize_lang(code) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("This is what I think about the new editor and how it works for you", "en"),
        ("Das ist nicht der Grund, warum ich mit dem Schnitt auf der Arbeit bin", "de"),
        ("Esto es lo que pienso de la nueva herramienta para los vídeos con el equipo", "es"),
        ("வீடியோ எடிட்டிங் பற்றிய என் குறிப்புகள்", "ta"),
        ("वीडियो एडिटिंग पर मेरे नोट्स", "hi"),
        ("動画編集についてのメモです", "ja"),
        ("🎬🎬", None),
    ],
)
def test_detect_language(text, expected):
    assert detect_language(text) == expected


# ---------------------------------------------------------------- regressions


def test_error_message_comes_from_the_deciding_check():
    rules = {"frequency_cap": "1/P7D", "disclosure_required": True}
    decision = evaluate(r(ai_generated=True), rules, [{"at": NOW - timedelta(days=1)}], NOW)
    assert decision.action == "reschedule" and decision.fixes
    assert decision.to_error().message.startswith("DEST_RULE_FREQUENCY_CAP")


def test_shortener_is_a_promo_signal():
    result = detect_promo(r(body="notes https://bit.ly/3xYz"))
    assert [s.type for s in result.signals] == ["shortener"]


def test_effective_language_policy():
    assert RuleSet.model_validate({"language": "de"}).effective_language_policy == "must_match"
    assert RuleSet.model_validate({"language": "en"}).effective_language_policy == "any"
    assert RuleSet.model_validate({"language": "de", "language_policy": "any"}).effective_language_policy == "any"
    assert RuleSet.model_validate({}).effective_language_policy == "any"
