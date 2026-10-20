"""Jobs API (IS3): ``POST /api/v1/jobs`` (public kinds only) → 202 ``{job_id}``; ``GET /api/v1/jobs/{id}``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.errors import ErrorEnvelope
from app.db.session import SessionDep
from app.errors import ErrorCode, IbxError
from app.jobs import queue
from app.jobs.context import public_job
from app.jobs.registry import HANDLERS

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class JobIn(BaseModel):
    kind: str = Field(description="A public job kind, e.g. 'selfcheck'.")
    payload: dict[str, Any] = Field(default_factory=dict)
    callback_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)


class JobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str


class JobView(BaseModel):
    """``GET /api/v1/jobs/{job_id}`` (IS3). ``result.links`` are signed download links."""

    job_id: str
    kind: str
    status: str = Field(description="queued | running | succeeded | failed")
    progress: int = Field(ge=0, le=100)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    attempts: int | None = None


def accepted(request: Request, job_id: str) -> JSONResponse:
    """The standard 202 for every job-creating endpoint."""
    url = f"/api/v1/jobs/{job_id}"
    return JSONResponse({"job_id": job_id, "status": "queued", "status_url": url}, status_code=202, headers={"Location": url})


@router.post("", status_code=202, response_model=JobAccepted, summary="Start a background job (public kinds)")
async def create_job(body: JobIn, request: Request, session: SessionDep) -> JSONResponse:
    spec = HANDLERS.get(body.kind)
    if spec is None or not spec.public:
        public = sorted(k for k, s in HANDLERS.items() if s.public)
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "This is not a public job kind.", details={"public_kinds": public})
    job = await queue.enqueue(
        session,
        body.kind,
        body.payload,
        now=request.app.state.clock.now(),
        callback_url=body.callback_url,
        correlation_id=getattr(request.state, "correlation_id", None),
        idempotency_key=request.headers.get("Idempotency-Key"),
        max_attempts=request.app.state.settings.JOB_MAX_ATTEMPTS,
    )
    await session.commit()
    return accepted(request, job.id)


@router.get("/{job_id}", summary="Job status, progress, result links and error",
            responses={200: {"model": JobView}, 404: {"model": ErrorEnvelope}})
async def get_job(job_id: str, request: Request, session: SessionDep) -> dict[str, Any]:
    job = await queue.get_job(session, job_id)
    return await public_job(job, request.app.state.storage, request.app.state.settings)
