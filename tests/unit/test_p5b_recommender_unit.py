"""OUT1 unit tests: text features, hard filters, factor scores, reasons, LLM phrasing guard, evaluation and the
labelling CLI. No Docker, no keys."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.recommender import data
from app.recommender import eval as ev
from app.recommender.profiles import ContentProfile, DestProfile
from app.recommender.ranker import DEFAULT_WEIGHTS, PastResults, Ranker, past_results_score
from app.recommender.reasons import _faithful, phrase_reasons
from app.recommender.text import Bm25Index, TfidfIndex, tokenize
from app.rules.models import FrequencyCap, PublishEvent
from app.tools import label as cli

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def dest(name: str, **kw) -> DestProfile:
    base = {"ref": f"https://x.example/{name.lower().replace(' ', '-')}", "name": name, "url": "https://x.example/",
            "connector_key": "lemmy", "surface": "community", "languages": ["en"], "rules_text": "be nice"}
    return DestProfile(**{**base, **kw})


def content(**kw) -> ContentProfile:
    base = {"title": "Editing YouTube videos faster", "body": "Our video editing workflow for creators.",
            "tags": ["video", "editing"], "language": "en", "is_promotional": False, "ai_generated": False}
    return ContentProfile.from_content({**base, **kw})


# ------------------------------------------------------------------ text


def test_tokenize_strips_urls_handles_stopwords_and_stems():
    toks = tokenize("The Videos at !videos@lemmy.world https://a.b/c are EDITING launches 人工知能")
    assert toks == ["video", "edit", "launch", "人工", "工知", "知能"]


def test_tfidf_cosine_and_bm25_prefer_the_matching_doc():
    docs = [tokenize("video editing community"), tokenize("startup founders"), tokenize("baking bread")]
    idx = TfidfIndex(docs)
    q = idx.vector(tokenize("video editing tips"))
    sims = [TfidfIndex.cosine(q, d) for d in idx.doc_vectors]
    assert sims[0] > 0 and sims[1] == sims[2] == 0
    assert Bm25Index(docs).scores(tokenize("founders"))[1] > 0


# ------------------------------------------------------------------ hard filters


@pytest.mark.parametrize(("dkw", "ckw", "code"), [
    ({"status": "excluded", "excluded_reason": "demoted"}, {}, "DESTINATION_EXCLUDED"),
    ({"surface": "ping-api", "connector_key": "webhook"}, {}, "FORMAT_NOT_AN_AUDIENCE"),
    ({"connector_key": "peertube", "surface": "instance"}, {}, "FORMAT_NEEDS_VIDEO"),
    ({"connector_key": "assisted", "surface": "directory"}, {}, "FORMAT_NEEDS_PRODUCT"),
    ({"self_promo": "disallowed"}, {"is_promotional": True}, "DEST_RULE_SELF_PROMO"),
    ({"ai_content": "prohibited"}, {"ai_generated": True}, "DEST_RULE_AI_CONTENT"),
    ({"languages": ["de"]}, {}, "DEST_RULE_LANGUAGE"),
])
def test_each_hard_filter_excludes_with_its_code(dkw, ckw, code):
    ranker = Ranker([dest("Video Editing", **dkw), dest("Other")])
    top, excluded = ranker.rank(content(**ckw), k=5)
    assert [s.filter.code for s in excluded] == [code]
    assert excluded[0].filter.reason
    assert [s.dest.name for s in top] == ["Other"]


def test_cta_word_is_promotional_for_rules_but_not_a_product_listing():
    c = content(body="We also tested an annual discount.")
    assert c.is_promotional and not c.is_product_announcement
    _, excluded = Ranker([dest("Dir", connector_key="assisted", surface="launch")]).rank(c)
    assert excluded[0].filter.code == "FORMAT_NEEDS_PRODUCT"


def test_automation_is_assisted_delivery_unless_automated_only():
    d = dest("Bots need approval", automation="approval")
    top, _ = Ranker([d]).rank(content())
    assert top[0].filter.delivery == "assisted" and "assisted delivery" in top[0].reason
    top, excluded = Ranker([d]).rank(content(), automated_only=True)
    assert not top and excluded[0].filter.code == "DEST_RULE_AUTOMATION_NOT_ALLOWED"


def test_used_up_cap_is_excluded_with_retry_at_and_partial_use_lowers_headroom():
    d = dest("Weekly", frequency_cap=FrequencyCap.model_validate("2/P7D"), destination_id="dest_1")
    one = [PublishEvent(published_at=NOW - timedelta(days=2))]
    two = one + [PublishEvent(published_at=NOW - timedelta(days=1))]
    top, _ = Ranker([d]).rank(content(), history={"dest_1": one}, now=NOW)
    assert top[0].factors["rules_headroom"] == pytest.approx(0.75)
    top, excluded = Ranker([d]).rank(content(), history={"dest_1": two}, now=NOW)
    assert not top and excluded[0].filter.code == "DEST_RULE_FREQUENCY_CAP"
    assert excluded[0].filter.retry_at == NOW - timedelta(days=2) + timedelta(days=7)


def test_publishable_only_excludes_uncertified_and_unconfirmed():
    _, excluded = Ranker([dest("A"), dest("B", level="L3")]).rank(content(), publishable_only=True)
    assert [s.filter.code for s in excluded] == ["DESTINATION_NOT_CERTIFIED", "DEST_RULES_UNCONFIRMED"]


# ------------------------------------------------------------------ factors and ranking


def test_language_mismatch_scales_both_audience_factors():
    en, de = dest("Video Editing", languages=["en"]), dest("Video Editing DE", languages=["de", "en"])
    scored = {s.dest.name: s for s in Ranker([en, de]).factors(content(language="de"))}
    assert scored["Video Editing"].language_note and scored["Video Editing DE"].language_note is None
    assert scored["Video Editing"].factors["relevance"] == pytest.approx(scored["Video Editing DE"].factors["relevance"] * 0.2)


def test_past_results_are_smoothed_towards_the_network_mean():
    assert past_results_score(None, 5.0) == 0.5
    one_lucky = past_results_score(PastResults(posts=1, engagement_sum=50), 5.0)
    many_good = past_results_score(PastResults(posts=20, engagement_sum=400), 5.0)
    assert 0.5 < one_lucky < many_good < 1


def test_ranking_is_deterministic_and_reasons_name_the_evidence():
    ds = [dest(f"Video Editing {i}") for i in range(5)] + [dest("Baking"), dest("Startup founders")]
    a, _ = Ranker(ds).rank(content(), k=7)
    b, _ = Ranker(list(reversed(ds))).rank(content(), k=7)
    assert [s.dest.ref for s in a] == [s.dest.ref for s in b]
    assert a[0].dest.name.startswith("Video Editing")
    assert "audience fit" in a[0].reason.lower() and "video" in a[0].reason and len(a[0].reason) <= 300
    assert sum(a[0].contributions.values()) == pytest.approx(a[0].score)


# ------------------------------------------------------------------ LLM phrasing never re-ranks


async def test_phrasing_keeps_faithful_rewrites_and_drops_invented_numbers():
    results = [{"destination_ref": "a", "reason": "Audience fit 0.42 (terms video). Before posting: not certified yet."},
               {"destination_ref": "b", "reason": "Relevance 5/5 for IncuBrix's audience."}]

    async def fake_llm(prompt, schema, **kw):
        assert "0.42" in prompt and kw["mock"] is not None
        meta = type("M", (), {"__dict__": {"provider": "groq", "model": "m", "fallback_used": False}})()
        return schema.model_validate({"reasons": [
            {"destination_ref": "a", "reason": "Strong match on video topics; not certified yet."},
            {"destination_ref": "b", "reason": "Rated 5/5 and 10,000 members love it."}]}), meta

    record = await phrase_reasons(results, llm=fake_llm)
    assert record["rewritten"] == 1 and record["rejected_unfaithful"] == 1 and record["used"] is True
    assert results[0]["reason"].startswith("Strong match") and results[0]["reason_facts"].startswith("Audience fit")
    assert results[1]["reason"].startswith("Relevance 5/5")
    assert _faithful("score 0.4", "score 0.4") and not _faithful("score 0.4", "score 0.9")


async def test_phrasing_through_the_real_chain_falls_back_to_the_mock(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    from app.providers.llm import get_llm_settings

    get_llm_settings.cache_clear() if hasattr(get_llm_settings, "cache_clear") else None
    results = [{"destination_ref": "a", "reason": "Audience fit 0.42."}]
    record = await phrase_reasons(results)
    assert record["used"] is False and record.get("provider") == "mock"
    assert results[0]["reason"] == "Audience fit 0.42."


# ------------------------------------------------------------------ evaluation on the real files


@pytest.fixture(scope="module")
def ctx():
    return ev.build_context()


@pytest.fixture
def label_copy(tmp_path: Path) -> Path:
    path = tmp_path / "labels.csv"
    shutil.copy(data.LABELS, path)
    return path


def test_items_and_catalogue_match_the_protocol(ctx):
    groups = [i["group"] for i in ctx.items]
    assert len(ctx.items) == 20 and groups.count("off_topic") == 3 and groups.count("non_english") == 3
    assert len(ctx.ids("dev")) == len(ctx.ids("test")) == 10
    assert ctx.n == 175 and len({d.ref for d in ctx.catalogue}) == 175


def test_every_label_row_is_honestly_marked_and_in_the_pool():
    rows = data.read_labels()
    pool = {(r["item_id"], r["destination_ref"]) for r in data.read_pool()}
    assert rows and all(r["labeler"] == data.AGENT or r["labeler"].startswith("human:") for r in rows)
    assert {(r["item_id"], r["destination_ref"]) for r in rows} == pool
    assert all(r["agent_label"] for r in rows if r["labeler"] == data.AGENT)


def test_report_says_pending_review_and_recomputes_when_labels_change(ctx, label_copy):
    report = ev.evaluate(labels_path=label_copy, ctx=ctx)
    assert report["label_status"] == "agent-drafted labels, pending human review"
    assert "pending human review" in report["claim"]
    assert set(report["test"]) == {*ev.SYSTEMS, "random_expected"}
    before = report["test"]["recommender_tuned"]["p_at_10"]
    # a reviewer flips every relevant label in the tuned top 10 of the test items to 0
    rows = data.read_labels(label_copy)
    tops = {(e["item_id"], t["destination_ref"]) for e in report["per_item"] if e["split"] == "test"
            for t in e["recommender_tuned_top10"]}
    for r in rows:
        if (r["item_id"], r["destination_ref"]) in tops and r["label"] == "1":
            cli._mark(r, "0", "tester")
    data.write_labels(rows, label_copy)
    after = ev.evaluate(labels_path=label_copy, ctx=ctx)
    assert after["test"]["recommender_tuned"]["p_at_10"] < before or before == 0
    assert after["label_status"].startswith("partially human-reviewed")
    assert after["labels"]["agent_human_agreement"]["flipped"] > 0


def test_tuning_never_reads_test_labels(ctx, label_copy):
    rows = data.read_labels(label_copy)
    tuned = ev.tune(ctx, data.judgments(rows))["weights"]
    test_ids = set(ctx.ids("test"))
    for r in rows:
        if r["item_id"] in test_ids:
            r["label"] = "0" if r["label"] == "1" else "1"
    assert ev.tune(ctx, data.judgments(rows))["weights"] == tuned


# ------------------------------------------------------------------ labelling CLI


def scripted(*answers: str):
    it = iter(answers)
    return lambda _prompt: next(it)


def test_review_page_mode_accepts_flips_and_saves(label_copy):
    out: list[str] = []
    rc = cli.main(["--labels", str(label_copy), "review", "--reviewer", "ana", "--item", "c04"],
                  ask=scripted("2 3", "", "q"), out=out.append)
    assert rc == 0
    rows = [r for r in data.read_labels(label_copy) if r["item_id"] == "c04"]
    reviewed = [r for r in rows if r["labeler"] == "human:ana"]
    assert len(reviewed) == 10 and all(r["reviewed_at"] for r in reviewed)
    flipped = [r for r in reviewed if r["label"] != r["agent_label"]]
    assert len(flipped) == 2
    screen = "\n".join(out)
    assert not any(w in screen for w in ("bm25", "relevance_only", "recommender_default", "systems="))  # blind


def test_set_and_stats(label_copy):
    row = data.read_labels(label_copy)[0]
    out: list[str] = []
    assert cli.main(["--labels", str(label_copy), "set", row["item_id"], row["destination_ref"], "y", "--reviewer", "bo"],
                    out=out.append) == 0
    assert cli.main(["--labels", str(label_copy), "stats"], out=out.append) == 0
    assert "1 rows reviewed" in out[-1] or "partially human-reviewed: 1/" in out[-1]


def test_reviewer_name_is_required_and_plain(label_copy):
    out: list[str] = []
    assert cli.main(["--labels", str(label_copy), "review", "--reviewer", "human:x"], out=out.append) == 2


def test_default_weights_are_the_documented_ones():
    assert DEFAULT_WEIGHTS == {"audience_fit": 1.0, "relevance": 0.3, "trust": 0.1, "past_results": 0.1,
                               "rules_headroom": 0.2}
