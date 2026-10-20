"""The hard-case seed file loads as catalogue rows (CSV through the API, and the DB loader)."""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings

SEED = Path(__file__).resolve().parents[2] / "seed" / "hard_case_destinations.csv"
NAMES = {"dest-weekly-cap-1", "dest-no-promo", "dest-ai-disclosure-es", "dest-partner-revocable", "dest-strict-limits"}


async def test_seed_csv_loads_through_the_api(p2d):
    response = await p2d.client.post("/api/v1/destinations", content=SEED.read_bytes(), headers={"Content-Type": "text/csv"})
    assert response.status_code == 201, response.text
    results = response.json()["results"]
    by_name = {r["destination"]["name"]: r["destination"] for r in results if r.get("destination")}
    errors = [r for r in results if r["status"] not in ("created", "duplicate")]
    # every row whose connector is registered loads; the others report CONNECTOR_NOT_FOUND (never silently dropped)
    assert all(e["code"] == "CONNECTOR_NOT_FOUND" for e in errors), errors
    assert len(by_name) + len(errors) == 5 and {"dest-weekly-cap-1", "dest-no-promo"} <= set(by_name)
    weekly = by_name["dest-weekly-cap-1"]
    assert weekly["frequency_cap"] == "1/P7D" and weekly["rules"]["status"] == "confirmed"
    assert by_name["dest-no-promo"]["self_promo_allowed"] is False
    assert by_name["dest-ai-disclosure-es"]["language"] == "es" and by_name["dest-ai-disclosure-es"]["disclosure_required"] is True
    assert by_name["dest-strict-limits"]["spec_overrides"] == {"title_max_chars": 40, "body_max_chars": 280}


async def test_seed_loader_db_mode(pg_url, monkeypatch):
    from app.catalogue import seed

    monkeypatch.setenv("DATABASE_URL", pg_url)
    get_settings.cache_clear()
    try:
        first = await seed.load_db(SEED)
        second = await seed.load_db(SEED)
    finally:
        get_settings.cache_clear()
    assert {r["name"] for r in first if r["status"] == "created"} <= NAMES
    assert all(r["status"] in ("duplicate", "error") for r in second)
    weekly = next(r for r in first if r["name"] == "dest-weekly-cap-1")
    assert not any("CONNECTION_REF_UNRESOLVED" in w for w in weekly["warnings"])  # mock connection created first
