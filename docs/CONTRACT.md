# The connector contract, as implemented

This is how our service implements the IncuBrix connector contract (the brief's Appendix A). It is written from
the code: `app/contract/` (models, manifest, router, registry) and `app/connectors/base.py` (the pipeline every
connector inherits). When this page and the code disagree, the code wins; please tell us.

## 1. Routes

| Route | What |
|---|---|
| `POST /connector/v1/{key}/{method}` | One route per contract method, with a typed request body (in `openapi.yaml`). |
| `POST /api/v1/connector/{key}/{method}` | The same routes, mirrored under `/api/v1` (Integration Standard point 2). |
| `GET /connector/v1/{key}/manifest` (and the mirror) | The connector's manifest, Appendix A keys only. |
| `GET /api/v1/connectors` | Every registered connector with its manifest. |

- Methods: `validate`, `publish`, `get_status`, `delete`, `fetch_metrics`, `refresh_auth`, `build_assist_pack`.
- Aliases (not in the OpenAPI file): `status` → `get_status`, `metrics` → `fetch_metrics`, `assist` and
  `assist_pack` → `build_assist_pack`.
- Every route needs `Authorization: Bearer <API_TOKEN>`.
- An unknown connector → 404 `CONNECTOR_NOT_FOUND`. A method that is not in the manifest's `capabilities`, or is
  not a contract method → 404 `METHOD_NOT_SUPPORTED`. Both checks run **before** the body is validated.
- Contract methods are synchronous (they answer in the request). Slow work (certification, imports,
  publications) runs as jobs under `/api/v1`.

Example (simulation stack, dev token):

```bash
curl -s -H "Authorization: Bearer ibx-dev-token" http://127.0.0.1:8088/connector/v1/discourse/manifest
```

## 2. Request and response objects (`app/contract/models.py`)

The brief names `rendition`, `destination` and `connection` but does not define them. These are our explicit
models. **Inbound models accept unknown fields** (`extra="allow"`) and a few aliases, so a real IncuBrix payload
is not refused. **Outbound models are strict**: a success body has exactly the Appendix A keys.

| Object | Fields (aliases in brackets) |
|---|---|
| `Rendition` | `rendition_id` (`id`), `content_id`, `surface`, `content_type` (text, image, video, video_link, link), `title`, `body` (`text`, `caption`, `description`), `body_format` (markdown, html, plain), `canonical_url`, `link_url` (`link`, `url`), `media[]` {url, mime, bytes, duration_s, width, height, alt}, `tags[]` (`hashtags`), `mentions[]`, `language` (`lang`), `is_promotional`, `ai_generated` |
| `Destination` | `destination_id` (`id`), `connector_key`, `platform_key`, `surface`, `name`, `url`, `instance` {the connector's `instance_param`, e.g. `base_url`}, `language`, `spec_overrides`, `rules`, plus any Appendix A record field (e.g. `frequency_cap`, `self_promo_allowed`) |
| `Connection` | `connection_id` (`id`), `connector_key`, `auth_type`, `credentials` {name: secret}, `expires_at` |

| Method | Request body | Success body (exact keys) |
|---|---|---|
| `validate` | `{rendition, destination, connection?}` | `{ok, violations[]}` (violation: field, rule, message, limit, actual) |
| `publish` | `{rendition, destination, connection, idempotency_key}` | `{platform_post_id, live_url, status}` |
| `get_status` | `{platform_post_id, connection, destination?}` | `{status, live_url}` |
| `delete` | `{platform_post_id, connection, destination?}` | `{deleted}` |
| `fetch_metrics` | `{platform_post_id, connection, since?, destination?}` | `{views, likes, comments, shares, saves, watch_time_s, clicks}` (`null` = the platform does not expose it, never a fake 0) |
| `refresh_auth` | `{connection, destination?}` | `{expires_at}` |
| `build_assist_pack` | `{rendition, destination}` | `{files[], caption, deep_link, checklist[]}` |

`status` is one of `published`, `draft`, `pending`, `scheduled`, `awaiting_human`, `deleted`,
`removed_by_platform`, `unknown`.

`destination` on the four post methods is optional (our addition). Appendix A calls them with only
`platform_post_id` and `connection`; section 6 explains how we find the instance.

**Diagnostics.** Send `X-IncuBrix-Verbose: 1` to get an extra `_meta` object: connector version, correlation id,
simulation flag, warnings (for example `rules_source=none_supplied`), `intent_state`, `replayed`, `reconciled`,
`delete_mode`, and a per-call IS9 manifest. Without the header the body has only the Appendix A keys.

## 3. The manifest (`app/connectors/<key>/manifest.yaml`, model in `app/contract/manifest.py`)

The format is the brief's: `key`, `version`, `tier`, `auth`, `surfaces[]` {`key`, `content_types`, `spec`},
`rate_limits.per_connection` {`requests`, `per_seconds`}, `capabilities`, `error_map`, `instance_param`.
The model is strict (`extra="forbid"`), so a typo in a manifest is an error, not a silent default.

- `spec` keys used by the spec check: `title_required`, `title_min_chars`, `title_max_chars`, `body_required`,
  `body_min_chars`, `body_max_chars`, `count_unit` (`codepoints` default, `graphemes`, `utf16`, `bytes`),
  `links_allowed`, `tag_max`, `tag_pattern`, `tag_max_chars`, media limits. `count_unit` and `tag_pattern` are
  checked when the manifest loads.
- `error_map` keys are `"<status>"` or `"<status>:<body_token>"` (regex `^[1-5][0-9]{2}(:[A-Za-z0-9_.\-]+)?$`);
  values must be our error codes. Matching order is in [ERRORS.md](ERRORS.md#3-how-a-platform-answer-becomes-a-code).
- `capabilities` must equal the hooks the connector implements (the conformance tests check this).
- After adding an error code, regenerate the JSON schema: `uv run python -m app.contract.manifest`
  (writes `app/contract/manifest.schema.json`; a test fails if it is stale).

A new connector is a folder `app/connectors/<key>/` with `manifest.yaml` and `connector.py` exporting `Connector`
(a `BaseConnector` subclass). The registry finds it at start; there is no central list. Scaffold one with
`uv run python scripts/new_connector.py <key>`.

### Registered connectors (generated from the manifests)

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

## 4. The publish pipeline (`BaseConnector.publish`)

```
kill switch → stored result for this idempotency_key? (replay, or the same error again, or 409 if the body differs)
→ spec checks (422 DEST_SPEC_VIOLATION, platform never called)
→ simulation rewrite → rate-limit state + local token bucket → circuit breaker
→ CLAIM: one short DB transaction under advisory locks on the key and on the destination:
         rules gate (409 / reschedule / required fixes such as the AI disclosure) + write-ahead PENDING intent
→ [RECONCILE: if an earlier attempt's outcome is unknown, look for its marker on the platform first]
→ platform call (_create) → error mapping → breaker and rate-limit bookkeeping → intent SUCCEEDED / FAILED / ...
```

Why this order:

- **Replay before the rules gate.** A retry of a post that already succeeded must get the stored result. If the
  rules ran first, the retry would be blocked by the frequency cap that the post itself used up.
- **Spec before rules.** A post that breaks both gets 422 (spec), not 409 (rule). This is documented in
  `docs/RULES.md`.
- **No DB connection is held during the platform call.** The PENDING row is the lock. A duplicate request sent at
  the same moment sees a fresh PENDING row, waits and then replays the result. A different post to the same
  destination counts the PENDING row in its frequency-cap history, so two concurrent posts cannot both pass a
  1-per-week cap.
- After a crash mid-publish, a PENDING row older than its lease (6 × `PLATFORM_TIMEOUT_S`, 60 s by default) is
  treated as unknown. Until then a retry gets 409 `IDEMPOTENCY_IN_PROGRESS` (retryable).

### Write-ahead intent and the marker

Every publish writes a row in `publish_intents` (key = `idempotency_key`) **before** calling the platform. The
row has a **marker**, `ibx-` plus 12 base32 characters of sha256(idempotency_key), which the connector puts into
the post (see section 9). States: `PENDING`, `SUCCEEDED`, `FAILED`, `RETRYABLE`, `UNKNOWN`.

| What happened on the platform call | Intent | Answer | Next call with the same key |
|---|---|---|---|
| Success | SUCCEEDED | 200 | 200, same body, header `Idempotent-Replayed: true` |
| Timeout after the request was sent | UNKNOWN | 503 `PLATFORM_UNAVAILABLE`, `details.intent_state=UNKNOWN`, message says "safe to retry with the same idempotency_key" | looks for the marker (`_lookup`): found → 200 with that post and the `RECONCILED_EXISTING_POST` warning; not found → a new create |
| 5xx or 408 answer to the create | UNKNOWN | 503 | same as a timeout: a proxy 502/504 or a crash after commit may hide a created post |
| Strict response model fails (renamed field) | UNKNOWN | 503, `details.reason=response_schema_mismatch`, `details.fields` | reconcile by marker |
| `PLATFORM_CONTENT_REJECTED` | FAILED | 422 | the same 422 again (`details.replayed=true`) |
| Rate limit, auth error, connect error (nothing sent) | RETRYABLE | 429 / 424 / 503 | a new attempt (rules checked again) |

If the marker is not visible yet and the earlier attempt is younger than `IDEMPOTENCY_SETTLE_S` (5 s), the retry
gets a retryable 503 instead of creating a second post. Evidence: `evidence/must_haves/MH6_timeout_retry.log`,
`evidence/metrics/duplicates_under_retry.csv`.

### The idempotency key: body vs header (IS4)

- The body `idempotency_key` is the key.
- An `Idempotency-Key` header is used when the body has none.
- Both present and different → 409 `IDEMPOTENCY_KEY_REUSED` with `details.reason=header_body_mismatch`.
- Neither → a key `gen-<uuid>` is generated, echoed in the `Idempotency-Key` response header, and the response
  carries `X-IncuBrix-Warning: IDEMPOTENCY_KEY_GENERATED` (a retry will not be deduplicated).
- The same key with a different body → 409 `IDEMPOTENCY_KEY_REUSED`.
- **Contract routes are not stored by the generic HTTP idempotency layer** (`app/reliability/http_idempotency.py`).
  Publish has its own ledger (above); delete and assist are naturally idempotent; reads are never replayed. Every
  other `POST` under `/api/v1` is covered by the HTTP layer (same key + same body → the stored response).

## 5. Posting rules on the contract path (decision D9)

`publish` and `build_assist_pack` look for rules in this order (`BaseConnector.resolve_rules`,
`app/rules/gate.py:resolve_ruleset`):

1. inline `destination.rules`;
2. the Appendix A record fields on the destination (`frequency_cap`, `self_promo_allowed`, `link_policy`,
   `disclosure_required`, `automation_allowed`, `language`);
3. our catalogue's newest **confirmed** rule set for this destination: by `destination_id`, else by
   `destination_key` (`<connector>|<normalised instance>|<surface>`);
4. none.

Fields from earlier sources win. Invalid rules → 422 `REQUEST_VALIDATION_FAILED` (input never echoed). A misspelt
rule key is refused with a suggestion.

**No rules anywhere:** the post is **allowed** after the spec checks, with the warning
`rules_source=none_supplied` in `_meta.warnings`, `_meta.rules_source=none_supplied` and the log line
`rules_source_none_supplied`. Reason: Appendix A's `publish` carries no rules, and IncuBrix's contract suite will
send its own destination objects; refusing them all would fail Must-have 1. Any rule that **is** supplied is
always enforced.

**The orchestrated path is stricter.** `POST /api/v1/publications` posts only to a destination that has
confirmed rules (else 409 `DEST_RULES_UNCONFIRMED`) and is certified (level L1+, else 409
`DESTINATION_NOT_CERTIFIED`). A rule block is reported before "not certified".

For assisted packs "no automated posting" counts as satisfied (a person posts). A pack is not a post, so it uses
no cap slot, but a pack that breaks another rule is refused with the rule's 409.

Frequency-cap history: SUCCEEDED, PENDING and UNKNOWN intents on the same `destination_key`, newest 200.
Certification test posts never count (excluded by their `cert-<run_id>` key and by post id **on that
destination only**).

## 6. `get_status`, `delete`, `fetch_metrics` with only Appendix A arguments

Appendix A gives these methods `platform_post_id` and `connection`, not the destination, but a multi-instance
connector (Lemmy, Ghost, Discourse ...) needs to know which instance to call. We find it in this order
(`BaseConnector._simple`, `_instance_from_ledger`):

1. `destination.instance.<instance_param>` if the caller sends a destination (optional field);
2. `connection.<instance_param>` or `connection.instance` (extra fields on the connection);
3. **our publish ledger**: the instance this service published `platform_post_id` to.

**Ids that exist on two instances (MH1 fix).** Mock tenants and fresh instances all start at post id 1. When
a create returns an id that this connector already returned for **another** instance, `publish` returns a tagged
id `<platform id>~i<10 hex of the instance>` (`tagged_post_id`). It is still a plain string. The ledger lookup is
then unambiguous, and the hooks always receive the platform's own id (`platform_id` strips the tag). For older
rows with the same id on several instances, the caller's connection (its id, or a fingerprint of its inline
credentials) picks its own newest post; if even that cannot tell them apart, the answer is a clear 422 that names
the field to send.

## 7. Errors

Every failure is `{"error": {"code", "message", "retryable", "details"}}`. See [ERRORS.md](ERRORS.md) for the
codes, the HTTP statuses, the `Retry-After` rules and each connector's mapping.

Breaker: 5 consecutive outages (5xx, 408, timeout, connect error, schema drift) per `connector|instance` open the
circuit for `CIRCUIT_OPEN_S` (300 s); calls then fail fast with 503 `PLATFORM_UNAVAILABLE` and
`details.circuit=open`. A 4xx refusal of one post never counts as an outage.

Rate limits: a 429 (or a platform's own "rate limited" body) stores `blocked_until` per
`connector|connection|instance`; the next call short-circuits with 429 and `details.short_circuit=true`,
without calling the platform. The reset time comes from `Retry-After`, `X-RateLimit-Reset`, the IETF `RateLimit`
header, body hints, or (Lemmy) the site's rate-limit config.

Auth: `CONNECTION_AUTH_EXPIRED` on a stored connection pauses it (`paused_auth`); later calls fail fast with 424
until the owner reconnects.

## 8. Simulation, credentials and secrets

- `SIMULATION_MODE=true` rewrites every destination to mock-platforms (`MOCK_PLATFORM_URL/<connector>/<tenant>`,
  tenant derived from the original URL) and replaces credentials with placeholders from the connector's
  `sim_credentials()`: `sim-<name>` by default; the webhook gets `{"secret": SIM_SECRET}` (the same setting the
  partner mock reads; default `sim-secret`); Ghost gets a well-formed fake Admin key the Ghost mock accepts.
  Nothing real is called and no real key is needed.
- A destination that already points at mock-platforms and has no credentials gets the same placeholders, but only
  in simulation or `APP_ENV=dev|test` (never in a demo/prod local-mode run).
- **Own-instance credential fallback (local mode).** A call with no credentials that targets exactly our own Lemmy
  or Ghost container (or its toxiproxy URL) uses the dev-only credentials the bootstrap wrote to
  `BOOTSTRAP_OUT_DIR`. Any other host gets nothing.
- Stored connections (`POST /api/v1/connections`) are Fernet-encrypted at rest and never returned; only the
  credential names are shown.
- Every live secret is registered with the log redactor; responses, logs and evidence are redacted.
- `scripts/gen_secrets.sh [prod]` writes fresh `API_TOKEN`, `POSTGRES_PASSWORD`, S3 keys, `SECRETS_KEY` and
  `CALLBACK_SECRET` into `.env` (umask 077; keys already there are kept). With `APP_ENV=demo|prod` the api and
  worker refuse to start on the repo's dev placeholders.

## 9. Per-connector notes

| Connector | Marker (how a retry finds an earlier post) | Lookup window | Removal | Metrics | Measured limits and facts |
|---|---|---|---|---|---|
| `lemmy` | invisible markdown line `[//]: # (ibx-…)` at the end of the body | creator's 50 newest posts | soft delete, then admin purge; `delete_mode` `purged` / `soft` / `already_gone` | likes = upvotes, comments | title 3..200, body 50,000 UTF-16 units (manifest 49,971 = minus the marker line); rate limit is **400** `rate_limit_error` with no header, reset read from the site config; a revoked token is 401 `incorrect_login` (measured on 0.19.20) |
| `ghost` | marker at the end of the slug | none: server-side filter `slug:~$'<marker>'` | `DELETE` → 204 (`permanent`) | not offered | title 255; Admin JWT signed per request with the real wall clock (5-minute tokens); no `newsletter` parameter is ever sent |
| `discourse` | `external_id = marker` | none: `GET /t/external_id/<marker>.json` | **soft delete** (staff still see it; public 404), reported as `soft_deleted` | views, likes, comments, clicks | title 15..255, body 20..32,000; a bad key is **403** `invalid_access`; a second delete is 422; real instance only under `--profile full` |
| `devto` | `[//]: # (ibx-…)` at the end of `body_markdown` | pages of `/api/articles/me/all` | **no delete API**: `delete` is not offered (404 `METHOD_NOT_SUPPORTED`); certification unpublishes to draft and never claims `deleted:true` | page views, reactions, comments | title 128, max 4 tags `[a-z0-9]+`; `instance.draft_only: true` for any real dev.to run |
| `webhook` | `Idempotency-Key` header = marker; partner dedups | `GET /posts?idempotency_key=` | `DELETE` (`deleted`) | not offered | every request signed `X-IncuBrix-Signature: t=<ts>,v1=<hmac>`; spec in `docs/PARTNER_WEBHOOK.md` |
| `assisted` | deterministic pack folder `assist/<marker>/` | n/a | pack files purged (`pack_revoked`) | n/a | a person posts; outcome reported via `POST /api/v1/assist-tasks/{id}/complete` (`posted`, `rejected_by_community`, `removed_by_community`, `abandoned`) |
| `slack` | message metadata `{event_type: ibx_publication, event_payload: {marker}}` | 100 newest messages | `chat.delete` | likes = reactions, comments = replies | errors come as HTTP 200 `ok:false`, mapped as `"200:<code>"`; mock only (no real workspace token) |
| `mrss` | item guid = marker | n/a (our own feed) | item removed from our feed | clicks via `GET /api/v1/r/{guid}` | the feed is our service: `GET /api/v1/feeds/{feed_key}/mrss.xml` (public, no token) |
| `peertube` | `[//]: # (ibx-…)` at the end of the description | 25 newest videos | `DELETE` → 204 | views, likes, comments, watch time | measured on real v8.3.0 under `--profile video`; the watch page answers 200 after delete, so deletion is proven by the API |

Liveness on our own Lemmy: once federation is off (every start after the first bootstrap) the backend serves no
page at `ap_id`, so certification confirms the post through the API (`GET /api/v3/post`).

## 10. Job callbacks (IS3)

A job created with a `callback_url` gets one signed POST when it ends:

```
X-IncuBrix-Timestamp: <unix seconds>
X-IncuBrix-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(CALLBACK_SECRET, "<t>." + raw body)>
```

The receiver recomputes the HMAC over `"<t>."` plus the exact body bytes, compares in constant time, and refuses
a timestamp more than 300 s from its clock. The secret is `CALLBACK_SECRET` (defaults to `API_TOKEN`). The old
`sha256=<hex>` form is refused by our verifier (`app/jobs/callbacks.py:verify`). Callback URLs pass the same SSRF
guard as every other server-side fetch.

## 11. Testing the contract

- `uv run pytest -q tests/contract` (conformance kit: manifests, exact keys, capabilities, 404s).
- `scripts/test.sh` runs the whole suite, then a simulation stack with the e2e tests and the self-check.
- `scripts/test.sh --live` brings the stack up in local mode (real Lemmy and Ghost containers) and runs the
  `live_local` tests.
- `scripts/prove_mh1.sh` checks every registered connector over HTTP on a running stack.
