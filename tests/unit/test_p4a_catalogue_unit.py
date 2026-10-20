"""Catalogue import unit tests: CSV parsing, URL checks, surface aliases, dry-run import, relevance (lexicon + LLM ±1), page meta."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.catalogue import relevance as rel
from app.catalogue.detect import page_meta, platform_hint
from app.catalogue.enrich import detect_language, lemmy_community
from app.catalogue.importer import Importer, check_url_syntax, parse_csv, report_csv
from app.config import Settings
from app.contract.registry import get_registry
from app.errors import IbxError
from mocks.platforms.routes import internet

ROOT = Path(__file__).resolve().parents[2]


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, APP_ENV="test", **kw)


def _dry(text: str) -> dict:
    import asyncio

    return asyncio.run(Importer(_settings(), get_registry()).run(parse_csv(text.encode("utf-8-sig")), dry_run=True))


def test_parse_csv_bom_and_sniffed_delimiter() -> None:
    rows = parse_csv("﻿name;url;platform_key\nA;https://a.example/;discourse\n\n".encode())
    assert rows == [{"name": "A", "url": "https://a.example/", "platform_key": "discourse"}]
    rows = parse_csv(b"Name\tURL\nB\thttps://b.example\n")
    assert rows[0]["url"] == "https://b.example"


def test_parse_csv_needs_name_and_url() -> None:
    with pytest.raises(IbxError):
        parse_csv(b"title,link\nx,y\n")
    with pytest.raises(IbxError):
        parse_csv(b"   ")


@pytest.mark.parametrize("url", ["not a url", "ftp://x.example/a", "http://", "https://exa mple.com", "javascript:alert(1)",
                                 "mailto:a@b.example", "https://user:pw@x.example/", "//", "https://-bad-.example/"])
def test_bad_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        check_url_syntax(url)


def test_url_normalisation_for_dedupe() -> None:
    fetch, norm = check_url_syntax("HTTP://WWW.Forum.Example.org/c/AI/?utm_source=x#top")
    assert norm == "https://forum.example.org/c/AI"
    assert fetch.startswith("http://forum.example.org")


def test_dry_run_maps_surface_aliases_and_reports_unknown_surface() -> None:
    report = _dry(
        "name,url,platform_key,connector_key,surface\n"
        "L,https://lemmy.example/c/ai,lemmy,lemmy,community\n"
        "D,https://forum.example,discourse,,forum\n"
        "T,https://dev.to/t/ai,forem,devto,tag\n"
        "X,https://forum2.example,discourse,,carrier-pigeon\n"
        "S,https://slack.example,slack,slack,workspace\n"
    )
    rows = report["rows"]
    assert [r["surface"] for r in rows[:3]] == ["post", "topic", "article"]
    assert rows[3]["status"] == "invalid" and rows[3]["code"] == "SURFACE_UNKNOWN" and "carrier-pigeon" in rows[3]["message"]
    assert rows[4]["status"] == "invalid" and rows[4]["code"] == "INSTANCE_MISSING"


def test_dry_run_duplicates_invalid_rows_and_unparsed_rules() -> None:
    report = _dry(
        "name,url,platform_key,frequency_cap,self_promo_allowed\n"
        "A,https://forum.example/,discourse,1/P7D,no\n"
        "A again,https://WWW.forum.example?utm_source=nl#x,discourse,,\n"
        ",https://nameless.example,discourse,,\n"
        "B,https://b.example,discourse,max 2 a week,restricted\n"
        "C,http://169.254.169.254/latest,discourse,,\n"
    )
    rows = report["rows"]
    assert rows[0]["status"] == "ok"
    assert rows[1]["status"] == "duplicate" and rows[1]["duplicate_of_row"] == 1
    assert rows[2]["status"] == "invalid" and rows[2]["code"] == "ROW_INVALID"
    assert rows[3]["status"] == "warning" and "RULES_UNPARSED" in rows[3]["code"]
    # syntax is fine; the SSRF guard refuses it when the network stage runs (not in a dry run)
    assert report["summary"]["duplicate"] == 1
    assert report["summary"]["would_import"] == 3
    text = report_csv(report)
    assert text.splitlines()[0].startswith("row_no,status,code,message,destination_id")


def test_real_seed_catalogue_dry_run() -> None:
    report = _dry((ROOT / "catalogue" / "seeds" / "seed_v1.csv").read_text(encoding="utf-8"))
    summary = report["summary"]
    assert summary["rows"] == 175
    assert all(r["status"] != "invalid" or r["code"] in ("INSTANCE_MISSING",) for r in report["rows"])
    lemmy = [r for r in report["rows"] if r["connector_key"] == "lemmy"]
    assert lemmy and all(r["surface"] == "post" for r in lemmy)
    assert all(1 <= int(r["relevance_score"]) <= 5 and len(r["relevance_reason"]) <= 200
               for r in report["rows"] if r["status"] in ("ok", "warning"))


def test_lexicon_relevance_scores_and_reason() -> None:
    core = rel.lexicon_score(rel.RelevanceInput(name="AI Founders Hub", category="ai", audience_tags=["ai", "startups"]))
    assert core.score == 5 and "ai (name" in core.reason and len(core.reason) <= 200
    off = rel.lexicon_score(rel.RelevanceInput(name="Gardening club", category="hobby", audience_tags=["plants"]))
    assert off.score == 1
    adjacent = rel.lexicon_score(rel.RelevanceInput(name="Technology", category="tech", audience_tags=["news"]))
    assert adjacent.score == 2
    multilingual = rel.lexicon_score(rel.RelevanceInput(name="Comunidad de inteligencia artificial"))
    assert multilingual.score >= 3
    japanese = rel.lexicon_score(rel.RelevanceInput(name="人工知能コミュニティ"))
    assert japanese.score >= 3


async def test_llm_adjustment_needs_verified_evidence() -> None:
    item = rel.RelevanceInput(name="Acme Support Forum", category="ai", audience_tags=["ai"])

    async def good(prompt: str, schema: type) -> tuple[dict, dict]:
        assert "Acme Support Forum" in prompt
        return {"adjustment": -1, "reason": "Vendor support forum.", "evidence": ["Support Forum"]}, {"provider": "groq", "model": "m"}

    result = await rel.score(item, use_llm=True, llm=good)
    assert result.method == "lexicon+llm" and result.score == result.lexicon_score - 1 and result.llm["provider"] == "groq"

    async def invented(prompt: str, schema: type) -> tuple[dict, dict]:
        return {"adjustment": 1, "reason": "Huge.", "evidence": ["10 million members"]}, {"provider": "groq"}

    result = await rel.score(item, use_llm=True, llm=invented)
    assert result.score == result.lexicon_score and "dropped" in result.reason

    def mock(prompt: str, schema: type) -> tuple[dict, dict]:
        return {"adjustment": 1, "reason": "x", "evidence": []}, {"provider": "mock"}

    result = await rel.score(item, use_llm=True, llm=mock)
    assert result.method == "lexicon" and result.llm["used"] is False

    async def broken(prompt: str, schema: type) -> tuple[dict, dict]:
        raise TimeoutError

    result = await rel.score(item, use_llm=True, llm=broken)
    assert result.method == "lexicon" and "failed" in result.llm["why"]

    async def bad_json(prompt: str, schema: type) -> tuple[dict, dict]:
        return {"adjustment": 3, "reason": "x"}, {"provider": "groq"}

    result = await rel.score(item, use_llm=True, llm=bad_json)
    assert result.method == "lexicon" and "invalid" in result.llm["why"]


def test_page_meta_and_hints() -> None:
    site = internet.site_profile("discourse-de-ai-x1")
    meta = page_meta(internet._page(site, ""))
    assert meta.lang == "de" and meta.title and platform_hint(meta) == "discourse"
    assert platform_hint(page_meta(internet._page(internet.site_profile("ghost-en-ai-x1"), ""))) == "ghost"
    assert platform_hint(page_meta(internet._page(internet.site_profile("lemmy-en-ai-x1"), ""))) is None
    assert lemmy_community("https://lemmy.world/c/technology") == "technology"


@pytest.mark.parametrize("lang", ["es", "de", "fr", "ja", "zh", "ru", "pt", "it", "nl"])
def test_language_detection_on_mock_pages(lang: str) -> None:
    site = internet.site_profile(f"plain-{lang}-video-x1")
    code, _confidence, detector = detect_language(f"{site.title} {site.text}")
    assert code == lang, (detector, code)


def test_fixture_generator_mix() -> None:
    text, expected = internet.generate_fixture(1000, base_url="http://mock-platforms:8080", run_tag="t1", seed=7)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 1000
    kinds = {e["expected"] for e in expected}
    assert kinds == {"duplicate", "invalid", "unreachable", "imported"}
    dups = sum(e["expected"] == "duplicate" for e in expected)
    assert 70 <= dups <= 130
    langs = {e.get("lang") for e in expected if e.get("lang")}
    assert len(langs) >= 8
