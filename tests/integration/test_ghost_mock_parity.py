"""The mock Ghost answers with the same shapes and error bodies as the real Ghost 6.70 (recorded fixtures)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.connectors.ghost.connector import admin_token
from tests.integration.test_lemmy_mock_parity import key_paths

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "recorded" / "ghost"
KEY_ID, SECRET = "6acfcd9b8944510001d12d99", bytes.fromhex("2e" * 32)
# Post fields the recordings have that the mock does not reproduce (the connector reads none of them).
NOT_MOCKED = ("posts[].authors[].", "posts[].tiers", "posts[].primary_", "posts[].og_", "posts[].twitter_", "posts[].meta_",
              "posts[].email_", "posts[].feature_image_", "posts[].frontmatter", "posts[].codeinjection_",
              "posts[].custom_template", "posts[].count.")


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]


def headers(*, kid: str = KEY_ID, now: float | None = None) -> dict[str, str]:
    token, _ = admin_token(kid, SECRET, time.time() if now is None else now)
    return {"Authorization": f"Ghost {token}"}


def assert_same_shape(name: str, response: httpx.Response) -> None:
    expected = recorded(name)
    assert response.status_code == expected["status"], (name, response.text)
    if "json" in expected:
        missing = {p for p in key_paths(expected["json"]) - key_paths(response.json()) if not p.startswith(NOT_MOCKED)}
        assert not missing, f"{name}: mock lacks {sorted(missing)}"


@pytest.fixture
def ghost(mock_platforms_url: str) -> str:
    return f"{mock_platforms_url}/ghost/parity"


def test_post_lifecycle_shapes_match_the_recordings(ghost):
    admin = f"{ghost}/ghost/api/admin"
    assert_same_shape("site", httpx.get(f"{admin}/site/"))
    created = httpx.post(f"{admin}/posts/", params={"source": "html"}, headers=headers(),
                         json={"posts": [{"title": "IBX fixture post", "slug": "ibx-fixture-1", "status": "published", "html": "<p>x</p>"}]})
    assert_same_shape("create_post_ok", created)
    post = created.json()["posts"][0]
    assert post["url"] == f"{ghost}/ibx-fixture-1/"
    assert_same_shape("get_post_ok", httpx.get(f"{admin}/posts/{post['id']}/", headers=headers()))
    assert_same_shape("get_post_by_slug_ok", httpx.get(f"{admin}/posts/slug/ibx-fixture-1/", headers=headers()))
    assert_same_shape("live_url_get", httpx.get(post["url"]))
    assert_same_shape("delete_post", httpx.delete(f"{admin}/posts/{post['id']}/", headers=headers()))
    assert_same_shape("delete_post_not_found", httpx.delete(f"{admin}/posts/{post['id']}/", headers=headers()))
    assert_same_shape("get_post_not_found_after_delete", httpx.get(f"{admin}/posts/{post['id']}/", headers=headers()))
    assert_same_shape("live_url_after_delete", httpx.get(post["url"]))


def _first_error(response: httpx.Response) -> dict[str, Any]:
    error = response.json()["errors"][0]
    return {k: error[k] for k in ("message", "type", "code", "context")}


def test_error_bodies_match_the_recordings(ghost):
    admin = f"{ghost}/ghost/api/admin"
    cases = {
        "expired_token": httpx.get(f"{admin}/posts/", params={"limit": 1}, headers=headers(now=time.time() - 600)),
        "unknown_key_id": httpx.get(f"{admin}/posts/", params={"limit": 1}, headers=headers(kid="unknown0000000000000000")),
        "create_post_title_too_long": httpx.post(f"{admin}/posts/", headers=headers(),
                                                 json={"posts": [{"title": "T" * 256, "status": "draft"}]}),
        "get_post_not_found": httpx.get(f"{admin}/posts/000000000000000000000000/", headers=headers()),
    }
    for name, response in cases.items():
        expected = recorded(name)
        assert response.status_code == expected["status"], name
        want = expected["json"]["errors"][0]
        assert _first_error(response) == {k: want[k] for k in ("message", "type", "code", "context")}, name


def test_duplicate_slug_gets_a_suffix_like_ghost(ghost):
    admin = f"{ghost}/ghost/api/admin"
    body = {"posts": [{"title": "Same", "slug": "same-slug", "status": "published"}]}
    first = httpx.post(f"{admin}/posts/", json=body, headers=headers()).json()["posts"][0]["slug"]
    second = httpx.post(f"{admin}/posts/", json=body, headers=headers()).json()["posts"][0]["slug"]
    assert (first, second) == ("same-slug", "same-slug-2")  # measured on the real Ghost 6.70
