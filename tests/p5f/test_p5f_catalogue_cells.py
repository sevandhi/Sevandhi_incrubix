"""Found in a proof run: (a) a catalogue cell ``link_policy: none`` means "no links allowed" (it was read as a blank
cell and became ``any``); (b) ``POST /api/v1/destinations`` refuses a malformed URL with 422 and a field-level reason."""

from __future__ import annotations

import pytest

from app.catalogue.models import DestinationIn

FORUM = {"connector_key": "fakeforum", "category": "saas", "language": "en"}


@pytest.mark.parametrize("cell", ["none", "None", "NONE"])
def test_link_policy_none_is_a_value_not_a_blank_cell(cell):
    row = DestinationIn.model_validate({"name": "x", "url": "https://x.example.org/", "connector_key": "discourse",
                                        "link_policy": cell, "frequency_cap": ""})
    assert row.link_policy == cell and row.frequency_cap is None  # other blank spellings stay blank
    assert row.rules_dict()["link_policy"] == cell


async def test_link_policy_none_is_stored_as_no_links(p2d):
    dest = await p2d.add(name="No Links Forum", url="https://no-links.example.org/", **FORUM, link_policy="none",
                         frequency_cap="1/P7D")
    assert dest["link_policy"] == "none" and dest["rules"]["rules"]["link_policy"] == "none"
    csv_body = ("name,url,connector_key,link_policy,frequency_cap\n"
                "No Links Csv,https://no-links-csv.example.org/,fakeforum,none,1/P7D\n")
    response = await p2d.client.post("/api/v1/destinations", content=csv_body.encode(), headers={"Content-Type": "text/csv"})
    assert response.status_code == 201, response.text
    assert response.json()["results"][0]["destination"]["link_policy"] == "none"


@pytest.mark.parametrize("url", ["not a url", "http://exa mple.org/", "ftp://example.org/file", "https://",
                                 "http://user:secret@example.org/", "http://example.org:99999/", "http://-bad-.example/"])
async def test_malformed_url_is_a_422_with_a_field_reason(p2d, url):
    response = await p2d.client.post("/api/v1/destinations", json={"name": "Bad Url", "url": url, **FORUM})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "REQUEST_VALIDATION_FAILED"
    assert error["details"]["errors"][0]["loc"] == ["url"] and error["details"]["errors"][0]["msg"]
    assert "secret" not in response.text  # never echo credentials


async def test_malformed_url_in_a_batch_is_reported_per_row(p2d):
    response = await p2d.client.post("/api/v1/destinations", json={"rows": [
        {"name": "Good", "url": "https://good-row.example.org/", **FORUM},
        {"name": "Bad", "url": "not a url", **FORUM}]})
    assert response.status_code == 201, response.text
    results = response.json()["results"]
    assert results[0]["status"] == "created"
    assert results[1]["status"] == "invalid" and results[1]["code"] == "REQUEST_VALIDATION_FAILED"
