"""Strict models of the Lemmy 0.19 API responses we read (shapes from tests/fixtures/recorded/lemmy).

Only the fields the connector uses are declared, and each one is required. Extra fields are ignored,
so Lemmy may add fields freely, but if it renames or drops one we use (``post.id`` → ``post.post_id``),
``model_validate`` raises ``ValidationError``. The base connector turns that into PLATFORM_UNAVAILABLE
with ``details.reason = "response_schema_mismatch"``: a loud failure instead of a silent ``None``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- posts


class Post(_Model):
    id: int
    name: str
    body: str | None = None  # Lemmy omits the body of a post that has none
    creator_id: int
    community_id: int
    deleted: bool  # soft-deleted by its creator
    removed: bool  # removed by a moderator or admin
    ap_id: str  # the post's canonical URL, e.g. http://lemmy:8536/post/1


class PostCounts(_Model):
    comments: int
    score: int
    upvotes: int
    downvotes: int


class PostView(_Model):
    post: Post
    counts: PostCounts


class PostResponse(_Model):
    """POST /api/v3/post (create) and POST /api/v3/post/delete."""

    post_view: PostView


class GetPostResponse(_Model):
    """GET /api/v3/post?id=..."""

    post_view: PostView


class PersonDetails(_Model):
    """GET /api/v3/user?username=...&sort=New: the person's newest posts (used to find a marker)."""

    posts: list[PostView]


# ---------------------------------------------------------------- communities, login, site


class Community(_Model):
    id: int
    name: str


class CommunityView(_Model):
    community: Community


class GetCommunityResponse(_Model):
    """GET /api/v3/community?name=..."""

    community_view: CommunityView


class LoginResponse(_Model):
    """POST /api/v3/user/login. ``jwt`` is null while an email check or a sign-up application is pending."""

    jwt: str | None


class SuccessResponse(_Model):
    """POST /api/v3/admin/purge/post."""

    success: bool


class LocalSiteRateLimit(_Model):
    """``post`` posts are allowed per ``post_per_second`` seconds (a token bucket per IP address)."""

    post: int
    post_per_second: int


class SiteView(_Model):
    local_site_rate_limit: LocalSiteRateLimit


class Person(_Model):
    id: int
    name: str


class LocalUserView(_Model):
    person: Person


class MyUser(_Model):
    local_user_view: LocalUserView


class SiteResponse(_Model):
    """GET /api/v3/site. ``my_user`` is only present when the request carries a valid token."""

    version: str
    site_view: SiteView
    my_user: MyUser | None = None
