"""IncuBrix connector contract objects (Appendix A).

The brief names ``rendition``, ``destination`` and ``connection`` but does not define them,
so the shapes below are our explicit, documented reading (docs/CONTRACT.md).

* Inbound models are liberal: unknown fields are accepted and kept (``extra="allow"``), and a
  few common aliases are understood, so the IncuBrix runtime's real payloads are not rejected.
* Outbound models are strict: they serialise to exactly the Appendix A keys and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr

ContentType = Literal["text", "image", "video", "video_link", "link"]
BodyFormat = Literal["markdown", "html", "plain"]
PostStatus = Literal[
    "published",
    "draft",
    "pending",
    "scheduled",
    "awaiting_human",
    "deleted",
    "removed_by_platform",
    "unknown",
]


class _Inbound(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class _Outbound(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- inbound


class Media(_Inbound):
    url: str
    mime: str | None = Field(default=None, validation_alias=AliasChoices("mime", "mime_type", "content_type"))
    bytes: int | None = Field(default=None, validation_alias=AliasChoices("bytes", "size", "size_bytes"))
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    alt: str | None = Field(default=None, validation_alias=AliasChoices("alt", "alt_text"))


class Rendition(_Inbound):
    """One piece of content already rendered for one surface."""

    rendition_id: str | None = Field(default=None, validation_alias=AliasChoices("rendition_id", "id"))
    content_id: str | None = None
    surface: str | None = None
    content_type: ContentType = "text"
    title: str | None = None
    body: str = Field(default="", validation_alias=AliasChoices("body", "text", "caption", "description"))
    body_format: BodyFormat = "markdown"
    canonical_url: str | None = None
    link_url: str | None = Field(default=None, validation_alias=AliasChoices("link_url", "link", "url"))
    media: list[Media] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, validation_alias=AliasChoices("tags", "hashtags"))
    mentions: list[str] = Field(default_factory=list)
    language: str | None = Field(default=None, validation_alias=AliasChoices("language", "lang"))
    is_promotional: bool | None = None
    ai_generated: bool | None = None


class Destination(_Inbound):
    """One place that reaches one audience. ``instance`` holds the connector's instance_param."""

    destination_id: str | None = Field(default=None, validation_alias=AliasChoices("destination_id", "id"))
    connector_key: str | None = None
    platform_key: str | None = None
    surface: str | None = None
    name: str | None = None
    url: str | None = None
    instance: dict[str, Any] = Field(default_factory=dict)
    language: str | None = None
    spec_overrides: dict[str, Any] | None = None
    rules: dict[str, Any] | None = None


class Connection(_Inbound):
    """Credentials for one account on one platform. Secrets never leave as plain text."""

    connection_id: str | None = Field(default=None, validation_alias=AliasChoices("connection_id", "id"))
    connector_key: str | None = None
    auth_type: Literal["oauth2", "api_key", "bot_token", "none"] | None = None
    credentials: dict[str, SecretStr] = Field(default_factory=dict)
    expires_at: datetime | None = None

    def secret(self, name: str) -> str | None:
        value = self.credentials.get(name)
        return value.get_secret_value() if value is not None else None


# ---------------------------------------------------------------- outbound (exact Appendix A keys)


class Violation(_Outbound):
    field: str
    rule: str
    message: str
    limit: Any | None = None
    actual: Any | None = None


class ValidateResult(_Outbound):
    ok: bool
    violations: list[Violation] = Field(default_factory=list)


class PublishResult(_Outbound):
    platform_post_id: str
    live_url: str | None
    status: PostStatus


class StatusResult(_Outbound):
    status: PostStatus
    live_url: str | None


class DeleteResult(_Outbound):
    deleted: bool


class MetricsResult(_Outbound):
    """Normalised metrics. ``None`` means the platform does not expose that metric (never a fake 0)."""

    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    watch_time_s: float | None = None
    clicks: int | None = None


class RefreshAuthResult(_Outbound):
    expires_at: datetime | None


class AssistFile(_Outbound):
    name: str
    url: str
    mime: str | None = None


class AssistPack(_Outbound):
    files: list[AssistFile]
    caption: str
    deep_link: str
    checklist: list[str]


# ---------------------------------------------------------------- request bodies for the HTTP contract


class ValidateRequest(_Inbound):
    rendition: Rendition
    destination: Destination
    connection: Connection | None = None


class PublishRequest(_Inbound):
    rendition: Rendition
    destination: Destination
    connection: Connection
    idempotency_key: str | None = None


class PostRefRequest(_Inbound):
    platform_post_id: str = Field(validation_alias=AliasChoices("platform_post_id", "post_id"))
    connection: Connection
    destination: Destination | None = None


class MetricsRequest(PostRefRequest):
    since: datetime | None = None


class RefreshAuthRequest(_Inbound):
    connection: Connection
    destination: Destination | None = None


class AssistPackRequest(_Inbound):
    rendition: Rendition
    destination: Destination
