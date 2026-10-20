"""Evaluate rules capture: the deterministic mock vs each available free LLM, on our saved rules pages.

    uv run python scripts/eval_rules_capture.py                      # mock + every provider that is reachable / has a key
    uv run python scripts/eval_rules_capture.py --ollama-url http://127.0.0.1:11439/v1 \
        --ollama-model qwen2.5:1.5b-instruct --ollama-model qwen2.5:7b-instruct

Pages: ``tests/fixtures/rules_pages/*.html`` (our own text, not copies of real communities) with hand-written
expected rules in ``expected.yaml``. Each provider is called ALONE (no fallback to the mock), so a failure counts
against that provider. Gemini / Groq / OpenRouter run automatically when their key is set in the environment/.env.

Writes:
    evidence/metrics/rules_capture_eval.csv          one row per provider x page x field
    evidence/metrics/rules_capture_eval_summary.csv  one row per provider (accuracy, quote verification, latency)

Metrics, per provider:
    field_accuracy           proposed value == expected value (null == null counts), over all page x field cells
    accuracy_after_quotes    the same after quote verification (a value whose quote is not in the page becomes null)
    stated_recall            share of the stated rules (expected not null) found with a verified quote and the right value
    false_rules              cells where the page states nothing but a value was proposed and its quote verified
    quote_verification_rate  proposed non-null values whose quote is in the page, over all proposed non-null values
    p50/max latency per page, failures (no valid JSON from that provider)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalogue.rules_capture import page_text, quote_in  # noqa: E402
from app.providers.llm import (  # noqa: E402
    LLMSettings,
    complete_json,
    openai_compat,  # noqa: E402
    reset_cooldowns,
)
from app.providers.llm.base import LLMUnavailableError  # noqa: E402
from app.providers.llm.mock import MockProvider, extract_rules  # noqa: E402
from app.providers.llm.rules_task import FIELDS, SYSTEM_PROMPT, RulesExtraction, user_prompt  # noqa: E402

PAGES = ROOT / "tests" / "fixtures" / "rules_pages"
OUT = ROOT / "evidence" / "metrics"


def load_pages() -> list[tuple[str, str, dict[str, Any]]]:
    expected = yaml.safe_load((PAGES / "expected.yaml").read_text(encoding="utf-8"))
    pages = []
    for name in sorted(expected):
        html = (PAGES / f"{name}.html").read_text(encoding="utf-8")
        pages.append((name, page_text(html.encode(), "text/html"), expected[name]["expected"]))
    return pages


async def ollama_models(url: str) -> list[str]:
    base = url.rstrip("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{base}/api/tags")
            return [m["name"] for m in response.json().get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


async def providers(args: argparse.Namespace, settings: LLMSettings) -> list[tuple[str, Any]]:
    """(label, provider) pairs to evaluate. The mock is always first."""
    out: list[tuple[str, Any]] = [("mock", None)]
    for name, key in (("gemini", settings.GEMINI_API_KEY), ("groq", settings.GROQ_API_KEY),
                      ("openrouter", settings.OPENROUTER_API_KEY)):
        if not (key is not None and key.get_secret_value()):
            print(f"skip {name}: no key")
        elif name == "gemini" and args.gemini_model:
            for model in args.gemini_model:  # label gemini:<model>, so several free models can be compared
                out.append((f"gemini:{model}", openai_compat.build(name, settings.model_copy(update={"GEMINI_MODEL": model}))))
        else:
            out.append((name, openai_compat.build(name, settings)))
    url = args.ollama_url or settings.OLLAMA_URL
    available = await ollama_models(url)
    if not available:
        print(f"skip ollama: not reachable at {url}")
    for model in args.ollama_model or [settings.OLLAMA_MODEL]:
        if model in available:
            s = settings.model_copy(update={"OLLAMA_URL": url, "OLLAMA_MODEL": model})
            out.append((f"ollama:{model}", openai_compat.build("ollama", s)))
        elif available:
            print(f"skip ollama model {model}: not pulled (have {', '.join(available)})")
    return out


async def run_one(label: str, provider: Any, text: str, settings: LLMSettings,
                  rate_limit_wait_s: float = 0.0) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """One page through one provider. A free-tier 429 is waited out once (up to ``rate_limit_wait_s``) and the page is
    asked again, so the free-tier quota does not count as a wrong answer; any other failure counts."""
    obj, info = await _run_once(provider, text, settings)
    if obj is None and provider is not None and rate_limit_wait_s > 0 and "rate_limited" in info["error"]:
        print(f"{label}: rate limited, waiting {rate_limit_wait_s:.0f} s and asking once more")
        await asyncio.sleep(rate_limit_wait_s)
        reset_cooldowns()
        obj, again = await _run_once(provider, text, settings)
        again["retries"] += 1
        again["error"] = again["error"] or ""
        info = again
    return obj, info


async def _run_once(provider: Any, text: str, settings: LLMSettings) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    reset_cooldowns()  # each page is judged on its own: a cooldown from the previous page must not skip this one
    started = time.perf_counter()
    chain = [MockProvider(lambda _p: extract_rules(text))] if provider is None else [provider]
    try:
        obj, meta = await complete_json(user_prompt(text[: settings.LLM_MAX_INPUT_CHARS]), RulesExtraction,
                                        system=SYSTEM_PROMPT, settings=settings, providers=chain, max_tokens=900)
        info = {"latency_ms": round((time.perf_counter() - started) * 1000, 1), "tokens": meta.total_tokens,
                "model": meta.model, "error": "", "retries": max(0, sum(1 for a in meta.attempts if a.outcome != "skipped") - 1)}
        return obj.model_dump(), info
    except LLMUnavailableError as err:
        detail = "; ".join(f"{a.outcome}: {a.detail}" for a in err.meta.attempts)[:300]
        return None, {"latency_ms": round((time.perf_counter() - started) * 1000, 1), "tokens": err.meta.total_tokens,
                      "model": getattr(provider, "model", ""), "error": detail, "retries": 0}


def same(a: Any, b: Any) -> bool:
    return a == b


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL"))
    parser.add_argument("--ollama-model", action="append", help="repeat to evaluate several pulled models")
    parser.add_argument("--only", help="comma-separated labels to run (e.g. mock,ollama:qwen2.5:1.5b-instruct)")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--pace-s", type=float, default=6.0,
                        help="seconds between two calls to a hosted free tier (its per-minute limit); 0 = no pause")
    parser.add_argument("--rate-limit-wait-s", type=float, default=65.0, help="wait once after a 429, then ask again")
    parser.add_argument("--gemini-model", action="append", help="repeat to evaluate several Gemini models (default: GEMINI_MODEL)")
    parser.add_argument("--timeout-s", type=float, default=60.0, help="per call to a hosted provider")
    parser.add_argument("--ipv4", action="store_true", help="LLM_HTTP_IPV4=true (the IPv6 route to the provider times out)")
    args = parser.parse_args()

    settings = LLMSettings(LLM_TIMEOUT_S=args.timeout_s)
    if args.ipv4:
        settings = settings.model_copy(update={"LLM_HTTP_IPV4": True})
    pages = load_pages()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    detail_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    chosen = await providers(args, settings)
    if args.only:
        wanted = set(args.only.split(","))
        chosen = [p for p in chosen if p[0] in wanted]
    for label, provider in chosen:
        cells = correct_raw = correct_verified = stated = stated_hit = false_rules = proposed = verified = failures = 0
        latencies: list[float] = []
        tokens = 0
        model = "mock-heuristic-v1"
        hosted = label.split(":")[0] in ("gemini", "groq", "openrouter")
        for index, (name, text, expected) in enumerate(pages):
            if hosted and index and args.pace_s > 0:
                await asyncio.sleep(args.pace_s)
            obj, info = await run_one(label, provider, text, settings, args.rate_limit_wait_s if hosted else 0.0)
            latencies.append(info["latency_ms"])
            tokens += info["tokens"] or 0
            model = info["model"] or model
            if obj is None:
                failures += 1
            for field in FIELDS:
                exp = expected.get(field)
                guess = (obj or {}).get(field) or {}
                value, quote = guess.get("value"), guess.get("quote")
                ok_quote = value is not None and quote_in(quote, text)
                after = value if ok_quote else None
                cells += 1
                correct_raw += same(value, exp)
                correct_verified += same(after, exp)
                if value is not None:
                    proposed += 1
                    verified += ok_quote
                if exp is not None:
                    stated += 1
                    stated_hit += same(after, exp)
                elif after is not None:
                    false_rules += 1
                detail_rows.append({
                    "provider": label, "model": info["model"], "page": name, "field": field,
                    "expected": "" if exp is None else exp, "proposed": "" if value is None else value,
                    "quote_verified": ok_quote if value is not None else "", "after_verification": "" if after is None else after,
                    "correct": same(value, exp), "correct_after_verification": same(after, exp),
                    "page_latency_ms": info["latency_ms"], "error": info["error"],
                })
            print(f"{label:34s} {name:34s} {info['latency_ms']:9.0f} ms {'FAILED ' + info['error'] if obj is None else ''}")
        summary = {
            "provider": label, "model": model, "pages": len(pages), "cells": cells,
            "field_accuracy": round(correct_raw / cells, 3),
            "accuracy_after_quotes": round(correct_verified / cells, 3),
            "stated_rules": stated, "stated_recall": round(stated_hit / stated, 3) if stated else None,
            "false_rules": false_rules,
            "proposed_values": proposed,
            "quote_verification_rate": round(verified / proposed, 3) if proposed else None,
            "failures": failures,
            "latency_p50_ms": round(statistics.median(latencies), 1),
            "latency_max_ms": round(max(latencies), 1),
            "total_tokens": tokens, "cost_usd": 0.0,
        }
        summary_rows.append(summary)
        print(summary)
    with (out_dir / "rules_capture_eval.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(detail_rows[0]))
        writer.writeheader()
        writer.writerows(detail_rows)
    with (out_dir / "rules_capture_eval_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"wrote {out_dir / 'rules_capture_eval.csv'} and rules_capture_eval_summary.csv "
          f"(generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, {os.cpu_count()} CPUs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
