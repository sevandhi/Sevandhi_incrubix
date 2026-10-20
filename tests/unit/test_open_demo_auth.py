"""API_TOKEN_REQUIRED: on by default (bearer token needed); off only for the open live demo, where the
per-IP rate limit is still enforced."""

from __future__ import annotations

from tests.conftest import api_client


async def test_token_required_by_default(app_factory, pg_url):
    app = await app_factory(pg_url=pg_url)
    async with api_client(app, token=None) as anon:
        response = await anon.get("/api/v1/connectors")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_open_demo_needs_no_token_but_keeps_the_rate_limit(app_factory, pg_url):
    app = await app_factory(pg_url=pg_url, API_TOKEN_REQUIRED=False, RATE_LIMIT_PER_MIN=3)
    async with api_client(app, token=None) as anon:
        statuses = [(await anon.get("/api/v1/connectors")).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]
