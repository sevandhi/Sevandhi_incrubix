# AI_USE.md

**Author: Sevandhi, Python developer, architect, hands-on implementer**

The brief asks me to disclose every AI coding tool and what it was used for. This file does that in full. It also covers how AI is used inside the service at run time.

I treat AI the way a senior engineer treats any tool: pick the right one for the job, give it sharp context, and verify everything it returns. I did not use one chatbot for everything. I built a small AI toolchain, each tool in the lane where it is strongest, and I stayed the architect and decision-maker throughout.

---

## 1. My AI toolchain (design, build, debug, test, document)

| Tool | Lane | What I used it for |
|---|---|---|
| **Claude Code (Anthropic)** | Implementation engine | Generating most of the code, tests, scripts, mocks and documentation from my architecture plan and decisions; assisting the catalogue research |
| **GitHub Copilot** | In-editor pair programmer | Inline completions, boilerplate, repetitive pytest cases, type hints and docstrings while I was writing and refactoring code by hand |
| **ChatGPT** | Tech-doubt desk and analysis | Quick technical doubts (async patterns, SQLAlchemy and Pydantic behaviour, HTTP and API edge cases), comparing design options, reasoning through trade-offs |
| **Perplexity** | Research | Sourced research: finding public rules pages, API documentation and platform policies, then checking claims against the cited links |
| **Gemini** (chat and API free tier) | Second opinion, and run-time provider | Cross-checking designs and debugging hypotheses; also the optional run-time LLM provider for rules capture (off by default, see section 2) |
| **Local Ollama** | Offline run-time provider | Optional free local fallback for rules capture (off by default) |

Run-time providers are separate from the dev tools above. Free tiers only; no paid AI service was used.

### How I ran the workflow

I ran this like an engineering team where I was the lead and the AI tools were fast, tireless specialists.

- **Research:** Perplexity for sourced answers, Gemini and ChatGPT for a second read. Anything that ended up in the data went through verbatim-quote verification (section 3).
- **Architecture and design:** I designed the architecture myself: the service layout, connectors, rules engine, idempotency and certification flow. I recorded decision points (D1 to D11) and answered each one. Examples: D2 (host the demo on Render free plus Supabase free), D9 (allow a contract publish that carries no rules, with a warning), Dev.to stays at L2, free LLMs only, and a person confirms every rule. ChatGPT and Gemini were my sounding boards on the trade-offs; the decisions were mine.
- **Implementation:** Claude Code generated the service, connectors, rules engine, catalogue pipeline, recommender and demo packaging from my plan. Copilot accelerated the hands-on editing and refactoring in my editor.
- **Debugging:** I brought failing traces and hypotheses to ChatGPT, Gemini and Claude Code, compared their diagnoses, and fixed the root cause rather than the symptom.
- **Testing:** Unit, integration, contract and end-to-end tests, with Copilot and Claude Code helping to write them. Fixes found in code review were written test-first: each regression test was seen failing before the fix.
- **Documentation:** README, REPORT, SOURCES, docs/ and the ADRs, drafted with AI and edited by me.
- **Tech doubts:** ChatGPT and Perplexity as the quick-answer layer, so I kept moving instead of getting stuck.

Constraints applied throughout: free tools only; no code copied from AGPL/GPL projects; no posting to real communities; no secrets in the repo; never fake evidence; known gaps stated.

### My responsibility

I directed the work, designed the architecture, made every design decision, ran and reviewed the results, and can explain and change any part of the system. AI multiplied my output; it did not replace my judgement. The study guide (docs/STUDY_GUIDE.md) covers the architecture and key flows I rehearsed.

---

## 2. AI at run time (inside the service)

- **Default: no AI.** `LLM_PROVIDER=mock` is a deterministic extractor (regex and keywords), and every test uses it
  or respx. The posting path (rules gate → tailoring → connector) makes **no LLM call**. The rules engine, spec
  checks, tailoring, idempotency and certification are plain code.
- **Optional, free only:** one OpenAI-compatible adapter (`app/providers/llm/`). It tries Gemini API free tier →
  Groq free → OpenRouter `:free` models → local Ollama (`qwen2.5:1.5b-instruct`, Apache-2.0) → the mock. No
  provider is paid. The LLM is used for:
  1. **Rules capture** (Advanced: "an AI step reads a community's rules page"). The LLM proposes values as strict
     JSON. A value is kept only if its quote appears **verbatim** in the fetched page; otherwise the conservative
     default is used and the field is flagged. **A person confirms** in `/ui/rules/{id}` before any rule is used.
     The LLM never posts and never decides alone.
  2. **Relevance adjustment** (optional): at most ±1 on the lexicon score, and only if its evidence is verbatim in
     the data that was sent.
  3. **Recommendation reason phrasing** (optional): it may only rewrite text. A rewrite that adds a number not in
     the facts is rejected, and order and scores never change.
- Each call records provider, model, `fallback_used`, tokens and cost (0) in the job manifest (IS9).
- **Only public rules pages, text a person pastes, and our own content are sent to an LLM.** Credentials and
  private data never are. Free-tier data terms are in SOURCES.md.
- **Measured results:**
  - The local 1.5B model was worse than the deterministic extractor on our 22 rules pages (0.50 vs 0.83 field
    accuracy).
  - That is why the default stays `mock`.
  - <!-- TODO(final-numbers): Gemini free-tier result from scripts/eval_rules_capture.py, if run -->

## 3. AI-generated data and content (disclosed)

- **Recommender labels** (`catalogue/labels/labels.csv`, 895 rows) and **relevance labels**
  (`tests/fixtures/relevance/hand_labels_v1.csv`, 59 rows) were AI-drafted. They are marked
  `labeler=agent-draft` / "AI-drafted, pending human review", **pending human review**. They are not called
  hand-labelled anywhere.
- **Real-catalogue rules** (`catalogue/research/rules_research_v1.csv`) were researched with AI assistance from
  public rules pages. A script verified every value's quote (≤25 words) verbatim against the saved page text. Every
  row is `rules_status=proposed` until a person confirms it.
- **Content items** for the recommender evaluation, the **rules-page test fixtures** (22 HTML pages), the
  **disclosure translations** (de, es, fr, hi, ja, pt, ta) and the certification test-post text were AI-generated.
  The translations need a native-speaker check.
- No generated media is published. The PeerTube test card is an ffmpeg `testsrc` pattern, not AI. Certification
  test posts are fixed neutral text, and `ai_generated` is recorded in the manifests.
