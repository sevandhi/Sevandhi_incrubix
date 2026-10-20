"""Catalogue import end to end in process: API → job → worker → mock internet → DB → report."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.jobs.worker import Worker
from mocks.platforms.routes import internet
from tests.conftest import _ServerThread, api_client


@pytest.fixture(scope="module")
def internet_url() -> Any:
    from mocks.platforms.app import create_app as create_mock_app

    old = internet.LATENCY_SCALE, internet.HANG_S
    internet.LATENCY_SCALE, internet.HANG_S = 0.0, 6.0
    with _ServerThread(create_mock_app()) as server:
        yield f"http://127.0.0.1:{server.port}"
    internet.LATENCY_SCALE, internet.HANG_S = old


class Env:
    def __init__(self, app: Any, client: httpx.AsyncClient, worker: Worker) -> None:
        self.app, self.client, self.worker = app, client, worker

    async def run_import(self, files: Any = None, **kw: Any) -> dict[str, Any]:
        started = await self.client.post("/api/v1/catalogue/imports", files=files, **kw)
        assert started.status_code == 202, started.text
        while await self.worker.run_once() is not None:
            pass
        body = started.json()
        status = (await self.client.get(body["import_url"])).json()
        job = (await self.client.get(body["status_url"])).json()
        return {"started": body, "status": status, "job": job}


@pytest.fixture
async def env(app_factory: Any, pg_url: str, internet_url: str) -> AsyncIterator[Env]:
    app = await app_factory(pg_url=pg_url, MOCK_PLATFORM_URL=internet_url, PUBLIC_BASE_URL="http://ibx.test")
    http = httpx.AsyncClient()
    worker = Worker(settings=app.state.settings, session_factory=app.state.session_factory, storage=app.state.storage,
                    clock=app.state.clock, http=http, worker_id="p4a-test")
    async with api_client(app) as client:
        yield Env(app, client, worker)
    await http.aclose()


def _report(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


async def test_import_fixture_end_to_end(env: Env, internet_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.catalogue.importer as importer_mod

    original = importer_mod.Importer.__init__

    def short_timeout(self: Any, *a: Any, **kw: Any) -> None:
        kw["timeout_s"] = 3.0  # the in-thread mock shares the CPU with the test; the stack proof uses 5 s
        original(self, *a, **kw)

    monkeypatch.setattr(importer_mod.Importer, "__init__", short_timeout)
    text, expected = internet.generate_fixture(150, base_url=internet_url, run_tag="it1", seed=3)
    result = await env.run_import(files={"file": ("cat.csv", text.encode("utf-8-sig"), "text/csv")})
    assert result["job"]["status"] == "succeeded", result["job"]
    assert set(result["job"]["result"]["links"]) >= {"report", "summary", "manifest"}
    report = await env.client.get(result["started"]["report_url"])
    assert report.status_code == 200 and report.headers["content-type"].startswith("text/csv")
    rows = _report(report.text)
    assert len(rows) == 150
    status = Counter(r["status"] for r in rows)
    want = Counter(e["expected"] for e in expected)
    assert status["duplicate"] == want["duplicate"]
    assert status["invalid"] == want["invalid"]
    assert status["unreachable"] == want["unreachable"]
    assert status["ok"] + status["warning"] == want["imported"]
    by_row = {int(r["row_no"]): r for r in rows}
    for exp in expected:
        got = by_row[exp["row_no"]]
        if exp["expected"] != "imported":
            continue
        assert got["destination_id"], got
        assert 1 <= int(got["relevance_score"]) <= 5
        if exp["kind"] == "discourse":
            assert got["platform_key"] == "discourse" and int(got["est_audience"]) == exp["members"]
        if exp["kind"] == "lemmy":
            assert got["connector_key"] == "lemmy" and got["surface"] == "post" and int(got["est_audience"]) == exp["subscribers"]
        if exp["kind"] == "forem":
            assert got["connector_key"] == "devto"
        if exp["kind"] == "ghost":
            assert got["connector_key"] == "ghost"
        if exp["kind"] == "plain":
            assert got["connector_key"] == "assisted"
    counts = result["status"]["counts"]
    assert counts["imported"] == want["imported"]

    listed = (await env.client.get("/api/v1/destinations", params={"limit": 500})).json()
    assert listed["total"] == want["imported"]

    # the same file again: every importable row is now a duplicate of the database
    again = await env.run_import(files={"file": ("cat.csv", text.encode(), "text/csv")})
    summary = again["status"]["summary"]
    assert summary["ok"] + summary["warning"] == 0
    assert summary["codes"].get("DUPLICATE_IN_DB") == want["imported"]


async def test_detection_language_and_robots(env: Env, internet_url: str) -> None:
    base = f"{internet_url}/internet"
    text = (
        "name,url,platform_key,surface,language\n"
        f"Foro,{base}/discourse-es-ai-d1,,,\n"
        f"Gemeinschaft,{base}/lemmy-de-video-d9/c/videos,,community,\n"
        f"Tube,{base}/peertube-fr-creators-d3,,,\n"
        f"Blog,{base}/ghost-en-founders-d4,,,\n"
        f"Forem,{base}/forem-en-ai-d5/t/ai,,,\n"
        f"Plain,{base}/plain-ja-marketing-d6,,,\n"
        f"Hidden,{base}/norobots-en-ai-d7,discourse,,\n"
        f"Internal,http://10.0.0.5/admin,discourse,,\n"
        f"Wrong language,{base}/discourse-ja-saas-d8,discourse,,en\n"
    )
    assert not any(internet.site_profile(n).hangs for n in re.findall(r"/internet/([^/,]+)", text))
    result = await env.run_import(content=text.encode(), headers={"Content-Type": "text/csv"})
    rows = _report((await env.client.get(result["started"]["report_url"])).text)
    got = {r["name"]: r for r in rows}
    assert got["Foro"]["platform_key"] == "discourse" and got["Foro"]["language"] == "es"
    assert got["Gemeinschaft"]["platform_key"] == "lemmy" and got["Gemeinschaft"]["language"] == "de"
    assert got["Tube"]["platform_key"] == "peertube" and got["Tube"]["language"] == "fr"
    assert got["Blog"]["connector_key"] == "ghost"
    assert got["Forem"]["connector_key"] == "devto"
    assert got["Plain"]["connector_key"] == "assisted" and "PLATFORM_UNDETECTED" in got["Plain"]["code"]
    assert got["Plain"]["language"] == "ja"
    assert got["Hidden"]["status"] == "warning" and "ROBOTS_DISALLOWED" in got["Hidden"]["code"]
    assert got["Internal"]["status"] == "invalid" and got["Internal"]["code"] == "URL_NOT_ALLOWED"
    assert got["Wrong language"]["language"] == "en" and "LANGUAGE_MISMATCH" in got["Wrong language"]["code"]
    detail = (await env.client.get(f"/api/v1/destinations/{got['Foro']['destination_id']}")).json()
    assert detail["est_audience"] is not None and detail["relevance_score"] >= 3


async def test_dry_run_writes_nothing_and_csv_url_guard(env: Env, internet_url: str) -> None:
    text = f"name,url,platform_key\nA,{internet_url}/internet/discourse-en-ai-z2,discourse\n"
    result = await env.run_import(content=text.encode(), headers={"Content-Type": "text/csv"}, params={"dry_run": "true"})
    assert result["status"]["summary"]["would_import"] == 1
    assert (await env.client.get("/api/v1/destinations")).json()["total"] == 0

    refused = await env.client.post("/api/v1/catalogue/imports", json={"csv_url": "http://169.254.169.254/latest/x.csv"})
    assert refused.status_code == 422
    bad = await env.client.post("/api/v1/catalogue/imports", content=b"title,link\nx,y\n", headers={"Content-Type": "text/csv"})
    assert bad.status_code == 422


async def test_relevance_endpoint(env: Env) -> None:
    created = await env.client.post("/api/v1/destinations", json={"name": "Startup founders AI", "url": "https://x.example/f",
                                                                  "connector_key": "discourse", "category": "founders"})
    dest_id = created.json()["destination_id"]
    response = await env.client.post("/api/v1/catalogue/relevance", json={
        "rows": [{"name": "Chess club", "category": "games"}], "destination_ids": [dest_id, "dest_missing"]})
    body = response.json()["results"]
    assert body[0]["relevance_score"] == 1
    assert body[1]["stored"] is True and body[1]["relevance_score"] >= 4
    assert body[2]["error"] == "DESTINATION_NOT_FOUND"
    stored = (await env.client.get(f"/api/v1/destinations/{dest_id}")).json()
    assert stored["relevance_score"] == body[1]["relevance_score"]
