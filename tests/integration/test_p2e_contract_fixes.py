"""Regression tests for the contract fixes (rules resolution, Appendix A arguments, error mapping, pool use,
idempotency keys, promo history, placeholder credentials).

Each test was written before its fix and seen failing. They run the real HTTP contract against the
scaffolded ``sample`` connector, a real Postgres (testcontainers) and mock-platforms on a real socket.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.conftest import SAMPLE_KEY, api_client

CONNECTION = {"connector_key": SAMPLE_KEY, "auth_type": "api_key", "credentials": {"token": "tok-p2e-1"}}
VERBOSE = {"X-IncuBrix-Verbose": "1"}


def bare_destination(mock_url: str, tenant: str, **extra: Any) -> dict[str, Any]:
    """Exactly what Appendix A promises: no ``rules`` key."""
    return {"connector_key": SAMPLE_KEY, "surface": "post", "instance": {"base_url": f"{mock_url}/{SAMPLE_KEY}/{tenant}"}, **extra}


def body(mock_url: str, tenant: str, key: str, *, title: str = "Hello", text: str = "A first post", **dest: Any) -> dict[str, Any]:
    return {
        "rendition": {"title": title, "body": text, "content_type": "text", "ai_generated": False},
        "destination": bare_destination(mock_url, tenant, **dest),
        "connection": CONNECTION,
        "idempotency_key": key,
    }


def creates(mock_url: str, tenant: str) -> int:
    counts = httpx.get(f"{mock_url}/_counts", params={"platform": SAMPLE_KEY, "tenant": tenant}).json()["counts"]
    return counts.get(SAMPLE_KEY, {}).get(tenant, {}).get("POST /posts", 0)


def stored_posts(tenant: str) -> list[dict[str, Any]]:
    from mocks.platforms.tenants import store

    return list(store.state(SAMPLE_KEY, tenant)["posts"].values())


@pytest.fixture
async def api(app_factory, pg_url, mock_platforms_url, sample_registry, fake_clock):
    app = await app_factory(pg_url=pg_url, registry=sample_registry, clock=fake_clock, MOCK_PLATFORM_URL=mock_platforms_url)
    async with api_client(app) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


PUBLISH = f"/connector/v1/{SAMPLE_KEY}/publish"


# ------------------------------------------------------------------ rules resolution


async def test_bare_appendix_a_destination_publishes_with_rules_source_warning(api, mock_platforms_url):
    response = await api.post(PUBLISH, json=body(mock_platforms_url, "b1-bare", "b1-k1"), headers=VERBOSE)
    assert response.status_code == 200, response.text
    meta = response.json()["_meta"]
    assert meta["rules_source"] == "none_supplied"
    assert any("rules_source=none_supplied" in w for w in meta["warnings"])
    assert creates(mock_platforms_url, "b1-bare") == 1


async def test_bare_destination_still_gets_spec_checks(api, mock_platforms_url):
    response = await api.post(PUBLISH, json=body(mock_platforms_url, "b1-spec", "b1-k2", title="x" * 201))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DEST_SPEC_VIOLATION"


async def test_appendix_a_record_fields_are_enforced(api, mock_platforms_url):
    dest = {"frequency_cap": "1/P7D", "automation_allowed": True}
    first = await api.post(PUBLISH, json=body(mock_platforms_url, "b1-rec", "b1-r1", **dest), headers=VERBOSE)
    assert first.status_code == 200, first.text
    assert first.json()["_meta"]["rules_source"] == "destination_record"
    second = await api.post(PUBLISH, json=body(mock_platforms_url, "b1-rec", "b1-r2", title="Another", **dest))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DEST_RULE_FREQUENCY_CAP"


async def _catalogue_destination(app: Any, mock_url: str, tenant: str, rules: dict[str, Any]) -> str:
    from app.connectors.base import normalise_instance
    from app.db.tables import DestinationRow, RuleSetRow

    url = f"{mock_url}/{SAMPLE_KEY}/{tenant}"
    dest_key = f"{SAMPLE_KEY}|{normalise_instance(url)}|post"
    async with app.state.session_factory() as session, session.begin():
        session.add(DestinationRow(id=f"dest_{tenant}", destination_key=dest_key, connector_key=SAMPLE_KEY, name=tenant, url=url,
                                   normalized_url=url, platform_key=SAMPLE_KEY, surface="post", instance={"base_url": url}))
        await session.flush()
        session.add(RuleSetRow(destination_id=f"dest_{tenant}", version=1, rules=rules, status="confirmed"))
    return f"dest_{tenant}"


async def test_catalogue_rules_found_by_destination_key(api, mock_platforms_url):
    await _catalogue_destination(api.app, mock_platforms_url, "b1-cat", {"self_promo_allowed": False})
    promo = body(mock_platforms_url, "b1-cat", "b1-c1", text="Buy my course now at https://creator.example/course?utm_source=x")
    promo["rendition"]["is_promotional"] = True
    response = await api.post(PUBLISH, json=promo)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DEST_RULE_SELF_PROMO"
    assert creates(mock_platforms_url, "b1-cat") == 0


async def test_inline_rules_beat_catalogue_rules(api, mock_platforms_url):
    dest_id = await _catalogue_destination(api.app, mock_platforms_url, "b1-inl", {"self_promo_allowed": False})
    promo = body(mock_platforms_url, "b1-inl", "b1-i1", text="Buy my course now", destination_id=dest_id,
                 rules={"self_promo_allowed": True})
    promo["rendition"]["is_promotional"] = True
    response = await api.post(PUBLISH, json=promo, headers=VERBOSE)
    assert response.status_code == 200, response.text
    assert response.json()["_meta"]["rules_source"].startswith("inline")


# ------------------------------------------------------------------ gate.apply_decision + promo history


async def test_disclosure_is_inserted_not_refused(api, mock_platforms_url):
    ai = body(mock_platforms_url, "r6-disc", "r6-d1", rules={"disclosure_required": True})
    ai["rendition"]["ai_generated"] = True
    response = await api.post(PUBLISH, json=ai)
    assert response.status_code == 200, response.text
    posted = stored_posts("r6-disc")[0]["body"]
    assert posted != "A first post" and "A first post" in posted  # the disclosure line was added


async def test_self_promo_ratio_uses_stored_promo_flag(api, mock_platforms_url):
    rules = {"self_promo": "ratio(2)", "automation_allowed": True}
    plain = await api.post(PUBLISH, json=body(mock_platforms_url, "r6-ratio", "r6-p1", text="How do you all handle backups?", rules=rules))
    assert plain.status_code == 200, plain.text
    promo = body(mock_platforms_url, "r6-ratio", "r6-p2", text="Subscribe to my channel", rules=rules)
    promo["rendition"]["is_promotional"] = True
    second = await api.post(PUBLISH, json=promo)
    assert second.status_code == 200, second.text  # 1 promotional in the last 2 posts is allowed


# ------------------------------------------------------------------ invalid rules are a 422, not a 500


async def test_invalid_inline_rules_are_422_not_500(api, mock_platforms_url):
    bad = body(mock_platforms_url, "m3", "m3-k1", rules={"automation_allowed": True, "frequency_cap": "oops"})
    response = await api.post(PUBLISH, json=bad)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "REQUEST_VALIDATION_FAILED" and error["retryable"] is False
    assert "oops" not in response.text  # input values are never echoed
    assert creates(mock_platforms_url, "m3") == 0


async def test_invalid_record_field_is_422(api, mock_platforms_url):
    response = await api.post(PUBLISH, json=body(mock_platforms_url, "m3b", "m3-k2", frequency_cap="every tuesday"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


# ------------------------------------------------------------------ Appendix A arguments only


async def test_status_delete_metrics_with_only_appendix_a_arguments(api, mock_platforms_url):
    published = await api.post(PUBLISH, json=body(mock_platforms_url, "b2", "b2-k1"))
    assert published.status_code == 200, published.text
    ref = {"platform_post_id": published.json()["platform_post_id"], "connection": CONNECTION}
    status = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status", json=ref)
    assert status.status_code == 200, status.text
    assert status.json() == {"status": "published", "live_url": published.json()["live_url"]}
    metrics = await api.post(f"/connector/v1/{SAMPLE_KEY}/fetch_metrics", json={**ref, "since": None})
    assert metrics.status_code == 200, metrics.text
    deleted = await api.post(f"/connector/v1/{SAMPLE_KEY}/delete", json=ref)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True}


async def test_instance_from_connection_extra_field(api, mock_platforms_url):
    published = await api.post(PUBLISH, json=body(mock_platforms_url, "b2c", "b2-k2"))
    conn = {**CONNECTION, "base_url": f"{mock_platforms_url}/{SAMPLE_KEY}/b2c"}
    status = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status",
                            json={"platform_post_id": published.json()["platform_post_id"], "connection": conn})
    assert status.status_code == 200, status.text


async def test_unknown_post_without_instance_is_a_clear_422(api):
    response = await api.post(f"/connector/v1/{SAMPLE_KEY}/get_status", json={"platform_post_id": "999", "connection": CONNECTION})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "REQUEST_VALIDATION_FAILED"
    assert "base_url" in error["message"] and "platform_post_id" in error["message"]


# ------------------------------------------------------------------ a 5xx after the create is UNKNOWN


async def test_5xx_after_create_reconciles_instead_of_posting_twice(api, mock_platforms_url, fake_clock):
    request = body(mock_platforms_url, "m4", "m4-k1")
    first = await api.post(PUBLISH, json=request, headers={"X-Sim-Fault": "create_then_502"})
    assert first.status_code == 503, first.text
    assert first.json()["error"]["details"]["intent_state"] == "UNKNOWN"
    assert len(stored_posts("m4")) == 1  # the platform did create it
    fake_clock.advance(10)
    retry = await api.post(PUBLISH, json=request, headers=VERBOSE)
    assert retry.status_code == 200, retry.text
    assert retry.json()["_meta"]["reconciled"] is True
    assert creates(mock_platforms_url, "m4") == 1


# ------------------------------------------------------------------ unmapped 4xx is a content rejection


async def test_unmapped_4xx_is_rejected_and_does_not_open_the_breaker(api, mock_platforms_url):
    for i in range(6):
        response = await api.post(PUBLISH, json=body(mock_platforms_url, "m5", f"m5-{i}"), headers={"X-Sim-Fault": "400"})
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "PLATFORM_CONTENT_REJECTED" and error["retryable"] is False
        assert "circuit" not in error["details"]
    ok = await api.post(PUBLISH, json=body(mock_platforms_url, "m5", "m5-ok"))
    assert ok.status_code == 200, ok.text  # the breaker never opened


# ------------------------------------------------------------------ no pool exhaustion under load


@pytest.fixture
async def small_pool_api(app_factory, pg_url, mock_platforms_url, sample_registry):
    app = await app_factory(pg_url=pg_url, registry=sample_registry, MOCK_PLATFORM_URL=mock_platforms_url, DB_POOL_SIZE=5)
    async with api_client(app) as client:
        yield client


async def test_forty_parallel_publishes_never_500(small_pool_api, mock_platforms_url):
    requests = [small_pool_api.post(PUBLISH, json=body(mock_platforms_url, "m6", f"m6-{i}", title=f"Post {i}")) for i in range(40)]
    responses = await asyncio.gather(*requests)
    statuses = [r.status_code for r in responses]
    assert statuses == [200] * 40, [r.text for r in responses if r.status_code != 200][:3]
    assert creates(mock_platforms_url, "m6") == 40


async def test_concurrent_cap_still_lets_only_one_through(small_pool_api, mock_platforms_url):
    dest = {"frequency_cap": "1/P7D", "automation_allowed": True}
    requests = [small_pool_api.post(PUBLISH, json=body(mock_platforms_url, "m6cap", f"m6c-{i}", title=f"P{i}", **dest)) for i in range(8)]
    statuses = sorted(r.status_code for r in await asyncio.gather(*requests))
    assert statuses == [200] + [409] * 7
    assert creates(mock_platforms_url, "m6cap") == 1


# ------------------------------------------------------------------ one key on the /api/v1 mirror


async def test_mirror_publish_without_key_echoes_one_key_that_replays(api, mock_platforms_url):
    request = body(mock_platforms_url, "m7", "unused")
    request.pop("idempotency_key")
    first = await api.post(f"/api/v1/connector/{SAMPLE_KEY}/publish", json=request)
    assert first.status_code == 200, first.text
    keys = first.headers.get_list("Idempotency-Key")
    assert len(keys) == 1
    assert len(first.headers.get_list("X-IncuBrix-Warning")) == 1
    retry = await api.post(f"/api/v1/connector/{SAMPLE_KEY}/publish", json=request, headers={"Idempotency-Key": keys[0]})
    assert retry.status_code == 200
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert creates(mock_platforms_url, "m7") == 1


# ------------------------------------------------------------------ the canary never reaches the logs


async def test_inline_credential_never_reaches_the_logs(api, mock_platforms_url, capsys):
    canary = "canary-LOG-5b7e2a91c4"
    for i, fault in enumerate(("500", "401", "422", "rename_field:id->post_id", None)):
        request = body(mock_platforms_url, "m15", f"m15-{i}")
        request["connection"] = {**CONNECTION, "credentials": {"token": canary}}
        headers = {"X-Sim-Fault": fault} if fault else {}
        await api.post(PUBLISH, json=request, headers=headers)
    bad_rules = body(mock_platforms_url, "m15", "m15-r", rules={"frequency_cap": canary})
    await api.post(PUBLISH, json=bad_rules)
    captured = capsys.readouterr()
    assert captured.out or captured.err  # the pipeline did log
    assert canary not in captured.out and canary not in captured.err


# ------------------------------------------------------------------ one SSRF guard on every fetch


async def test_w2_m4_media_url_guard_applies_outside_prod(api):
    for url in ("http://toxiproxy:8474/proxies", "http://169.254.169.254/latest/meta-data", "http://10.9.8.7/x.png"):
        response = await api.post("/api/v1/media", json={"url": url})
        assert response.status_code == 422, (url, response.text)
        assert "proxies" not in response.text


async def test_w2_m4_assisted_pack_cannot_read_internal_services(contract_client):
    request = {
        "rendition": {"title": "A pack", "body": "Some text for the pack.", "media": [{"url": "http://toxiproxy:8474/proxies", "mime": "image/png"}]},
        "destination": {"connector_key": "assisted", "instance": {"submit_url": "https://news.example.org/submit"}},
    }
    response = await contract_client.post("/connector/v1/assisted/build_assist_pack", json=request)
    assert response.status_code == 422, response.text


async def test_w2_m4_own_file_links_are_verified_and_limited_to_media(api):
    from app.storage.links import signed_download_url

    app = api.app
    await app.state.storage.put_bytes("assist/secret/pack.json", b"{}", "application/json")
    await app.state.storage.put_bytes("media/abc", b"\x89PNG\r\n\x1a\n" + b"0" * 20, "image/png")
    forged = signed_download_url("media/abc", app.state.settings).replace("sig=", "sig=0")
    not_media = signed_download_url("assist/secret/pack.json", app.state.settings)
    good = signed_download_url("media/abc", app.state.settings)
    assert (await api.post("/api/v1/media", json={"url": forged})).status_code == 403
    assert (await api.post("/api/v1/media", json={"url": not_media})).status_code == 422
    stored = await api.post("/api/v1/media", json={"url": good})
    assert stored.status_code == 201, stored.text
    assert stored.json()["mime"] == "image/png"
