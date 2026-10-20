# Reviewer guide

For an IncuBrix reviewer: how to start the service, find the seeded test destinations, and check each must-have
and hard case with one command. Every command below was run against a fresh simulation stack before it was
written down. Claims and their evidence files are listed in [`evidence/LEDGER.md`](../evidence/LEDGER.md).

You need Docker (with compose), `uv`, `curl` and `jq`. No API keys and no accounts.

## 1. Five-minute quickstart

```bash
git clone https://github.com/sevandhi/Sevandhi_incrubix.git && cd Sevandhi_incrubix   # private repo; reviewer access is granted
SIMULATION_MODE=true docker compose up -d --build --wait            # about 40 s after the images are built
uv run python -m app.catalogue.seed --api http://127.0.0.1:8088 --certify   # 5 hard-case destinations, certified
```

Then set these once in your shell (the rest of this page uses them):

```bash
export API=http://127.0.0.1:8088 MOCK=http://127.0.0.1:8089 TOKEN=ibx-dev-token
H="Authorization: Bearer $TOKEN"; J="Content-Type: application/json"; RUN=$(date +%s)
dest() { curl -s -H "$H" "$API/api/v1/destinations?q=$1" | jq -r '.destinations[0].destination_id'; }
certify() {
  RID=$(curl -s -H "$H" -H "$J" "$API/api/v1/certifications" -d "{\"destination_id\":\"$1\"}" | jq -r .run_id)
  until curl -s -H "$H" "$API/api/v1/certifications/$RID" | jq -e '.state | test("PASSED|DEMOTED|FAILED_PRECONDITION|RESCHEDULED|INCONCLUSIVE")' >/dev/null; do sleep 1; done
  curl -s -H "$H" "$API/api/v1/certifications/$RID" | jq '{run_id, state, level_after, failure_code, why: .result.failure_message}'
}
CAP=$(dest dest-weekly-cap-1); NOPROMO=$(dest dest-no-promo); ES=$(dest dest-ai-disclosure-es)
PARTNER=$(dest dest-partner-revocable); STRICT=$(dest dest-strict-limits)
```

- Only two host ports are published: the API (`IBX_API_PORT`, default 8088) and mock-platforms
  (`IBX_MOCK_PORT`, default 8089). The API docs are at `http://127.0.0.1:8088/docs`; the contract is
  `openapi.yaml`.
- `RUN` makes every mock tenant and idempotency key unique, so you can repeat a command after a new
  `RUN=$(date +%s)`.
- The token is the dev placeholder. With `APP_ENV=demo` or `prod` the service refuses to start on it; run
  `scripts/gen_secrets.sh` first and use the `API_TOKEN` it writes to `.env`.
- `docker compose down -v` removes everything.

### Simulation mode vs local mode

| Mode | Start | What answers |
|---|---|---|
| Simulation (`SIMULATION_MODE=true`) | as above | every destination is rewritten to **mock-platforms** (faithful mocks of Discourse, Lemmy, Ghost, Forem/Dev.to, the HMAC partner, Slack, PeerTube, an "internet" for catalogue imports). No real account, no key. Fault injection with the `X-Sim-Fault` header works. |
| Local (default, `SIMULATION_MODE=false`) | `docker compose up -d --build --wait` | real **Lemmy 0.19.20** and **Ghost 6** containers (bootstrapped automatically, no internet route), toxiproxy in front of them; mocks for the rest. Destinations such as `http://lemmy:8536/c/ibx_test` and `http://ghost:2368` certify for real. |
| Local + real Discourse | add `--profile full` | the real `discourse/discourse` image too (about 2 minutes to start, about 1.3 GB RAM) |
| Local + real PeerTube | `docker compose -f compose.yaml -f deploy/compose/peertube.yaml --profile video up -d --wait` | PeerTube v8.3.0 |
| Screenshots in certification packs | add `--profile evidence` and `SCREENSHOT_URL=http://shooter:3000` | headless Chromium |

Certification never posts outside our own containers: in local mode any other host is refused at PREFLIGHT
(`FAILED_PRECONDITION`, nothing sent).

## 2. The seeded hard-case destinations

`python -m app.catalogue.seed` loads `seed/hard_case_destinations.csv` (and the mock-only placeholder connections
in `seed/mock_connections.json`). Ids are generated, so look them up by name:

```bash
curl -s -H "$H" "$API/api/v1/destinations?q=dest-" | jq -r '.destinations[] | [.destination_id, .name, .level, .status] | @tsv'
```

| Name | Connector | What it is for | Rules |
|---|---|---|---|
| `dest-weekly-cap-1` | discourse | second post inside a weekly cap | `frequency_cap 1/P7D` |
| `dest-no-promo` | discourse | promotional post to a no-promotion community | `self_promo_allowed false` |
| `dest-ai-disclosure-es` | discourse | Spanish community that requires AI disclosure | `language es`, `disclosure_required true`, no self-promo |
| `dest-partner-revocable` | webhook (HMAC partner) | a partner who revokes permission | `5/P1D` |
| `dest-strict-limits` | discourse | title and body over the limit | `spec_overrides {title_max_chars 40, body_max_chars 280}`, `link_policy no_shorteners` |

All five have rules confirmed by an operator (`rules_status=confirmed`, `rules_source=manual`) and reach L4 (L3
for the webhook) with `--certify`.

## 3. Must-haves

| # | One command | What you should see |
|---|---|---|
| MH1 contract | `uv run pytest -q tests/contract` (offline) and `scripts/prove_mh1.sh` (every connector over HTTP on a stack) | all pass; e.g. `curl -s -H "$H" -X POST -H "$J" $API/connector/v1/devto/delete -d '{"platform_post_id":"1","connection":{}}'` → 404 `METHOD_NOT_SUPPORTED` |
| MH2 six Core connectors | `scripts/prove_mh2.sh` | 6 Core runs PASSED (Discourse L4, Lemmy L4, Dev.to L2, Ghost L3, webhook L3, assisted L1), plus the real local Lemmy and Ghost |
| MH3 one row, no code | see "Configuration, not code" below | the new row certifies |
| MH4 rules | hard case 4 below | rescheduled / 409 with the reason |
| MH5 never spam | `curl -s -H "$H" "$API/api/v1/test-posts" \| jq 'del(.test_posts)'` | `still_live: 0`, `max_lifetime_s` a few seconds; deleted / soft_deleted / unpublished_to_draft counted separately |
| MH6 safe retries | hard case 1 below | the platform count stays 1 |
| MH7 secrets + simulation | `scripts/prove_mh7.sh` | canary token found nowhere; gitleaks findings classified |
| MH8 local instances | `docker compose up -d --build --wait` (local mode), then `scripts/test.sh` | every service healthy; the whole test suite and the e2e suite pass |
| MH9 Integration Standard | `uv run python scripts/selfcheck.py --api $API --token $TOKEN --out /tmp/is_selfcheck.txt` | `7/7 passed` (IS2, 3, 4, 5, 6, 8, 10) |

`scripts/prove_*.sh` bring up their own stack unless `IBX_API_URL` points at one, and they **write** to
`evidence/`. `scripts/prove_all.sh` runs everything and regenerates `evidence/LEDGER.md`.

**Configuration, not code (MH3):** a new forum is one row, with no connection:

```bash
D=$(curl -s -H "$H" -H "$J" "$API/api/v1/destinations" -d '{"name":"Forum 41","url":"http://mock-platforms:8080/discourse/forum-41-'$RUN'","connector_key":"discourse","language":"en"}' | jq -r .destination_id)
certify $D        # → "state": "PASSED", "level_after": "L4"
```

The same works as CSV (`Content-Type: text/csv`) and for every connector type: `ghost`, `lemmy`, `devto`,
`webhook`, `assisted` (and `slack`, `peertube`, `mrss`). In simulation a real-looking URL such as
`https://dev.to` is rewritten to the mock, so it is safe; a Dev.to row certifies at **L2** with the reason
"removal was 'unpublish_to_draft', not a delete: L3 is not claimed".

**The batch runner (IS10):**

```bash
uv run python -m app.batch run test_pack/inputs/sample --out /tmp/ibx_batch --api $API --token $TOKEN --strict
# 20/20 cases passed; summary: /tmp/ibx_batch/summary.csv
```

## 4. Hard cases: one command each

Run section 1 first. Outputs are shortened here.

### HC1. A publish that times out, retried with the same idempotency key

```bash
BODY='{"rendition":{"title":"A post whose answer never arrives","body":"The platform saves it, then the answer times out."},"destination":{"connector_key":"discourse","surface":"topic","url":"http://mock-platforms:8080/discourse/hc1-'$RUN'"},"connection":{"connector_key":"discourse"},"idempotency_key":"hc1-'$RUN'"}'
curl -s -H "$H" -H "$J" -H "X-Sim-Fault: timeout_after_create" "$API/connector/v1/discourse/publish" -d "$BODY"   # ~10 s, then 503
curl -s -i -H "$H" -H "$J" "$API/connector/v1/discourse/publish" -d "$BODY"                                      # 200, Idempotent-Replayed: true
curl -s "$MOCK/_counts?platform=discourse&tenant=hc1-$RUN"                                                        # "POST /posts.json": 1
```

The first call: 503 `PLATFORM_UNAVAILABLE`, `intent_state: UNKNOWN`, "outcome unknown, safe to retry with the
same idempotency_key". The retry finds the post by its marker (`GET /t/external_id/<marker>.json`) and returns it.
The platform saw exactly one create. (The returned id may look like `1~i92ff4be869`: post ids start at 1 on every
mock forum, so the id is tagged with its instance; see [CONTRACT.md](CONTRACT.md#6-get_status-delete-fetch_metrics-with-only-appendix-a-arguments).)
On real Lemmy and Ghost the same is proven with toxiproxy holding the answer for 13 s: `scripts/prove_mh6.sh`.

### HC2. A 429 with a reset time; an expired token; content rejected by the platform

```bash
pub() { curl -s -i -H "$H" -H "$J" -H "X-Sim-Fault: $1" "$API/connector/v1/discourse/publish" -d '{"rendition":{"title":"A post that meets a platform error","body":"Testing how the platform error is mapped."},"destination":{"connector_key":"discourse","surface":"topic","url":"http://mock-platforms:8080/discourse/hc2-'$2'-'$RUN'"},"connection":{"connector_key":"discourse"},"idempotency_key":"hc2-'$2'-'$RUN'"}' | grep -iE '^HTTP|^retry-after|^\{'; }
pub "429:30" rl                 # 429 PLATFORM_RATE_LIMIT, Retry-After: 30, details.retry_at, reset_source Retry-After
pub "403:invalid_access" auth   # 424 CONNECTION_AUTH_EXPIRED, retry_policy after_reconnect
pub "422" rej                   # 422 PLATFORM_CONTENT_REJECTED, the platform's message in details.platform_message
```

A second publish to the rate-limited forum before the reset gets 429 with `short_circuit: true` and is not sent
to the platform. Real-platform versions of all three (real Lemmy and Ghost, mocks for the rest) are in
`evidence/levels/core/CORE3_error_matrix.csv` ([ERRORS.md](ERRORS.md#5-the-core-error-matrix-6-connectors--3-errors)).

### HC3. Title or body over the limit; media the destination does not accept

```bash
# contract publish, title over a 40-character limit → 422 DEST_SPEC_VIOLATION with field-level reasons, platform not called
curl -s -H "$H" -H "$J" "$API/connector/v1/discourse/publish" -d '{"rendition":{"title":"This title is much longer than the forty characters this forum allows","body":"A body that is long enough for the forum to accept it."},"destination":{"connector_key":"discourse","surface":"topic","url":"http://mock-platforms:8080/discourse/hc3-'$RUN'","spec_overrides":{"title_max_chars":40}},"connection":{"connector_key":"discourse"},"idempotency_key":"hc3-'$RUN'"}'
# a video file to a forum topic → ok:false, content_type and media[0] violations
curl -s -H "$H" -H "$J" "$API/connector/v1/discourse/validate" -d '{"rendition":{"title":"A short video for the forum","body":"Here is the video file we made for this forum.","content_type":"video","media":[{"url":"https://example.org/clip.mp4","mime":"video/mp4"}]},"destination":{"connector_key":"discourse","surface":"topic","url":"http://mock-platforms:8080/discourse/hc3-'$RUN'"}}'
# the orchestrated path fits the content instead: title cut at a word boundary, the change listed
P=$(curl -s -H "$H" -H "$J" "$API/api/v1/publications" -d '{"destination_id":"'$STRICT'","content":{"title":"Run '$RUN': this title is much longer than the forty characters allowed here","body":"A short body about testing posts to many communities.","language":"en","ai_generated":false}}' | jq -r .publication_id)
sleep 3; curl -s -H "$H" "$API/api/v1/publications/$P" | jq '{state, live_url, warnings: .tailoring.warnings}'   # published; TITLE_TRUNCATED: 80→35 chars at word boundary
```

Titles carry `$RUN` because a Discourse forum refuses a title it has already seen: repeating a command with the
same title gets the platform's own 422 duplicate-title rejection (`PLATFORM_CONTENT_REJECTED`), which is correct.

### HC4. A second post inside the frequency cap; a promotional post to a no-promotion community

```bash
for i in 1 2; do curl -s -H "$H" -H "$J" "$API/api/v1/publications" -d '{"destination_id":"'$CAP'","content":{"title":"How do you test posts to long-tail communities?","body":"We compared three ways to test forum posts end to end. Which one has worked for you?","language":"en","ai_generated":false}}' | jq -c '.publication | {state, code, retry_at, reason}'; done
# 1st: {"state":"pending",...}   2nd: {"state":"rescheduled","code":"DEST_RULE_FREQUENCY_CAP","retry_at":"<first + 7 days>","reason":"... cap of 1 post(s) per P7D reached ..."}
curl -s -H "$H" -H "$J" "$API/api/v1/publications" -d '{"destination_id":"'$NOPROMO'","content":{"title":"Our new course is live this week","body":"Buy my course today with code SAVE50 at https://mycourse.example.com/?utm_source=forum","language":"en","is_promotional":true,"ai_generated":false}}' | jq '.error | {code, message}'
# 409 DEST_RULE_SELF_PROMO, naming every signal that fired (flag, utm, self_reference, cta)
```

`dest-weekly-cap-1` allows one post per week, so on a stack where it was already used both calls are
rescheduled. The contract path enforces the same rules from the destination record: a second
`POST /connector/v1/discourse/publish` with `"frequency_cap":"1/P7D"` on the same forum answers 409
`DEST_RULE_FREQUENCY_CAP` with `Retry-After: 604800`. A promotional post is detected even when
`is_promotional` is false (own links, UTM/affiliate parameters, link shorteners, calls to action in 11 languages).

### HC5. A platform renames a field: certification fails and the destination is demoted

```bash
D=$(curl -s -H "$H" -H "$J" "$API/api/v1/destinations" -d '{"name":"Drift blog '$RUN'","url":"http://mock-platforms:8080/ghost/drift-'$RUN'","connector_key":"ghost","language":"en"}' | jq -r .destination_id)
certify $D                                                   # PASSED, L3
curl -s -H "$J" "$MOCK/_faults" -d '{"platform":"ghost","tenant":"drift-'$RUN'","method":"POST","fault":"rename_field:url->permalink","count":1}'
certify $D                                                   # DEMOTED, L0, why: "response_schema_mismatch: ... posts.0.url ..."
curl -s -H "$H" "$API/api/v1/destinations/$D" | jq '{level, status, excluded_reason}'   # L0, excluded
```

CLEANUP finds the orphan post by its marker and deletes it; a `certification_failed` alert is opened; publications
to the destination are refused with 409 `DESTINATION_EXCLUDED` until a recertification passes.

### HC6. A CSV with duplicates, bad URLs and mixed languages; a partner who revokes permission

```bash
printf '%s\n' 'name,url,connector_key,language' \
  "AI forum,http://mock-platforms:8080/internet/discourse-en-ai-r$RUN,discourse," \
  "AI forum again (case),HTTP://MOCK-PLATFORMS:8080/internet/discourse-en-ai-r$RUN,discourse," \
  "AI forum again (slash + utm),http://mock-platforms:8080/internet/discourse-en-ai-r$RUN/?utm_source=list,discourse," \
  'Broken URL,ht!tp:/not a url,discourse,' \
  "Foro de video,http://mock-platforms:8080/internet/lemmy-es-video-r$RUN,lemmy," \
  "KI Forum,http://mock-platforms:8080/internet/discourse-de-ai-r$RUN,discourse," > /tmp/messy.csv
IMP=$(curl -s -H "$H" -H "Content-Type: text/csv" --data-binary @/tmp/messy.csv "$API/api/v1/catalogue/imports" | jq -r .import_id)
sleep 5; curl -s -H "$H" "$API/api/v1/catalogue/imports/$IMP/report" | cut -d, -f1-4
# 1,ok | 2,duplicate,DUPLICATE_IN_FILE,same platform and URL as row 1 | 3,duplicate,... | 4,invalid,URL_INVALID,the URL contains spaces | 5,ok | 6,ok
```

The report also has the detected language, platform, member count and relevance score for each row
(`?format=json` for JSON). Then the partner:

```bash
curl -s -X POST "$MOCK/partner/dest-partner-revocable/_partner/revoke"
P=$(curl -s -H "$H" -H "$J" "$API/api/v1/publications" -d '{"destination_id":"'$PARTNER'","content":{"title":"News for our partner site","body":"A short update for the partner readers this week.","language":"en","ai_generated":false}}' | jq -r .publication_id)
sleep 3; curl -s -H "$H" "$API/api/v1/publications/$P" | jq '{state, code: .error.error.code, connection_state: .error.error.details.connection_state}'
# {"state":"failed","code":"CONNECTION_AUTH_EXPIRED","connection_state":"paused"}
```

The stored connection is now `paused_auth`, and every later call fails fast without contacting the partner. Run
this one last: undoing the revoke on the mock (`.../_partner/restore`) does not un-pause our connection (the
webhook connector has no `refresh_auth`; a new connection must be stored), so later posts to this partner fail
until then.

### HC7. A non-English community; a destination that requires AI-content disclosure

```bash
curl -s -H "$H" -H "$J" "$API/api/v1/publications" -d '{"destination_id":"'$ES'","content":{"title":"How do you test posts to many communities?","body":"We compared three ways to test forum posts end to end. Which one has worked for you?","language":"en","ai_generated":false}}' | jq '.error | {code, message}'
# 409 DEST_RULE_LANGUAGE: content is in 'en' (declared) but the community requires es; ...
P=$(curl -s -H "$H" -H "$J" "$API/api/v1/publications" -d '{"destination_id":"'$ES'","content":{"title":"¿Cómo probáis las publicaciones en comunidades? ('$RUN')","body":"Comparamos tres formas de probar publicaciones en foros de principio a fin. ¿Cuál os ha funcionado mejor?","language":"es","ai_generated":true}}' | jq -r .publication_id)
sleep 3; curl -s -H "$H" "$API/api/v1/publications/$P" | jq '{state, live_url, warnings: .tailoring.warnings}'
# published; AI_DISCLOSURE_APPENDED: 'es' disclosure added at the bottom
curl -s "$(curl -s -H "$H" "$API/api/v1/publications/$P" | jq -r .live_url | sed "s#http://mock-platforms:8080#$MOCK#")" | grep -o 'Aviso[^<]*'
# Aviso: esta publicación se creó con ayuda de inteligencia artificial (IA).
```

Disclosures exist in en, es, de, fr, pt, hi, ja and ta. Content whose `ai_generated` is not stated is treated as
AI. A destination that requires a disclosure but forbids changing the text answers 409 `DEST_RULE_DISCLOSURE`.

### The full graded rehearsal

`scripts/prove_rehearsal.sh` runs all 51 graded cases in `test_pack/inputs/rehearsal/HC1..HC7_*.yaml` through the
batch runner and scores each 4 / 2 / 0 the way screening does. It writes `evidence/hard_cases/` and
`test_pack/outputs/rehearsal/latest/`.

## 5. The live demo

The demo runs as one all-in-one container on Render (free plan) with a Supabase (free) Postgres, in simulation
mode with the hard-case destinations seeded and certified at start. How it is built: `deploy/render/DEPLOY.md`.

| | |
|---|---|
| URL | https://ibx-destination-network-demo.onrender.com (interactive API docs: https://ibx-destination-network-demo.onrender.com/docs ; network dashboard: https://ibx-destination-network-demo.onrender.com/ui/network) |
| Token | none: the demo is open (`API_TOKEN_REQUIRED=false`, simulation only, nothing reaches a real site); the per-IP rate limit still applies |
| Cold start | the free Render plan stops the service after a period without traffic; the first request then waits while it starts (up to about a minute; the first deploy reached `/ready` in 107 s). Call `GET https://ibx-destination-network-demo.onrender.com/health` first. |
| Rate limit | `RATE_LIMIT_PER_MIN` per client IP (429 `API_RATE_LIMITED`) |

Every command on this page works against the demo after
`export API=https://ibx-destination-network-demo.onrender.com TOKEN=any MOCK=https://ibx-destination-network-demo.onrender.com/sim`. The demo has no separate mock port: its mock
platforms run inside the same process and `/sim/<path>` forwards to them (all methods are open on the demo). The `X-Sim-Fault` header works too.
`POST /api/v1/sim/reset` puts the demo destinations back to their seeded state.

## 6. Known limitations (also in REPORT.md)

- **Dev.to has no delete API.** Certification unpublishes to draft (author path) and the level is **L2**;
  `deleted:true` is never reported. The real dev.to was never called (no key); its mock follows the public Forem
  v1 docs.
- **Real Discourse runs only under `--profile full`** (about 2 minutes to start). The default stack uses a
  Discourse mock built from recorded real responses. Discourse delete is a **soft** delete and is reported as
  `soft_deleted`.
- **Real Slack was never called** (no workspace token); Slack is certified on a mock of the Web API. PeerTube was
  certified on a real local v8.3.0 container (`--profile video`).
- Test posts only ever go to our own containers and mocks. The real catalogue (`catalogue/`) is research only and
  stays at L0; its rules are `proposed` until a person confirms them.
