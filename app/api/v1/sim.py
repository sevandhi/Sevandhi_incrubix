"""Simulation helpers (sim / dev / demo only): queue mock-platform faults and read request counters."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.errors import ErrorCode, IbxError

router = APIRouter(prefix="/api/v1/sim", tags=["simulation"])


class FaultIn(BaseModel):
    tenant: str = "*"
    method: str = "*"
    fault: str = Field(description="timeout_after_create[:s] | 429:<s> | 401 | 422 | 500 | <status>:<token> | rename_field:a->b")
    count: int = Field(default=1, ge=-1)
    platform: str = "*"
    path: str | None = None


def _mock_url(request: Request) -> str:
    settings = request.app.state.settings
    if not settings.sim_tools_enabled:
        raise IbxError(ErrorCode.SIMULATION_ONLY, "Fault injection is only available in simulation, dev or demo mode.")
    return settings.MOCK_PLATFORM_URL.rstrip("/")


async def _proxy(request: Request, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    url = _mock_url(request) + path
    try:
        response = await request.app.state.http.request(method, url, timeout=5, **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise IbxError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"mock-platforms is unreachable ({type(exc).__name__}).") from exc
    if response.status_code >= 400:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"mock-platforms refused: {response.text[:200]}", http_status=400)
    return response.json()


@router.post("/faults", status_code=201, summary="Queue a fault on mock-platforms")
async def add_fault(body: FaultIn, request: Request) -> dict[str, Any]:
    return await _proxy(request, "POST", "/_faults", json=body.model_dump())


@router.get("/faults", summary="List queued faults")
async def list_faults(request: Request) -> dict[str, Any]:
    return await _proxy(request, "GET", "/_faults")


@router.get("/counts", summary="Requests received by mock-platforms, per platform/tenant/route")
async def counts(request: Request, platform: str | None = None, tenant: str | None = None) -> dict[str, Any]:
    params = {k: v for k, v in {"platform": platform, "tenant": tenant}.items() if v}
    return await _proxy(request, "GET", "/_counts", params=params)
