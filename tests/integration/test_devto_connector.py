"""Dev.to / Forem connector end to end against the mock Forem (real sockets, real Postgres).

Key honesty checks: ``delete`` is not a capability (404 METHOD_NOT_SUPPORTED) and the certification
removal helper never reports ``deleted: true``.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.contract.models import Connection, Destination
from app.contract.router import get_connector
from tests.conftest import api_client

KEY = "devto"
CONNECTION = {"connector_key": KEY, "auth_type": "api_key", "credentials": {"api_key": "dev-forem-key"}}


def dest(mock: str, tenant: str, **instance: Any) -> dict[str, Any]:
    return {"connector_key": KEY, "surface": "article", "instance": {"base_url": f"{mock}/forem/{tenant}", **instance},
            "rules": {"automation_allowed": True}}


def publish_body(mock: str, tenant: str, key: str, *, tags: list[str] | None = None, connection: dict | None = None,
                 title: str | None = None, **instance: Any) -> dict[str, Any]:
    return {
        "rendition": {"title": title or f"Notes on release planning {uuid.uuid4().hex[:6]}", "body": "Body of the article.",
                      "tags": tags if tags is not None else ["python", "devops"], "ai_generated": False},
        "destination": dest(mock, tenant, **instance),
        "connection": connection or CONNECTION,
        "idempotency_key": key,
    }


def ref(mock: str, tenant: str, post_id: str, **instance: Any) -> dict[str, Any]:
    return {"platform_post_id": post_id, "connection": CONNECTION, "destination": dest(mock, tenant, **instance)}


def creates(mock: str, tenant: str) -> int:
    counts = httpx.get(f"{mock}/_counts", params={"platform": "forem", "tenant": tenant}).json()["counts"]
    return counts.get("forem", {}).get(tenant, {}).get("POST /api/articles", 0)


@pytest.fixture
async def api(app_factory, pg_url, mock_platforms_url):
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


def devto(api) -> Any:
    return get_connector(SimpleNamespace(app=api.app), KEY)  # type: ignore[arg-type]


async def test_publish_status_metrics_and_no_delete(api, mock_platforms_url):
    mock = mock_platforms_url
    r = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forem-1", "f-rt"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "published" and httpx.get(out["live_url"]).status_code == 200
    status = await api.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "forem-1", out["platform_post_id"]))
    assert status.json() == {"status": "published", "live_url": out["live_url"]}
    httpx.post(f"{mock}/forem/forem-1/_mock/articles/{out['platform_post_id']}/engage", json={"views": 10, "reactions": 3, "comments": 2})
    metrics = await api.post(f"/connector/v1/{KEY}/fetch_metrics", json=ref(mock, "forem-1", out["platform_post_id"]))
    m = metrics.json()
    assert (m["views"], m["likes"], m["comments"]) == (11, 3, 2)  # 10 engaged + 1 page view above
    assert m["shares"] is None and m["clicks"] is None and m["saves"] is None

    deleted = await api.post(f"/connector/v1/{KEY}/delete", json=ref(mock, "forem-1", out["platform_post_id"]))
    assert deleted.status_code == 404 and deleted.json()["error"]["code"] == "METHOD_NOT_SUPPORTED"


async def test_admin_unpublish_is_honest_and_public_url_404(api, mock_platforms_url):
    mock = mock_platforms_url
    out = (await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forem-admin", "f-admin"))).json()
    removal = await devto(api).unpublish_to_draft(
        out["platform_post_id"], Connection.model_validate(CONNECTION),
        Destination.model_validate(dest(mock, "forem-admin", forem_admin=True)),
    )
    assert removal["deleted"] is False and removal["unpublished"] is True
    assert removal["removal_mode"] == "admin_unpublish" and removal["warnings"][0].startswith("PLATFORM_NO_DELETE")
    assert httpx.get(out["live_url"]).status_code == 404
    status = await api.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "forem-admin", out["platform_post_id"]))
    assert status.json() == {"status": "draft", "live_url": None}


async def test_author_revert_to_draft_wipes_content(api, mock_platforms_url):
    mock = mock_platforms_url
    author = {**CONNECTION, "credentials": {"api_key": "user-author-key"}}
    out = (await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forem-author", "f-author", connection=author))).json()
    removal = await devto(api).unpublish_to_draft(
        out["platform_post_id"], Connection.model_validate(author), Destination.model_validate(dest(mock, "forem-author"))
    )
    assert removal == {"deleted": False, "unpublished": True, "removal_mode": "unpublish_to_draft", "wiped": True,
                       "warnings": [removal["warnings"][0]]}
    assert httpx.get(out["live_url"]).status_code == 404
    mine = httpx.get(f"{mock}/forem/forem-author/api/articles/me/all", headers={"api-key": "user-author-key"}).json()
    assert mine[0]["published"] is False and mine[0]["title"].startswith("[removed]")


async def test_draft_only_mode(api, mock_platforms_url):
    mock = mock_platforms_url
    r = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forem-draft", "f-draft", draft_only=True))
    assert r.json()["status"] == "draft" and r.json()["live_url"] is None
    status = await api.post(f"/connector/v1/{KEY}/get_status", json=ref(mock, "forem-draft", r.json()["platform_post_id"]))
    assert status.json() == {"status": "draft", "live_url": None}


async def test_timeout_after_create_then_retry_finds_marker(api, mock_platforms_url):
    mock = mock_platforms_url
    body = publish_body(mock, "forem-t", "f-timeout")
    first = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-Sim-Fault": "timeout_after_create:5"})
    assert first.status_code == 503 and first.json()["error"]["details"]["intent_state"] == "UNKNOWN"
    retry = await api.post(f"/connector/v1/{KEY}/publish", json=body, headers={"X-IncuBrix-Verbose": "1"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["_meta"]["reconciled"] is True
    assert creates(mock, "forem-t") == 1


async def test_errors_auth_spec_platform_and_rate_limit(api, mock_platforms_url):
    mock = mock_platforms_url
    bad = {**CONNECTION, "credentials": {"api_key": "revoked-1"}}
    r = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forem-e", "f-auth", connection=bad))
    assert r.status_code == 424 and r.json()["error"]["code"] == "CONNECTION_AUTH_EXPIRED"

    five = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forem-e", "f-tags", tags=["a", "b", "c", "d", "e"]))
    assert five.status_code == 422 and five.json()["error"]["code"] == "DEST_SPEC_VIOLATION"

    body = publish_body(mock, "forem-e", "f-long", title="x" * 200)
    body["destination"]["spec_overrides"] = {"title_max_chars": 500}  # let the platform itself refuse it
    long = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert long.status_code == 422
    assert long.json()["error"]["code"] == "PLATFORM_CONTENT_REJECTED"
    assert "maximum is 128" in long.json()["error"]["details"]["platform_message"]

    httpx.post(f"{mock}/forem/forem-rl/_mock/rate_limit", json={"limit": 1, "window_s": 90, "retry_after": "http-date"})
    assert (await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forem-rl", "f-rl-1"))).status_code == 200
    limited = await api.post(f"/connector/v1/{KEY}/publish", json=publish_body(mock, "forem-rl", "f-rl-2"))
    assert limited.status_code == 429
    details = limited.json()["error"]["details"]
    assert details["reset_source"] == "Retry-After" and 80 <= details["retry_after_s"] <= 92


@pytest.mark.live_local
@pytest.mark.skipif(not os.environ.get("DEVTO_API_KEY"), reason="DEVTO_API_KEY not set: real dev.to is never called")
async def test_real_devto_draft_only(api):  # pragma: no cover - needs a real key; drafts only, never public
    key = os.environ["DEVTO_API_KEY"]
    connection = {"connector_key": KEY, "auth_type": "api_key", "credentials": {"api_key": key}}
    body = publish_body("unused", "unused", f"f-real-{uuid.uuid4().hex[:8]}", connection=connection)
    body["destination"] = {"connector_key": KEY, "surface": "article", "rules": {"automation_allowed": True},
                           "instance": {"base_url": "https://dev.to", "draft_only": True}}
    r = await api.post(f"/connector/v1/{KEY}/publish", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft" and r.json()["live_url"] is None
    removal = await devto(api).unpublish_to_draft(r.json()["platform_post_id"], Connection.model_validate(connection),
                                                  Destination.model_validate(body["destination"]))
    assert removal["deleted"] is False and removal["removal_mode"] == "unpublish_to_draft"
