# Certification

No destination counts until it is certified: a real test post is published on **our own** instance (or a mock of
the platform), confirmed live, read back, removed, and the evidence is stored. Code: `app/certification/`
(`harness.py` state machine, `levels.py`, `evidence.py`, `sweeper.py`, `handlers.py`), API
`app/api/v1/certifications.py`.

## 1. Run one

```bash
API=http://127.0.0.1:8088; H="Authorization: Bearer ibx-dev-token"
# one catalogue row (no code, no connection needed for a mock or own-instance URL)
D=$(curl -s -H "$H" -H "Content-Type: application/json" "$API/api/v1/destinations" \
  -d '{"name":"Forum 41","url":"http://mock-platforms:8080/discourse/forum-41","connector_key":"discourse","language":"en"}' | jq -r .destination_id)
RID=$(curl -s -H "$H" -H "Content-Type: application/json" "$API/api/v1/certifications" -d "{\"destination_id\":\"$D\"}" | jq -r .run_id)
curl -s -H "$H" "$API/api/v1/certifications/$RID" | jq '{state, level_after, steps: [.steps[]?.step]}'
```

`POST /api/v1/certifications` answers 202 with `{job_id, run_id, status_url, run_url}`; the run is the job
`certification.run`. `GET /api/v1/certifications/{run_id}` returns the state, the steps, the result and **signed
download links** to every evidence file. `python -m app.catalogue.seed --api URL --certify` certifies every seed row
and waits.

## 2. The state machine

Native, protocol, feed and bridge connectors:

```
QUEUED → PREFLIGHT → VALIDATE → PUBLISH → CONFIRM_LIVE → STATUS → [METRICS] → DELETE → CONFIRM_DELETED → PASSED
```

- `METRICS` runs only when the manifest lists `metrics`.
- A connector without a delete API (Dev.to) ends with `CONFIRM_REMOVED` instead of `CONFIRM_DELETED`, and its
  evidence file is `removal_confirmation.json` (`"deleted": false`), so nothing is ever called a delete that was
  not one.

Assisted connectors (a person posts, so there is no post to make):

```
QUEUED → PREFLIGHT → VALIDATE → BUILD_PACK → CONFIRM_PACK → DELETE (purge the pack's files) → CONFIRM_DELETED (links dead) → PASSED (L1)
```

`CONFIRM_PACK` checks that every signed link downloads, that the deep link answers (fetched only on our own
hosts) and that every confirmed rule appears in the checklist.

What each step proves:

| Step | Check |
|---|---|
| PREFLIGHT | the connector is registered and not disabled; the target is our own instance (see section 3); credentials refresh when the connector supports it |
| VALIDATE | the fixed test post passes the surface's spec checks |
| PUBLISH | contract `publish` with idempotency key `cert-<run_id>`; the test post is recorded in `test_posts` first |
| CONFIRM_LIVE | GET of `live_url` answers 200 and the page contains the run's token or marker. For Lemmy (federation off, no page at `ap_id`), PeerTube (the watch page is a single-page app that answers 200 even after delete) and MRSS (the item URL is on the public base URL the worker cannot reach), the platform API confirms the post instead; the step records which method was used |
| STATUS | contract `get_status` says `published` with the same `live_url` |
| METRICS | contract `fetch_metrics` answers the 7 keys (unsupported ones `null`) |
| DELETE | contract `delete` (or Dev.to's unpublish to draft); the delete mode is recorded |
| CONFIRM_DELETED | `get_status` says `deleted` and (where a page exists) the live URL answers 404/410 |

### Outcomes

| Final state | When | Effect on the destination |
|---|---|---|
| `PASSED` | every step passed | level set (section 4), `last_certified_at` now, status `active`; open `certification_failed` alerts resolved |
| `DEMOTED` | any step failed for a reason about the destination (a 4xx refusal, a schema change, a dead page ...) | `FAILED` → `CLEANUP` (delete the test post by id, or find it by its marker and delete it) → level **L0**, status **excluded**, a `level_history` row and a `certification_failed` alert. Publications to it are refused with 409 `DESTINATION_EXCLUDED` |
| `FAILED_PRECONDITION` | PREFLIGHT refused a host that is not one of our own instances | nothing was sent; level and status unchanged; no alert |
| `RESCHEDULED` | a platform rate limit or quota, or an open circuit breaker, in any step | CLEANUP, then a new run at the platform's reset time (at most `CERT_RESCHEDULE_MAX_DELAY_S`); level kept |
| `INCONCLUSIVE` | `CERT_RESCHEDULE_MAX` (3) reschedules in a row | level kept; a `certification_inconclusive` alert |

A renamed field (hard case 5) fails `PUBLISH` with `response_schema_mismatch`; the run result and the alert name
the field (`failure_reason`, `failure_fields`), CLEANUP removes the orphan post by its marker, and the destination
is demoted. A brand-new L0 destination that fails is excluded too.

## 3. Safety (Must-have 5)

- **Own instances only.** Outside simulation, PREFLIGHT refuses any host that is not in `CERT_OWN_HOSTS`
  (default: `lemmy, ghost, discourse, mock-platforms, toxiproxy, localhost, 127.0.0.1`) with
  `DEST_NOT_OWN_INSTANCE` and sends nothing. The same check runs on the `live_url` before it is fetched. In
  simulation every call goes to mock-platforms anyway. Real catalogue rows therefore stay L0: we never post to
  them.
- **Write-ahead test-post record.** The harness writes `test_posts(platform_post_id="marker:<marker>")` **before**
  publishing and updates it with the real id after. A post that never reached the platform is recorded as
  `not_created`, never as deleted.
- **TTL sweeper.** Job `certification.sweep` runs every `SWEEPER_EVERY_S` (60 s) and removes every test post still
  live after `TEST_POST_TTL_S` (300 s), so nothing a crashed run leaves behind lives past 10 minutes. A failed
  removal opens one `test_post_delete_failed` alert per post and is retried on the next sweep.
- **Neutral test content.** The certification post is fixed text written by us (not AI), posted under its own
  permissive rule set (`CERT_RULES`: automation allowed, no cap, any language). It never uses up the destination's
  frequency cap: test posts are excluded from the cap history by their `cert-<run_id>` key and their post id.
- Evidence files are redacted; signed-link signatures are masked as `sig=***` when exported.

### The test-post audit (`GET /api/v1/test-posts`)

Each removal is counted under its own word, so a soft delete or an unpublish is never reported as a delete:

| Key | Meaning |
|---|---|
| `deleted` | gone from the platform: Lemmy purge, Ghost delete, webhook delete, Slack/PeerTube/MRSS delete |
| `soft_deleted` | Discourse soft delete (staff still see it; the public gets 404) |
| `unpublished_to_draft` | Dev.to: no delete API, so the article is reverted to a draft and its text replaced by a tombstone |
| `pack_revoked` | assisted: the pack's stored files were purged (no post ever existed) |
| `not_created` | the write-ahead row of a post that never reached the platform |
| `already_gone`, `unconfirmed` | removed by someone else; a removal we could not confirm |
| `removed`, `still_live`, `max_lifetime_s` | totals and the longest lifetime |

Each row also has its `outcome`, `delete_mode` (prefixed `cleanup_` or `sweeper_` when CLEANUP or the sweeper
removed it) and `lifetime_s`. `scripts/prove_mh5.sh` turns this into `evidence/must_haves/MH5_*`.

## 4. Levels

From `app/certification/levels.py` (brief: L0 catalogued only … L4 native plus metrics):

| Level | Given when |
|---|---|
| L0 | catalogued only, never certified, or demoted |
| L1 | assisted: the pack was built, its links worked and were then revoked |
| L2 | feed or bridge tier (MRSS), **or** a native connector whose removal is not a real delete |
| L3 | native/protocol: publish, live URL, status and a **real delete** (`deleted`, `soft_deleted` or `purged`) |
| L4 | L3 plus a passing `fetch_metrics` step |

**Dev.to is L2.** The Forem API has no article delete. `/unpublish` needs an admin or moderator; an author can only
revert to draft. Certification proves publish, live URL, status and metrics, then unpublishes (removal mode
`unpublish_to_draft`, or `admin_unpublish` on our own Forem mock with `instance.forem_admin: true`). "Delete
tested" is not proven, so L3 is not claimed and `deleted:true` is never reported. The level reason says so.

Levels reached in simulation by the shipped connectors: Discourse L4 (`soft_deleted`), Lemmy L4 (`purged`),
Ghost L3 (no metrics capability), webhook L3, Dev.to L2, assisted L1, Slack L4 (mock workspace), PeerTube L4,
MRSS L2. On our local real instances: Lemmy L4, Ghost L3, PeerTube L4 (profile `video`). Real Discourse (profile
`full`) passed its live connector tests (publish, lookup, metrics, soft delete), but no certification-harness run on
it is in the evidence. See `evidence/certification/` and `evidence/LEDGER.md` for the runs behind these.

## 5. The evidence pack

Stored in S3 at `certifications/<destination_id>/<run_id>/`:

| File | Content |
|---|---|
| `run.json` | state timeline, durations, harness version, redacted requests and responses |
| `steps/NN_<STEP>.json` | one file per step |
| `manifest.snapshot.yaml` | the connector manifest used |
| `deletion_confirmation.json` (or `removal_confirmation.json`) | how the test post was removed and how that was confirmed |
| `summary.md` | a short human summary |
| `live.png`, `after_delete.png` | screenshots, when the shooter runs |
| `screenshots.json` | what each picture is, or why there is none |

**Screenshots.** Start the stack with `--profile evidence` and set `SCREENSHOT_URL=http://shooter:3000`. The
shooter (`deploy/shooter/`, headless Chromium via Playwright, no host port) takes `live.png` after
`CONFIRM_LIVE`/`CONFIRM_PACK` and `after_delete.png` after `CONFIRM_DELETED`/`CONFIRM_REMOVED`.
`screenshots.json` records for each picture `kind` (`page` = a browser screenshot of the URL; `api_render` = our
own labelled HTML render of a JSON answer, with a red banner "API RESPONSE RENDER - not a screenshot of a web
page"), `url`, `http_status`, `label` and time, or `taken: false` with the reason. Lemmy's backend serves no HTML,
so for a real page start `--profile ui` and set
`SCREENSHOT_URL_REWRITES=http://lemmy:8536=>http://lemmy-ui:1234`. In simulation the pictures show the
mock-platforms pages and are labelled as such. Without `SCREENSHOT_URL` no picture is taken and
`screenshots.json` says so.

**Export into the repo** (downloads through the API's signed links; nothing typed by hand):

```bash
uv run python scripts/export_cert.py --all-latest --api http://127.0.0.1:8088 --token ibx-dev-token
uv run python scripts/export_cert.py --run cert_xxx
uv run python scripts/export_cert.py --destination dest_xxx
```

Files land in `evidence/certification/<connector>/<destination>/<run_id>/`.

## 6. Recertification, self-heal and rules drift (Outstanding)

Code: `app/recert/` (`scheduler.py`, `drift.py`, `health.py`), alerts `app/providers/alerts/`. Details and the
metrics side are in [METRICS.md](METRICS.md).

- **Scheduler.** A daily tick (`RECERT_TICK_EVERY_S`) queues a `recert.run` for every active L1+ destination
  certified more than `RECERT_AFTER_DAYS` ago (21; must stay below 30), oldest first, at most
  `RECERT_MAX_PER_TICK`, with a stable per-destination jitter and at most `RECERT_CONCURRENCY` runs at once across
  workers. A pass keeps or restores the level; a failure demotes and excludes the destination and opens an alert.
- **Retry of demoted destinations.** A destination demoted by certification is re-tested after
  `RECERT_RETRY_EXCLUDED_AFTER_S` (24 h) and restored if it passes. Manual exclusions are never touched.
- **Self-heal.** Every 60 s the scheduler reads `circuit_state` (an open breaker, or a schema-mismatch error) and
  queues a `self_heal` run for the affected destinations (with a cooldown).
- **Rules drift.** Each recertification re-hashes the same sources rules capture reads (Lemmy sidebar, Discourse
  `about.json`, `rules_url`; robots-aware, 1 request/s per host, SSRF-guarded). A change sets the rule set to
  `needs_review` (publications are refused with `DEST_RULES_UNCONFIRMED`) and opens a `rules_drift` alert until a
  person confirms a new version.
- **Alerts** always go to the log; an HMAC-signed webhook (`ALERT_WEBHOOK_URL`) and a GitHub issue
  (`GITHUB_TOKEN` + `GITHUB_REPO`) are opt-in.
- **Try it with a clock jump** (the CLI needs the database, so run it inside the worker container):
  `docker compose exec worker python -m app.recert due --now 2026-11-15T00:00:00Z`, then `... tick --now <iso>`;
  or over HTTP `POST /api/v1/recert/tick {"now": "2026-11-15T00:00:00Z", "dry_run": true}`.
  `GET /api/v1/network/health` and the page `/ui/network` show every destination's level, last certification,
  due date, failure streak, rules status and open alerts.
- Evidence: `evidence/levels/out/OUT3_recert.log` (real Lemmy behind toxiproxy, a Discourse mock with a renamed
  field, and a real rules change: demote, exclude, alert, recover, restore).
