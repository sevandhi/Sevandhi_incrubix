# ADR 0001: Python 3.12 + FastAPI + Pydantic v2

Status: accepted (Day 0).

## Context
Appendix A allows Python 3.11+ or TypeScript (Node 20+). We need:
- an OpenAPI 3.1 file that matches the running service (IS2);
- strict models for every platform response, so that a renamed field is noticed;
- asynchronous HTTP for importing 1,000 catalogue rows quickly;
- one language for the API, worker, mocks, batch runner and tests, so all of it can be explained live.

## Decision
- **Language and framework:** Python 3.12 managed with uv, FastAPI, Pydantic v2 and pydantic-settings.
- **Database and HTTP:** SQLAlchemy 2 (async) with asyncpg and Alembic, and httpx.
- **Inbound contract models** accept unknown fields (`extra="allow"`). **Outbound bodies** carry exactly the
  Appendix A keys (`app/contract/models.py`).
- **Every connector reads platform answers through strict models** in `platform_models.py`: each field we read is
  required. A missing or renamed field raises `response_schema_mismatch`. On publish the outcome is then UNKNOWN,
  so the retry reconciles by marker. In certification the run fails and the destination is demoted.

## Consequences
- OpenAPI comes from the code. `scripts/export_openapi.py --check` and the self-check keep `openapi.yaml` equal to
  the running service.
- A field rename on a platform cannot pass silently. The hard case "a platform renames a field" is handled by
  construction.
- Rejected: TypeScript/NestJS. It would mean two schema systems to keep in step, and the obvious Lemmy client
  library is AGPL.
