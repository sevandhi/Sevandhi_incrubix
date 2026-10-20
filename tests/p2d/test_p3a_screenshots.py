"""The certification pack carries a screenshot. With ``SCREENSHOT_URL`` set (the shooter, compose
profile ``evidence``), the harness stores ``live.png`` after CONFIRM_LIVE and ``after_delete.png`` after the
removal is confirmed, plus ``screenshots.json`` saying what each picture is: a real page (``page``) or a
labelled render of an API response (``api_render``) where no web page exists. Without a shooter, or if it
fails, the run is unaffected and ``screenshots.json`` says why there is no picture. Never a fake picture.

A fake shooter (same HTTP contract as deploy/shooter/shooter.py) runs on a real socket here.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request, Response

from tests.conftest import _ServerThread

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
CALLS: list[dict[str, Any]] = []


def _fake_shooter() -> FastAPI:
    app = FastAPI()

    @app.post("/shoot")
    async def shoot(request: Request) -> Response:
        body = await request.json()
        CALLS.append({"path": "/shoot", **body})
        return Response(PNG, media_type="image/png", headers={"X-Shot-Kind": "page", "X-Shot-Status": "200",
                                                              "X-Shot-Content-Type": "text/html", "X-Shot-Final-Url": body["url"]})

    @app.post("/render")
    async def render(request: Request) -> Response:
        body = await request.json()
        CALLS.append({"path": "/render", **body})
        return Response(PNG, media_type="image/png", headers={"X-Shot-Kind": "api_render"})

    return app


@pytest.fixture(scope="module")
def shooter_url():
    with _ServerThread(_fake_shooter()) as server:
        yield f"http://127.0.0.1:{server.port}"


@pytest.fixture
def with_shooter(p2d, shooter_url):
    settings = p2d.app.state.settings
    settings.SCREENSHOT_URL = shooter_url
    CALLS.clear()
    yield p2d
    settings.SCREENSHOT_URL = None


async def _shots(env, run: dict) -> dict:
    key = next(k for k in env.storage.objects if k.endswith(f"{run['run_id']}/screenshots.json"))
    return json.loads((await env.storage.get_bytes(key)).decode())


async def test_page_screenshots_are_stored_for_a_web_visible_destination(with_shooter):
    env = with_shooter
    dest = await env.add(name="Shot Blog", url="https://shot-blog.example.org", connector_key="ghost")
    run = await env.certify(dest["destination_id"])
    assert run["state"] == "PASSED", run["steps"]
    assert {"live.png", "after_delete.png", "screenshots.json"} <= set(run["evidence"])
    shots = await _shots(env, run)
    assert shots["live"]["kind"] == "page" and shots["after_delete"]["kind"] == "page"
    assert shots["live"]["url"] == run["result"]["live_url"] and shots["live"]["taken"] is True
    assert [c["path"] for c in CALLS] == ["/shoot", "/shoot"]
    key = next(k for k in env.storage.objects if k.endswith(f"{run['run_id']}/live.png"))
    assert (await env.storage.get_bytes(key)) == PNG


async def test_lemmy_without_a_post_page_gets_a_labelled_api_render(with_shooter):
    env = with_shooter
    dest = await env.add(name="Shot Lemmy", url="https://shot-lemmy.example.org/c/ibx_test", connector_key="lemmy")
    run = await env.certify(dest["destination_id"])
    assert run["state"] == "PASSED", run["steps"]
    shots = await _shots(env, run)
    assert shots["live"]["kind"] == "api_render" and "not a screenshot of a web page" in shots["live"]["label"]
    assert {c["path"] for c in CALLS} == {"/render"}
    assert CALLS[0]["data"]["status"] == "published"


async def test_assisted_pack_page_and_revocation_render(with_shooter):
    env = with_shooter
    dest = await env.add(name="Shot Assist", url=f"{env.mock}/assist/shot/submit", connector_key="assisted")
    run = await env.certify(dest["destination_id"])
    assert run["state"] == "PASSED", run["steps"]
    shots = await _shots(env, run)
    assert shots["live"]["kind"] == "page" and "/assist/shot/submit" in shots["live"]["url"]
    assert shots["after_delete"]["kind"] == "api_render"


async def test_without_a_shooter_the_pack_says_so_and_the_run_passes(p2d):
    dest = await p2d.add(name="No Shooter", url="https://no-shooter.example.org", connector_key="ghost")
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED"
    assert "live.png" not in run["evidence"]
    shots = await _shots(p2d, run)
    assert shots["live"]["taken"] is False and "SCREENSHOT_URL" in shots["live"]["why"]


async def test_a_broken_shooter_never_fails_certification(p2d):
    p2d.app.state.settings.SCREENSHOT_URL = "http://127.0.0.1:9"
    try:
        dest = await p2d.add(name="Broken Shooter", url="https://broken-shooter.example.org", connector_key="ghost")
        run = await p2d.certify(dest["destination_id"])
    finally:
        p2d.app.state.settings.SCREENSHOT_URL = None
    assert run["state"] == "PASSED"
    shots = await _shots(p2d, run)
    assert shots["live"]["taken"] is False and shots["live"]["error"]
