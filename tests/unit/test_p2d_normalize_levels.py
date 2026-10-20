"""Unit tests: URL normalisation, catalogue row parsing, levels, MIME sniffing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.media import sniff_mime
from app.catalogue.models import DestinationIn
from app.catalogue.normalize import clean_instance_url, normalize_url
from app.certification.levels import level_for
from app.errors import IbxError


@pytest.mark.parametrize(("raw", "expected"), [
    ("HTTP://WWW.Forum.Example.org:443/c/AI/?utm_source=x&utm_medium=y#top", "https://forum.example.org/c/AI"),
    ("forum.example.org", "https://forum.example.org"),
    ("https://forum.example.org/latest?page=2&utm_campaign=z", "https://forum.example.org/latest?page=2"),
    ("http://mock-platforms:8080/discourse/forum-41/", "https://mock-platforms:8080/discourse/forum-41"),
    ("https://bücher.example/", "https://xn--bcher-kva.example"),
    ("https://forum.example.org//", "https://forum.example.org"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_clean_instance_url_keeps_scheme_and_port():
    assert clean_instance_url("http://Lemmy:8536/c/ibx_test/") == "http://lemmy:8536/c/ibx_test"
    assert clean_instance_url("https://www.x.org:443/a/") == "https://x.org/a"


@pytest.mark.parametrize("bad", ["", "ftp://x.org/file", "https://"])
def test_bad_urls_are_validation_errors(bad):
    with pytest.raises(IbxError):
        normalize_url(bad)


def test_catalogue_row_csv_spelling():
    row = DestinationIn.model_validate({"name": "Forum 41", "url": "https://f.org", "connector_key": "Discourse",
                                        "audience_tags": "ai;founders", "frequency_cap": "1/P7D", "self_promo_allowed": "false",
                                        "est_audience": "12,000", "instance_category_id": "5", "geo": "", "notes": "kept"})
    assert row.connector_key == "discourse" and row.audience_tags == ["ai", "founders"] and row.est_audience == 12000
    assert row.instance == {"category_id": "5"} and row.geo is None and row.model_extra == {"notes": "kept"}
    assert row.rules_dict() == {"frequency_cap": "1/P7D", "self_promo_allowed": "false"}
    assert DestinationIn(name="n", url="u", connector_key="x").rules_dict() is None


def _manifest(tier: str, caps: list[str]) -> SimpleNamespace:
    return SimpleNamespace(tier=tier, capabilities=caps)


def test_levels():
    native = _manifest("native", ["publish", "status", "delete", "metrics"])
    assert level_for(native, delete_mode="deleted", metrics_ok=True)[0] == "L4"
    assert level_for(native, delete_mode="deleted", metrics_ok=False)[0] == "L3"
    assert level_for(_manifest("protocol", ["publish", "status", "delete"]), delete_mode="deleted", metrics_ok=False)[0] == "L3"
    level, reason = level_for(native, delete_mode="unpublish_to_draft", metrics_ok=True)
    assert level == "L2" and "not claimed" in reason
    assert level_for(_manifest("feed", ["publish", "status", "delete"]), delete_mode="deleted", metrics_ok=False)[0] == "L2"
    assert level_for(_manifest("assisted", ["assist"]), delete_mode="pack_revoked", metrics_ok=False)[0] == "L1"


def test_sniff_mime():
    assert sniff_mime(b"\x89PNG\r\n\x1a\nxxxx") == "image/png"
    assert sniff_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_mime(b"\x00\x00\x00\x18ftypmp42") == "video/mp4"
    assert sniff_mime(b"<html>") is None
