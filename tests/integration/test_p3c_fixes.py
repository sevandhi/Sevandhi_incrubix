"""Regression tests for app defects found in a proof run. Each test was written before its fix and seen failing.

* MH1: the same platform id on two instances of one connector (mock tenants all start at id 1) must not make
  Appendix A's ``get_status`` / ``delete`` / ``fetch_metrics(platform_post_id, connection)`` answer 422.
* MH4: a certification test post on ANOTHER instance with the same platform id must not hide a real post
  from the frequency cap (contract path and catalogue path).
* HC5: a renamed response field is named (``response_schema_mismatch``) in the publish error.

They run the real HTTP contract against the scaffolded ``sample`` connector, a real Postgres and mock-platforms.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from tests.conftest import SAMPLE_KEY, api_client

CONNECTION = {"connector_key": SAMPLE_KEY, "auth_type": "api_key", "credentials": {"token": "tok-p3c-1"}}
OTHER_CONNECTION = {"connector_key": SAMPLE_KEY, "auth_type": "api_key", "credentials": {"token": "tok-p3c-2"}}
ANON = {"connector_key": SAMPLE_KEY}
PUBLISH = f"/connector/v1/{SAMPLE_KEY}/publish"


def body(mock_url: str, tenant: str, key: str, *, connection: dict[str, Any] = CONNECTION, title: str = "Hello",
         **dest: Any) -> dict[str, Any]:
    return {
        "rendition": {"title": title, "body": "A first post", "content_type": "text", "ai_generated": False},
        "destination": {"connector_key": SAMPLE_KEY, "surface": "post",
                        "instance": {"base_url": f"{mock_url}/{SAMPLE_KEY}/{tenant}"}, **dest},
        "connection": connection,
        "idempotency_key": key,
    }


def stored(tenant: str) -> dict[int, dict[str, Any]]:
    from mocks.platforms.tenants import store

    return store.state(SAMPLE_KEY, tenant)["posts"]


@pytest.fixture
async def api(app_factory, pg_url, mock_platforms_url, sample_registry, fake_clock):
    app = await app_factory(pg_url=pg_url, registry=sample_registry, clock=fake_clock, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


async def _two_posts_with_id_1(api: httpx.AsyncClient, mock: str, fake_clock: Any,
                               second_connection: dict[str, Any] = CONNECTION) -> tuple[dict[str, Any], dict[str, Any]]:
    first = await api.post(PUBLISH, json=body(mock, "mh1-a", "mh1-a1"))
    assert first.status_code == 200, first.text
    fake_clock.advance(5)
    second = await api.post(PUBLISH, json=body(mock, "mh1-b", "mh1-b1", connection=second_connection))
    assert second.status_code == 200, second.text
    assert list(stored("mh1-a")) == [1] and list(stored("mh1-b")) == [1]  # both platforms really answered id 1
    return first.json(), second.json()


# ------------------------------------------------------------------ MH1: same platform id on two instances


async def test_mh1_returned_ids_are_unambiguous_and_appendix_a_calls_reach_the_right_instance(api, mock_platforms_url, fake_clock):
    a, b = await _two_posts_with_id_1(api, mock_platforms_url, fake_clock)
    assert set(a) == set(b) == {"platform_post_id", "live_url", "status"}  # Appendix A shape unchanged
    assert a["platform_post_id"] != b["platform_post_id"]
    for post, tenant in ((a, "mh1-a"), (b, "mh1-b")):
        ref = {"platform_post_id": post["platform_post_id"], "connection": CONNECTION}
        status = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status", json=ref)
        assert status.status_code == 200, status.text
        assert status.json() == {"status": "published", "live_url": post["live_url"]}
        metrics = await api.post(f"/connector/v1/{SAMPLE_KEY}/fetch_metrics", json={**ref, "since": None})
        assert metrics.status_code == 200, metrics.text
        deleted = await api.post(f"/connector/v1/{SAMPLE_KEY}/delete", json=ref)
        assert deleted.status_code == 200 and deleted.json() == {"deleted": True}, deleted.text
        assert stored(tenant) == {} or all(p.get("status") == "deleted" for p in stored(tenant).values())


async def test_mh1_returned_id_still_works_with_an_explicit_destination(api, mock_platforms_url, fake_clock):
    _, b = await _two_posts_with_id_1(api, mock_platforms_url, fake_clock)
    ref = {"platform_post_id": b["platform_post_id"], "connection": CONNECTION,
           "destination": {"connector_key": SAMPLE_KEY, "instance": {"base_url": f"{mock_platforms_url}/{SAMPLE_KEY}/mh1-b"}}}
    status = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status", json=ref)
    assert status.status_code == 200, status.text
    assert status.json()["live_url"] == b["live_url"]


async def test_mh1_replay_returns_the_same_unambiguous_id(api, mock_platforms_url, fake_clock):
    _, b = await _two_posts_with_id_1(api, mock_platforms_url, fake_clock)
    again = await api.post(PUBLISH, json=body(mock_platforms_url, "mh1-b", "mh1-b1"))
    assert again.status_code == 200 and again.json()["platform_post_id"] == b["platform_post_id"]


async def _make_legacy(api: httpx.AsyncClient) -> None:
    """Rows written by older versions stored the platform's raw id: rewrite both intents back to id '1'."""
    from app.db.tables import PublishIntentRow

    async with api.app.state.session_factory() as session, session.begin():
        for key in ("mh1-a1", "mh1-b1"):
            row = await session.get(PublishIntentRow, key)
            row.result = {**row.result, "platform_post_id": "1"}


async def test_mh1_legacy_rows_prefer_the_newest_post_of_the_callers_connection(api, mock_platforms_url, fake_clock):
    _, b = await _two_posts_with_id_1(api, mock_platforms_url, fake_clock)
    await _make_legacy(api)
    status = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status", json={"platform_post_id": "1", "connection": CONNECTION})
    assert status.status_code == 200, status.text
    assert status.json()["live_url"] == b["live_url"]  # the newest post of this connection


async def test_mh1_legacy_rows_are_told_apart_by_the_connection(api, mock_platforms_url, fake_clock):
    a, b = await _two_posts_with_id_1(api, mock_platforms_url, fake_clock, second_connection=OTHER_CONNECTION)
    await _make_legacy(api)
    for conn, post in ((CONNECTION, a), (OTHER_CONNECTION, b)):
        status = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status", json={"platform_post_id": "1", "connection": conn})
        assert status.status_code == 200, status.text
        assert status.json()["live_url"] == post["live_url"]


async def test_mh1_genuinely_ambiguous_without_a_connection_hint_is_a_clear_422(api, mock_platforms_url, fake_clock):
    await _two_posts_with_id_1(api, mock_platforms_url, fake_clock)
    await _make_legacy(api)
    response = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status", json={"platform_post_id": "1", "connection": ANON})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "REQUEST_VALIDATION_FAILED"
    assert "2" in error["message"] and "base_url" in error["message"] and "connection" in error["message"]


# ------------------------------------------------------------------ MH4: test posts on another instance never hide real posts


async def _test_post(api: httpx.AsyncClient, platform_post_id: str, destination_id: str | None) -> None:
    from app.db.tables import TestPostRow

    now = api.app.state.clock.now()
    async with api.app.state.session_factory() as session, session.begin():
        session.add(TestPostRow(connector_key=SAMPLE_KEY, destination_id=destination_id, platform_post_id=platform_post_id,
                                run_id="cert_other", created_at=now, expires_at=now + timedelta(minutes=10)))


async def test_mh4_contract_cap_counts_a_post_whose_id_equals_a_test_post_on_another_instance(api, mock_platforms_url):
    await _test_post(api, "1", "dest_some_other_instance")  # certification of ANOTHER tenant also got id 1
    rules = {"frequency_cap": "1/P7D", "automation_allowed": True}
    first = await api.post(PUBLISH, json=body(mock_platforms_url, "mh4-cap", "mh4-c1", **rules))
    assert first.status_code == 200, first.text
    assert list(stored("mh4-cap")) == [1]
    second = await api.post(PUBLISH, json=body(mock_platforms_url, "mh4-cap", "mh4-c2", title="Another", **rules))
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "DEST_RULE_FREQUENCY_CAP"
    assert list(stored("mh4-cap")) == [1]  # the platform was not called a second time


async def test_mh4_catalogue_history_counts_a_post_whose_id_equals_a_test_post_on_another_destination(api, mock_platforms_url):
    from app.certification.publications import publish_history
    from app.connectors.base import normalise_instance
    from app.db.tables import DestinationRow, PublishIntentRow

    url = f"{mock_platforms_url}/{SAMPLE_KEY}/mh4-cat"
    dest_key = f"{SAMPLE_KEY}|{normalise_instance(url)}|post"
    now = api.app.state.clock.now()
    async with api.app.state.session_factory() as session, session.begin():
        session.add(DestinationRow(id="dest_mh4_cat", destination_key=dest_key, connector_key=SAMPLE_KEY, name="mh4", url=url,
                                   normalized_url=url, platform_key=SAMPLE_KEY, surface="post", instance={"base_url": url}))
        session.add(PublishIntentRow(idempotency_key="real-1", request_hash="x", connector_key=SAMPLE_KEY, destination_key=dest_key,
                                     marker="m", state="SUCCEEDED", attempts=1, result={"platform_post_id": "1"},
                                     created_at=now, updated_at=now, instance=url, connection_ref="anon"))
    await _test_post(api, "1", "dest_some_other_instance")
    async with api.app.state.session_factory() as session:
        dest = await session.get(DestinationRow, "dest_mh4_cat")
        history = await publish_history(session, dest)
    assert len(history) == 1
    # a test post on THIS destination with that id is still not counted
    async with api.app.state.session_factory() as session, session.begin():
        await session.execute(update(PublishIntentRow).where(PublishIntentRow.idempotency_key == "real-1")
                              .values(idempotency_key="unrelated-key"))
    await _test_post(api, "1", "dest_mh4_cat")
    async with api.app.state.session_factory() as session:
        dest = await session.get(DestinationRow, "dest_mh4_cat")
        assert await publish_history(session, dest) == []


# ------------------------------------------------------------------ HC5: a renamed field is named


async def test_hc5_renamed_field_on_create_names_response_schema_mismatch(api, mock_platforms_url):
    response = await api.post(PUBLISH, json=body(mock_platforms_url, "hc5", "hc5-k1"),
                              headers={"X-Sim-Fault": "rename_field:id->post_id"})
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["details"]["reason"] == "response_schema_mismatch"
    assert error["details"]["intent_state"] == "UNKNOWN"
    assert "response_schema_mismatch" in error["message"] and "id" in error["message"]
    assert "timeout or server error" not in error["message"]
    assert "same idempotency_key" in error["message"]  # still says the retry is safe
