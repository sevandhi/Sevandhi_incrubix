"""The Discourse mock must answer like the real Discourse 2026.10 recorded in tests/fixtures/recorded/discourse:
same status codes, same error bodies, and only field names the real API also returns (with the same JSON types)."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mocks.platforms.app import create_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "discourse"
ADMIN = {"Api-Key": "dev-key", "Api-Username": "ibx_admin"}
BODY = "Fixture topic recorded by the IncuBrix bootstrap on a local test instance."


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]


def kind(value: Any) -> str:
    if value is None:
        return "null"
    return {bool: "bool", int: "number", float: "number", str: "string", list: "list", dict: "object"}[type(value)]


def assert_like(mock: dict[str, Any], real: dict[str, Any], *, allow_extra: tuple[str, ...] = ()) -> None:
    extra = set(mock) - set(real) - set(allow_extra)
    assert not extra, f"mock-only fields: {sorted(extra)}"
    for key in set(mock) & set(real):
        if mock[key] is not None and real[key] is not None:
            assert kind(mock[key]) == kind(real[key]), key


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


@pytest.fixture
def forum(client) -> str:
    return f"/discourse/fid-{uuid.uuid4().hex[:8]}"


def create(client, forum: str, title: str, external_id: str | None = None, headers: dict | None = None):
    payload = {"title": title, "raw": BODY, "category": 5, **({"external_id": external_id} if external_id else {})}
    return client.post(f"{forum}/posts.json", json=payload, headers=headers or ADMIN)


def test_create_and_get_match_the_recorded_shapes(client, forum):
    created = create(client, forum, "IBX fixture topic 90fff62e recorded locally", "ibx-fix1")
    real = recorded("create_topic_ok")
    assert created.status_code == real["status"] == 200
    assert_like(created.json(), real["json"])
    topic = client.get(f"{forum}/t/{created.json()['topic_id']}.json", headers=ADMIN)
    real_topic = recorded("get_topic_ok")
    assert topic.status_code == 200
    assert_like(topic.json(), real_topic["json"])
    assert_like(topic.json()["post_stream"]["posts"][0], real_topic["json"]["post_stream"]["posts"][0], allow_extra=("link_counts",))
    for field in ("id", "slug", "posts_count", "views", "like_count", "deleted_at", "external_id", "visible"):
        assert field in topic.json()


def test_errors_match_the_recordings(client, forum):
    bad = create(client, forum, "Some valid looking title", headers={"Api-Key": "revoked-key", "Api-Username": "ibx_admin"})
    real = recorded("invalid_api_key")
    assert bad.status_code == real["status"] == 403 and bad.json() == real["json"]

    long = create(client, forum, "Long title " + "x" * 245)
    real = recorded("create_topic_title_too_long")
    assert long.status_code == real["status"] == 422
    assert set(long.json()) == set(real["json"]) and long.json()["action"] == "create_post"
    assert long.json()["errors"][0] == real["json"]["errors"][0]  # "Title is too long (maximum is 255 characters)"

    create(client, forum, "IBX duplicate title for the fidelity test")
    dup = create(client, forum, "IBX duplicate title for the fidelity test")
    real = recorded("create_topic_duplicate_title")
    pattern = r"^This title has already been used by <a href='.+/t/[a-z0-9-]+/\d+'>another topic</a>\.$"
    assert dup.status_code == 422 and re.match(pattern, dup.json()["errors"][0]) and re.match(pattern, real["json"]["errors"][0])

    missing = client.get(f"{forum}/t/99999999.json", headers=ADMIN)
    real = recorded("get_topic_not_found")
    assert missing.status_code == 404 and missing.json()["errors"] == real["json"]["errors"]
    assert missing.json()["error_type"] == real["json"]["error_type"] == "not_found"


def test_external_id_delete_and_live_url_match_the_recordings(client, forum):
    created = create(client, forum, "IBX external id fidelity topic", "ibx-fid2").json()
    topic_id, slug = created["topic_id"], created["topic_slug"]
    lookup = client.get(f"{forum}/t/external_id/ibx-fid2.json", headers=ADMIN)
    real = recorded("get_topic_by_external_id")
    assert lookup.status_code == real["status"] == 301
    assert lookup.headers["location"].endswith(f"/t/{slug}/{topic_id}.json")
    assert real["headers"]["location"].endswith(".json")

    live = client.get(f"{forum}/t/{topic_id}")
    assert live.status_code == recorded("live_url_get")["status"] == 301
    assert client.get(f"{forum}/t/{slug}/{topic_id}").status_code == 200

    deleted = client.delete(f"{forum}/t/{topic_id}.json", headers=ADMIN)
    real = recorded("delete_topic")
    assert deleted.status_code == real["status"] == 200 and deleted.content == b""
    assert deleted.headers["content-type"].startswith("text/plain")

    admin = client.get(f"{forum}/t/{topic_id}.json", headers=ADMIN)
    real_admin = recorded("get_topic_after_delete_admin")["json"]
    assert admin.status_code == 200 and admin.json()["deleted_at"] and admin.json()["deleted_by"]["username"]
    assert admin.json()["posts_count"] == real_admin["posts_count"] == 0
    public = client.get(f"{forum}/t/{topic_id}.json")
    assert public.status_code == recorded("get_topic_after_delete_public")["status"] == 404
    assert client.get(f"{forum}/t/{slug}/{topic_id}").status_code == 404
    # measured on the real 2026.10 instance (not in the recordings): delete again -> 422, lookup -> 404
    again = client.delete(f"{forum}/t/{topic_id}.json", headers=ADMIN)
    assert again.status_code == 422 and again.json()["errors"][0].startswith("There was an error deleting that topic.")
    assert client.get(f"{forum}/t/external_id/ibx-fid2.json", headers=ADMIN).status_code == 404


def test_basic_info_shape(client, forum):
    info = client.get(f"{forum}/site/basic-info.json")
    assert info.status_code == 200
    assert set(info.json()) == set(recorded("site_basic_info")["json"])
