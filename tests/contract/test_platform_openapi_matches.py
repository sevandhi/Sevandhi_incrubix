"""IS2: the committed openapi.yaml is OpenAPI 3.1.0 and equals what the running app serves."""

from __future__ import annotations

import sys

import httpx
import yaml

from app.config import Settings
from app.main import create_app
from tests.conftest import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
from export_openapi import OUT, build_spec  # noqa: E402


def test_committed_openapi_is_current():
    committed = yaml.safe_load(OUT.read_text(encoding="utf-8"))
    assert committed == build_spec(), "openapi.yaml is stale: run uv run python scripts/export_openapi.py"


def test_openapi_version_and_core_paths():
    spec = yaml.safe_load(OUT.read_text(encoding="utf-8"))
    assert spec["openapi"] == "3.1.0"
    for path in ("/connector/v1/{key}/publish", "/api/v1/connector/{key}/publish", "/connector/v1/{key}/manifest",
                 "/api/v1/jobs/{job_id}", "/api/v1/errors", "/health", "/ready"):
        assert path in spec["paths"], path
    assert "ErrorEnvelope" in spec["components"]["schemas"]


async def test_served_openapi_matches_committed():
    app = create_app(Settings(_env_file=None, SERVICE_VERSION="0.1.0"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        served = (await client.get("/openapi.json")).json()
    assert served == yaml.safe_load(OUT.read_text(encoding="utf-8"))
