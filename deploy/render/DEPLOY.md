# Live demo on Render free + Supabase free

The live demo is one Docker web service on Render's free plan, built from `Dockerfile.demo`. It uses a free Supabase
project for Postgres and S3-compatible storage. Everything costs $0, and no card is needed for either service.

## What runs where

| Piece | Where | Notes |
|---|---|---|
| API, job worker, scheduler and mock platforms | **One** Render free container: `Dockerfile.demo`, entry point `app.demo.allinone` | One Python process (Render free has 512 MB RAM and 0.1 CPU). The worker and scheduler run as background tasks in that process. The mock platforms are a second uvicorn server inside it, bound to 127.0.0.1 and reached as `http://mock-platforms:8080` through an in-process host alias. **Simulation only**: no call ever goes to a real site. |
| Postgres 15+ | Supabase free (500 MB), **session pooler** (port 5432), `?sslmode=require` | Session mode is needed because the worker uses `LISTEN/NOTIFY` and the scheduler and migrations hold session advisory locks. Do **not** use the transaction pooler (6543). The direct connection (`db.<ref>.supabase.co`) is IPv6-only, and Render has no outbound IPv6. |
| S3 storage | Supabase Storage S3 endpoint `https://<ref>.storage.supabase.co/storage/v1/s3`, path-style | Holds job manifests, certification evidence and uploads. Bucket `ibx`, private. Links go out through our signed `/api/v1/files/...` route. |

Limits checked on the web on 2026-10-20 (re-check before relying on them):
- **Render free:**
  - The service spins down after 15 min with no inbound HTTP. The next request waits for a cold start.
  - A workspace gets 750 instance hours a month, enough for one service running 24/7.
  - Restarts can happen at any time, and the filesystem is ephemeral (we keep nothing on disk).
- **Supabase free:**
  - 500 MB database and 1 GB storage.
  - A project is **paused after 7 days of inactivity**. The in-process scheduler queries the database every few seconds, so a running demo counts as active.
  - Session-mode clients are capped at the pool size (15 by default). The demo uses at most 7 per instance: a pool of 3 plus 3 overflow, plus 1 LISTEN connection. Two instances overlapping during a deploy still fit.

## Path A: scripted (Render REST API + Supabase Management API)

`scripts/deploy_render.py` reads its secrets only from the environment or the git-ignored `.env`. It never prints
them; every value shows as `<secret:NAME>`. Re-running it is safe, because it reuses a project or service that has
the same name.

Put these in `.env` (mode 600):

| Name | What |
|---|---|
| `RENDER_API_KEY` | Render → Account settings → API keys |
| `SUPABASE_ACCESS_TOKEN` | supabase.com/dashboard/account/tokens |
| `SUPABASE_DB_PASSWORD` | a strong password for the new project's database. It cannot be read back later. |
| `GEMINI_API_KEY` | optional, free tier. Without it the deterministic mock extractor is used. |
| `SUPABASE_ORG_SLUG` / `RENDER_OWNER_ID` | only if the account has several organisations or workspaces |
| `DEMO_GITHUB_REPO` | default `https://github.com/sevandhi/Sevandhi_incrubix`. Render must have access to it (GitHub connected in Render). |
| `DEMO_BRANCH` | optional. The default is the repo's default branch. |

Run the steps in order:

```bash
uv run python scripts/deploy_render.py plan          # dry run: every API call + body, secrets masked, no network
uv run python scripts/deploy_render.py supabase      # create/reuse project 'ibx-destination-network-demo' (ap-southeast-1),
                                                     #   wait ACTIVE_HEALTHY, write SUPABASE_PROJECT_REF, SUPABASE_REGION,
                                                     #   DEMO_DATABASE_URL (session pooler, sslmode=require) into .env
# ONE manual step (Supabase has no public API that creates S3 keys):
#   https://supabase.com/dashboard/project/<ref>/storage/s3 → "New access key"
#   → put SUPABASE_S3_ACCESS_KEY and SUPABASE_S3_SECRET_KEY into .env
uv run python scripts/deploy_render.py check-db      # TLS + session advisory lock + LISTEN/NOTIFY through the pooler
uv run python scripts/deploy_render.py check-storage # bucket 'ibx' head/create + put/get/delete through S3 (path-style)
uv run python scripts/deploy_render.py render        # create/reuse web service (free, singapore, Dockerfile.demo);
                                                     #   generates DEMO_API_TOKEN once; writes RENDER_SERVICE_ID, DEMO_URL
uv run python scripts/deploy_render.py wait          # poll $DEMO_URL/ready, print the first-deploy time
```

The API calls follow the official OpenAPI documents (api.supabase.com/api/v1-json and api-docs.render.com, fetched
2026-10-20):
- `GET /v1/organizations`
- `GET|POST /v1/projects` with `organization_slug`, `db_pass` and `region_selection {type: specific, code}`
- `GET /v1/projects/{ref}`, polled until `status = ACTIVE_HEALTHY`
- `GET /v1/projects/{ref}/config/storage` (`s3Protocol` enabled)
- `GET /v1/projects/{ref}/config/database/pooler`, which gives the `db_user` and `db_host` of the session URL
- Render `GET /v1/owners` and `GET /v1/services?name=`
- `POST /v1/services`, with `type: web_service` and `serviceDetails {runtime: docker, plan: free, region, healthCheckPath, envSpecificDetails {dockerfilePath, dockerContext}}`, plus `envVars` and `autoDeployTrigger: off`
- On a re-run: `PUT /v1/services/{id}/env-vars` and `POST /v1/services/{id}/deploys`

The service shape comes from `render.yaml`, so Path A and Path B deploy the same thing.

## Path B: by hand (dashboard + Blueprint)

1. **Supabase.**
   1. New project, Free plan. Pick the region nearest to Render's (Singapore → `ap-southeast-1`) and save the database password.
   2. **Connect → Session pooler**: copy the URI and append `?sslmode=require`. It looks like `postgresql://postgres.<ref>:<PASSWORD>@aws-*-<region>.pooler.supabase.com:5432/postgres?sslmode=require`. URL-encode special characters in the password.
   3. **Storage → Buckets**: create bucket `ibx`, **private**. The app also creates it if it is missing.
   4. **Storage → S3**: note the endpoint and region, then **New access key** and copy the key id and secret (shown once).
2. **GitHub**: push the repo; it may be private. Render needs access to it.
3. **Render**:
   1. **New → Blueprint** → choose the repo. Render reads `render.yaml`: one free Docker web service with health check `/health`.
   2. When asked, fill in the `sync: false` values: `DATABASE_URL`, `S3_ENDPOINT` and `S3_PUBLIC_ENDPOINT` (both the S3 endpoint), `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` and `GEMINI_API_KEY` (may stay empty).
   3. `API_TOKEN` and `CALLBACK_SECRET` are generated by Render. Read the token under **Environment** (reveal).
4. Wait for the first deploy, which builds the image in about 5–10 min. Then open `https://<service>.onrender.com/ready`; it should return 200 with every check `ok`.

## After the deploy (both paths)

1. **Smoke test**:
   - `curl -s https://<service>.onrender.com/ready` → 200.
   - `curl -s -H "Authorization: Bearer $TOKEN" https://<service>.onrender.com/api/v1/destinations?limit=50` lists the 14 demo destinations. They are certified a minute or two after the first start; watch `level`.
   - Without the token you get 401 `UNAUTHORIZED`.
2. **Keep-alive** (stops the 15-minute spin-down). Set up a free monitor every **10 min** on `GET /health`, using cron-job.org or UptimeRobot (UptimeRobot's free plan is for non-commercial use). A second UptimeRobot monitor on `/ready` every 5 min gives the **uptime record** the brief's Must-have 9 asks for: export it as `evidence/metrics/uptime.csv`. One service running 24/7 uses about 744 of the 750 free hours a month, so do not run a second free service in the same workspace.
3. **Measure the cold start** and give the reviewers the measured number.
   1. Stop the keep-alive for more than 15 min.
   2. Run `time curl -s -o /dev/null -w '%{http_code}\n' https://<service>.onrender.com/ready` and record the seconds.
   3. Turn the keep-alive back on.

   The local rehearsal of this exact shape (below) gives the in-container part. Render adds its own wake-up on top: the official docs say about a minute.
4. **Run the checks against the live URL**. This proves Must-have 9 and Integration Standard points 1–10 on the demo:
   - `API_TOKEN=$TOKEN scripts/integration_selfcheck.sh https://<service>.onrender.com`
   - `uv run python scripts/rehearse.py --api https://<service>.onrender.com --token $TOKEN --mock https://<service>.onrender.com/sim`
5. **Reset** between reviewer sessions, or after a test demoted a destination: `curl -X POST -H "Authorization: Bearer $TOKEN" https://<service>.onrender.com/api/v1/sim/reset`. It returns 202 with a `job_id`. Poll `/api/v1/jobs/{job_id}` until `succeeded`; `result.certification_results` shows every demo destination certified again.
   - The reset deletes the demo destinations and their publish history, then creates and certifies them again.
   - It also drops queued mock faults and clears breakers, rate-limit state and kill switches.
   - Test-post audit rows and alerts are kept.
   - A publication still in flight when you reset fails (its destination is gone), so reset between tests, not during one.

## What to give the reviewers (Submission Form, "Live demo URL")

- **URL**: `https://<service>.onrender.com`. Also give `/docs` (Swagger UI) and `/openapi.json`.
- **Token**: the `API_TOKEN` value. Send it in the submission (form or email), never in the repository. Every `/api/v1` and `/connector/v1` call needs `Authorization: Bearer <token>`.
  - Without the token, only these are open: `/health`, `/ready`, `/docs`, signed file links, the MRSS feed, and `GET /sim/...`.
  - `GET /sim/...` is the read-only view of the simulated platforms, for example `GET /sim/_counts` for the platform request counters.
- **Rate limit**: 300 requests per minute per client IP, then 429 `API_RATE_LIMITED` with `Retry-After`.
  - It covers `/api/v1`, `/connector/v1` and `/sim`.
  - `TRUSTED_PROXIES=10.0.0.0/8` believes `X-Forwarded-For` only from Render's internal proxy hop.
  - If Render's hop were outside that range, all callers would share one bucket. That still limits abuse, just more coarsely.
- **Cold start**: state the number measured in step 3. With the keep-alive running there is normally none.
- **Mode**: say that the demo runs in **simulation**. Every connector posts to built-in mock platforms (one per connector type), never to a real community. The hard-case destinations are `dest-weekly-cap-1`, `dest-no-promo`, `dest-ai-disclosure-es`, `dest-partner-revocable` and `dest-strict-limits`, plus `demo-<connector>` for each connector.
- **Fault injection**: hidden tests such as "a platform renames a field" use `POST /api/v1/sim/faults` (token required).

## Updating the demo

`autoDeployTrigger: off`, so a push never restarts the demo by surprise. To ship a change: push, then **Manual Deploy**
in Render (or `uv run python scripts/deploy_render.py render`, which re-applies the env and deploys). Each deploy
starts a fresh container. The database is kept, migrations run under the advisory lock, and the demo rows are only
created if missing.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/ready` 503, `database` not ok | Wrong URL (transaction pooler 6543, or the IPv6 direct host), password not URL-encoded, or the Supabase project is paused (restore it in the dashboard). |
| `/ready` 503, `storage` not ok | Wrong S3 endpoint or region, keys revoked, or S3 disabled under Storage → S3. |
| Deploy fails at start with "refuses to start with the default dev secret(s)" | `API_TOKEN` was not generated or set. |
| "does not resolve to 127.0.0.1" at start | uvicorn was started without `--loop asyncio`. Use `scripts/demo_start.sh`. |
| Many 429s from a test harness | All its calls come from one IP. Raise `RATE_LIMIT_PER_MIN` in Render's environment. |
| Out of memory | Lower `WORKER_CONCURRENCY` to 2. The measured peak is in `evidence/integration/DEMO_local_rehearsal.txt`. |

## Local rehearsal of this exact shape

`uv run python scripts/demo_rehearsal.py` builds `Dockerfile.demo` and runs it with `--cpus 0.1 --memory 512m`, using
these stand-ins:
- `postgres:16-alpine` behind PgBouncer in **session** mode, with pool 15 and TLS required (for the Supabase session pooler)
- SeaweedFS S3, path-style (for Supabase Storage)

It measures:
- the first boot (until `/ready`, and until all 14 demo destinations are certified)
- a restart, which is what a Render spin-up is
- memory

It then runs `selfcheck.py` and `rehearse.py` against the container, calls the reset, scans the log for secrets, and
removes everything afterwards. Results go to `evidence/integration/DEMO_local_rehearsal.*`.
