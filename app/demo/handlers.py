"""Job handler ``demo.seed``: optionally reset, then seed + certify the demo destinations.

Payload: ``{"reset": bool, "wait_s": float}``. With ``wait_s`` > 0 the job waits (with progress) until the
certification runs it queued have finished, so a 202 from ``POST /api/v1/sim/reset`` followed by a job poll tells the
caller exactly when the demo destinations are certified again. Simulation only (``SIMULATION_ONLY`` otherwise).
"""

from __future__ import annotations

from typing import Any

from app.contract.registry import get_registry
from app.demo import seed as demo_seed
from app.jobs import JobContext, job_handler


@job_handler(demo_seed.SEED_JOB_KIND, max_attempts=3)
async def run_demo_seed(ctx: JobContext) -> dict[str, Any]:
    demo_seed.refuse_unless_allowed(ctx.settings)
    registry = get_registry()
    result: dict[str, Any] = {}
    if ctx.payload.get("reset"):
        await ctx.progress(2, "resetting the demo destinations and the mock platforms")
        result["reset"] = await demo_seed.reset(ctx.session_factory, registry, ctx.settings)
    await ctx.progress(5, "seeding the demo destinations")
    seeded = await demo_seed.seed(ctx.session_factory, registry, ctx.settings, now=ctx.clock.now())
    result.update(seeded)
    wait_s = float(ctx.payload.get("wait_s") or 0)
    if wait_s > 0 and seeded["certifications"]:
        result["certification_results"] = await demo_seed.wait_for_runs(
            ctx.session_factory, seeded["certifications"], wait_s, progress=ctx.progress)
    if seeded["errors"]:
        ctx.warn(f"{len(seeded['errors'])} demo row(s) could not be created: {', '.join(sorted(seeded['errors']))}")
    return result
