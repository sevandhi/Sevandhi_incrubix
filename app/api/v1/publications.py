"""Publications: post one content item to one destination, with the rules checked first (Must-have 4).

    POST /api/v1/publications                 202 {job_id, publication_id, publication{state, reason, code, retry_at}},
                                              or 409 with the rule's error envelope when a rule blocks the post
    GET  /api/v1/publications/{id}
    GET  /api/v1/publications?destination_id=
    POST /api/v1/publications/{id}/delete     202 job
    POST /api/v1/assist-tasks/{id}/complete   a person posted the assisted pack (id = publication id)

The rules gate runs at once, under a per-destination lock: a blocked post gets a 409 with the rule's error
envelope (an excluded destination: DESTINATION_EXCLUDED; a destination below L1, i.e. never certified:
DESTINATION_NOT_CERTIFIED, checked after the rules so a rule block names the rule) (``details.publication_id`` names the stored publication); otherwise the 202 says ``pending`` or
``rescheduled`` (with ``retry_at``). The job then tailors and publishes (or, for a rescheduled post,
re-checks and publishes at ``retry_at``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.errors import ErrorEnvelope
from app.catalogue import service
from app.catalogue.models import AssistCompleteIn, PublicationIn
from app.certification import publications as pubs
from app.contract.router import get_connector
from app.db.session import SessionDep
from app.db.tables import PublicationRow, new_id
from app.errors import ErrorCode, IbxError
from app.jobs import queue

router = APIRouter(tags=["publications"])


def _accepted(job_id: str, out: dict[str, Any]) -> JSONResponse:
    url = f"/api/v1/jobs/{job_id}"
    body = {"job_id": job_id, "status": "queued", "status_url": url, "publication_id": out["publication_id"],
            "publication_url": f"/api/v1/publications/{out['publication_id']}", "publication": out}
    return JSONResponse(body, status_code=202, headers={"Location": url})


@router.post("/api/v1/publications", status_code=202, summary="Publish content to a destination (rules first)",
             responses={409: {"model": ErrorEnvelope, "description": "A posting rule blocks this post (the rule's code)"}})
async def create_publication(body: PublicationIn, request: Request, session: SessionDep) -> JSONResponse:
    state = request.app.state
    now = state.clock.now()
    dest = await service.get_destination(session, body.destination_id)
    manifest = state.registry.manifest(dest.connector_key)
    if manifest is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, f"Unknown connector '{dest.connector_key}'.")
    content = (await service.create_content(session, body.content, storage=state.storage) if body.content is not None
               else await service.get_content(session, body.content_id))
    assisted = manifest.tier == "assisted" or ("assist" in manifest.capabilities and "publish" not in manifest.capabilities)
    pub_id = new_id("pub")
    pub = PublicationRow(id=pub_id, content_id=content.id, destination_id=dest.id, idempotency_key=pubs.intent_key(pub_id),
                         state="pending", is_test=False, attempts=0, extra={"tailor": body.tailor})
    session.add(pub)
    await pubs.lock_destination_slot(session, dest.destination_key)
    at = max(now, body.scheduled_for) if body.scheduled_for else now
    if dest.status == "excluded":
        pubs.block_for_destination(pub, dest)
    else:
        rules = await service.confirmed_rules(session, dest.id)
        ruleset = pubs.ruleset_for(dest, rules, assisted=assisted)
        rendition = service.rendition_for(content, dest.surface, state.settings)
        history = await pubs.publish_history(session, dest, exclude_publication=pub_id)
        decision = pubs.evaluate(rendition, ruleset, history, at, list(content.owner_domains or []))
        pubs.apply_decision(pub, decision, at=at)
        if pub.state != "blocked" and pubs.not_certified(dest):
            # No real post before the destination is certified. A rule block is reported first,
            # because it is the more useful reason (the post would be refused even after certification).
            pubs.block_for_destination(pub, dest)
    if pub.state == "blocked":
        # A rule (or an excluded destination) says no -> a synchronous 409 with the rule's envelope.
        # The publication row is kept as the audit record (its id is in details); no job is queued.
        await session.commit()
        request.state.idempotency_keep_error = True  # keep the row and replay this 409 for the same Idempotency-Key
        raise pubs.blocked_error(pub)
    run_after = pub.scheduled_for if pub.state in ("rescheduled", "pending") and pub.scheduled_for else now
    job = await queue.enqueue(session, "publication.run", {"publication_id": pub_id}, now=now, run_after=max(run_after, now),
                              callback_url=body.callback_url, correlation_id=getattr(request.state, "correlation_id", None),
                              idempotency_key=request.headers.get("Idempotency-Key"))
    pubs.add_job(pub, job.id)
    await session.flush()
    await session.refresh(pub)
    out = await pubs.publication_out(pub, state.settings)
    await session.commit()
    return _accepted(job.id, out)


@router.get("/api/v1/publications/{publication_id}", summary="A publication's state, reason and result")
async def get_publication(publication_id: str, request: Request, session: SessionDep) -> dict[str, Any]:
    pub = await pubs.get_publication(session, publication_id)
    return await pubs.publication_out(pub, request.app.state.settings)


@router.get("/api/v1/publications", summary="List publications")
async def list_publications(request: Request, session: SessionDep, destination_id: str | None = None,
                            state: str | None = None, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    stmt = select(PublicationRow)
    if destination_id:
        stmt = stmt.where(PublicationRow.destination_id == destination_id)
    if state:
        stmt = stmt.where(PublicationRow.state == state)
    rows = (await session.execute(stmt.order_by(PublicationRow.created_at.desc()).limit(limit))).scalars().all()
    return {"publications": [await pubs.publication_out(r, request.app.state.settings) for r in rows]}


@router.post("/api/v1/publications/{publication_id}/delete", status_code=202, summary="Delete a published post")
async def delete_publication(publication_id: str, request: Request, session: SessionDep) -> JSONResponse:
    pub = await pubs.get_publication(session, publication_id, lock=True)
    if not pub.platform_post_id:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"Publication '{publication_id}' has no platform post (state {pub.state}).",
                       http_status=409)
    job = await queue.enqueue(session, "publication.delete", {"publication_id": publication_id}, now=request.app.state.clock.now(),
                              correlation_id=getattr(request.state, "correlation_id", None))
    pubs.add_job(pub, job.id)
    await session.flush()
    await session.refresh(pub)
    out = await pubs.publication_out(pub, request.app.state.settings)
    await session.commit()
    return _accepted(job.id, out)


@router.post("/api/v1/assist-tasks/{publication_id}/complete",
             summary="Report what happened to an assisted post: posted, rejected/removed by the community, or abandoned")
async def complete_assist_task(publication_id: str, body: AssistCompleteIn, request: Request, session: SessionDep) -> dict[str, Any]:
    """``posted`` → published; ``rejected_by_community`` → failed (PLATFORM_CONTENT_REJECTED, the reason as the
    platform's message); ``removed_by_community`` (after posting) → removed_by_platform; ``abandoned`` → abandoned,
    which frees the frequency-cap slot. The pack's files are deleted once the task ends."""
    pub = await pubs.get_publication(session, publication_id, lock=True)
    now = request.app.state.clock.now()
    allowed = {"posted": ("awaiting_human",), "rejected_by_community": ("awaiting_human",), "abandoned": ("awaiting_human",),
               "removed_by_community": ("awaiting_human", "published")}[body.outcome]
    done = {"posted": "published", "removed_by_community": "removed_by_platform"}.get(body.outcome)
    if done is not None and pub.state == done:
        return await pubs.publication_out(pub, request.app.state.settings)  # the same report twice is harmless
    if pub.state not in allowed:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED,
                       f"Publication '{publication_id}' is '{pub.state}'; outcome '{body.outcome}' needs {' or '.join(allowed)}.",
                       http_status=409)
    pub.live_url = body.live_url or pub.live_url
    pub.platform_post_id = body.platform_post_id or pub.platform_post_id
    extra = {**(pub.extra or {}), "outcome": body.outcome, "completed_by": body.completed_by, "completion_note": body.note}
    if body.outcome == "posted":
        pub.state, pub.published_at, extra["reason"] = "published", now, None
    elif body.outcome == "abandoned":
        pub.state, extra["reason"] = "abandoned", body.reason
    else:
        pub.state = "failed" if body.outcome == "rejected_by_community" else "removed_by_platform"
        pub.published_at = pub.published_at or (now if body.outcome == "removed_by_community" else None)
        err = IbxError(ErrorCode.PLATFORM_CONTENT_REJECTED, f"The community {body.outcome.replace('_by_community', '')} the post: {body.reason}",
                       details={"outcome": body.outcome, "platform_message": body.reason, "reported_by": body.completed_by,
                                "source": "human_report"})
        pub.error, extra["reason"] = err.to_body(), body.reason
    pub.extra = extra
    marker = (extra.get("assist_pack") or {}).get("marker")
    if marker and not extra.get("assist_pack_purged"):  # the task has ended: the pack's files are no longer needed
        dest = await service.get_destination(session, pub.destination_id)
        purge = getattr(get_connector(request, dest.connector_key), "purge_pack", None)
        if purge is not None:
            pub.extra = {**pub.extra, "assist_pack_purged": len(await purge(marker))}
    await session.flush()
    await session.refresh(pub)
    out = await pubs.publication_out(pub, request.app.state.settings)
    await session.commit()
    return out
