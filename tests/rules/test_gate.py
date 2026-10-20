"""resolve_ruleset / apply_decision / promo_flag, the helpers the connector publish path calls."""

from datetime import UTC, datetime, timedelta

import pytest

from app.contract.models import Destination, Rendition
from app.errors import ErrorCode, IbxError
from app.rules import PublishEvent, evaluate
from app.rules.gate import apply_decision, promo_flag, resolve_ruleset

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)


def dest(**kw) -> Destination:
    return Destination.model_validate({"connector_key": "lemmy", "surface": "post", **kw})


def test_no_rules_anywhere():
    assert resolve_ruleset(dest()) == (None, "none")


def test_inline_rules_get_destination_language():
    rules, source = resolve_ruleset(dest(rules={"disclosure_required": True}, language="ta"))
    assert source == "inline" and rules.languages == ["ta"] and rules.disclosure.required


def test_appendix_a_flat_destination_record_fields():
    record = dest(
        frequency_cap="1/P7D",
        self_promo_allowed=False,
        link_policy="no_shorteners",
        disclosure_required=True,
        automation_allowed=True,
        language="de",
        name="Forum 41",
        geo="DE",
    )
    rules, source = resolve_ruleset(record)
    assert source == "destination_record"
    assert rules.frequency_cap.max_posts == 1 and rules.self_promo == "disallowed"
    assert rules.link_policy == "no_shorteners" and rules.disclosure.required and rules.languages == ["de"]


def test_precedence_inline_over_record_over_catalogue():
    record = dest(rules={"frequency_cap": "3/P1D"}, self_promo_allowed=True)
    rules, source = resolve_ruleset(
        record, catalogue_rules={"frequency_cap": "1/P7D", "self_promo": "disallowed", "blocked_terms": ["crypto"]}
    )
    assert source == "inline+destination_record+catalogue"
    assert rules.frequency_cap.max_posts == 3 and rules.self_promo == "allowed" and rules.blocked_terms == ["crypto"]


def test_invalid_rules_become_a_422_not_a_500():
    with pytest.raises(IbxError) as info:
        resolve_ruleset(dest(rules={"frequency_cap": "oops"}))
    error = info.value
    assert error.code == ErrorCode.REQUEST_VALIDATION_FAILED and error.http_status == 422 and not error.retryable
    assert "oops" not in error.message and "oops" not in str(error.details)  # input values are never echoed


def test_misspelt_inline_rule_is_a_422():
    with pytest.raises(IbxError) as info:
        resolve_ruleset(dest(rules={"self_promo_alowed": False}))
    assert "self_promo_allowed" in str(info.value.details)


def test_apply_decision_inserts_disclosure_or_raises():
    rendition = Rendition(body="Notes", language="de", ai_generated=True)
    rules, _ = resolve_ruleset(dest(rules={"disclosure_required": True}, language="de"))
    fixed = apply_decision(rendition, evaluate(rendition, rules, [], NOW))
    assert fixed.body.endswith("Hinweis: Dieser Beitrag wurde mit Hilfe von KI erstellt.")
    capped, _ = resolve_ruleset(dest(rules={"frequency_cap": "1/P7D"}))
    decision = evaluate(rendition, capped, [PublishEvent(published_at=NOW - timedelta(hours=1))], NOW)
    with pytest.raises(IbxError) as info:
        apply_decision(rendition, decision)
    assert info.value.code == ErrorCode.DEST_RULE_FREQUENCY_CAP and "tailorings" not in info.value.message


def test_promo_flag_for_history_rows():
    assert promo_flag(Rendition(body="Subscribe for more!")) is True
    assert promo_flag(Rendition(body="What codec do you use?")) is False
    assert promo_flag(Rendition(body="notes https://creator.dev/x"), owner_domains=["creator.dev"]) is True


def test_ratio_works_when_history_carries_is_promotional():
    """With is_promotional stored on history, ratio(3) allows one promo post in three."""
    rules, _ = resolve_ruleset(dest(rules={"self_promo": "ratio(3)"}))
    history = [PublishEvent(published_at=NOW - timedelta(days=d), is_promotional=False) for d in (1, 2)]
    assert evaluate(Rendition(body="x", is_promotional=True), rules, history, NOW).action == "allow"
