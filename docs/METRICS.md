# Metrics: one normalised record for every connector

Outstanding level, OUT2: `fetch_metrics` works on several connectors and is normalised to **views, likes,
comments, shares and clicks** (plus the contract's optional `saves` and `watch_time_s`).

Code: `app/metrics/normalize.py` (record + mapping, the single source of truth), `app/metrics/service.py`
(fetch + store), `app/metrics/handlers.py` (scheduled poll), `app/api/v1/metrics.py` (API).

## The rules

- `null` means **the platform does not expose this metric**. It is never replaced by 0.
- `0` means the platform said 0. (Slack omits `reply_count` when a message has no thread: that is a real 0.)
- Counts are non-negative integers; `watch_time_s` is seconds (float).
- Most platforms only expose lifetime totals; `since` is passed to the connector and a warning
  (`METRICS_LIFETIME_TOTALS`) is stored when it cannot be applied.
- Every stored number is traceable: the service records the platform's own fields that the mapping uses
  (`raw.fields`), recomputes the record from them (`raw.derived`) and stores `raw.consistent`
  (`true` = the connector's numbers equal the recomputed ones). Response bodies are not stored.

## Mapping (generated from `app/metrics/normalize.py`; `uv run python -m app.metrics.normalize`)

<!-- BEGIN GENERATED MAPPING -->
| connector | views | likes | comments | shares | clicks | saves | watch_time_s |
|---|---|---|---|---|---|---|---|
| lemmy | null | `post_view.counts.upvotes` | `post_view.counts.comments` | null | null | null | null |
| discourse | `views` | `like_count` | `posts_count` | null | `post_stream.posts[0].link_counts[].clicks` | null | null |
| peertube | `views` | `likes` | `comments` | null | null | null | `stats/overall.totalWatchTime` |
| slack | null | `messages[].reactions[].count` | `messages[].reply_count` | null | null | null | null |
| mrss | null | null | null | null | `feeds/_clicks/<guid>.json:clicks` | null | null |
| devto | `page_views_count` | `public_reactions_count` | `comments_count` | null | null | null | null |
| ghost | n/a | n/a | n/a | n/a | n/a | n/a | n/a |  <!-- the Admin API has no per-post engagement counts we can read without members analytics -->
| webhook | n/a | n/a | n/a | n/a | n/a | n/a | n/a |  <!-- the partner protocol (docs/PARTNER_WEBHOOK.md) has no metrics call -->
| assisted | n/a | n/a | n/a | n/a | n/a | n/a | n/a |  <!-- a person posts the pack; no API to read -->

Notes per field:

- **lemmy.views**: Lemmy has no view counter
- **lemmy.likes**: upvotes (not score: downvotes are kept in raw)
- **lemmy.shares**: no share/cross-post counter in the API
- **lemmy.clicks**: no link click counter
- **lemmy.saves**: saves are private per user; no count
- **lemmy.watch_time_s**: not a video platform
- **discourse.views**: topic views (Discourse counts one per user/IP per day)
- **discourse.likes**: likes on all posts of the topic
- **discourse.comments**: posts_count - 1 (replies; the first post is ours)
- **discourse.shares**: share-button use is not counted in the API
- **discourse.clicks**: sum over links in our post; null when it has no links
- **discourse.saves**: bookmarks are private; no count
- **discourse.watch_time_s**: not a video platform
- **peertube.shares**: no share counter
- **peertube.clicks**: no click counter
- **peertube.saves**: playlists are not counted
- **peertube.watch_time_s**: seconds; null when the stats endpoint is not readable
- **slack.views**: Slack does not expose message views
- **slack.likes**: sum of all emoji reactions
- **slack.comments**: thread replies; Slack omits the field when there are none (= 0)
- **slack.shares**: no share counter
- **slack.clicks**: no link click counter
- **slack.saves**: saved items are private
- **slack.watch_time_s**: not a video platform
- **mrss.views**: feed readers fetch the whole feed; per-item views are unknowable
- **mrss.likes**: a feed has no reactions
- **mrss.comments**: a feed has no comments
- **mrss.shares**: not knowable from a feed
- **mrss.clicks**: clicks through our own GET /api/v1/r/{id} redirect
- **mrss.saves**: not knowable from a feed
- **mrss.watch_time_s**: not knowable from a feed
- **devto.views**: author-only field of /api/articles/me/all
- **devto.shares**: no share counter
- **devto.clicks**: no click counter
- **devto.saves**: reading-list saves are not in the API
- **devto.watch_time_s**: not a video platform
- **ghost**: no metrics capability (the Admin API has no per-post engagement counts we can read without members analytics); `fetch_metrics` answers 404 `METHOD_NOT_SUPPORTED`.
- **webhook**: no metrics capability (the partner protocol (docs/PARTNER_WEBHOOK.md) has no metrics call); `fetch_metrics` answers 404 `METHOD_NOT_SUPPORTED`.
- **assisted**: no metrics capability (a person posts the pack; no API to read); `fetch_metrics` answers 404 `METHOD_NOT_SUPPORTED`.
<!-- END GENERATED MAPPING -->

A test (`tests/unit/test_p5a_unit.py::test_docs_metrics_md_embeds_the_generated_mapping`) keeps this table equal
to the code.

## API

| Call | What it does |
|---|---|
| `POST /api/v1/metrics/fetch {publication_id}` | one `fetch_metrics` call for a published publication → 200 with the stored snapshot |
| `POST /api/v1/metrics/fetch {destination_id, platform_post_id}` | the same for any post on a destination (e.g. a contract publish) |
| `POST /api/v1/metrics/poll {destination_id?, limit?}` | 202 job `metrics.poll` (see below) |
| `GET /api/v1/metrics?publication_id=&destination_id=&connector_key=&limit=` | stored snapshots, newest first |
| `GET /api/v1/metrics/mapping` | the mapping above as JSON |

The fetch runs the full contract pipeline (kill switch, stored connection, rate-limit state, breaker, error
mapping). A connector without the metrics capability answers 404 `METHOD_NOT_SUPPORTED`; a post that is gone
answers 404 `NOT_FOUND`.

**Scheduled poll** (`metrics.poll`, every `METRICS_POLL_EVERY_S` = 6 h): every publication in state `published`,
not a test post, published within `METRICS_POLL_WINDOW_DAYS` (30), on a destination that is not `excluded` and
whose connector has the metrics capability; at most `METRICS_POLL_MAX` (200) per run, 4 at a time.

## `metrics_snapshots` (read by the recommender)

One row per fetch. Never updated; the newest row per `(destination_id, platform_post_id)` is the current value.

| Column | Type | Meaning |
|---|---|---|
| `id` | bigint | snapshot id |
| `connector_key` | text | e.g. `lemmy` |
| `destination_id` | text | the destination |
| `publication_id` | text, nullable | the publication (null for a post fetched by `platform_post_id`) |
| `platform_post_id` | text | the platform's id |
| `metrics` | jsonb | **exactly** `{views, likes, comments, shares, clicks, saves, watch_time_s}`; ints / float / null |
| `raw` | jsonb | provenance, see below |
| `captured_at` | timestamptz | the app clock at fetch time |

`raw` keys: `source` (`real_local_instance` = our own Lemmy/Discourse/PeerTube container, `mock` = mock-platforms or
simulation mode, `own_service` = our MRSS feed and click redirect, `real_platform`), `simulation` (bool),
`supported` (the keys this connector can fill), `fields` (the platform's raw fields used), `derived`, `consistent`,
`calls` (method, path, HTTP status of each platform call), `warnings`, `since`, `normalizer_version`,
`connector_version`.

Suggested use: engagement = likes + comments + shares + clicks over the non-null keys
(`app.metrics.normalize.engagement`); `GET /api/v1/network/health` reports it per destination as
`yield.engagement_per_post` from the newest snapshot of each post. Only rows with `source` other than `mock`
should be described as real engagement.

## Evidence (OUT2)

`evidence/levels/out/OUT2_metrics.csv`, written by `scripts/p5a_out2_metrics.py` against a running stack: one row
per connector and key with the raw field, the raw value, the normalised value and the source. Real values come only
from legitimate actions by the instance admin on our own local instances (described in the CSV's `engagement_action`
column); no fake accounts, no fake engagement.

## Recertification, alerts and network health (OUT3)

See `app/recert/scheduler.py` (module docstring). In short: a daily tick re-tests L1+ destinations whose
certification is older than `RECERT_AFTER_DAYS` (21, must be < 30); self-heal runs follow an open breaker or a
schema mismatch; a failure demotes to L0 + `excluded` and opens a `certification_failed` alert; a pass restores
the level and resolves it. Recertification also re-hashes the rules sources (rules drift → rule set
`needs_review` + `rules_drift` alert). Alerts go to the log, an HMAC-signed webhook (`ALERT_WEBHOOK_URL`, same
signature scheme as job callbacks) and optionally a GitHub issue (`GITHUB_TOKEN` + `GITHUB_REPO`).
`python -m app.recert tick --now 2026-11-20T09:00:00Z` is the clock-jump demo; `GET /api/v1/network/health` and
`/ui/network` show trust level, last certification, failure streak, rules version and yield.
Evidence: `evidence/levels/out/OUT3_recert.log` (`scripts/p5a_out3_recert.py`).

Note: the `app.recert` CLI needs the database, so on the compose stack run it inside a container:
`docker compose exec worker python -m app.recert tick --now <iso> [--dry-run]`.

## The six numbers of the form's Metrics tab: where each one comes from

Every number below is written by a script into the evidence file shown. None is typed by hand. The values are
quoted in `REPORT.md` §5 and listed with their status in `evidence/LEDGER.md`.

| Form metric | How it is measured | Script (re-run) | Evidence file |
|---|---|---|---|
| Certification: destinations certified per connector, time per run | one certification run per Core connector on the mocks plus the real local Lemmy and Ghost; `duration_ms` from the run record | `scripts/prove_mh2.sh` (+ `scripts/export_cert.py --all-latest`) | `evidence/metrics/certification_runs.csv`, packs in `evidence/certification/` |
| Reliability: publish success in simulation and on local instances; duplicates under retry | timeout-after-create, `create_then_502`, 8 parallel same-key requests and toxiproxy delays; posts counted **on the platform** (mock counters, real Lemmy/Ghost API) | `scripts/prove_mh6.sh`, `scripts/prove_mh7.sh` (simulation run), `scripts/load_publish.py` | `evidence/metrics/duplicates_under_retry.csv`, `evidence/integration/M6_parallel_publish.txt`, `evidence/must_haves/MH7_sim_run.log` |
| Rule safety: violations caught vs missed | 113 table cases (`tests/rules/rule_cases.yaml`) through the real engine; exits 1 on any miss | `uv run python scripts/rule_safety.py` | `evidence/metrics/rule_safety.csv` |
| Catalogue: rows imported, duplicates, errors, time for 1,000 rows | a fresh generated 1,000-row CSV against the mock internet (latency, hangs, robots, bad URLs, duplicates), three runs | `uv run python scripts/prove_adv1.py --api … --token …` | `evidence/metrics/catalogue_import.csv`, `evidence/levels/adv/ADV1_*` |
| Recommendation: precision of the top 10 | P@10 on the 10 test items, weights tuned on dev only; **labels agent-drafted, pending human review** | `uv run python -m app.recommender.eval --print` | `evidence/levels/out/OUT1_precision.json` |
| Cost per 1,000 publications | sum of `cost.total_usd` in the job manifests (LLM tokens × `LLM_PRICE_PER_1K_TOKENS_USD`, platform APIs $0). The publication path makes no LLM call, so the result is $0; the calculation is in `REPORT.md` §6 | `scripts/prove_mh9.sh` (IS9 manifest) | `evidence/integration/IS9_manifest.json` |

Unsupported values are `null`, never 0 (the same rule as the metrics record above). A number that depends on
agent-drafted labels or on unconfirmed rules says so wherever it is quoted.
