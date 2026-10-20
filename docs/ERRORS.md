# Errors

Every error from every route (contract and `/api/v1`) has the same shape. The code list below is generated from
`app/errors.py` (`CATALOGUE`), and each connector's mapping is generated from its manifest, so neither can drift
from the code. Regenerate with `uv run python scripts/gen_docs.py` (a test runs `--check`).

## 1. The envelope

```json
{"error": {"code": "DEST_RULE_FREQUENCY_CAP",
           "message": "DEST_RULE_FREQUENCY_CAP: cap of 1 post(s) per P7D reached ...; next slot 2026-10-27T07:25:48Z",
           "retryable": true,
           "details": {"retry_policy": "later", "retry_at": "2026-10-27T07:25:48Z", "...": "..."}}}
```

- `code` is UPPER_SNAKE_CASE. The first eight codes are the brief's shared connector codes; the rest are ours,
  in the same style.
- `retryable` says whether the same request can succeed later without a change. `details.retry_policy` says how:
  `never`, `later` (at `details.retry_at`), `backoff`, `after_reconnect`, `next_window`.
- **429, 409 and 503 answers get a `Retry-After` header** (seconds) whenever `details` has `retry_at` or
  `retry_after_s`. A second post inside a 1-per-week cap gets `Retry-After: 604800`.
- **Nothing the caller sent is echoed.** A validation error lists only `loc`, `type` and `msg` for each field
  (`details.errors`), never the input value. Unknown connector, job or destination ids are not repeated in the
  message. A 500 hides the exception text and returns `details.correlation_id` (find it in the logs).
- Secrets are redacted in messages, details and logs (bearer tokens, API keys, JWTs, `xox*-` tokens, passwords in
  URLs, and every live secret value the service knows).
- Platform errors keep the platform's own short message in `details.platform_message` (redacted, at most 300
  characters), the status in `details.platform_status`, and which manifest entry matched in
  `details.error_map_key`.

## 2. All codes

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

`GET /api/v1/errors` returns the same list as JSON from the running service.

## 3. How a platform answer becomes a code

`BaseConnector.map_error` (in `app/connectors/base.py`) reads the connector's manifest `error_map`, in this order:

1. a `"<status>:<token>"` key whose token **equals** one of the JSON error fields of the body (`error`,
   `error_type`, `code`, `errors[]`, `type`); for example Lemmy's `{"error": "rate_limit_error"}` with status 400;
2. a `"<status>:<token>"` key whose token appears **anywhere** in the raw body (non-JSON or nested formats);
3. a plain `"<status>"` key;
4. otherwise the default for that status:

| Platform status, not in the manifest | Code | Retryable | Counts toward the circuit breaker |
|---|---|---|---|
| 429 | `PLATFORM_RATE_LIMIT` | yes, at the reset time | no |
| 401, 403 | `CONNECTION_AUTH_EXPIRED` (a stored connection is paused) | after reconnect | no |
| 5xx, 408 | `PLATFORM_UNAVAILABLE` | yes | yes |
| any other 4xx | `PLATFORM_CONTENT_REJECTED`, with `details.unmapped=true` and a log warning | no | no |

Other failures:

| Failure | Code | Notes |
|---|---|---|
| connect error, connect timeout, pool timeout | `PLATFORM_UNAVAILABLE` | `details.reason=connect_failed`, `outcome=not_sent` |
| read timeout after the request was sent | `PLATFORM_UNAVAILABLE` | `details.reason=timeout`; on a publish `outcome=unknown` (the retry reconciles by marker) |
| 5xx or 408 answer to a create | `PLATFORM_UNAVAILABLE` | `outcome=unknown` on a publish |
| the platform answer does not fit our strict response model (a renamed or missing field) | `PLATFORM_UNAVAILABLE` | `details.reason=response_schema_mismatch`, `details.fields=[...]`; certification fails and the destination is demoted (hard case 5) |
| circuit open (5 outages in a row on this instance) | `PLATFORM_UNAVAILABLE` | `details.circuit=open`; no platform call |
| a known rate-limit block | `PLATFORM_RATE_LIMIT` | `details.short_circuit=true`; no platform call |

The rate-limit reset time is read from `Retry-After` (seconds or HTTP date), `X-RateLimit-Reset` (epoch or
delta), the IETF `RateLimit` header, or body hints such as Discourse's `extras.wait_seconds`. Lemmy sends no
header, so the Lemmy connector reads the site's own rate-limit config (`details.reset_source=lemmy_site_config`).
With no hint at all, `RATE_LIMIT_DEFAULT_RETRY_S` (60 s) is used and `reset_source=default`.

## 4. Each connector's mapping (generated from the manifests)

<!-- BEGIN GENERATED: connector_errors -->
| Connector | Platform answer (`status` or `status:body_token`) | Shared code |
|---|---|---|
| `assisted` | *(none: base defaults only)* | |
| `devto` | `401` | `CONNECTION_AUTH_EXPIRED` |
| `devto` | `403` | `CONNECTION_AUTH_EXPIRED` |
| `devto` | `400` | `PLATFORM_CONTENT_REJECTED` |
| `devto` | `422` | `PLATFORM_CONTENT_REJECTED` |
| `devto` | `429` | `PLATFORM_RATE_LIMIT` |
| `discourse` | `403:invalid_access` | `CONNECTION_AUTH_EXPIRED` |
| `discourse` | `403` | `CONNECTION_AUTH_EXPIRED` |
| `discourse` | `401` | `CONNECTION_AUTH_EXPIRED` |
| `discourse` | `400:invalid_parameters` | `PLATFORM_CONTENT_REJECTED` |
| `discourse` | `400` | `PLATFORM_CONTENT_REJECTED` |
| `discourse` | `422` | `PLATFORM_CONTENT_REJECTED` |
| `discourse` | `429` | `PLATFORM_RATE_LIMIT` |
| `ghost` | `400` | `PLATFORM_CONTENT_REJECTED` |
| `ghost` | `401` | `CONNECTION_AUTH_EXPIRED` |
| `ghost` | `403` | `CONNECTION_AUTH_EXPIRED` |
| `ghost` | `413` | `PLATFORM_CONTENT_REJECTED` |
| `ghost` | `422` | `PLATFORM_CONTENT_REJECTED` |
| `ghost` | `429` | `PLATFORM_RATE_LIMIT` |
| `lemmy` | `400:rate_limit_error` | `PLATFORM_RATE_LIMIT` |
| `lemmy` | `400:not_logged_in` | `CONNECTION_AUTH_EXPIRED` |
| `lemmy` | `400:incorrect_login` | `CONNECTION_AUTH_EXPIRED` |
| `lemmy` | `400:site_ban` | `CONNECTION_AUTH_EXPIRED` |
| `lemmy` | `400:email_not_verified` | `CONNECTION_AUTH_EXPIRED` |
| `lemmy` | `400:registration_application_is_pending` | `CONNECTION_AUTH_EXPIRED` |
| `lemmy` | `400:invalid_post_title` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:invalid_body_field` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:invalid_url` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:blocked_url` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:slurs` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:couldnt_find_community` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:banned_from_community` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:only_mods_can_post_in_community` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:language_not_allowed` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `400:couldnt_create_post` | `PLATFORM_UNAVAILABLE` |
| `lemmy` | `400` | `PLATFORM_CONTENT_REJECTED` |
| `lemmy` | `401` | `CONNECTION_AUTH_EXPIRED` |
| `lemmy` | `403` | `CONNECTION_AUTH_EXPIRED` |
| `lemmy` | `429` | `PLATFORM_RATE_LIMIT` |
| `mrss` | *(none: base defaults only)* | |
| `peertube` | `400:invalid_grant` | `CONNECTION_AUTH_EXPIRED` |
| `peertube` | `401` | `CONNECTION_AUTH_EXPIRED` |
| `peertube` | `400` | `PLATFORM_CONTENT_REJECTED` |
| `peertube` | `403` | `PLATFORM_CONTENT_REJECTED` |
| `peertube` | `413` | `PLATFORM_CONTENT_REJECTED` |
| `peertube` | `415` | `PLATFORM_CONTENT_REJECTED` |
| `peertube` | `429` | `PLATFORM_RATE_LIMIT` |
| `slack` | `200:token_revoked` | `CONNECTION_AUTH_EXPIRED` |
| `slack` | `200:token_expired` | `CONNECTION_AUTH_EXPIRED` |
| `slack` | `200:invalid_auth` | `CONNECTION_AUTH_EXPIRED` |
| `slack` | `200:not_authed` | `CONNECTION_AUTH_EXPIRED` |
| `slack` | `200:account_inactive` | `CONNECTION_AUTH_EXPIRED` |
| `slack` | `200:missing_scope` | `CONNECTION_AUTH_EXPIRED` |
| `slack` | `200:not_allowed_token_type` | `CONNECTION_AUTH_EXPIRED` |
| `slack` | `200:team_access_not_granted` | `CONNECTION_AUTH_EXPIRED` |
| `slack` | `429` | `PLATFORM_RATE_LIMIT` |
| `slack` | `200:ratelimited` | `PLATFORM_RATE_LIMIT` |
| `slack` | `200:rate_limited` | `PLATFORM_RATE_LIMIT` |
| `slack` | `200:message_limit_exceeded` | `PLATFORM_RATE_LIMIT` |
| `slack` | `200:msg_too_long` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:msg_blocks_too_long` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:no_text` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:invalid_blocks` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:invalid_metadata_format` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:metadata_too_large` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:channel_not_found` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:not_in_channel` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:is_archived` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:restricted_action` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:restricted_action_read_only_channel` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:ekm_access_denied` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:no_permission` | `PLATFORM_CONTENT_REJECTED` |
| `slack` | `200:internal_error` | `PLATFORM_UNAVAILABLE` |
| `slack` | `200:fatal_error` | `PLATFORM_UNAVAILABLE` |
| `slack` | `200:service_unavailable` | `PLATFORM_UNAVAILABLE` |
| `slack` | `200:request_timeout` | `PLATFORM_UNAVAILABLE` |
| `webhook` | `400:timestamp_outside_tolerance` | `PLATFORM_UNAVAILABLE` |
| `webhook` | `400` | `PLATFORM_CONTENT_REJECTED` |
| `webhook` | `401` | `CONNECTION_AUTH_EXPIRED` |
| `webhook` | `403` | `CONNECTION_AUTH_EXPIRED` |
| `webhook` | `409` | `PLATFORM_CONTENT_REJECTED` |
| `webhook` | `413` | `PLATFORM_CONTENT_REJECTED` |
| `webhook` | `422` | `PLATFORM_CONTENT_REJECTED` |
| `webhook` | `429` | `PLATFORM_RATE_LIMIT` |
<!-- END GENERATED: connector_errors -->

Notes:
- Slack answers errors as HTTP 200 with `{"ok": false, "error": "..."}`; any answer whose `ok` is not true goes
  through `map_error`, so its keys are `"200:<code>"`.
- Discourse answers both "bad key" and "no permission in this category" with 403 `invalid_access`. On a 403 to a
  create the connector probes `GET /session/current.json`: a working key → `PLATFORM_CONTENT_REJECTED` with
  `details.reason=category_permission`, and the connection stays active.
- Ghost auth errors add a hint to check the Admin API key and the server clock (Ghost tokens are valid for
  5 minutes from their `iat`).

## 5. The Core error matrix (6 connectors × 3 errors)

Evidence: `evidence/levels/core/CORE3_error_matrix.csv` and `.log`, written by `scripts/prove_core3.sh` against a
running stack. Each cell sends a real request that makes the platform (or our mock of it) fail in its own way,
then checks the code, `retryable`, the reset time and that the next call behaves correctly (short-circuit after a
429; a paused connection fails fast after an auth error).

| Connector | Expired / revoked token | Rate limit | Rejected content |
|---|---|---|---|
| Lemmy | **real local instance**: a logged-out JWT → 401 `incorrect_login` → 424 | **real local instance**: post limit lowered to 1 per 10 s → 400 `rate_limit_error` → 429, reset from the site config | **real local instance**: 250-character title → 400 `invalid_post_title` → 422 |
| Ghost | **real local instance**: unknown Admin key → 401 → 424 | mock (429 + `Retry-After: 90`): the real Ghost Admin API never returned a 429 locally | **real local instance**: 300-character title → 422 `ValidationError` → 422 |
| Discourse | mock: revoked key → 403 `invalid_access` → 424 | mock: the forum's own topic limiter → 429 + `Retry-After` + `extras.wait_seconds` | mock: duplicate title → 422 `errors[]` |
| Dev.to | mock: revoked key → 401 → 424 | mock: 429 with `Retry-After` as an HTTP date | mock: 140-character title → 422 "Title is too long" |
| Webhook | mock partner: access revoked → 401 → 424, connection paused | mock partner: 429 + `X-RateLimit-Reset` + `Retry-After` | mock partner: 350-character title → 422 |
| Assisted | **N/A**: `auth: none`; a person posts with their own account, so no token of ours can expire | **N/A**: no platform API is called; the destination's frequency cap still applies before a pack is built | a person reports `rejected_by_community` via `POST /api/v1/assist-tasks/{id}/complete` → the publication fails with `PLATFORM_CONTENT_REJECTED` and the reason |

The mocks copy the real platforms' recorded answers where we have them (`tests/fixtures/recorded/`, recorded from
our local Lemmy 0.19.20, Ghost 6.70 and Discourse 2026.10). Dev.to has no recorded fixture: its mock follows the
public Forem v1 documentation, because no real dev.to key was used.

## 6. Adding a mapping

1. Write the test first: in the connector's unit tests, build an `httpx.Response` with the platform's status and
   body (ideally a recorded one) and assert `connector.map_error(response).code`.
2. Add the key to the connector's `manifest.yaml` `error_map`, for example `"503": PLATFORM_UNAVAILABLE` or
   `"400:token_expired": CONNECTION_AUTH_EXPIRED`. Put specific `status:token` keys before the plain status.
3. If the platform sends its reset time in a new way, extend `app/reliability/retry_after.py`.
4. Run the tests: `uv run pytest -q tests/unit tests/contract`.
5. If you added a new `ErrorCode` (append it at the **end** of the enum in `app/errors.py`, with its `CATALOGUE`
   entry), regenerate: `uv run python -m app.contract.manifest`, `uv run python scripts/export_openapi.py`,
   `uv run python scripts/gen_docs.py`.
