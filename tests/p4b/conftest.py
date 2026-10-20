"""Rules capture fixtures: the in-process environment (API + worker + mock platforms, simulation mode) and a small local
"rules site" on a real socket that serves our saved rules pages and a robots.txt."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from tests.conftest import _ServerThread
from tests.p2d.conftest import _p2d_mock_server, p2d, p2d_mock_url, p2d_registry  # noqa: F401 - re-exported fixtures

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "rules_pages"
HITS: list[str] = []


async def _robots(request) -> Response:  # noqa: ANN001
    HITS.append("/robots.txt")
    return PlainTextResponse("User-agent: *\nDisallow: /private/\n")


async def _page(request) -> Response:  # noqa: ANN001
    name = request.path_params["name"]
    HITS.append(f"/rules/{name}")
    path = PAGES / name
    if path.suffix != ".html" or not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"))


async def _private(request) -> Response:  # noqa: ANN001
    HITS.append("/private/")
    return HTMLResponse("<p>No bots allowed.</p>")


@pytest.fixture(scope="session")
def _rules_site() -> Iterator[_ServerThread]:
    app = Starlette(routes=[Route("/robots.txt", _robots), Route("/rules/{name}", _page), Route("/private/rules", _private)])
    with _ServerThread(app) as server:
        yield server


@pytest.fixture
def rules_site(_rules_site: _ServerThread, monkeypatch: pytest.MonkeyPatch) -> str:
    from app.catalogue import rules_capture
    from app.providers.llm import LLMSettings, config, reset_cooldowns

    monkeypatch.setattr(rules_capture, "MIN_INTERVAL_S", 0.0)  # politeness is tested on its own
    mock_only = LLMSettings(_env_file=None, LLM_PROVIDER="mock")
    monkeypatch.setattr(config, "get_llm_settings", lambda: mock_only)  # tests never call a real LLM
    reset_cooldowns()
    HITS.clear()
    return f"http://127.0.0.1:{_rules_site.port}"


@pytest.fixture
def site_hits(rules_site: str) -> list[str]:
    """Paths the rules site served during this test, in order."""
    return HITS
