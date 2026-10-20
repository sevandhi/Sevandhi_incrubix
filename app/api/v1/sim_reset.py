"""``POST /api/v1/sim/reset``: put the live demo back to its starting point.

Bearer token required (like every /api/v1 route). Simulation only: outside SIMULATION_MODE, or with APP_ENV=prod, the
answer is 409 ``SIMULATION_ONLY`` and nothing is touched. The work runs as a ``demo.seed`` job (IS3): the demo
destinations and their publish history are deleted and created again, queued mock faults are dropped, breakers,
rate-limit state and kill switches are cleared, and every demo destination is certified again. Poll the job: it
succeeds once the certification runs have finished (``result.certification_results``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.jobs import JobAccepted, accepted
from app.db.session import SessionDep
from app.demo import seed as demo_seed
from app.jobs import queue

router = APIRouter(tags=["simulation"])

RESET_WAIT_S = 240.0  # how long the job waits for the re-certification runs before it reports what is still running


@router.post("/api/v1/sim/reset", status_code=202, response_model=JobAccepted,
             summary="Demo only: reset + re-seed + re-certify the demo destinations (simulation mode)")
async def reset_demo(request: Request, session: SessionDep) -> JSONResponse:
    settings = request.app.state.settings
    demo_seed.refuse_unless_allowed(settings)
    job = await queue.enqueue(
        session, demo_seed.SEED_JOB_KIND, {"reset": True, "wait_s": RESET_WAIT_S}, now=request.app.state.clock.now(),
        correlation_id=getattr(request.state, "correlation_id", None),
        idempotency_key=request.headers.get("Idempotency-Key"), max_attempts=1,
    )
    await session.commit()
    return accepted(request, job.id)
