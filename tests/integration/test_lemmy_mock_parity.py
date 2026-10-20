"""The mock Lemmy answers with the same shapes and error bodies as the real Lemmy 0.19.20 (recorded fixtures).

For each recorded exchange, the mock's answer must have the same status and contain every key path of the
recorded body (lists compared by their items). This keeps sim-mode results evidence-backed, not invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "recorded" / "lemmy"
ADMIN = {"Authorization": "Bearer parity-admin-token"}


def key_paths(value: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths |= key_paths(item, path)
    elif isinstance(value, list):
        for item in value:
            paths |= key_paths(item, f"{prefix}[]")
    return paths


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]


def assert_same_shape(name: str, response: httpx.Response, *, ignore: tuple[str, ...] = ()) -> None:
    expected = recorded(name)
    assert response.status_code == expected["status"], (name, response.text)
    missing = {p for p in key_paths(expected["json"]) - key_paths(response.json()) if not p.startswith(ignore)}
    assert not missing, f"{name}: mock lacks {sorted(missing)}"


@pytest.fixture
def lemmy(mock_platforms_url: str) -> str:
    return f"{mock_platforms_url}/lemmy/parity"


def _create(lemmy: str, **payload: Any) -> httpx.Response:
    body = {"name": "IBX fixture post", "community_id": 2, "body": "x\n\n[//]: # (ibx-fix)\n", **payload}
    return httpx.post(f"{lemmy}/api/v3/post", json=body, headers=ADMIN)


def test_success_shapes_match_the_recordings(lemmy):
    assert_same_shape("login_ok", httpx.post(f"{lemmy}/api/v3/user/login", json={"username_or_email": "ibx_admin", "password": "pw-1234"}))
    httpx.get(f"{lemmy}/api/v3/community", params={"name": "ibx_test"})  # communities are created on first use
    created = _create(lemmy)
    assert_same_shape("create_post_ok", created)
    post_id = created.json()["post_view"]["post"]["id"]
    # the site and community_view blocks of these two are not read by the connector
    assert_same_shape("get_post_ok", httpx.get(f"{lemmy}/api/v3/post", params={"id": post_id}, headers=ADMIN),
                      ignore=("community_view", "moderators", "cross_posts"))
    assert_same_shape("creator_posts_lookup", httpx.get(f"{lemmy}/api/v3/user", params={"username": "ibx_admin", "sort": "New", "limit": 5}, headers=ADMIN),
                      ignore=("site", "moderates"))
    assert_same_shape("site_rate_limit_config", httpx.get(f"{lemmy}/api/v3/site", headers=ADMIN))
    assert_same_shape("live_url_get", httpx.get(f"{lemmy}/post/{post_id}"), ignore=("content", "tag"))
    assert_same_shape("delete_post_soft", httpx.post(f"{lemmy}/api/v3/post/delete", json={"post_id": post_id, "deleted": True}, headers=ADMIN))
    assert_same_shape("purge_post", httpx.post(f"{lemmy}/api/v3/admin/purge/post", json={"post_id": post_id, "reason": "x"}, headers=ADMIN))
    assert_same_shape("get_post_not_found_after_purge", httpx.get(f"{lemmy}/api/v3/post", params={"id": post_id}, headers=ADMIN))


def test_error_bodies_match_the_recordings(lemmy):
    httpx.get(f"{lemmy}/api/v3/community", params={"name": "ibx_test"})
    cases = {
        "login_incorrect": httpx.post(f"{lemmy}/api/v3/user/login", json={"username_or_email": "ibx_admin", "password": "wrong-pw"}),
        "create_post_invalid_token": httpx.post(f"{lemmy}/api/v3/post", json={"name": "abc", "community_id": 2},
                                                headers={"Authorization": "Bearer invalid-forged"}),
        "create_post_title_too_long": _create(lemmy, name="T" * 201),
        "create_post_community_not_found": _create(lemmy, community_id=999999),
        "get_post_not_found": httpx.get(f"{lemmy}/api/v3/post", params={"id": 999999}),
    }
    for name, response in cases.items():
        assert response.status_code == recorded(name)["status"], name
        assert response.json() == recorded(name)["json"], name  # the exact body: {"error": "<token>"}


def test_rate_limit_matches_the_recording(lemmy):
    httpx.get(f"{lemmy}/api/v3/community", params={"name": "ibx_test"})
    httpx.put(f"{lemmy}/api/v3/site", json={"rate_limit_post": 2, "rate_limit_post_per_second": 600}, headers=ADMIN).raise_for_status()
    assert _create(lemmy).status_code == _create(lemmy).status_code == 200
    limited = _create(lemmy)
    assert limited.status_code == 400 and limited.json() == recorded("create_post_rate_limited")["json"]
    assert not {"retry-after", "x-ratelimit-reset"} & {h.lower() for h in limited.headers}  # measured: no reset header
    limits = httpx.get(f"{lemmy}/api/v3/site").json()["site_view"]["local_site_rate_limit"]
    assert (limits["post"], limits["post_per_second"]) == (2, 600)


def test_measured_length_rules(lemmy):
    httpx.get(f"{lemmy}/api/v3/community", params={"name": "ibx_test"})
    assert _create(lemmy, name="ab").json() == {"error": "invalid_post_title"}
    assert _create(lemmy, name="a\nb c").json() == {"error": "invalid_post_title"}
    assert _create(lemmy, name="😀" * 200).status_code == 200
    assert _create(lemmy, body="a" * 49998 + "😀").status_code == 200  # 50000 UTF-16 units
    assert _create(lemmy, body="a" * 49999 + "😀").json() == {"error": "invalid_body_field"}
