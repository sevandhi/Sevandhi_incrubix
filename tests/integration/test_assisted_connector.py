"""Assisted connector end to end: pack files in object storage under prefix = marker, signed links, deep link
to the mock submit page (prefilled), every confirmed rule in the checklist, idempotent rebuild, purge."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.contract.router import get_connector
from tests.conftest import api_client

KEY = "assisted"
RULES = {"frequency_cap": "1/P7D", "self_promo_allowed": False, "link_policy": "no_shorteners", "disclosure_required": True,
         "language": "en", "automation_allowed": False, "blocked_terms": ["giveaway"], "rules_url": "https://forum.test/rules"}


def body(mock: str, tenant: str, *, rules: dict | None = RULES, media: list | None = None, **rendition: Any) -> dict[str, Any]:
    return {
        "rendition": {"title": "What tools do you use for release notes?",
                      "body": "We are comparing approaches and would love to hear yours.",
                      "link_url": "https://example.com/notes", "tags": ["release", "tools"], "ai_generated": False,
                      "media": media if media is not None else [{"url": f"{mock}/assist/{tenant}/media/diagram.png", "mime": "image/png"}],
                      **rendition},
        "destination": {"connector_key": KEY, "surface": "post", "instance": {"submit_url": f"{mock}/assist/{tenant}/submit"},
                        "rules": rules},
    }


@pytest.fixture
async def api(app_factory, pg_url, mock_platforms_url):
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=mock_platforms_url, MEDIA_FETCH_TIMEOUT_S=1.0)
    async with api_client(app) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


def objects(api) -> dict[str, tuple[bytes, str]]:
    return api.app.state.storage.objects


async def test_pack_files_caption_deep_link_and_checklist(api, mock_platforms_url):
    r = await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=body(mock_platforms_url, "site-1"),
                       headers={"X-IncuBrix-Verbose": "1"})
    assert r.status_code == 200, r.text
    pack = r.json()
    marker = pack["_meta"]["marker"]
    assert set(pack) - {"_meta"} == {"files", "caption", "deep_link", "checklist"}
    names = [f["name"] for f in pack["files"]]
    assert names == ["01-diagram.png", "title.txt", "caption.txt", "checklist.md"]
    for f in pack["files"]:
        assert f["url"].startswith(f"http://127.0.0.1:8088/api/v1/files/assist/{marker}/") and "sig=" in f["url"]
    stored = objects(api)
    assert stored[f"assist/{marker}/media/01-diagram.png"][0].startswith(b"\x89PNG")
    assert f"assist/{marker}/pack.json" in stored
    assert stored[f"assist/{marker}/caption.txt"][0].decode() == pack["caption"]
    assert "https://example.com/notes" in pack["caption"] and "#release #tools" in pack["caption"]

    page = httpx.get(pack["deep_link"])  # the submit page opens, prefilled
    assert page.status_code == 200 and "What tools do you use for release notes?" in page.text
    query = parse_qs(urlsplit(pack["deep_link"]).query)
    assert query["title"] == ["What tools do you use for release notes?"] and query["url"] == ["https://example.com/notes"]

    checklist = "\n".join(pack["checklist"])
    for rule_words in ("at most 1 time every 1 week", "no self-promotion", "no link shorteners", "must be disclosed",
                       "a person must post this by hand", "giveaway", "https://forum.test/rules", "Attach the file 01-diagram.png",
                       "/api/v1/assist-tasks/"):
        assert rule_words in checklist, rule_words
    checks = [item for item in pack["checklist"] if item.startswith("Check before posting:")]
    assert not any("automat" in item.lower() for item in checks)  # a person posts it: the automation rule is met


async def test_rebuild_is_idempotent_and_purge_removes_objects(api, mock_platforms_url):
    first = (await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=body(mock_platforms_url, "site-2"),
                            headers={"X-IncuBrix-Verbose": "1"})).json()
    keys_before = set(objects(api))
    second = (await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=body(mock_platforms_url, "site-2"),
                             headers={"X-IncuBrix-Verbose": "1"})).json()
    assert second["_meta"]["marker"] == first["_meta"]["marker"] and set(objects(api)) == keys_before
    connector = get_connector(SimpleNamespace(app=api.app), KEY)  # type: ignore[arg-type]
    removed = await connector.purge_pack(first["_meta"]["marker"])
    assert len(removed) == 5 and not any(k.startswith(f"assist/{first['_meta']['marker']}/") for k in objects(api))
    assert await connector.purge_pack(first["_meta"]["marker"]) == []


async def test_unconfirmed_rules_and_rule_warnings(api, mock_platforms_url):
    unconfirmed = await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=body(mock_platforms_url, "site-3", rules=None, media=[]))
    assert unconfirmed.status_code == 200
    assert any(item.startswith("Rules not confirmed") for item in unconfirmed.json()["checklist"])
    promo = body(mock_platforms_url, "site-3", media=[], body="Buy my course now! Subscribe to my channel today.",
                 is_promotional=True)
    refused = await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=promo)
    # a pack that breaks a rule is refused like a publish (it used to be a checklist warning)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "DEST_RULE_SELF_PROMO"
    neutral = await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=body(mock_platforms_url, "site-3", media=[]))
    assert neutral.status_code == 200
    checks = [i for i in neutral.json()["checklist"] if i.startswith("Check before posting:")]
    assert not any("automat" in w.lower() for w in checks)  # a person posts the pack: not a problem here


async def test_media_download_failures(api, mock_platforms_url):
    missing = body(mock_platforms_url, "site-4", media=[{"url": f"{mock_platforms_url}/assist/site-4/nope/x.png"}])
    r = await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=missing)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INPUT_FETCH_FAILED"
    hang = await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=body(mock_platforms_url, "site-4"),
                          headers={"X-Sim-Fault": "timeout_after_create:3"})
    assert hang.status_code == 422 and hang.json()["error"]["code"] == "INPUT_FETCH_FAILED" and hang.json()["error"]["retryable"] is True
    ok = await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=body(mock_platforms_url, "site-4"))
    assert ok.status_code == 200  # the retry builds the same pack


async def test_spec_overrides_apply_before_building(api, mock_platforms_url):
    too_long = body(mock_platforms_url, "site-5", media=[])
    too_long["destination"]["spec_overrides"] = {"title_max_chars": 10}
    r = await api.post(f"/connector/v1/{KEY}/build_assist_pack", json=too_long)
    assert r.status_code == 422 and r.json()["error"]["code"] == "DEST_SPEC_VIOLATION"
    assert not objects(api)
