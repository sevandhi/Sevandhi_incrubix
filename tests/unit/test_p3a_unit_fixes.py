"""Unit regression tests (no database): redaction, Ghost errors, the secret registry, signed callbacks, trusted
proxies, heartbeat path, runtime data in the image, inline HTML, plus the loopback rule for URL fetches. Each test was written before its fix and seen failing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.errors import ErrorCode, IbxError
from tests.conftest import ROOT, TEST_TOKEN

GHOST_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "recorded" / "ghost"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"APP_ENV": "test", "API_TOKEN": TEST_TOKEN, "MOCK_PLATFORM_URL": "http://mock-platforms:8080"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ------------------------------------------------------------------ the redactor keeps the word "ghost"


@pytest.mark.parametrize("text", [
    "ghost answered HTTP 401: Unknown Admin API Key used to access the API.",
    "Check the Admin API key (Ghost tokens are valid for 5 minutes from their 'iat').",
    "the token expired yesterday",
    "Basic auth failed",
])
def test_redactor_leaves_plain_words_after_ghost_or_token(text):
    from app.reliability.redact import redact_text

    assert redact_text(text) == text


@pytest.mark.parametrize(("text", "leaked"), [
    ("Authorization: Ghost abcdefgh", "abcdefgh"),  # header context: any value is masked
    ('{"authorization": "Ghost zzzzzzzzzz"}', "zzzzzzzzzz"),
    ("Ghost 64fa9c0e3b:2f1a9e7c", "64fa9c0e3b:2f1a9e7c"),  # token-shaped value
    ("token sk_live_51HxYzAbCdEf0123456789", "sk_live_51HxYzAbCdEf0123456789"),
    ("Bearer zzzzzzzzzzzz", "zzzzzzzzzzzz"),
])
def test_redactor_still_masks_real_credentials(text, leaked):
    from app.reliability.redact import redact_text

    assert leaked not in redact_text(text)


# ------------------------------------------------------------------ Ghost 422 keeps errors[0].context


def test_ghost_422_message_keeps_the_context(make_settings):
    from app.clock import FakeClock
    from app.connectors.base import ConnectorContext
    from app.contract.registry import discover
    from app.logging import get_logger

    registry = discover()
    ctx = ConnectorContext(manifest=registry.manifest("ghost"), settings=make_settings(), http=httpx.AsyncClient(),
                           clock=FakeClock(), log=get_logger("t"), db_session_factory=None)
    ghost = registry.classes["ghost"](ctx)
    data = json.loads((GHOST_FIXTURES / "create_post_title_too_long.json").read_text())["response"]
    err = ghost.map_error(httpx.Response(422, json=data["json"]))
    assert err.code == ErrorCode.PLATFORM_CONTENT_REJECTED
    assert "exceeds maximum length of 255 characters" in err.message
    assert "exceeds maximum length of 255 characters" in err.details["platform_message"]
    assert err.details["platform_context"].startswith("Value in [posts.title]")


# ------------------------------------------------------------------ the secret registry is bounded


def test_secret_registry_is_bounded_and_keeps_pinned_and_recent_values():
    from app.reliability.redact import SecretRegistry

    reg = SecretRegistry(max_values=50)
    reg.add("PINNED-SETTINGS-SECRET", pinned=True)
    for n in range(500):
        reg.add(f"inline-credential-{n:04d}")
    assert len(reg) <= 51
    assert reg.scrub("x PINNED-SETTINGS-SECRET y") == "x *** y"
    assert reg.scrub("inline-credential-0499") == "***"  # the newest are kept
    assert reg.scrub("inline-credential-0000") == "inline-credential-0000"  # the oldest were evicted
    reg.add("inline-credential-0460")  # re-use refreshes a value
    for n in range(500, 540):
        reg.add(f"inline-credential-{n:04d}")
    assert reg.scrub("inline-credential-0460") == "***"


# ------------------------------------------------------------------ callbacks are signed with a timestamp


def test_callback_signature_covers_a_timestamp_and_has_a_replay_window():
    from app.jobs import callbacks

    body = callbacks.encode({"job_id": "job_1", "status": "succeeded"})
    signature = callbacks.sign(b"secret", body, timestamp=1_700_000_000)
    assert signature.startswith("t=1700000000,v1=")
    assert callbacks.verify(b"secret", body, signature, now=1_700_000_100)
    assert not callbacks.verify(b"secret", body, signature, now=1_700_000_301)  # replayed after 5 minutes
    assert not callbacks.verify(b"secret", body, signature.replace("t=1700000000", "t=1700000200"), now=1_700_000_200)
    assert not callbacks.verify(b"secret", body + b" ", signature, now=1_700_000_000)
    assert not callbacks.verify(b"other", body, signature, now=1_700_000_000)
    assert not callbacks.verify(b"secret", body, None)
    assert not callbacks.verify(b"secret", body, "sha256=" + "0" * 64)  # the old, replayable form is refused


async def test_callback_delivery_sends_the_timestamp_header():
    from app.jobs import callbacks

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(headers=request.headers, body=request.content)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await callbacks.deliver(client, "https://cb.example/x", {"job_id": "j"}, b"k") == "delivered"
    ts = seen["headers"][callbacks.TIMESTAMP_HEADER]
    assert seen["headers"][callbacks.SIGNATURE_HEADER].startswith(f"t={ts},v1=")
    assert callbacks.verify(b"k", seen["body"], seen["headers"][callbacks.SIGNATURE_HEADER])


# ------------------------------------------------------------------ X-Forwarded-For from trusted proxies only


def _scope(client: str, xff: str | None) -> dict[str, Any]:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return {"client": (client, 5555), "headers": headers}


def test_client_ip_uses_x_forwarded_for_only_behind_a_trusted_proxy():
    from app.api.security import client_ip

    trusted = settings(TRUSTED_PROXIES="10.0.0.0/8,172.18.0.5")
    assert client_ip(_scope("203.0.113.9", "198.51.100.1"), trusted) == "203.0.113.9"  # untrusted peer: header ignored
    assert client_ip(_scope("172.18.0.5", "198.51.100.1"), trusted) == "198.51.100.1"
    # the right-most address that is not one of our proxies is the client; a forged left part is ignored
    assert client_ip(_scope("10.1.1.1", "6.6.6.6, 198.51.100.7, 10.2.2.2"), trusted) == "198.51.100.7"
    assert client_ip(_scope("10.1.1.1", "not-an-ip"), trusted) == "10.1.1.1"
    assert client_ip(_scope("172.18.0.5", "198.51.100.1"), settings()) == "172.18.0.5"  # default: trust nobody


# ------------------------------------------------------------------ loopback fetches only in a local dev/test run


async def test_loopback_fetches_are_refused_in_demo_even_in_simulation():
    from app.reliability.safe_fetch import check_url

    demo_sim = settings(APP_ENV="demo", SIMULATION_MODE=True)
    for url in ("http://localhost:8000/api/v1/jobs", "http://127.0.0.1:8000/ready"):
        with pytest.raises(IbxError) as err:
            await check_url(url, demo_sim)
        assert err.value.code == ErrorCode.REQUEST_VALIDATION_FAILED
    await check_url("http://mock-platforms:8080/assist/x/media/a.png", demo_sim)  # our mocks are fine in simulation
    await check_url("http://127.0.0.1:9/x", settings(APP_ENV="dev"))  # a local dev/test run may use loopback


# ------------------------------------------------------------------ no hard-coded heartbeat path


def test_worker_heartbeat_path_is_a_setting():
    import inspect

    from app.jobs import worker

    assert settings(WORKER_HEARTBEAT_FILE="/run/ibx/hb").WORKER_HEARTBEAT_FILE == "/run/ibx/hb"
    assert "/tmp/" not in inspect.getsource(worker)  # the path comes from settings only
    healthcheck = (ROOT / "compose.yaml").read_text().split("  worker:", 1)[1].split("healthcheck:", 1)[1].split("\n\n", 1)[0]
    assert "WORKER_HEARTBEAT_FILE" in healthcheck and "/tmp/ibx-worker-heartbeat'" not in healthcheck


# ------------------------------------------------------------------ runtime data is in the image


def test_image_contains_the_catalogue_and_seed_files():
    ignored = {line.strip().rstrip("/") for line in (ROOT / ".dockerignore").read_text().splitlines()}
    dockerfile = (ROOT / "Dockerfile").read_text()
    for folder in ("catalogue", "seed"):
        assert folder not in ignored
        assert f"COPY {folder} ./{folder}" in dockerfile


# ------------------------------------------------------------------ stored HTML is never shown inline


@pytest.mark.parametrize(("ctype", "expected"), [
    ("image/png", "inline"), ("application/json", "inline"), ("text/markdown; charset=utf-8", "inline"),
    ("text/html", "attachment"), ("image/svg+xml", "attachment"), ("application/xhtml+xml", "attachment"), (None, "attachment"),
])
def test_files_route_sends_scriptable_types_as_attachments(ctype, expected):
    from app.api.v1.files import disposition

    assert disposition(ctype) == expected


# ------------------------------------------------------------------ the mock Ghost page shows the post (a screenshot is not blank)


def test_mock_ghost_public_page_shows_title_and_lexical_content():
    from fastapi.testclient import TestClient

    from app.connectors.ghost.connector import lexical_markdown
    from mocks.platforms.routes import ghost as mock_ghost

    mock_ghost._posts("shot")["p1"] = {"id": "p1", "slug": "hello-ibx", "status": "published", "title": "Hello IBX",
                                        "lexical": lexical_markdown("Body text ibx-marker"), "html": None}
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(mock_ghost.router, prefix="/ghost")
    page = TestClient(app).get("/ghost/shot/hello-ibx/")
    assert page.status_code == 200 and "<h1>Hello IBX</h1>" in page.text and "Body text ibx-marker" in page.text
    gone = TestClient(app).get("/ghost/shot/missing/")
    assert gone.status_code == 404 and "<body><h1>404</h1>" in gone.text
