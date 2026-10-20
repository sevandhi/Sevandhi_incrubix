"""OUT1 on a running compose stack (simulation mode). Skipped unless IBX_P5B_E2E_API_URL is set.

    COMPOSE_PROJECT_NAME=ibx_p5b SIMULATION_MODE=true APP_ENV=demo IBX_API_PORT=<free> IBX_MOCK_PORT=<free> \\
      IBX_BOOTSTRAP_OUT_UID=10001 IBX_BOOTSTRAP_OUT_GID=10001 docker compose up -d --build --wait
    uv run python -m app.catalogue.seed --api http://127.0.0.1:<port> --certify
    IBX_P5B_E2E_API_URL=http://127.0.0.1:<port> IBX_P5B_EVIDENCE_DIR=evidence/levels/out \\
      uv run pytest -q tests/integration/test_p5b_stack_e2e.py

Flow: the certified hard-case rows → one publication uses up the 1/P7D cap of dest-weekly-cap-1 → recommendations
for a normal item (capped row excluded with retry_at, the Spanish row excluded by language, trust > 0 for certified
rows, reasons present) → a promotional item (the no-promo row excluded) → content_id path → LLM phrasing (mock in the
stack) keeps the order. Writes ``OUT1_api_e2e.json`` when IBX_P5B_EVIDENCE_DIR is set.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

API = os.environ.get("IBX_P5B_E2E_API_URL")
TOKEN = os.environ.get("IBX_P5B_E2E_TOKEN", "ibx-dev-token")
pytestmark = pytest.mark.skipif(not API, reason="needs a running stack (IBX_P5B_E2E_API_URL)")

ITEM = {"title": "How small teams choose which AI tools to pay for",
        "body": "Three founders, twelve AI tools, one budget spreadsheet. What we kept, what we cancelled and why.",
        "tags": ["ai", "saas", "founders"], "language": "en", "is_promotional": False, "ai_generated": False}


def test_recommendations_on_the_stack():
    c = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)
    dests = {d["name"]: d for d in c.get("/api/v1/destinations", params={"limit": 200}).json()["destinations"]}
    cap = dests["dest-weekly-cap-1"]
    assert cap["level"] != "L0", "run `python -m app.catalogue.seed --api ... --certify` first"
    content = c.post("/api/v1/content", json=ITEM).json()
    pub = c.post("/api/v1/publications", json={"destination_id": cap["destination_id"], "content_id": content["content_id"]})
    assert pub.status_code == 202, pub.text
    time.sleep(1)

    r = c.post("/api/v1/recommendations", json={"content": ITEM, "k": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    excluded = {x["name"]: x for x in body["excluded"]}
    results = {x["name"]: x for x in body["results"]}
    assert excluded["dest-weekly-cap-1"]["code"] == "DEST_RULE_FREQUENCY_CAP" and excluded["dest-weekly-cap-1"]["retry_at"]
    assert excluded["dest-ai-disclosure-es"]["code"] == "DEST_RULE_LANGUAGE"
    assert "dest-strict-limits" in results and results["dest-strict-limits"]["factors"]["trust"] > 0
    assert all(x["reason"] for x in body["results"])

    promo = c.post("/api/v1/recommendations", json={"content": {**ITEM, "is_promotional": True}, "k": 10}).json()
    assert {x["name"]: x["code"] for x in promo["excluded"]}["dest-no-promo"] == "DEST_RULE_SELF_PROMO"

    by_id = c.post("/api/v1/recommendations", json={"content_id": content["content_id"], "k": 10}).json()
    phrased = c.post("/api/v1/recommendations", json={"content": ITEM, "k": 10, "phrase_with_llm": True}).json()
    assert [x["name"] for x in phrased["results"]] == [x["name"] for x in body["results"]]

    out = os.environ.get("IBX_P5B_EVIDENCE_DIR")
    if out:
        doc = {"api": API, "simulation": True, "publication_for_cap": {k: pub.json()["publication"].get(k) for k in ("publication_id", "state", "reason")},
               "normal_item": body, "promotional_item_excluded": promo["excluded"],
               "content_id_results": [x["name"] for x in by_id["results"]], "llm_phrasing": phrased["reason_phrasing"]}
        path = Path(out) / "OUT1_api_e2e.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
