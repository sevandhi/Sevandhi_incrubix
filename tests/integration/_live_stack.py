"""Helpers for the ``live_local`` tests: they drive the compose stack with the REAL Lemmy and Ghost.

The tests talk to the published API port only. Lemmy, Ghost and toxiproxy sit on the internal-only
``ibx_sandbox`` network, so anything the tests must do there (count posts on the platform, add a toxiproxy
latency toxic, change Lemmy's rate limit) runs as a short Python snippet inside the ``api`` container.

Environment (defaults match ``docker compose up``):
    IBX_LIVE_API_URL   http://127.0.0.1:${IBX_API_PORT:-8088}
    IBX_LIVE_API_TOKEN ${API_TOKEN:-ibx-dev-token}
    COMPOSE_PROJECT_NAME  the compose project to ``exec`` into (default ibx)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
API_URL = os.environ.get("IBX_LIVE_API_URL", f"http://127.0.0.1:{os.environ.get('IBX_API_PORT', '8088')}")
API_TOKEN = os.environ.get("IBX_LIVE_API_TOKEN", os.environ.get("API_TOKEN", "ibx-dev-token"))
PLATFORM_TIMEOUT_S = 10.0  # the stack's default PLATFORM_TIMEOUT_S


def require_stack(connector: str) -> None:
    try:
        ready = httpx.get(f"{API_URL}/ready", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        pytest.skip(f"compose stack not reachable at {API_URL}")
    if connector not in ready.get("checks", {}).get("connectors", {}).get("registered", []):
        pytest.skip(f"connector {connector} is not registered in the running stack (rebuild the image)")


def api_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API_URL, headers={"Authorization": f"Bearer {API_TOKEN}"}, timeout=60)


def in_stack(code: str) -> Any:
    """Run Python inside the api container; the snippet prints one JSON value, which is returned."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-c", code],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"in_stack failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def toxic(proxy: str, latency_ms: int | None) -> None:
    """Add a downstream latency toxic to a toxiproxy proxy, or remove it (``None``)."""
    if latency_ms is None:
        code = (f"import httpx,json; r=httpx.delete('http://toxiproxy:8474/proxies/{proxy}/toxics/ibx_latency');"
                "print(json.dumps(r.status_code))")
    else:
        body = {"name": "ibx_latency", "type": "latency", "stream": "downstream", "attributes": {"latency": latency_ms}}
        code = (f"import httpx,json; r=httpx.post('http://toxiproxy:8474/proxies/{proxy}/toxics', json={body!r});"
                "print(json.dumps(r.status_code))")
    status = in_stack(code)
    assert status in (200, 204, 404), status


def rendition(title: str, body: str = "Live test post from the IncuBrix connector suite.") -> dict[str, Any]:
    return {"title": title, "body": body, "content_type": "text", "ai_generated": False}
