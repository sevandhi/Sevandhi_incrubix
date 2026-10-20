"""Catalogue finish: seed_v2 transformation, the import file, and the OUT4 counting rule (no network, no Docker)."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pc = _load("p5c_catalogue")
prove = _load("prove_out4")


@pytest.fixture(scope="module")
def v2(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, str]]:
    src = ROOT / "catalogue" / "seeds" / "seed_v1.csv"
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path_factory.mktemp("v2") / "seed_v2.csv"
    rows = pc.build_seed_v2(src, out, None)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before  # seed_v1 is never changed
    return rows


def test_seed_v2_keeps_every_row(v2: list[dict[str, str]]) -> None:
    v1 = pc.read_csv(ROOT / "catalogue" / "seeds" / "seed_v1.csv")
    assert [r["url"] for r in v2] == [r["url"] for r in v1]


def test_slack_rows_become_assisted_l1(v2: list[dict[str, str]]) -> None:
    slack = [r for r in v2 if r["platform_key"] == "slack"]
    assert len(slack) == 10
    assert not [r for r in v2 if r["connector_key"] == "slack"]
    for row in slack:
        assert (row["connector_key"], row["surface"], row["automation_allowed"], row["level_ceiling"]) == \
            ("assisted", "community", "no", "L1")


def test_mrss_rows_get_unique_feed_keys(v2: list[dict[str, str]]) -> None:
    keys = [r["instance_feed_key"] for r in v2 if r["connector_key"] == "mrss"]
    assert len(keys) == 4 and len(set(keys)) == 4 and all(keys)
    assert all(not r["instance_feed_key"] for r in v2 if r["connector_key"] != "mrss")


def test_every_v2_rule_parses_and_free_text_is_kept(v2: list[dict[str, str]]) -> None:
    for row in v2:
        assert pc._parse_rules(row) is None, row["name"]
    tech = next(r for r in v2 if r["url"] == "https://lemmy.world/c/technology")
    assert tech["frequency_cap"] == "10/P1D" and tech["automation_allowed"] == "no"
    assert "approved bots: max 10 articles/day" in tech["rules_text"]
    assert sum(r["ai_content_banned"] == "yes" for r in v2) == 7
    assert all(r["disclosure_required"] != "prohibited" for r in v2)


def test_import_file_has_no_rule_columns(tmp_path: Path, v2: list[dict[str, str]]) -> None:
    src = tmp_path / "v2.csv"
    pc.write_csv(src, v2, list(v2[0].keys()))
    rows = pc.build_import_csv(src, tmp_path / "in.csv")
    header = next(csv.reader((tmp_path / "in.csv").open()))
    assert not set(pc.RULE_COLUMNS) & set(header)  # D6: the importer would store them as confirmed rules
    assert all(r["est_audience"] == "" for r in rows if r["connector_key"] in pc.ENRICHED_PLATFORMS)


def _row(**over: str) -> dict[str, str]:
    row = {"import_status": "ok", "http_status": "200", "import_codes": "", "rules_url": "https://x.example/rules",
           "rules_url_status": "200", "capture_status": "succeeded", "rules_source": "page", "fields_verified": "1",
           "proposal_status": "proposed", "proposal_id": "rprop_1", "confirmed_by": "pending-human"}
    row.update(over)
    return row


@pytest.mark.parametrize(("over", "eligible", "countable"), [
    ({}, True, False),                                                     # waits for a person (D6)
    ({"proposal_status": "confirmed", "confirmed_by": "alice"}, True, True),
    ({"proposal_status": "confirmed", "confirmed_by": "agent:catalogue"}, True, False),  # an agent never confirms
    ({"http_status": "403", "import_status": "unreachable"}, False, False),
    ({"rules_url_status": "404"}, False, False),
    ({"rules_source": "default", "fields_verified": "0"}, False, False),   # defaulted: listed, not counted
    ({"capture_status": "failed"}, False, False),
    ({"rules_url": ""}, False, False),
])
def test_strict_rule_builder_and_proof_agree(over: dict[str, str], eligible: bool, countable: bool) -> None:
    row = _row(**over)
    built = pc.classify(row)
    assert (built[0], built[1]) == (eligible, countable)
    assert prove.strict(row) == (eligible, countable)


def test_prove_detects_a_tampered_count(tmp_path: Path) -> None:
    columns = prove.APPENDIX_A + prove.OUT4 + [c for c in pc.STATUS_COLUMNS]
    row = {c: "" for c in columns}
    row.update(_row(), name="X", url="https://x.example/", destination_id="dest_1", relevance_score="4",
               relevance_reason="ai", level="L0", rules_status="pending-human", eligible_on_confirm="yes",
               reviewable_on_confirm="yes",
               countable_now="no", source_sha256s="page:" + "a" * 64)
    path = tmp_path / "c.csv"
    pc.write_csv(path, [row], columns)
    counts = {"seed_rows": 1, "imported": 1, "url_200": 1, "rows_with_rules_url": 1, "rules_url_200": 1, "proposals": 1,
              "proposals_pending_human": 1, "proposals_confirmed": 0, "defaulted": 0, "eligible_on_confirm": 1,
              "reviewable_on_confirm": 1,
              "countable_now": 0, "target": 150}
    report = tmp_path / "r.md"
    report.write_text("```json\n" + json.dumps(counts) + "\n```\n")
    args = ["--csv", str(path), "--report", str(report), "--run", str(tmp_path / "none.json")]
    assert prove.main(args) == 0
    assert prove.main([*args, "--require-met"]) == 1
    report.write_text("```json\n" + json.dumps({**counts, "countable_now": 150}) + "\n```\n")
    assert prove.main(args) == 1
