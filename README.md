# IncuBrix Long-Tail Destination Network

One service that publishes content to the "long tail": Discourse forums, Lemmy communities, Dev.to, Ghost blogs,
partner sites (signed webhook), Slack, PeerTube, a Media RSS feed, and communities where a person must post
(assisted packs). **One connector serves many destinations**: forum number 41 is one catalogue row, not new code.
A destination only counts once it is **certified**: a real test post on our own instance is published, confirmed
live, read back and deleted, and the evidence is stored.

What it does:
- **Connector contract** (`POST /connector/v1/{key}/{method}`): `validate`, `publish`, `get_status`, `delete`,
  `fetch_metrics`, `refresh_auth`, `build_assist_pack`, one YAML manifest per connector, shared error codes.
- **Posting-rules engine**: frequency cap, self-promotion, link policy, AI disclosure, language, automation and
  blocked terms are checked **before every post**. A post that breaks a rule is blocked or rescheduled with a reason.
- **Safe publishing**: a write-ahead intent with an invisible marker, so a retry with the same idempotency key never
  creates a second post, even after a timeout. Rate limits use the platform's reset time; a circuit breaker stops
  calls after 5 failures; a kill switch per connector.
- **Certification harness**: publish → confirm live → status → (metrics) → delete → confirm deleted, with an evidence
  pack per run, trust levels L0–L4, automatic demotion and recertification before 30 days.
- **Catalogue tools**: CSV import (validate, de-duplicate, reachability, enrichment), relevance score 1–5 with a
  reason, rules capture (an AI step proposes rules from the rules page; a person confirms), tailoring per destination,
  and a recommender with reasons.

Status of every claim, with evidence: [`evidence/LEDGER.md`](evidence/LEDGER.md). What works and what does not:
[`REPORT.md`](REPORT.md). Reviewer quickstart: [`docs/REVIEWER_GUIDE.md`](docs/REVIEWER_GUIDE.md).

## Architecture

```mermaid
flowchart LR
  C[IncuBrix runtime / reviewer / batch runner] -->|HTTP + Bearer token| API
  subgraph svc[Our service: one image, MIT]
    API["api (FastAPI)<br/>/connector/v1/* contract<br/>/api/v1/* runtime, catalogue, jobs"]
    WK["worker<br/>Postgres job queue (SKIP LOCKED)<br/>scheduler: sweeper, recert, metrics poll, alerts"]
    BASE["BaseConnector pipeline<br/>kill switch, idempotency ledger, spec check,<br/>rate limit, breaker, rules gate, error map, redaction"]
    API --> BASE
    WK --> BASE
  end
  API --> PG[("Postgres 16<br/>catalogue, rules, intents, jobs,<br/>idempotency keys, certifications, metrics")]
  WK --> PG
  API --> S3[("S3 (SeaweedFS)<br/>evidence packs, assist packs,<br/>reports, media, feeds")]
  WK --> S3
  BASE --> MOCK["mock-platforms<br/>Discourse, Forem, Ghost, Lemmy, partner,<br/>Slack, PeerTube, assist, 'internet'"]
  BASE --> TOX[toxiproxy] --> LEM["real Lemmy 0.19"] & GHO["real Ghost 6"]
  BASE -. "--profile full" .-> DIS["real Discourse"]
  BASE -. "-f peertube.yaml --profile video" .-> PT["real PeerTube"]
  WK -. "optional, free only" .-> LLM["LLM adapter: Gemini / Groq / OpenRouter free,<br/>local Ollama, deterministic mock (default)"]
  WK -. "--profile evidence" .-> SHOT["shooter (Playwright screenshots)"]
  WK --> ALR["alerts: log, signed webhook, GitHub issue"]
```

- The real Lemmy, Ghost, Discourse and PeerTube containers sit on an internal network with **no internet route**.
  Test posts can only go to them or to the mocks (`CERT_OWN_HOSTS`, refused at PREFLIGHT otherwise).
- `SIMULATION_MODE=true` rewrites every connection to mock-platforms, so everything runs with zero real accounts.
- Code map: `app/contract` (routes, manifests, registry), `app/connectors/<key>` (one folder per connector),
  `app/reliability` (idempotency, rate limits, breaker, redaction, SSRF guard), `app/rules` + `app/tailoring`,
  `app/certification`, `app/catalogue`, `app/recommender`, `app/metrics`, `app/recert`, `app/providers/{llm,alerts}`,
  `app/jobs`, `app/batch`, `mocks/platforms`, `deploy/` (local instances and bootstrap).
- Design decisions: [`docs/adr/`](docs/adr/). Contract details: [`docs/CONTRACT.md`](docs/CONTRACT.md).

## Setup (≤10 commands)

Needs Docker with Compose v2 and, for the tests, [uv](https://docs.astral.sh/uv/). No API keys.

```bash
git clone https://github.com/sevandhi/Sevandhi_incrubix.git && cd Sevandhi_incrubix   # private repo
docker compose up -d --build --wait            # api, worker, postgres, s3, mocks, real Lemmy + Ghost (first build: minutes)
curl -s http://127.0.0.1:8088/ready            # {"status":"ready", ...}
uv sync                                        # Python 3.12 venv for tests and CLI tools
uv run python -m app.catalogue.seed --api http://127.0.0.1:8088 --token ibx-dev-token --certify
scripts/test.sh                                # every test suite (see below)
docker compose down -v                         # stop and remove everything
```

- Only two host ports are published: the API (`IBX_API_PORT`, default 8088) and mock-platforms (`IBX_MOCK_PORT`,
  default 8089). Change them if they are taken: `IBX_API_PORT=18088 IBX_MOCK_PORT=18089 docker compose up -d --wait`.
- `.env` is optional; `.env.example` lists every setting with dev-only defaults. With `APP_ENV=demo` or `prod` the
  service refuses the dev placeholders: run `scripts/gen_secrets.sh` to write fresh secrets into `.env`.
- Simulation mode (mocks only): `SIMULATION_MODE=true docker compose up -d --wait`.
- Optional profiles: `--profile full` (real Discourse, about 2 min, 1.3 GB RAM), `--profile evidence` (screenshots,
  set `SCREENSHOT_URL=http://shooter:3000`), `--profile ui` (lemmy-ui), `--profile llm` (local Ollama),
  `-f compose.yaml -f deploy/compose/peertube.yaml --profile video` (real PeerTube).
- API docs: `openapi.yaml` (OpenAPI 3.1, checked against the running service) and `http://127.0.0.1:8088/docs`.

Live demo (Render free + Supabase free, simulation mode): `<DEMO_URL>` with bearer token `<DEMO_TOKEN>`
<!-- TODO(final-numbers): demo URL, token delivery and measured cold-start time -->. The free web service sleeps
when idle, so the first request after a pause can take a while (cold start); deploy steps are in
`deploy/render/DEPLOY.md`.

## Tests

```bash
scripts/test.sh            # unit + contract + integration, then a compose stack in simulation: e2e + self-check
scripts/test.sh --fast     # unit + contract + integration only (Postgres via testcontainers, no stack)
scripts/test.sh --unit     # unit tests only, no Docker
scripts/test.sh --live     # local mode stack (real Lemmy + Ghost) and the live_local tests
uv run pytest -q -m "not live_local"           # the same as --fast
uv run python scripts/rule_safety.py           # rule violations caught vs missed -> evidence/metrics/rule_safety.csv
scripts/integration_selfcheck.sh http://127.0.0.1:8088   # Integration Standard IS2-IS10 on a running stack
scripts/prove_all.sh       # every proof script + the evidence ledger (about 25 min)
```

No test needs an API key: LLM calls use the deterministic mock or respx. Last full run:
<!-- TODO(final-numbers): pytest summary line from the final prove_all / test.sh run -->.

## Batch runner (IS10)

Runs a file or folder of YAML/JSON cases against the API and writes everything into one folder: one sub-folder per
case (request, response, result, job, downloaded outputs) plus `summary.csv`. The token is never written.

```bash
uv run python -m app.batch run test_pack/inputs/sample --out results/sample \
  --api http://127.0.0.1:8088 --token ibx-dev-token --strict          # exit 1 if any expectation fails
```

The hidden-test rehearsal cases (`test_pack/inputs/rehearsal/HC1..7_*.yaml`) need destinations and connections set
up first, so run them through `scripts/prove_rehearsal.sh` (or `uv run python scripts/rehearse.py --help`).

A case can use `{{run_id}}` / `{{uuid}}`, `repeat`, `parallel: N`, and `expect` (status, code, body fields, headers,
`job_status`). A 202 answer is followed to the end of its job.

## Connectors

<!-- BEGIN GENERATED: connectors -->
| Connector | Tier | Auth | instance_param | Capabilities | Surfaces |
|---|---|---|---|---|---|
| `assisted` | assisted | none | `submit_url` | assist | post |
| `devto` | native | api_key | `base_url` | publish, status, metrics | article |
| `discourse` | native | api_key | `base_url` | publish, status, delete, metrics | topic |
| `ghost` | native | api_key | `base_url` | publish, status, delete, refresh_auth | article |
| `lemmy` | native | api_key | `community_url` | publish, status, delete, metrics | post |
| `mrss` | feed | none | `feed_key` | publish, status, delete, metrics | item |
| `peertube` | native | oauth2 | `base_url` | publish, status, delete, metrics, refresh_auth | video |
| `slack` | native | bot_token | `channel_id` | publish, status, delete, metrics | message |
| `webhook` | protocol | api_key | `endpoint_url` | publish, status, delete | post |
<!-- END GENERATED: connectors -->

New connector: `uv run python scripts/new_connector.py <key>` scaffolds the connector, its mock route and a test
from `app/connectors/_template`. The registry finds it automatically; one catalogue row adds a destination.

## Error codes

Every error is `{"error": {"code", "message", "retryable", "details"}}`. The first eight codes are the IncuBrix
shared connector codes; the rest use the same style. More in [`docs/ERRORS.md`](docs/ERRORS.md). Generated from
`app.errors.CATALOGUE`, also served at `GET /api/v1/errors`.

<!-- BEGIN GENERATED: errors -->
| Code | HTTP | Retryable | Retry policy | Meaning |
|---|---|---|---|---|
| `DEST_SPEC_VIOLATION` | 422 | no | never | Content breaks the destination's format limits; field-level reasons in details.violations. |
| `DEST_RULE_FREQUENCY_CAP` | 409 | yes | later | Frequency cap reached; reschedule at details.retry_at. |
| `CONNECTION_AUTH_EXPIRED` | 424 | no | after_reconnect | Token expired or revoked; the connection is paused until the owner reconnects. |
| `PLATFORM_RATE_LIMIT` | 429 | yes | backoff | Platform rate limit; back off until details.retry_at (from the platform's reset time). |
| `PLATFORM_QUOTA_EXHAUSTED` | 429 | yes | next_window | Daily quota used up; reschedule to the next window. |
| `PLATFORM_CONTENT_REJECTED` | 422 | no | never | Platform refused the content; its message is in details.platform_message. |
| `PLATFORM_UNAVAILABLE` | 503 | yes | backoff | Platform error or timeout; retry. Calls stop after 5 consecutive failures (circuit open). |
| `CONNECTOR_DISABLED` | 503 | no | never | Kill switch is on for this connector. |
| `DEST_RULE_SELF_PROMO` | 409 | no | never | Destination does not allow self-promotion; details name the signal that fired. |
| `DEST_RULE_LINK_POLICY` | 409 | no | never | Links break the destination's link policy. |
| `DEST_RULE_DISCLOSURE` | 409 | no | never | AI-content disclosure is required but could not be placed. |
| `DEST_RULE_LANGUAGE` | 409 | no | never | Content language does not match the destination's language policy. |
| `DEST_RULE_AUTOMATION_NOT_ALLOWED` | 409 | no | never | Destination forbids automated posting; use the assisted connector. |
| `DEST_RULE_BLOCKED_TERM` | 409 | no | never | Content contains a term the destination blocks. |
| `DEST_RULES_UNCONFIRMED` | 409 | no | never | Destination rules are not confirmed by a person yet; only certification posts to own instances are allowed. |
| `DESTINATION_EXCLUDED` | 409 | no | never | Destination failed certification and is excluded until it passes again. |
| `METHOD_NOT_SUPPORTED` | 404 | no | never | The connector does not list this method in its manifest capabilities. |
| `CONNECTOR_NOT_FOUND` | 404 | no | never | Unknown connector key. |
| `DESTINATION_NOT_FOUND` | 404 | no | never | Unknown destination. |
| `CONNECTION_NOT_FOUND` | 404 | no | never | Unknown connection. |
| `NOT_FOUND` | 404 | no | never | Resource not found. |
| `JOB_NOT_FOUND` | 404 | no | never | Unknown job id. |
| `IDEMPOTENCY_KEY_REUSED` | 409 | no | never | The Idempotency-Key was already used with a different request body. |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | yes | backoff | A request with this Idempotency-Key is still being processed. |
| `REQUEST_VALIDATION_FAILED` | 422 | no | never | The request body or parameters are invalid; see details.errors (input values are never echoed). |
| `INPUT_FETCH_FAILED` | 422 | yes | backoff | An input URL could not be downloaded. |
| `UNAUTHORIZED` | 401 | no | never | Missing or invalid API token. |
| `API_RATE_LIMITED` | 429 | yes | backoff | This service's own rate limit was hit. |
| `STORAGE_UNAVAILABLE` | 503 | yes | backoff | Object storage is unreachable. |
| `DEPENDENCY_UNAVAILABLE` | 503 | yes | backoff | A required dependency (database, storage, queue) is unavailable. |
| `SIMULATION_ONLY` | 403 | no | never | This endpoint only works in simulation/demo mode. |
| `INTERNAL_ERROR` | 500 | no | never | Unexpected error (a bug, not a passing fault); the correlation id is in the logs. |
| `HTTP_METHOD_NOT_ALLOWED` | 405 | no | never | This HTTP method is not allowed on this path. |
| `DESTINATION_NOT_CERTIFIED` | 409 | no | never | Destination is not certified yet (level L0); certify it first (POST /api/v1/certifications). Certification test posts are the only posts allowed before that. |
<!-- END GENERATED: errors -->

## Settings

All settings come from environment variables (or `.env`); nothing else reads the environment. Generated from
`app/config.py` and `app/providers/llm/config.py`; every one is also in `.env.example`. Compose-only variables:
`IBX_API_PORT`, `IBX_MOCK_PORT`, `COMPOSE_PROJECT_NAME`, `IBX_BOOTSTRAP_OUT_UID/GID` and the `IBX_*` dev passwords of
the local Lemmy/Ghost/Discourse containers (see `deploy/compose/*.yaml`).

<!-- BEGIN GENERATED: settings -->
| Setting | Default | Group | Notes |
|---|---|---|---|
| `APP_ENV` | `dev` | service | one of: dev, test, demo, prod. |
| `SERVICE_VERSION` | `0.1.0` | service |  |
| `GIT_SHA` | `unknown` | service |  |
| `LOG_LEVEL` | `INFO` | service |  |
| `LOG_JSON` | `true` | service |  |
| `SEED` | `42` | service |  |
| `DATABASE_URL` | `postgresql+asyncpg://ibx:***@postgres:5432/ibx` | database |  |
| `DB_AUTO_MIGRATE` | `true` | database |  |
| `DB_POOL_SIZE` | `10` | database |  |
| `S3_ENDPOINT` | `http://s3:8333` | object storage |  |
| `S3_PUBLIC_ENDPOINT` | `http://127.0.0.1:8333` | object storage |  |
| `S3_ACCESS_KEY` | *(secret; dev placeholder)* | object storage |  |
| `S3_SECRET_KEY` | *(secret; dev placeholder)* | object storage |  |
| `S3_BUCKET` | `ibx` | object storage |  |
| `S3_REGION` | `us-east-1` | object storage |  |
| `S3_PRESIGN_EXPIRY_S` | `3600` | object storage |  |
| `API_TOKEN` | *(secret; dev placeholder)* | API security | Dev placeholders below: APP_ENV=demo\|prod refuses them. scripts/gen_secrets.sh writes fresh values into .env. |
| `SECRETS_KEY` | *(secret; empty)* | API security | Fernet key; derived from API_TOKEN in dev when empty |
| `CALLBACK_SECRET` | *(secret; empty)* | API security | HMAC key for job callbacks; defaults to API_TOKEN |
| `RATE_LIMIT_PER_MIN` | `600` | API security | per client IP; 0 disables |
| `TRUSTED_PROXIES` | *(empty)* | API security | IPs/CIDRs whose X-Forwarded-For is believed |
| `HTTP_IDEMPOTENCY_ENABLED` | `true` | API security |  |
| `SIMULATION_MODE` | `false` | simulation / mocks | true: every connection is rewritten to mock-platforms (zero keys) |
| `MOCK_PLATFORM_URL` | `http://mock-platforms:8080` | simulation / mocks |  |
| `SIM_SECRET` | `sim-secret` | simulation / mocks | placeholder signing secret our partner mock accepts (simulation only, not a real secret) |
| `BOOTSTRAP_OUT_DIR` | `/bootstrap/out` | simulation / mocks | credentials written by deploy/bootstrap (lemmy.json, ghost.json) |
| `IBX_TOXIPROXY_URL` | `http://toxiproxy:8474` | simulation / mocks | toxiproxy control API (fault injection on real instances) |
| `IBX_LEMMY_PROXY_URL` | `http://toxiproxy:18536` | simulation / mocks |  |
| `IBX_GHOST_PROXY_URL` | `http://toxiproxy:12368` | simulation / mocks |  |
| `DEMO_SEED_ON_START` | `true` | simulation / mocks | create + certify the demo destinations (seed/*.csv) when the demo container starts; existing rows are left as they are |
| `DISABLED_CONNECTORS` | *(empty)* | connectors and reliability |  |
| `PLATFORM_TIMEOUT_S` | `10.0` | connectors and reliability |  |
| `TEST_POST_TTL_S` | `300` | connectors and reliability |  |
| `CIRCUIT_FAILS` | `5` | connectors and reliability |  |
| `CIRCUIT_OPEN_S` | `300` | connectors and reliability |  |
| `IDEMPOTENCY_TTL_H` | `24` | connectors and reliability |  |
| `IDEMPOTENCY_LOCK_WAIT_S` | `15.0` | connectors and reliability |  |
| `IDEMPOTENCY_SETTLE_S` | `5.0` | connectors and reliability |  |
| `RATE_LIMIT_MAX_WAIT_S` | `2.0` | connectors and reliability | how long the local token bucket may wait before failing fast |
| `RATE_LIMIT_DEFAULT_RETRY_S` | `60` | connectors and reliability | used when a platform says "rate limited" without a reset time |
| `LEMMY_RL_FALLBACK_S` | `60` | connectors and reliability |  |
| `WORKER_POLL_S` | `2.0` | jobs |  |
| `WORKER_CONCURRENCY` | `4` | jobs |  |
| `JOB_MAX_ATTEMPTS` | `5` | jobs |  |
| `JOB_STALE_AFTER_S` | `900` | jobs |  |
| `SCHEDULER_ENABLED` | `true` | jobs |  |
| `SCHEDULER_TICK_S` | `5.0` | jobs |  |
| `CALLBACK_TIMEOUT_S` | `5.0` | jobs |  |
| `WORKER_HEARTBEAT_FILE` | `/tmp/ibx-worker-heartbeat` | jobs | container-local liveness file (compose healthcheck) |
| `LLM_PROVIDER` | `mock` | LLM | one of: auto, gemini, groq, openrouter, ollama, mock, openai_compat. LLM (free only). auto = gemini -> groq -> openrouter -> ollama -> mock (providers without a key are skipped). Measured 2026-10-20 (scripts/eval_rules_capture.py --ipv4 --gemini-model ..., 22 saved rules pages, free tier, evidence/metrics/rules_capture_eval_summary.csv): gemini-3.5-flash field accuracy 0.871 vs mock 0.833, false rules 0 vs 1 (0.99 on the 16 pages it answered; 6 pages got HTTP 503/429 and count as wrong). gemini-3.8-flash mostly timed out (>60 s) or hit 429 that day: 0.591. So with a key, LLM_PROVIDER=auto + GEMINI_MODEL=gemini-3.5-flash beats the mock (failed calls fall back to it). The default stays mock until a person decides (a person confirms every rule anyway). |
| `OLLAMA_URL` | `http://ollama:11434/v1` | LLM |  |
| `LLM_MODEL` | `mock-heuristic-v1` | LLM |  |
| `LLM_PRICE_PER_1K_TOKENS_USD` | `0.0` | LLM | local models are free; used for the per-unit cost in manifests |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8088` | files in/out, certification and the test-post sweeper | how callers reach the API; signed download links start here |
| `FILE_LINK_EXPIRY_S` | `3600` | files in/out, certification and the test-post sweeper | lifetime of signed /api/v1/files links |
| `MEDIA_MAX_BYTES` | `52428800` | files in/out, certification and the test-post sweeper | 50 MiB per uploaded media file |
| `MEDIA_ALLOWED_MIME` | `image/png,image/jpeg,image/gif,image/webp,video/mp4,video/webm,application/pdf` | files in/out, certification and the test-post sweeper |  |
| `MEDIA_FETCH_TIMEOUT_S` | `15.0` | files in/out, certification and the test-post sweeper |  |
| `CERT_OWN_HOSTS` | `lemmy,ghost,discourse,mock-platforms,toxiproxy,localhost,127.0.0.1` | files in/out, certification and the test-post sweeper | certification test posts only ever go to these hosts (own local instances and mocks) |
| `FETCH_LOCAL_HOSTS` | `mock-platforms,lemmy,ghost,discourse,localhost,127.0.0.1` | files in/out, certification and the test-post sweeper | own containers that server-side URL fetches may reach (simulation or dev/test only); never toxiproxy/postgres/s3 |
| `CERT_LIVE_TIMEOUT_S` | `30.0` | files in/out, certification and the test-post sweeper | how long CONFIRM_LIVE / CONFIRM_DELETED poll the live URL |
| `SWEEPER_EVERY_S` | `60` | files in/out, certification and the test-post sweeper | the test-post TTL sweeper's period |
| `CERT_RESCHEDULE_MAX` | `3` | files in/out, certification and the test-post sweeper | a rate-limited / circuit-open run is rescheduled (level kept) at most this many times in a row |
| `CERT_RESCHEDULE_MAX_DELAY_S` | `3600` | files in/out, certification and the test-post sweeper | never wait longer than this for a platform's reset time |
| `SCREENSHOT_URL` | *(empty)* | files in/out, certification and the test-post sweeper | e.g. http://shooter:3000 |
| `SCREENSHOT_URL_REWRITES` | *(empty)* | files in/out, certification and the test-post sweeper | "from=>to" prefixes, e.g. a Lemmy post via lemmy-ui |
| `METRICS_POLL_EVERY_S` | `21600` | metrics, recertification, rules drift, alerts | scheduled metrics poll of recent publications (6 h) |
| `METRICS_POLL_WINDOW_DAYS` | `30` | metrics, recertification, rules drift, alerts | only publications published in this window are polled |
| `METRICS_POLL_MAX` | `200` | metrics, recertification, rules drift, alerts | publications per poll run |
| `RECERT_AFTER_DAYS` | `21` | metrics, recertification, rules drift, alerts | re-test L1+ destinations certified longer ago (< 30 days) |
| `RECERT_TICK_EVERY_S` | `86400` | metrics, recertification, rules drift, alerts | the recertification tick runs daily |
| `RECERT_MAX_PER_TICK` | `100` | metrics, recertification, rules drift, alerts | destinations enqueued per tick (the rest wait for the next tick) |
| `RECERT_CONCURRENCY` | `2` | metrics, recertification, rules drift, alerts | recertification runs at the same time, across all workers |
| `RECERT_JITTER_S` | `600` | metrics, recertification, rules drift, alerts | each run starts 0..JITTER s after the tick (stable per destination and day) |
| `RECERT_RETRY_EXCLUDED_AFTER_S` | `86400` | metrics, recertification, rules drift, alerts | a destination demoted by certification is re-tested after this |
| `RECERT_SELF_HEAL_EVERY_S` | `60` | metrics, recertification, rules drift, alerts | scan circuit_state for open breakers / schema mismatches |
| `RECERT_SELF_HEAL_COOLDOWN_S` | `3600` | metrics, recertification, rules drift, alerts | at most one self-heal run per destination in this window |
| `RULES_DRIFT_ENABLED` | `true` | metrics, recertification, rules drift, alerts | recertification re-hashes the rules sources and flags changes |
| `ALERT_DELIVER_EVERY_S` | `60` | metrics, recertification, rules drift, alerts | deliver new / resolved alerts to the configured sinks |
| `ALERT_WEBHOOK_URL` | *(empty)* | metrics, recertification, rules drift, alerts | HMAC-signed JSON POST per alert event; empty = off |
| `ALERT_WEBHOOK_SECRET` | *(secret; empty)* | metrics, recertification, rules drift, alerts | HMAC key for the alert webhook; defaults to CALLBACK_SECRET/API_TOKEN |
| `GITHUB_TOKEN` | *(secret; empty)* | metrics, recertification, rules drift, alerts | optional: open (and close) a GitHub issue per alert |
| `GITHUB_REPO` | *(empty)* | metrics, recertification, rules drift, alerts | "owner/repo" for the alert issues |
| `GITHUB_API_URL` | `https://api.github.com` | metrics, recertification, rules drift, alerts |  |
| `LLM_TIMEOUT_S` | `30.0` | LLM providers | per call to a hosted provider |
| `LLM_MAX_BACKOFF_S` | `5.0` | LLM providers | a 429 asking to wait longer than this falls through to the next provider |
| `LLM_MAX_INPUT_CHARS` | `12000` | LLM providers | page text sent to a provider (quotes are verified against the full text) |
| `LLM_COOLDOWN_S` | `60.0` | LLM providers | skip a provider this long after it was unreachable or rate-limited |
| `LLM_HTTP_IPV4` | `false` | LLM providers | bind the adapter's own HTTP client to IPv4 (hosts whose IPv6 route times out) |
| `LLM_MAX_OUTPUT_TOKENS` | `16384` | LLM providers | ceiling when a cut-off answer (finish_reason=length) is retried with more |
| `GEMINI_API_KEY` | *(secret; empty)* | LLM providers |  |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai` | LLM providers |  |
| `GEMINI_MODEL` | `gemini-3.8-flash` | LLM providers |  |
| `GEMINI_REASONING_EFFORT` | `low` | LLM providers | Gemini 3.x always thinks; thinking tokens count against max_tokens. reasoning_effort: minimal\|low\|medium\|high (empty = not sent). |
| `GEMINI_MIN_OUTPUT_TOKENS` | `4096` | LLM providers |  |
| `GROQ_API_KEY` | *(secret; empty)* | LLM providers |  |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | LLM providers |  |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | LLM providers |  |
| `OPENROUTER_API_KEY` | *(secret; empty)* | LLM providers |  |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | LLM providers |  |
| `OPENROUTER_MODEL` | `google/gemma-4-31b-it:free` | LLM providers |  |
| `OLLAMA_MODEL` | `qwen2.5:1.5b-instruct` | LLM providers |  |
| `OLLAMA_TIMEOUT_S` | `240.0` | LLM providers | CPU inference is slow |
<!-- END GENERATED: settings -->

Regenerate the tables above after changing errors, settings or manifests: `uv run python scripts/gen_docs.py`
(`--check` is run by `tests/unit/test_docs_generated.py`).

## Documents

| File | What |
|---|---|
| [REPORT.md](REPORT.md) | approaches, what works and what does not, measured numbers, cost, next steps |
| [SOURCES.md](SOURCES.md) | every dependency, image, model, API and data source, with licences |
| [AI_USE.md](AI_USE.md) | how AI coding tools and runtime LLMs were used |
| [docs/REVIEWER_GUIDE.md](docs/REVIEWER_GUIDE.md) | quickstart and one command per hard case |
| [docs/CONTRACT.md](docs/CONTRACT.md), [docs/ERRORS.md](docs/ERRORS.md) | the connector contract as implemented |
| [docs/RULES.md](docs/RULES.md) | posting-rules decision table |
| [docs/CERTIFICATION.md](docs/CERTIFICATION.md), [docs/METRICS.md](docs/METRICS.md) | certification, levels, metrics, recertification |
| [docs/PARTNER_WEBHOOK.md](docs/PARTNER_WEBHOOK.md) | the signed webhook protocol for partner sites |
| [docs/LABELLING_RUBRIC.md](docs/LABELLING_RUBRIC.md) | how the recommender labels were made |
| [docs/VISION.md](docs/VISION.md), [docs/adr/](docs/adr/) | where this goes next; design decisions |
| [docs/CLARIFICATIONS.md](docs/CLARIFICATIONS.md) | questions sent to IncuBrix and the defaults used |
| [evidence/LEDGER.md](evidence/LEDGER.md) | every form row, its status and its evidence |

Licence: MIT ([LICENSE](LICENSE)).
