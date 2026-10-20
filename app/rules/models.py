"""Posting-rule data: RuleSet (what a destination allows), PublishEvent (what we already posted)
and Decision (what the engine says about one post).

RuleSet accepts the flat catalogue spelling too (``self_promo_allowed: false``,
``disclosure_required: true``, ``frequency_cap: "1/P7D"``, ``link_policy: allow``), so one catalogue
row or one ``destination.rules`` dict is enough. A key it does not know is REJECTED with a
validation error that suggests the closest known key: a typo must never switch a rule off.
Bookkeeping keys (notes, confirmed_by, ...) are kept in ``metadata``.
"""

from __future__ import annotations

import difflib
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import ErrorCode, IbxError
from app.rules.durations import normalize_duration, parse_duration
from app.rules.lang import normalize_lang, normalize_langs

Action = Literal["allow", "allow_with_changes", "reschedule", "block"]
RuleSource = Literal["manual", "llm", "default_conservative", "api_sidebar", "page"]
LinkPolicy = Literal["any", "none", "no_shorteners", "allowlist"]

_TRUE = {"true", "yes", "y", "1", "allowed", "allow", "required", "on"}
_FALSE = {"false", "no", "n", "0", "disallowed", "not_allowed", "banned", "forbidden", "none", "off"}


def as_bool(value: Any) -> bool | None:
    """Read a yes/no value from YAML, CSV or JSON. Returns None for empty/unknown."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def as_utc(moment: datetime) -> datetime:
    """Naive datetimes are read as UTC; everything is compared in UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


# ---------------------------------------------------------------- RuleSet parts


class FrequencyCap(BaseModel):
    """At most ``max_posts`` posts in any rolling ``window`` (ISO-8601 duration)."""

    model_config = ConfigDict(extra="forbid")

    max_posts: int = Field(ge=1)
    window: str = "P7D"

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        if isinstance(value, int):
            return {"max_posts": value}
        if isinstance(value, str):  # "1/P7D", "2/week", "3 per P1D"
            match = re.fullmatch(r"\s*(\d+)\s*(?:/|per)\s*(\S+)\s*", value, flags=re.I)
            if not match:
                raise ValueError("frequency_cap: expected '<max>/<ISO-8601 window>', e.g. 1/P7D")
            return {"max_posts": int(match.group(1)), "window": match.group(2)}
        if isinstance(value, dict):
            value = dict(value)
            for alias in ("max", "posts", "count"):
                if alias in value and "max_posts" not in value:
                    value["max_posts"] = value.pop(alias)
            for alias in ("per", "period", "window_iso"):
                if alias in value and "window" not in value:
                    value["window"] = value.pop(alias)
        return value

    @field_validator("window")
    @classmethod
    def _check_window(cls, value: str) -> str:
        return normalize_duration(value)

    @property
    def window_td(self) -> timedelta:
        return parse_duration(self.window)


class Disclosure(BaseModel):
    """AI-content disclosure. ``text_by_lang`` overrides the built-in texts in disclosure.yaml."""

    model_config = ConfigDict(extra="forbid")

    required: bool = False
    placement: Literal["top", "bottom"] = "bottom"
    text_by_lang: dict[str, str] = Field(default_factory=dict)
    auto_insert: bool = True  # False: a missing disclosure blocks instead of being added

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        flag = as_bool(value) if not isinstance(value, dict) else None
        if flag is not None:
            return {"required": flag}
        return value

    @field_validator("text_by_lang")
    @classmethod
    def _norm_langs(cls, value: dict[str, str]) -> dict[str, str]:
        return {normalize_lang(k) or k: v for k, v in value.items() if v}


# ---------------------------------------------------------------- RuleSet

# Flat / alternative spellings -> canonical field.
_KEY_ALIASES = {
    "self_promo_allowed": "self_promo",
    "self_promotion": "self_promo",
    "self_promotion_allowed": "self_promo",
    "promo": "self_promo",
    "disclosure_required": "disclosure",
    "ai_disclosure": "disclosure",
    "ai_disclosure_required": "disclosure",
    "freq_cap": "frequency_cap",
    "cap": "frequency_cap",
    "language": "languages",
    "lang": "languages",
    "allowed_languages": "languages",
    "automation": "automation_allowed",
    "bots_allowed": "automation_allowed",
    "banned_terms": "blocked_terms",
    "blocked_words": "blocked_terms",
    "allowlist": "link_allowlist",
    "rules_source": "source",
}
# Keys that describe the rules rather than being rules; kept in RuleSet.metadata.
_METADATA_KEYS = {
    "notes",
    "confirmed_by",
    "confirmed_at",
    "captured_at",
    "version",
    "evidence",
    "quotes",
    "confidence",
    "proposal_id",
    "status",
    "destination_id",
    "id",
}
_LINK_POLICY_ALIASES = {
    "any": "any",
    "allow": "any",
    "allowed": "any",
    "all": "any",
    "yes": "any",
    "true": "any",
    "none": "none",
    "no_links": "none",
    "no": "none",
    "false": "none",
    "disallowed": "none",
    "forbidden": "none",
    "no_shorteners": "no_shorteners",
    "no-shorteners": "no_shorteners",
    "noshorteners": "no_shorteners",
    "allowlist": "allowlist",
    "whitelist": "allowlist",
}


class RuleSet(BaseModel):
    """One destination's posting rules. Defaults are permissive: a rule that is not stated is not enforced.
    Use ``RuleSet.conservative_default()`` when nothing is known."""

    model_config = ConfigDict(extra="forbid")

    frequency_cap: FrequencyCap | None = None
    self_promo: str = "allowed"  # allowed | disallowed | ratio(n) = at most 1 in n posts promotional
    link_policy: LinkPolicy = "any"
    link_allowlist: list[str] = Field(default_factory=list)
    disclosure: Disclosure = Field(default_factory=Disclosure)
    # None = not stated: must_match if the community language is stated and not English, else any.
    language_policy: Literal["any", "must_match"] | None = None
    languages: list[str] = Field(default_factory=list)  # the community's language(s)
    automation_allowed: bool = True
    blocked_terms: list[str] = Field(default_factory=list)
    source: RuleSource = "manual"
    rules_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data: dict[str, Any] = {}
        metadata = dict(value.get("metadata") or {})
        for key, item in value.items():
            name = _KEY_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
            if name in _METADATA_KEYS:
                metadata[name] = item
            elif name != "metadata":
                data[name] = item
        data["metadata"] = metadata
        unknown = sorted(set(data) - set(cls.model_fields))
        if unknown:
            known = sorted(set(cls.model_fields) | set(_KEY_ALIASES))
            hints = []
            for key in unknown:
                close = difflib.get_close_matches(key, known, n=1)
                hints.append(f"'{key}'" + (f" (did you mean '{close[0]}'?)" if close else ""))
            raise ValueError(f"unknown rule key(s): {', '.join(hints)}")

        cap = data.get("frequency_cap")
        if "frequency_cap" in data and (
            cap is None or (isinstance(cap, str) and cap.strip().lower() in ("", "none", "no", "unlimited"))
        ):
            data["frequency_cap"] = None

        if "self_promo" in data:
            data["self_promo"] = _parse_self_promo(data["self_promo"])

        policy = data.get("link_policy")
        if isinstance(policy, list | tuple):  # a list means "only these domains"
            data["link_allowlist"] = list(policy)
            data["link_policy"] = "allowlist"
        elif isinstance(policy, dict) and "allowlist" in policy:
            data["link_allowlist"] = list(policy["allowlist"])
            data["link_policy"] = "allowlist"
        elif isinstance(policy, str | bool):
            text = str(policy).strip().lower()
            match = re.fullmatch(r"(allowlist|whitelist)\s*[:\[(]\s*(.*?)\s*[\])]?", text)
            if match:
                data["link_policy"] = "allowlist"
                data["link_allowlist"] = [d for d in re.split(r"[,;\s]+", match.group(2)) if d]
            elif text in _LINK_POLICY_ALIASES:
                data["link_policy"] = _LINK_POLICY_ALIASES[text]
        elif policy is None:
            data.pop("link_policy", None)
        if isinstance(data.get("link_allowlist"), str):
            data["link_allowlist"] = [d for d in re.split(r"[,;\s]+", data["link_allowlist"]) if d]

        policy_lang = data.get("language_policy")
        if isinstance(policy_lang, str):
            text = policy_lang.strip().lower()
            data["language_policy"] = "must_match" if text in ("must_match", "match", "strict", "required") else text
        if "languages" in data:
            data["languages"] = normalize_langs(data["languages"])

        for key in ("automation_allowed",):
            if key in data:
                flag = as_bool(data[key])
                if flag is None:
                    data.pop(key)  # unknown: keep the field default
                else:
                    data[key] = flag
        if isinstance(data.get("blocked_terms"), str):
            data["blocked_terms"] = [t.strip() for t in re.split(r"[;\n]+", data["blocked_terms"]) if t.strip()]
        if data.get("disclosure") is None:
            data.pop("disclosure", None)
        return data

    @field_validator("self_promo")
    @classmethod
    def _check_self_promo(cls, value: str) -> str:
        if value in ("allowed", "disallowed") or re.fullmatch(r"ratio\(\d+\)", value):
            return value
        raise ValueError("self_promo must be allowed, disallowed or ratio(n)")

    # ---- helpers

    @property
    def promo_ratio(self) -> int | None:
        """n for ``ratio(n)``: at most one post in n may be promotional."""
        match = re.fullmatch(r"ratio\((\d+)\)", self.self_promo)
        return int(match.group(1)) if match else None

    @property
    def effective_language_policy(self) -> str:
        """The stated policy; if none is stated, a non-English community language implies must_match."""
        if self.language_policy is not None:
            return self.language_policy
        if self.languages and "en" not in self.languages:
            return "must_match"
        return "any"

    @property
    def language_policy_implied(self) -> bool:
        return self.language_policy is None and self.effective_language_policy == "must_match"

    @classmethod
    def conservative_default(cls, *, languages: list[str] | tuple[str, ...] = ()) -> RuleSet:
        """Rules for a destination we know nothing about. Strict on purpose: no automated posting
        (assisted only), one post a week, no self-promotion, no link shorteners, AI disclosure,
        and the post must be in the community's language."""
        return cls(
            frequency_cap=FrequencyCap(max_posts=1, window="P7D"),
            self_promo="disallowed",
            link_policy="no_shorteners",
            disclosure=Disclosure(required=True),
            language_policy="must_match",
            languages=normalize_langs(list(languages)),
            automation_allowed=False,
            source="default_conservative",
        )

    @classmethod
    def for_destination(cls, destination: Any) -> RuleSet | None:
        """The RuleSet stored on a Destination (contract model or dict), with the destination's language
        filled in when the rules do not name one. None when the destination has no rules."""
        rules = destination.get("rules") if isinstance(destination, dict) else getattr(destination, "rules", None)
        language = (
            destination.get("language") if isinstance(destination, dict) else getattr(destination, "language", None)
        )
        if rules is None:
            return None
        ruleset = rules if isinstance(rules, RuleSet) else cls.model_validate(rules)
        if not ruleset.languages and normalize_lang(language):
            ruleset = ruleset.model_copy(update={"languages": [normalize_lang(language)]})
        return ruleset


def _parse_self_promo(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().lower().replace(" ", "")
        match = re.fullmatch(r"ratio[:(]?(\d+)\)?|1(?::|/|in)(\d+)", text)
        if match:
            return f"ratio({match.group(1) or match.group(2)})"
        if text in ("allowed", "disallowed"):
            return text
    flag = as_bool(value)
    if flag is True:
        return "allowed"
    if flag is False:
        return "disallowed"
    return str(value)


# ---------------------------------------------------------------- history


# Statuses that mean "nothing was posted". A timeout is NOT here: the post may have gone out.
_NOT_COUNTED = {"failed", "error", "errored", "rejected", "blocked", "cancelled", "canceled", "rescheduled", "skipped"}


class PublishEvent(BaseModel):
    """One earlier post (or reserved slot) on the same destination. Deleted posts still count:
    a community's "one post a week" is about posting, not about what is still visible."""

    model_config = ConfigDict(extra="allow")

    published_at: datetime
    destination_key: str | None = None
    status: str = "published"
    is_promotional: bool | None = None
    rendition_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        if isinstance(value, dict) and "published_at" not in value:
            for alias in ("at", "created_at", "posted_at", "scheduled_for"):
                if alias in value:
                    value = {**value, "published_at": value[alias]}
                    break
        return value

    @field_validator("published_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)

    @property
    def counts_toward_cap(self) -> bool:
        return self.status.lower() not in _NOT_COUNTED


# ---------------------------------------------------------------- results


class Fix(BaseModel):
    """A change the caller (or tailoring) must make before the post may go out."""

    kind: Literal["insert_disclosure"]
    field: str = "body"
    text: str
    placement: Literal["top", "bottom"] = "bottom"
    lang: str = "en"


Outcome = Literal["pass", "fix", "reschedule", "block"]


class CheckOutcome(BaseModel):
    """The result of one check (one module in app/rules/checks/)."""

    check: str
    outcome: Outcome = "pass"
    code: ErrorCode | None = None
    reason: str | None = None
    retry_at: datetime | None = None
    fixes: list[Fix] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    """What to do with one post on one destination.

    action: allow | allow_with_changes (apply ``fixes`` first) | reschedule (at ``retry_at``) | block.
    code:   the DEST_RULE_* code of the first failing check in check order (None when allowed).
    """

    action: Action
    code: ErrorCode | None = None
    message: str | None = None  # the deciding check's reason (used as the error message)
    reasons: list[str] = Field(default_factory=list)
    retry_at: datetime | None = None
    fixes: list[Fix] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckOutcome] = Field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.action in ("allow", "allow_with_changes")

    def to_error(self) -> IbxError | None:
        """The contract error for a blocked or rescheduled post (None when allowed)."""
        if self.allowed or self.code is None:
            return None
        details: dict[str, Any] = {
            **self.details,
            "reasons": self.reasons,
            "evidence": self.evidence,
            "warnings": self.warnings,
        }
        if self.retry_at is not None:
            details["retry_at"] = self.retry_at.isoformat().replace("+00:00", "Z")
        message = self.message or (self.reasons[0] if self.reasons else None)
        return IbxError(self.code, message, details=details)
