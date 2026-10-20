"""Post metrics, normalised (Outstanding OUT2; docs/METRICS.md).

    POST /api/v1/metrics/fetch     {publication_id} or {destination_id, platform_post_id} → 200 one stored snapshot
    POST /api/v1/metrics/poll      {destination_id?, limit?} → 202 job ``metrics.poll`` (also runs on a schedule)
    GET  /api/v1/metrics           stored snapshots, newest first (?publication_id= &destination_id= &connector_key=)
    GET  /api/v1/metrics/mapping   which platform field becomes which normalised key (null = not exposed)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.v1.jobs import accepted
from app.db.session import SessionDep
from app.jobs import queue
from app.metrics import normalize as nm
from app.metrics import service

router = APIRouter(tags=["metrics"])


class MetricsFetchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_id: str | None = Field(default=None, max_length=64, description="A published publication.")
    destination_id: str | None = Field(default=None, max_length=64, description="With platform_post_id: any post on it.")
    platform_post_id: str | None = Field(default=None, max_length=255)
    since: datetime | None = Field(default=None, description="Passed to fetch_metrics; most platforms only have lifetime totals.")

    @model_validator(mode="after")
    def _target(self) -> MetricsFetchIn:
        if not self.publication_id and not (self.destination_id and self.platform_post_id):
            raise ValueError("send publication_id, or destination_id with platform_post_id")
        return self


class MetricsPollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_id: str | None = Field(default=None, max_length=64)
    limit: int | None = Field(default=None, ge=1, le=1000)
    callback_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)


@router.post("/api/v1/metrics/fetch", summary="Fetch one post's metrics now, normalised and stored")
async def fetch_metrics(body: MetricsFetchIn, request: Request) -> dict[str, Any]:
    state = request.app.state
    common = {"session_factory": state.session_factory, "settings": state.settings, "storage": state.storage,
              "clock": state.clock, "registry": state.registry, "since": body.since}
    if body.publication_id:
        return await service.fetch_for_publication(publication_id=body.publication_id, **common)
    return await service.fetch_snapshot(destination_id=body.destination_id or "", platform_post_id=body.platform_post_id or "",
                                        **common)


@router.post("/api/v1/metrics/poll", status_code=202, summary="Snapshot every recent publication (background job)")
async def poll_metrics(body: MetricsPollIn, request: Request, session: SessionDep) -> JSONResponse:
    job = await queue.enqueue(session, "metrics.poll", body.model_dump(exclude={"callback_url"}, exclude_none=True),
                              now=request.app.state.clock.now(), callback_url=body.callback_url,
                              correlation_id=getattr(request.state, "correlation_id", None),
                              idempotency_key=request.headers.get("Idempotency-Key"))
    await session.commit()
    return accepted(request, job.id)


@router.get("/api/v1/metrics", summary="Stored metrics snapshots, newest first")
async def list_metrics(session: SessionDep, publication_id: str | None = None, destination_id: str | None = None,
                       connector_key: str | None = None, limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    items = await service.list_snapshots(session, publication_id=publication_id, destination_id=destination_id,
                                         connector_key=connector_key, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/api/v1/metrics/mapping", summary="Platform field → normalised metric, per connector")
async def metrics_mapping() -> dict[str, Any]:
    return {
        "keys": list(nm.KEYS),
        "null_means": "the platform does not expose this metric (never a fake 0)",
        "connectors": {key: {metric: {"field": src.field, "note": src.note or None} for metric, src in mapping.items()}
                       for key, mapping in nm.MAPPING.items()},
        "no_metrics_capability": nm.NO_METRICS,
    }
