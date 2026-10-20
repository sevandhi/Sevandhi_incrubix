"""The three helpers the contract publish path (app/connectors/base.py) needs around evaluate():

    rules, source = resolve_ruleset(destination, catalogue_rules=confirmed_row_rules_or_None)
    decision = evaluate(rendition, rules, history, now, owner_domains=owner_domains)
    rendition = apply_decision(rendition, decision)    # raises the 409, or returns the fixed rendition

and ``promo_flag(rendition, owner_domains)`` to store on each publish, so later ``PublishEvent``s carry
``is_promotional`` (needed by ``self_promo: ratio(n)``).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.contract.models import Rendition
from app.errors import ErrorCode, IbxError
from app.rules.fixes import apply_fixes
from app.rules.lang import normalize_lang
from app.rules.models import Decision, RuleSet
from app.rules.promo_detector import detect_promo

# Appendix A destination-record fields that are posting rules.
RECORD_RULE_FIELDS = ("frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed")


def _get(destination: Any, name: str) -> Any:
    if isinstance(destination, dict):
        return destination.get(name)
    value = getattr(destination, name, None)
    if value is None:
        value = (getattr(destination, "model_extra", None) or {}).get(name)
    return value


def _layer(name: str, raw: Any) -> dict[str, Any]:
    """Validate one source of rules on its own and return only the fields it actually states."""
    if isinstance(raw, RuleSet):
        return raw.model_dump(exclude_unset=True)
    try:
        return RuleSet.model_validate(raw).model_dump(exclude_unset=True)
    except ValidationError as exc:
        errors = [
            {
                "source": name,
                "field": ".".join(str(p) for p in e["loc"]) or name,
                "message": e["msg"].removeprefix("Value error, "),
            }
            for e in exc.errors()
        ]
        raise IbxError(
            ErrorCode.REQUEST_VALIDATION_FAILED,
            f"The destination's posting rules ({name}) are invalid: " + "; ".join(e["message"] for e in errors),
            details={"errors": errors},
        ) from None


def resolve_ruleset(destination: Any, catalogue_rules: Any = None) -> tuple[RuleSet | None, str]:
    """Merge every source of rules for one destination. Precedence (highest first):
      1. ``destination.rules``                      ("inline", sent with the call)
      2. the Appendix A record fields on destination ("destination_record": frequency_cap,
         self_promo_allowed, link_policy, disclosure_required, automation_allowed)
      3. ``catalogue_rules``                         ("catalogue", the confirmed rules in our DB)
    ``destination.language`` fills ``languages`` when no layer states them.
    Returns (None, "none") when no source has rules. Invalid rules raise a 422 REQUEST_VALIDATION_FAILED
    (input values are never echoed)."""
    layers: list[tuple[str, dict[str, Any]]] = []
    inline = _get(destination, "rules")
    if inline is not None:
        layers.append(("inline", _layer("inline", inline)))
    record = {f: _get(destination, f) for f in RECORD_RULE_FIELDS}
    record = {k: v for k, v in record.items() if v is not None and v != ""}
    if record:
        layers.append(("destination_record", _layer("destination_record", record)))
    if catalogue_rules is not None:
        layers.append(("catalogue", _layer("catalogue", catalogue_rules)))
    if not layers:
        return None, "none"

    merged: dict[str, Any] = {}
    for _name, data in reversed(layers):  # lowest precedence first, higher ones overwrite
        merged.update(data)
    language = normalize_lang(_get(destination, "language"))
    if not merged.get("languages") and language:
        merged["languages"] = [language]
    return RuleSet.model_validate(merged), "+".join(name for name, _ in layers)


def apply_decision(rendition: Rendition, decision: Decision) -> Rendition:
    """Block/reschedule -> raise the decision's 409 error. allow_with_changes -> return the rendition
    with the fixes applied (the AI disclosure inserted). allow -> the rendition unchanged."""
    error = decision.to_error()
    if error is not None:
        error.details.setdefault("decision", decision.action)
        raise error
    return apply_fixes(rendition, decision.fixes) if decision.fixes else rendition


def promo_flag(rendition: Rendition, owner_domains=()) -> bool:
    """Was this post promotional? Store it with the publish so history can feed ``ratio(n)``."""
    return detect_promo(rendition, owner_domains=owner_domains).is_promotional
