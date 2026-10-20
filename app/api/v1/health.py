"""Health, readiness and the error-code catalogue (IS5, IS8).

* ``GET /health``: the process is up (no dependencies touched).
* ``GET /ready``: database, job queue, object storage, connector registry, routers, and mock-platforms
  (simulation only). 200 when all pass, otherwise 503 ``DEPENDENCY_UNAVAILABLE`` with every check listed.
* ``GET /api/v1/errors``: every error code with HTTP status, retryability and meaning.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import func, select, text

from app.api.errors import ErrorEnvelope
from app.db.tables import JobRow
from app.errors import CATALOGUE, ErrorCode, IbxError
from app.reliability.redact import redact

router = APIRouter()


@router.get("/health", tags=["health"], summary="Liveness: the process is up")
async def health(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return {"status": "ok", "version": settings.SERVICE_VERSION, "git_sha": settings.GIT_SHA}


async def _check_db(request: Request) -> dict[str, Any]:
    factory = request.app.state.session_factory
    if factory is None:
        return {"ok": False, "error": "not configured"}
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            queued = (await session.execute(select(func.count()).select_from(JobRow).where(JobRow.status == "queued"))).scalar()
        return {"ok": True, "queued_jobs": queued}
    except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
        return {"ok": False, "error": redact(type(exc).__name__)}


@router.get("/ready", tags=["health"], summary="Readiness: database, queue, storage, registry",
            responses={503: {"model": ErrorEnvelope, "description": "A dependency is not ready (DEPENDENCY_UNAVAILABLE)"}})
async def ready(request: Request) -> dict[str, Any]:
    state = request.app.state
    checks: dict[str, Any] = {}
    db = await _check_db(request)
    checks["database"] = {"ok": db["ok"], **({"error": db["error"]} if "error" in db else {})}
    checks["queue"] = {"ok": db["ok"], "queued_jobs": db.get("queued_jobs")}
    storage = state.storage
    checks["storage"] = {"ok": bool(storage is not None and await storage.ping())}
    registry = state.registry
    checks["connectors"] = {"ok": not registry.errors, "registered": registry.keys(), "errors": registry.errors}
    checks["routers"] = {"ok": not state.router_errors, "errors": state.router_errors}
    if state.settings.SIMULATION_MODE:
        try:
            response = await state.http.get(f"{state.settings.MOCK_PLATFORM_URL.rstrip('/')}/health", timeout=3)
            checks["mock_platforms"] = {"ok": response.status_code == 200}
        except Exception as exc:  # noqa: BLE001
            checks["mock_platforms"] = {"ok": False, "error": type(exc).__name__}
    failing = sorted(name for name, check in checks.items() if not check["ok"])
    if failing:
        raise IbxError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            f"Not ready: {', '.join(failing)}.",
            details={"checks": checks},
        )
    return {"status": "ready", "checks": checks}


@router.get("/api/v1/errors", tags=["errors"], summary="Error-code catalogue")
async def error_catalogue() -> dict[str, Any]:
    return {
        "envelope": {"error": {"code": "UPPER_SNAKE_CASE", "message": "str", "retryable": "bool", "details": "object"}},
        "codes": [
            {
                "code": code.value,
                "http_status": info.http_status,
                "retryable": info.retryable,
                "retry_policy": info.retry_policy,
                "meaning": info.meaning,
            }
            for code, info in CATALOGUE.items()
        ],
    }
