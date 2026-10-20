# ADR 0002: Postgres is the only datastore (queue, locks, idempotency)

Status: accepted (Day 0).

## Context
- Jobs (IS3), idempotency keys (IS4), publish intents, certification runs and a scheduler that runs on one worker
  only all need durable state and locks.
- Redis + Celery would add a service, a second consistency model, and a race between "job queued" and "key stored".

## Decision
- **One Postgres 16** (`app/db`, `app/jobs`):
  - The job queue uses `SELECT … FOR UPDATE SKIP LOCKED` plus `LISTEN/NOTIFY` (`app/jobs/queue.py`).
  - The job row and its HTTP idempotency row commit in **one transaction**, under an advisory xact lock per
    (scope, key) (`app/reliability/http_idempotency.py`).
  - The scheduler is elected leader with `pg_try_advisory_lock` and uses an injectable clock, which is how the
    recertification clock jump is demonstrated.
  - Breaker, rate-limit state and kill switches are tables, so the api and the worker share them.
- **The connector service owns its database.** Appendix A says contract methods never write to IncuBrix's
  database. Our intents and publish log are this service's own state, and they are needed for idempotency.

## Consequences
- One fewer moving part. This also fits the free demo host, which runs one container plus Supabase Postgres in
  session-pooler mode (LISTEN and session advisory locks need a session connection).
- Throughput is limited by Postgres, which is ample at our volume. At 10× and beyond: partition the queue per
  connector (see VISION.md).
