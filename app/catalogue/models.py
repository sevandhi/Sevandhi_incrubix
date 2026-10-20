"""API models for destinations, connections, content and publications.

``DestinationIn`` is one catalogue row (plan §3.3). CSV spelling is accepted: empty cells are "not stated",
``audience_tags`` may be ``"ai;founders"``, booleans may be ``true/false/yes/no``. Columns named
``instance.<name>`` or ``instance_<name>`` are merged into ``instance``; any other unknown column is kept
in ``extra``. The row's rule columns are confirmed on creation only when they are an operator's own rules
(``rules_status=confirmed`` + ``rules_source=manual``); otherwise they are a proposal for a person (see
``service.create_destination``).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RULE_COLUMNS = ("frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed")
_BLANK = ("", "null", "none", "n/a", "na", "-")
# Cells where one of the blank spellings is a real value: link_policy "none" means "no links allowed".
_NOT_BLANK = {"link_policy": ("none",)}


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in _BLANK:
        return None
    return value


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class DestinationIn(BaseModel):
    """One catalogue row. Only ``name``, ``url`` and ``connector_key`` are required."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=2000)
    connector_key: str = Field(min_length=1, max_length=64)
    platform_key: str | None = None
    surface: str | None = None
    category: str | None = None
    audience_tags: list[str] = Field(default_factory=list)
    language: str | None = None
    geo: str | None = None
    est_audience: int | None = None
    rules_url: str | None = None
    frequency_cap: str | None = None
    self_promo_allowed: bool | str | None = None
    link_policy: str | list[str] | None = None
    disclosure_required: bool | str | None = None
    automation_allowed: bool | str | None = None
    relevance_score: int | None = Field(default=None, ge=1, le=5)
    relevance_reason: str | None = Field(default=None, max_length=2000)
    connection_ref: str | None = None
    connection_id: str | None = None
    instance: dict[str, Any] = Field(default_factory=dict)
    spec_overrides: dict[str, Any] | None = None
    rules: dict[str, Any] | None = Field(default=None, description="Full RuleSet (overrides the flat rule columns).")
    rules_status: str | None = Field(default=None, max_length=32, description=(
        "confirmed | proposed. Only rules_status=confirmed with rules_source=manual (an operator typed the rules) makes "
        "the row's rules confirmed; anything else stores them as a proposal a person confirms at /ui/rules/{id}. "
        "Unmarked rows: proposed in /api/v1/catalogue/imports, confirmed (manual) in POST /api/v1/destinations."))
    rules_source: str | None = Field(default=None, max_length=32, description=(
        "Where the row's rules come from: manual | agent_research | page | api_sidebar | ..."))
    rules_quotes: dict[str, Any] | None = Field(default=None, description=(
        "Optional {field: quote} shown beside the row's rules on the confirm page (not verified by the service)."))

    @model_validator(mode="before")
    @classmethod
    def _csv(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = {}
        for k, v in value.items():
            if k is None:
                continue
            key = str(k).strip()
            keep = isinstance(v, str) and v.strip().lower() in _NOT_BLANK.get(key, ())
            data[key] = v.strip() if keep else _blank_to_none(v)
        instance = _maybe_json(data.pop("instance", None)) or {}
        if not isinstance(instance, dict):
            instance = {}
        for key in list(data):
            for prefix in ("instance.", "instance_"):
                if key.startswith(prefix) and len(key) > len(prefix):
                    item = data.pop(key)
                    if item is not None:
                        instance[key[len(prefix):]] = item
                    break
        data["instance"] = instance
        tags = data.get("audience_tags")
        if isinstance(tags, str):
            data["audience_tags"] = [t.strip() for t in tags.replace(",", ";").split(";") if t.strip()]
        elif tags is None:
            data.pop("audience_tags", None)
        for key in ("spec_overrides", "rules", "rules_quotes"):
            data[key] = _maybe_json(data.get(key))
        if isinstance(data.get("est_audience"), str):
            digits = data["est_audience"].replace(",", "").replace("_", "").strip()
            data["est_audience"] = int(float(digits)) if digits else None
        if isinstance(data.get("relevance_score"), str):
            data["relevance_score"] = int(float(data["relevance_score"]))
        return data

    @field_validator("connector_key", "platform_key", "surface", "rules_status", "rules_source")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        return value.strip().lower() if isinstance(value, str) else value

    def rules_dict(self) -> dict[str, Any] | None:
        """The row's rules in catalogue spelling (None when the row states no rule at all)."""
        if self.rules is not None:
            rules = dict(self.rules)
        else:
            rules = {k: getattr(self, k) for k in RULE_COLUMNS if getattr(self, k) is not None}
            if not rules:
                return None
        if self.language and not any(k in rules for k in ("language", "languages", "lang")):
            rules["language"] = self.language
        if self.rules_url and "rules_url" not in rules:
            rules["rules_url"] = self.rules_url
        return rules


class DestinationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = None
    audience_tags: list[str] | None = None
    language: str | None = None
    geo: str | None = None
    est_audience: int | None = None
    rules_url: str | None = None
    relevance_score: int | None = Field(default=None, ge=1, le=5)
    relevance_reason: str | None = Field(default=None, max_length=2000)
    connection_id: str | None = None
    connection_ref: str | None = None
    spec_overrides: dict[str, Any] | None = None
    instance: dict[str, Any] | None = Field(default=None, description="Extra instance fields (the instance_param itself can not change).")
    status: Literal["active", "excluded"] | None = None
    excluded_reason: str | None = Field(default=None, max_length=2000)


class RulesIn(BaseModel):
    """Human-confirmed rules for one destination (any RuleSet spelling)."""

    model_config = ConfigDict(extra="forbid")

    rules: dict[str, Any]
    confirmed_by: str = Field(min_length=1, max_length=255)
    source: Literal["manual", "llm", "default_conservative", "api_sidebar", "page"] = "manual"
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ConnectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_key: str = Field(min_length=1, max_length=64)
    auth_type: Literal["oauth2", "api_key", "bot_token", "none"] = "api_key"
    credentials: dict[str, str] = Field(default_factory=dict, description="Encrypted at rest; never returned.")
    expires_at: datetime | None = None
    account_handle: str | None = Field(default=None, max_length=255)
    ref: str | None = Field(default=None, max_length=128, description="A name catalogue rows can use as connection_ref.")
    instance: dict[str, Any] = Field(default_factory=dict, description="Optional instance values, e.g. base_url.")


class MediaRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str | None = None
    media_id: str | None = None
    mime: str | None = None
    bytes: int | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    alt: str | None = None


class ContentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=1000)
    body: str = Field(default="", max_length=200_000)
    content_type: Literal["text", "image", "video", "video_link", "link"] = "text"
    body_format: Literal["markdown", "html", "plain"] = "markdown"
    canonical_url: str | None = None
    link_url: str | None = None
    media: list[MediaRef] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    language: str | None = None
    is_promotional: bool | None = None
    ai_generated: bool | None = None
    owner_domains: list[str] = Field(default_factory=list)


class PublicationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_id: str
    content_id: str | None = None
    content: ContentIn | None = Field(default=None, description="Inline content (instead of content_id).")
    tailor: bool = Field(default=True, description="Fit the content to the destination and record a diff.")
    scheduled_for: datetime | None = Field(default=None, description="Publish at this time (rules evaluated for it).")
    callback_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)

    @model_validator(mode="after")
    def _one_content(self) -> PublicationIn:
        if (self.content_id is None) == (self.content is None):
            raise ValueError("send exactly one of content_id or content")
        return self


class AssistCompleteIn(BaseModel):
    """What the person reports after handling an assist pack. For an assisted destination this
    report is the only "platform" signal, so a community's refusal or removal is recorded here."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["posted", "rejected_by_community", "removed_by_community", "abandoned"] = "posted"
    reason: str | None = Field(default=None, max_length=2000)  # required unless outcome is "posted"
    live_url: str | None = Field(default=None, max_length=2000)
    platform_post_id: str | None = Field(default=None, max_length=255)
    completed_by: str = Field(default="human", max_length=255)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_for_failures(self) -> AssistCompleteIn:
        if self.outcome != "posted" and not (self.reason or "").strip():
            raise ValueError(f"a reason is required when the outcome is '{self.outcome}'")
        return self


class CertificationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_id: str
    trigger: Literal["manual", "scheduled", "self_heal"] = "manual"
    callback_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)
