"""Certification runs (plan §6) and the test-post audit (Must-have 5).

    POST /api/v1/certifications            {destination_id} → 202 {job_id, run_id}
    GET  /api/v1/certifications/{run_id}   state, steps, result, evidence download links
    GET  /api/v1/certifications            ?destination_id=
    GET  /api/v1/test-posts                every certification test post: created → removed, lifetime in seconds,
                                           counted per outcome (deleted / soft_deleted / unpublished_to_draft / ...)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.catalogue import service
from app.catalogue.models import CertificationIn
from app.certification.harness import TERMINAL_STATES, create_run, latest_runs
from app.certification.levels import OUTCOMES, removal_outcome
from app.db.session import SessionDep
from app.db.tables import CertificationRunRow, TestPostRow
from app.errors import ErrorCode, IbxError
from app.jobs import queue
from app.storage.links import signed_download_url

router = APIRouter(tags=["certification"])


def _iso(moment: Any) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def run_out(run: CertificationRunRow, settings: Any) -> dict[str, Any]:
    result = dict(run.result or {})
    files = result.pop("evidence_files", None) or {}
    return {
        "run_id": run.id,
        "destination_id": run.destination_id,
        "trigger": run.trigger,
        "state": run.state,
        "passed": result.get("passed") if run.state in TERMINAL_STATES else None,
        "failure_code": run.failure_code,
        "level_after": result.get("level_after"),
        "steps": run.steps or [],
        "result": result or None,
        "evidence_prefix": run.evidence_prefix,
        "evidence": {name: signed_download_url(key, settings) for name, key in files.items()},
        "job_id": run.job_id,
        "duration_ms": run.duration_ms,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "created_at": _iso(run.created_at),
    }


@router.post("/api/v1/certifications", status_code=202, summary="Certify a destination (publish → live → status → delete)")
async def start_certification(body: CertificationIn, request: Request, session: SessionDep) -> JSONResponse:
    dest = await service.get_destination(session, body.destination_id)
    run = await create_run(session, dest.id, body.trigger)
    job = await queue.enqueue(session, "certification.run", {"run_id": run.id}, now=request.app.state.clock.now(),
                              callback_url=body.callback_url, correlation_id=getattr(request.state, "correlation_id", None),
                              idempotency_key=request.headers.get("Idempotency-Key"))
    run.job_id = job.id
    await session.commit()
    url = f"/api/v1/jobs/{job.id}"
    return JSONResponse({"job_id": job.id, "run_id": run.id, "status": "queued", "status_url": url,
                         "run_url": f"/api/v1/certifications/{run.id}"}, status_code=202, headers={"Location": url})


@router.get("/api/v1/certifications/{run_id}", summary="A certification run with its evidence links")
async def get_certification(run_id: str, request: Request, session: SessionDep) -> dict[str, Any]:
    run = await session.get(CertificationRunRow, run_id)
    if run is None:
        raise IbxError(ErrorCode.NOT_FOUND, f"Unknown certification run '{run_id}'.")
    return run_out(run, request.app.state.settings)


@router.get("/api/v1/certifications", summary="Recent certification runs")
async def list_certifications(request: Request, session: SessionDep, destination_id: str | None = None,
                              limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    runs = await latest_runs(session, destination_id, limit)
    return {"runs": [run_out(r, request.app.state.settings) for r in runs]}


@router.get("/api/v1/test-posts", summary="Test-post audit (MH5): created → removed, lifetime, outcome per post")
async def test_posts(request: Request, session: SessionDep, limit: int = Query(default=1000, ge=1, le=10000)) -> dict[str, Any]:
    """``deleted`` counts only posts that are gone from the platform (Lemmy purge, Ghost delete,
    webhook delete). ``soft_deleted`` (Discourse), ``unpublished_to_draft`` (Dev.to: no delete API),
    ``pack_revoked`` (assisted: no post exists), ``not_created`` (the write-ahead row of a post that never reached the
    platform), ``already_gone`` and ``unconfirmed`` are separate. ``removed`` = every row that is no longer live."""
    rows = (await session.execute(select(TestPostRow).order_by(TestPostRow.created_at.desc()).limit(limit))).scalars().all()
    now = request.app.state.clock.now()
    items = []
    for r in rows:
        end = r.deleted_at
        items.append({
            "id": r.id, "connector_key": r.connector_key, "destination_id": r.destination_id, "run_id": r.run_id,
            "platform_post_id": r.platform_post_id, "created_at": _iso(r.created_at), "deleted_at": _iso(end),
            "delete_mode": r.delete_mode, "outcome": removal_outcome(r.delete_mode) if end else "still_live",
            "lifetime_s": round((end - r.created_at).total_seconds(), 3) if end else None,
            "alive_for_s": None if end else round((now - r.created_at).total_seconds(), 3),
        })
    done = [i["lifetime_s"] for i in items if i["lifetime_s"] is not None]
    counts = {name: sum(1 for i in items if i["outcome"] == name) for name in OUTCOMES}
    return {"ttl_s": request.app.state.settings.TEST_POST_TTL_S, "count": len(items), **counts, "removed": len(done),
            "still_live": len(items) - len(done), "max_lifetime_s": max(done) if done else None, "test_posts": items}
