"""Strict models of the Ghost 6 Admin API responses we read (shapes from tests/fixtures/recorded/ghost).

Only the fields the connector uses are declared, and each one is required. Ghost may add fields
freely (extra fields are ignored), but if it renames or drops one we use (``posts[0].url`` →
``posts[0].permalink``), ``model_validate`` raises ``ValidationError`` and the base connector reports
PLATFORM_UNAVAILABLE with ``details.reason = "response_schema_mismatch"`` instead of a silent ``None``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class GhostPost(_Model):
    id: str
    uuid: str
    title: str
    slug: str
    status: str  # published | draft | scheduled | sent
    url: str  # public URL (a /p/<uuid>/ preview URL while the post is a draft)


class PostsEnvelope(_Model):
    """Every Admin API post response: ``{"posts": [...]}`` (create, read, read by slug, browse)."""

    posts: list[GhostPost]


class OnePost(_Model):
    """A create or read answer must hold exactly one post."""

    posts: list[GhostPost] = Field(min_length=1, max_length=1)


class BrowseIds(_Model):
    """``GET /posts/?fields=id``: used by refresh_auth to prove the key works."""

    posts: list[dict]
