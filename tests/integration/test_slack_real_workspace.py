"""Opt-in: the Slack connector against a REAL Slack workspace that YOU own (free plan is enough).

Runs only when both are set (otherwise skipped; it has never been run, because no token was available):
    SLACK_BOT_TOKEN     xoxb-... of an internal app with chat:write, channels:history, reactions:read
    SLACK_TEST_CHANNEL  the id of a private test channel the bot was invited to (never a community channel)

It posts one neutral message, checks status and metrics, deletes it and checks it is gone. The connector is
called in-process (no stack needed) with the token inline; the token is never printed.
"""

from __future__ import annotations

import os
import uuid

import pytest

TOKEN, CHANNEL = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_TEST_CHANNEL")
pytestmark = pytest.mark.skipif(not (TOKEN and CHANNEL), reason="SLACK_BOT_TOKEN / SLACK_TEST_CHANNEL not set (own workspace only)")


async def test_real_slack_roundtrip(app_factory, pg_url):
    from tests.conftest import api_client

    app = await app_factory(pg_url=pg_url, PLATFORM_TIMEOUT_S=15.0)
    conn = {"connector_key": "slack", "auth_type": "bot_token", "credentials": {"bot_token": TOKEN}}
    dest = {"connector_key": "slack", "rules": {"automation_allowed": True}, "instance": {"channel_id": CHANNEL}}
    body = {"rendition": {"title": "IncuBrix connector test", "body": "Neutral test message, deleted at once.", "ai_generated": False},
            "destination": dest, "connection": conn, "idempotency_key": f"slack-live-{uuid.uuid4().hex[:8]}"}
    async with api_client(app) as client:
        published = await client.post("/connector/v1/slack/publish", json=body)
        assert published.status_code == 200, published.json().get("error", {}).get("code")
        ref = {"platform_post_id": published.json()["platform_post_id"], "connection": conn, "destination": dest}
        assert (await client.post("/connector/v1/slack/get_status", json=ref)).json()["status"] == "published"
        assert (await client.post("/connector/v1/slack/fetch_metrics", json=ref)).status_code == 200
        assert (await client.post("/connector/v1/slack/delete", json=ref)).json() == {"deleted": True}
        assert (await client.post("/connector/v1/slack/get_status", json=ref)).json()["status"] == "deleted"
