# SOURCES.md

Every library, container image, model, hosted API, dataset and document this project uses, with its licence.
Checked on **2026-10-20** against the installed packages and the pinned images on the build machine.

- **Our code** (`app/`, `mocks/`, `scripts/`, `deploy/bootstrap/`, `deploy/shooter/shooter.py`, tests, docs) is **MIT** (see `LICENSE`).
- Nothing was copied from Postiz, lemmy-js-client, n8n or any other AGPL/GPL/non-commercial project. Platform behaviour
  was learned from public API documentation and by measuring our own local containers (section 6).
- How AI coding tools were used is in `AI_USE.md`.

## Dependencies

All 63 Python distributions installed by `uv sync` (runtime group plus dev group, including transitive ones).
Versions come from `uv.lock`. Licences come from each package's installed metadata (`License-Expression`, then
`License`, then the `License ::` classifiers), read with `importlib.metadata`. Where the metadata was empty or vague,
we read the licence file shipped in the package's `dist-info` and say so in the row.

- **Runtime** packages are in the Docker image and the shipped service.
- **Dev** packages are only used to run tests and lint. They are not in the image (`uv sync --no-dev`).
- **Direct** packages are listed in `pyproject.toml`. **Transitive** packages are pulled in by a direct one.
- `colorama` and `pywin32` are in the lock file as Windows-only markers of dev tools. They are not installed on Linux and are not listed below.

| Package | Version | Licence | Role |
|---|---|---|---|
| `alembic` | 1.20.0 | MIT | runtime (direct) |
| `annotated-doc` | 0.0.5 | MIT | runtime (transitive) |
| `annotated-types` | 0.8.0 | MIT | runtime (transitive) |
| `anyio` | 4.15.1 | MIT | runtime (transitive) |
| `asyncpg` | 0.32.0 | Apache-2.0 | runtime (direct) |
| `boto3` | 1.43.113 | Apache-2.0 | runtime (direct) |
| `botocore` | 1.43.113 | Apache-2.0 | runtime (transitive) |
| `certifi` | 2026.7.22 | **MPL-2.0** (see note 1) | runtime (transitive) |
| `cffi` | 2.1.1 | MIT-0 | runtime (transitive) |
| `charset-normalizer` | 3.5.2 | MIT | dev (transitive) |
| `click` | 8.5.0 | BSD-3-Clause | runtime (transitive) |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause | runtime (direct) |
| `docker` | 7.2.0 | Apache-2.0 | dev (transitive) |
| `fastapi` | 0.143.1 | MIT | runtime (direct) |
| `feedparser` | 6.0.14 | BSD-2-Clause | runtime (direct) |
| `feedparser-sgmllib` | 2.1.0 | PSF-2.0 | runtime (transitive) |
| `greenlet` | 3.5.6 | MIT AND PSF-2.0 | runtime (transitive) |
| `h11` | 0.16.0 | MIT | runtime (transitive) |
| `httpcore` | 1.0.9 | BSD-3-Clause | runtime (transitive) |
| `httptools` | 0.9.0 | MIT | runtime (transitive) |
| `httpx` | 0.28.1 | BSD-3-Clause | runtime (direct) |
| `hypothesis` | 6.169.1 | **MPL-2.0** (see note 1) | dev (direct) |
| `idna` | 3.20 | BSD-3-Clause | runtime (transitive) |
| `iniconfig` | 2.3.1 | MIT | dev (transitive) |
| `Jinja2` | 3.1.6 | BSD-3-Clause (metadata has only the "BSD License" classifier; confirmed from the shipped `LICENSE.txt`) | runtime (direct) |
| `jmespath` | 1.1.0 | MIT | runtime (transitive) |
| `lingua-language-detector` | 2.2.0 | Apache-2.0 (metadata has only the classifier; confirmed from the shipped licence file) | runtime (direct) |
| `lxml` | 6.1.3 | BSD-3-Clause (see note 2) | runtime (direct) |
| `Mako` | 1.4.3 | MIT | runtime (transitive) |
| `MarkupSafe` | 3.0.4 | BSD-3-Clause | runtime (transitive) |
| `opentelemetry-api` | 1.45.1 | Apache-2.0 | runtime (transitive) |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause | dev (transitive) |
| `pluggy` | 1.6.0 | MIT | dev (transitive) |
| `pycparser` | 3.11 | BSD-3-Clause | runtime (transitive) |
| `pydantic` | 2.14.1 | MIT | runtime (direct) |
| `pydantic-settings` | 2.16.0 | MIT | runtime (direct) |
| `pydantic_core` | 2.50.1 | MIT | runtime (transitive) |
| `Pygments` | 2.21.0 | BSD-2-Clause | dev (transitive) |
| `PyJWT` | 2.15.1 | MIT | runtime (direct) |
| `pytest` | 9.1.1 | MIT | dev (direct) |
| `pytest-asyncio` | 1.4.0 | Apache-2.0 | dev (direct) |
| `python-dateutil` | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause (metadata says "Dual License"; classifiers BSD + Apache) | runtime (transitive) |
| `python-dotenv` | 1.2.4 | BSD-3-Clause | runtime (transitive) |
| `python-multipart` | 0.0.32 | Apache-2.0 | runtime (direct) |
| `PyYAML` | 6.0.3 | MIT | runtime (direct) |
| `requests` | 2.34.2 | Apache-2.0 | dev (transitive) |
| `respx` | 0.23.1 | BSD-3-Clause | dev (direct) |
| `ruff` | 0.17.0 | MIT | dev (direct) |
| `s3transfer` | 0.19.2 | Apache-2.0 | runtime (transitive) |
| `six` | 1.17.0 | MIT | runtime (transitive) |
| `sortedcontainers` | 2.4.0 | Apache-2.0 | dev (transitive) |
| `SQLAlchemy` | 2.1.4 | MIT | runtime (direct) |
| `starlette` | 1.8.0 | BSD-3-Clause | runtime (transitive) |
| `structlog` | 26.1.0 | MIT OR Apache-2.0 | runtime (direct) |
| `testcontainers` | 4.15.0 | Apache-2.0 | dev (direct) |
| `typing-inspection` | 0.4.4 | MIT | runtime (transitive) |
| `typing_extensions` | 4.16.0 | PSF-2.0 | runtime (transitive) |
| `urllib3` | 2.8.0 | MIT | runtime (transitive) |
| `uvicorn` | 0.54.0 | BSD-3-Clause | runtime (direct) |
| `uvloop` | 0.23.0 | MIT OR Apache-2.0 | runtime (transitive) |
| `watchfiles` | 1.3.0 | MIT | runtime (transitive) |
| `websockets` | 17.2 | BSD-3-Clause | runtime (transitive) |
| `wrapt` | 2.5.1 | BSD-2-Clause | dev (transitive) |

**Totals:** 48 runtime packages (18 direct, 30 transitive) and 15 dev-only packages (6 direct, 9 transitive).
By licence family: MIT or MIT-0 26, BSD (2- or 3-clause) 16, Apache-2.0 12, dual Apache/BSD/MIT 5, PSF-2.0 2 (plus
`greenlet`, counted under MIT, which is MIT AND PSF-2.0), MPL-2.0 2. There is **no GPL, LGPL, AGPL or non-commercial
licence** in either group.

Notes:
1. **MPL-2.0** (`certifi` at runtime, `hypothesis` in dev only) is a file-level copyleft licence. It allows commercial
   use. Its only duty is to share changes to the MPL-licensed files themselves. We use both packages unmodified, so
   nothing more is needed. MPL-2.0 is on the project's allowed list.
2. **lxml** is BSD-3-Clause, with parts under PSF-2.0 (code derived from ElementTree, difflib) and the bundled
   libxml2/libxslt under MIT. Its `LICENSES.txt` mentions a GPL `test.py` test runner. That file is not in the
   installed wheel (checked in `RECORD`), so it is not shipped.
3. Per-package notes:
   - `lingua-language-detector` (https://github.com/pemistahl/lingua-py) does language detection of imported destination pages (ADV1). It
     ships its language models, so it is large: the wheel is about 160 MB, and the installed package is about 290 MB on this machine.
   - `lxml` (https://lxml.de) builds the Media RSS feeds we serve ourselves (the `mrss` connector).
   - `feedparser` (https://github.com/kurtmckee/feedparser) and its dependency `feedparser-sgmllib` (PSF-2.0) validate
     every rendered feed and back the `mrss` connector's `get_status` check.
   - `Jinja2` renders the two small HTML pages, `/ui/rules/{id}` (confirm rules) and `/ui/network`, with autoescaping on.

The standard library HTTP client is used in `deploy/bootstrap/` (a stdlib-only script, so nothing is installed there).

## Container images

Most images are pinned by digest in the compose files. The ones without a digest are pinned by tag.

| Image | Pinned as | Licence | Compose profile | Role |
|---|---|---|---|---|
| our image (`Dockerfile`), base `python:3.12-slim` | tag | Python PSF-2.0; Debian packages under their own free licences | default | `api`, `worker` and `mock-platforms` (one image, three commands) |
| `uv` 0.12.21 (pip-installed in our `Dockerfile` at build time) | version | MIT OR Apache-2.0 | build only | installs the locked dependencies |
| `postgres:16-alpine` | tag (ours), digest `sha256:7218…80ea` (lemmy-db, peertube-db) | PostgreSQL Licence (permissive) | default / video | our database, job queue and idempotency store; Lemmy's and PeerTube's databases |
| `chrislusf/seaweedfs:3.97` | tag | Apache-2.0 (image label) | default | S3-compatible storage (evidence, packs, reports, media) |
| our live-demo image (`Dockerfile.demo`), base `python:3.12-slim` | tag | as our `Dockerfile` | not in compose (Render free, `render.yaml`) | the all-in-one live demo: API + worker + mock platforms in one process |
| `edoburu/pgbouncer:latest` (PgBouncer) | tag | PgBouncer ISC; image scripts MIT | not in compose (only `scripts/demo_rehearsal.py`) | stand-in for Supabase's session pooler in the local rehearsal of the demo deploy; not shipped |
| `dessalines/lemmy:0.19.20` | digest `sha256:79e9…69d9` | **AGPL-3.0** | default | local real Lemmy (test instance) |
| `dessalines/lemmy-ui:0.19.20` | digest `sha256:ee4c…9d40` | **AGPL-3.0** | `ui` | Lemmy web UI, for human browsing and page screenshots |
| `asonix/pictrs:0.5` | digest `sha256:7c1c…5e83` | **AGPL-3.0** | `ui` | Lemmy's image service (with lemmy-ui only) |
| `ghost:6-alpine` (Ghost 6.70) | digest `sha256:879d…99e3` | MIT | default | local real Ghost (test instance) |
| `ghcr.io/shopify/toxiproxy:2.12.0` | digest `sha256:9378…214e` | MIT | default | fault injection (latency, timeouts) in front of the real Lemmy and Ghost |
| `python:3.12-slim` | digest `sha256:a6e3…11b1` | PSF-2.0 + Debian | default | `bootstrap`: creates dev-only accounts and keys in the local instances (stdlib script) |
| `discourse/discourse:2026.10.0-latest` | digest `sha256:7ccb…8083` | **GPL-2.0** | `full` | local real Discourse (test instance) and its bootstrap |
| `pgvector/pgvector:0.8.7-pg16-trixie` | digest `sha256:40d8…c972` | PostgreSQL Licence (pgvector and Postgres) | `full` | Discourse's database |
| `redis:7-alpine` | digest `sha256:858f…3499` (this is **Redis 7.4.11**) | **RSALv2 / SSPLv1** (see note below) | `full` | Discourse's cache and queue |
| `chocobozzz/peertube:v8.3.0-trixie` | tag | **AGPL-3.0** | `video` (used with `-f deploy/compose/peertube.yaml`) | local real PeerTube (test instance) |
| `redis:7.2.5-alpine` | tag | BSD-3-Clause (the last Redis line before the licence change) | `video` | PeerTube's queue |
| `mcr.microsoft.com/playwright/python:v1.62.0-noble` + pip `playwright==1.62.0` | tag + version | Apache-2.0 (Playwright). The bundled browsers keep their own licences (Chromium BSD-style, Firefox MPL-2.0, WebKit LGPL/BSD), and they run as separate programs. | `evidence` | `shooter`: headless browser that takes the certification screenshots |
| `ollama/ollama:0.35.1` | tag | MIT | `llm` | optional local LLM server |
| `zricethezav/gitleaks:v8.21.2` | tag | MIT | not in compose (`scripts/prove_mh7.py` runs it with `docker run`) | secret scan of the working tree (MH7) |

Only `api` and `mock-platforms` publish host ports. Every test platform sits on the internal-only `ibx_sandbox`
network, which has no internet route.

**Local LLM model.** The only model we run is `qwen2.5:1.5b-instruct` (Apache-2.0,
https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct). `qwen2.5:7b-instruct` (Apache-2.0) is allowed too, but its download
failed, so it was never evaluated. **`qwen2.5:3b-instruct` is refused**: its licence is the Qwen Research licence, which
allows non-commercial use only
(https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/blob/main/LICENSE). `qwen2.5:72b` (Qwen licence) is refused too.
`scripts/llm_ollama.sh` blocks both.

**Redis for Discourse (profile `full`).** Pinned to `redis:7.2.5-alpine` (BSD-3-Clause), the last release before
Redis 7.4 moved to RSALv2/SSPLv1. It was previously `redis:7-alpine`, which resolved to 7.4.11; it is changed so every
container we run is under an OSI open-source licence. It runs only in the optional `full` profile and is never shipped.

## AGPL/GPL servers: separate unmodified containers

Lemmy, lemmy-ui and pict-rs (AGPL-3.0), PeerTube (AGPL-3.0) and Discourse (GPL-2.0) are used **only as test platforms**:

- Each runs **unmodified**, from the official image, as a **separate container**.
- Our service talks to them **only over HTTP**, through their public APIs, the same way any client on the internet would.
- No code from them is linked, imported, copied or shipped. Our image contains none of it.
- They run on an internal Docker network with no internet route. Only our tests and certification runs use them.
- Our own clients (for example `app/connectors/lemmy/`) are written from the API documentation and our own measurements.
  We do not use the AGPL `lemmy-js-client`, and nothing from Postiz or n8n.

So the GPL/AGPL obligations, which attach to distributing or offering a modified version of those programs, do not reach
our MIT code. Someone who deploys our service in production does not need any of these containers: they only exist to
test against real platforms locally.

Other servers in the stack are permissive: PostgreSQL (PostgreSQL Licence), SeaweedFS (Apache-2.0), Ghost (MIT),
toxiproxy (MIT), Ollama (MIT), and Redis 7.2.5 (BSD-3-Clause). (Redis 7.4 is not used anywhere.)

## Hosted LLM providers (optional, free tier, off by default)

The default is `LLM_PROVIDER=mock`, the deterministic extractor in `app/providers/llm/mock.py`, which needs no network and no key.
A hosted provider is called only when its key is put in `.env` (never committed) and `LLM_PROVIDER` is `auto` or names it.
All providers go through one OpenAI-compatible adapter (`app/providers/llm/`). Every call's provider, model, token
counts and `cost_usd` (always 0.0) are recorded in the job manifest.

The terms below were read on 2026-10-18 (AI-assisted, with generic web searches). They can change, so check the links
before relying on them.

| Provider | Base URL / default model | Data terms (free tier) | Sources |
|---|---|---|---|
| Google **Gemini API** free tier | `https://generativelanguage.googleapis.com/v1beta/openai`, `gemini-3.8-flash` | On the free tier, prompts and responses are "used to improve our products". Human reviewers may read them. Do not send sensitive or personal data. Free-tier rate limits are not published as a table (they are shown per project in AI Studio), so we handle 429 with a cooldown and hard-code no limits. | https://ai.google.dev/gemini-api/docs/openai · https://ai.google.dev/gemini-api/docs/pricing · https://ai.google.dev/gemini-api/terms · https://ai.google.dev/gemini-api/docs/rate-limits |
| **Groq** | `https://api.groq.com/openai/v1`, `openai/gpt-oss-20b` (Apache-2.0 weights) | Not retained by default. Requests may be logged for up to 30 days for troubleshooting and abuse checks. | https://console.groq.com/docs/openai · https://console.groq.com/docs/models · https://console.groq.com/docs/rate-limits · https://console.groq.com/docs/your-data |
| **OpenRouter** `:free` models | `https://openrouter.ai/api/v1`, `google/gemma-4-31b-it:free` (Apache-2.0 weights) | Free endpoints follow each upstream provider's own logging and training policy. The limits are 20 requests/min, and 50/day (or 1,000/day after buying 10 credits). The adapter refuses any model id without `:free`. | https://openrouter.ai/docs/api-reference/limits · https://openrouter.ai/docs/guides/privacy/provider-logging · https://huggingface.co/google/gemma-4-31b-it |
| **Ollama** (local) | `http://ollama:11434/v1`, `qwen2.5:1.5b-instruct` | Prompts never leave the machine. | https://ollama.com/library/qwen2.5/tags |

**What we send to an LLM.** Only public pages (community rules pages and public sidebars), text a person pastes
from such a page, our own content, and catalogue fields we wrote ourselves. We never send credentials, tokens or private data.
Every quote the LLM returns must appear word for word in the source text, or it is dropped. A person confirms rules
before they are used. The LLM never posts and never decides alone.

**Which hosted provider was really used in the measurements.** As of 2026-10-20, the rules-capture evaluation file
(`evidence/metrics/rules_capture_eval_summary.csv`) has a row only for the deterministic `mock`. **No Gemini, Groq or
OpenRouter result has been measured yet in this repository.** A Gemini free-tier key has been provided on the build
machine (in the git-ignored `.env`). No evaluation row from it exists yet.
<!-- TODO(final-numbers): if a Gemini row is added to evidence/metrics/rules_capture_eval_summary.csv, state the model and run date here. -->

## Data sources

**Real destination catalogue** (`catalogue/`), catalogue data only. Nothing was ever posted to these sites.
- `catalogue/seeds/seed_v1.csv`: 175 real destinations found by AI-assisted web research, 2026-10-14/15.
  Every row URL answered HTTP 200 in the final check on 2026-10-15 05:56–05:58 UTC. The method, the discovery APIs
  (the lemmy.world community search, the joinpeertube instance index, Discourse `/about.json`, dev.to tag pages) and the
  excluded hosts are in `catalogue/seeds/README.md`.
- `catalogue/seeds/seed_v2.csv`: generated from v1 by `scripts/p5c_catalogue.py seed-v2`. The changes are listed row by row in `seed_v2_CHANGELOG.md`.
- `catalogue/real_catalogue_v1.csv` and `catalogue/reports/`: the real run through our import and rules-capture pipeline
  on 2026-10-18. It holds statuses, sha256 hashes of the fetched pages, and short verified quotes. It holds no page text.
- `catalogue/seeds/seed_v3.csv` and `catalogue/research/` (**still running at the time of writing**): an
  AI-assisted rules research pass. Each rule field is backed by a quote that was checked word for word against the
  fetched page, or is marked "not stated". All rows stay `proposed` until a person confirms them (decision D6).
  `catalogue/research/pages/` keeps plain-text copies of the public pages that were read, named by their sha256, so
  every quote can be checked again. These texts belong to their sites. They are kept only as research evidence and
  are not our content.
  <!-- TODO(final-numbers): replace this paragraph with the research's final counts, dates and method (see catalogue/research/README.md). -->
- **How pages were fetched:** read-only GET requests only. robots.txt was checked for every URL and every redirect hop.
  There was at most 1 request per second per host (1.1 s gap in the seed research), no login and no form submission.
  Hosts that block bots (robots.txt or Cloudflare) were dropped, not worked around. Quotes are at most 25 words.
  One early slip in the seed research is disclosed in `catalogue/seeds/README.md`: a single redirect was followed
  without a robots check, and that response was discarded.

**Labels (not human-reviewed yet)**
- `tests/fixtures/relevance/hand_labels_v1.csv`: 59 relevance labels drafted by an AI agent, **pending human review**.
- `catalogue/labels/labels.csv`: 895 recommender relevance labels drafted by an AI agent, **pending human review**.
  The items in `catalogue/labels/content_items.yaml` are 20 short content items written for this evaluation.

**Test fixtures and media (ours)**
- `tests/fixtures/recorded/`: 49 HTTP exchanges recorded from **our own local** Lemmy 0.19.20, Ghost 6.70 and Discourse
  2026.10 containers, with secrets redacted.
- `tests/fixtures/rules_pages/`: 22 HTML rules pages and their expected answers, written by us for the rules-capture evaluation.
- The mock "internet" for the 1,000-row import test is generated by code (`mocks/platforms/routes/internet.py`). The site names are synthetic.
- `app/connectors/peertube/assets/ibx_test_card.mp4`: a 4 KB, 2-second test card we generated from ffmpeg's `testsrc`
  pattern (`python -m app.connectors.peertube.make_test_card --docker`, using the ffmpeg inside the PeerTube container).
- `app/rules/disclosure.yaml`: the AI-disclosure sentences in 8 languages were written by an AI agent. They need review by native speakers.
- Certification test posts use fixed neutral text we wrote. No real person's content, voice or face is used anywhere.

## Platform documentation read (no code copied)

The connectors were written from these public docs and checked by measuring our own local containers. Where a fact was
measured, the manifest or the connector docs say so.
- Lemmy HTTP API v3: https://join-lemmy.org/api/ (measured on Lemmy 0.19.20)
- Ghost Admin API and Admin API JWT: https://ghost.org/docs/admin-api/ (measured on Ghost 6.70)
- Discourse API: https://docs.discourse.org/ (measured on Discourse 2026.10.0)
- Forem / dev.to API v1: https://developers.forem.com/api/v1 (docs only: real dev.to was never called; see REPORT.md)
- Slack Web API: https://docs.slack.dev/reference/methods/chat.postMessage, https://docs.slack.dev/reference/methods/conversations.history (docs only: no real workspace was used)
- PeerTube REST API: https://docs.joinpeertube.org/api/rest-getting-started (measured on PeerTube v8.3.0)
- Media RSS: https://www.rssboard.org/media-rss ; RSS 2.0: https://www.rssboard.org/rss-specification
- robots.txt, RFC 9309: https://www.rfc-editor.org/rfc/rfc9309
- LLM providers: the Gemini, Groq, OpenRouter and Ollama links in the table above.

The Lemmy, Ghost, Discourse, Forem, Media RSS and RFC links are the standard public locations of those documents. The
manifests and evidence files record what was measured, not the exact doc URLs that were read.

## Tools used to build and test (not shipped)

- uv (MIT OR Apache-2.0), ruff (MIT), pytest and the rest of the dev group (see the table above).
- Docker Engine and Docker Compose, used to run the stack and the test containers.
- Playwright (Apache-2.0) in the `shooter` container, for evidence screenshots only.
- gitleaks v8.21.2 (MIT), for the secret scan (MH7).
