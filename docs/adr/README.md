# Architecture decision records

| ADR | Decision |
|---|---|
| [0001](0001-python-fastapi.md) | Python 3.12 + FastAPI + Pydantic v2, with strict platform-response models |
| [0002](0002-postgres-queue.md) | Postgres is the only datastore: queue (SKIP LOCKED), locks, idempotency |
| [0003](0003-write-ahead-publish-intent.md) | Write-ahead publish intent + invisible marker: never double-post |
| [0004](0004-rules-gate-order.md) | Rules gate order: replay → spec → rules; deterministic checks |
| [0005](0005-d9-no-rules-default.md) | Contract publish with no rules anywhere: allowed with `rules_source=none_supplied` |
| [0006](0006-devto-level-2.md) | Dev.to certified at L2, never "deleted" |
| [0007](0007-mocks-and-real-instances.md) | Faithful mocks + real local instances on an internet-less network |
| [0008](0008-free-llm-chain-quote-verification.md) | One free LLM adapter chain, strict JSON, verbatim-quote verification, human confirms |
| [0009](0009-render-supabase-demo.md) | Live demo: Render free + Supabase free, all-in-one, simulation only |
