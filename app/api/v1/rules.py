"""Rules capture with human confirmation (ADV2).

    POST /api/v1/destinations/{id}/rules/capture   202 {job_id}; the job stores a proposal (status "proposed")
    GET  /api/v1/destinations/{id}/rule-proposals  the destination's proposals, newest first
    GET  /api/v1/rule-proposals/{proposal_id}      one proposal: rules, per-field value + verbatim quote + status
    POST /api/v1/rule-proposals/{id}/confirm       a person confirms (optionally editing fields) -> confirmed rule set
    POST /api/v1/rule-proposals/{id}/reject        a person rejects; nothing changes
    GET  /ui/rules/{proposal_id}                   the confirm page (Jinja2): the quotes beside the fields

A proposal is never used for posting. Only ``confirm`` creates a confirmed rule set, which is what publications
and the contract publish read.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.catalogue import rules_capture as rc
from app.catalogue import service
from app.catalogue.models import RulesIn
from app.db.session import SessionDep
from app.db.tables import RuleSetRow
from app.errors import ErrorCode, IbxError
from app.jobs import queue

router = APIRouter(tags=["rules"])


class RulesCaptureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000,
                                  description="Rules page to read instead of the destination's rules_url.")
    text: str | None = Field(default=None, max_length=100_000,
                             description="Rules text pasted by a person (e.g. when robots.txt forbids reading).")
    use_platform_api: bool = Field(default=True, description="Read the Lemmy sidebar / Discourse about.json first.")
    requested_by: str | None = Field(default=None, max_length=255)
    callback_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed_by: str = Field(min_length=1, max_length=255, description="The person who checked the rules.")
    overrides: dict[str, Any] = Field(default_factory=dict,
                                      description="Rule keys the person corrected (any RuleSet spelling).")
    note: str | None = Field(default=None, max_length=2000)


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rejected_by: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)


def _not_pending(row: RuleSetRow) -> IbxError:
    status = {"accepted": "confirmed"}.get(row.status, row.status)
    return IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"This proposal is already {status}.", http_status=409,
                    retryable=False, details={"reason": "proposal_not_pending", "status": status})


@router.post("/api/v1/destinations/{destination_id}/rules/capture", status_code=202,
             summary="Capture a destination's posting rules (proposal for a person to confirm)")
async def capture_rules(destination_id: str, body: RulesCaptureIn, request: Request, session: SessionDep) -> JSONResponse:
    dest = await service.get_destination(session, destination_id)
    if not (body.text or body.rules_url or dest.rules_url or (body.use_platform_api and dest.connector_key in ("lemmy", "discourse"))):
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "Nothing to read: the destination has no rules_url and no "
                       "platform API for rules. Send rules_url or the pasted text.")
    job = await queue.enqueue(session, "rules.capture", {"destination_id": dest.id, **body.model_dump(exclude={"callback_url"})},
                              now=request.app.state.clock.now(), callback_url=body.callback_url,
                              correlation_id=getattr(request.state, "correlation_id", None),
                              idempotency_key=request.headers.get("Idempotency-Key"), max_attempts=2)
    await session.commit()
    url = f"/api/v1/jobs/{job.id}"
    return JSONResponse({"job_id": job.id, "status": "queued", "status_url": url, "destination_id": dest.id},
                        status_code=202, headers={"Location": url})


@router.get("/api/v1/destinations/{destination_id}/rule-proposals", summary="A destination's rule proposals")
async def list_proposals(destination_id: str, session: SessionDep) -> dict[str, Any]:
    dest = await service.get_destination(session, destination_id)
    rows = (await session.execute(select(RuleSetRow).where(RuleSetRow.destination_id == dest.id,
                                                           RuleSetRow.id.like("rprop_%"))
                                  .order_by(RuleSetRow.version.desc()).limit(50))).scalars().all()
    return {"destination_id": dest.id, "proposals": [rc.proposal_out(r) for r in rows]}


@router.get("/api/v1/rule-proposals/{proposal_id}", summary="One rule proposal with its quotes")
async def get_proposal(proposal_id: str, session: SessionDep) -> dict[str, Any]:
    return rc.proposal_out(await rc.get_proposal(session, proposal_id))


async def _decide(session: Any, row: RuleSetRow, decision: dict[str, Any], status: str) -> None:
    row.status = status
    row.evidence = [*(row.evidence or []), {"type": "decision", **decision}]


@router.post("/api/v1/rule-proposals/{proposal_id}/confirm", summary="Confirm a rule proposal (a person)")
async def confirm_proposal(proposal_id: str, body: ConfirmIn, request: Request, session: SessionDep) -> dict[str, Any]:
    row = await rc.get_proposal(session, proposal_id, lock=True)
    if row.status != "proposed":
        raise _not_pending(row)
    dest = await service.get_destination(session, row.destination_id, lock=True)
    rules = {**row.rules, **body.overrides}
    parts = rc.proposal_parts(row)
    now: datetime = request.app.state.clock.now()
    evidence = [
        {"type": "rule_proposal", "proposal_id": row.id, "rules_page_sha256": row.rules_page_sha256,
         "provider": (parts["llm"] or {}).get("provider"), "model": (parts["llm"] or {}).get("model"),
         "edited_fields": sorted(body.overrides), "note": body.note, "proposal_source": row.source,
         "origin": parts["capture"].get("origin", "rules_capture")},
        *[{k: f.get(k) for k in ("field", "status", "applied", "quote", "source_url")} for f in parts["fields"]],
    ]
    # the confirmed set records where its values came from; a person confirmed them either way (also import rows)
    source = row.source if row.source in ("llm", "page", "api_sidebar") else "manual"
    confirmed = await service.put_rules(session, dest, RulesIn(rules=rules, confirmed_by=body.confirmed_by,
                                                               source=source, evidence=evidence), now=now)
    await _decide(session, row, {"action": "confirmed", "by": body.confirmed_by, "at": rc.now_iso(now),
                                 "confirmed_rules_id": confirmed.id, "edited_fields": sorted(body.overrides),
                                 "note": body.note}, "accepted")
    await session.flush()
    await session.refresh(confirmed)
    out = {"proposal_id": row.id, "status": "confirmed", "destination_id": dest.id, "rules": service.rules_out(confirmed)}
    await session.commit()
    return out


@router.post("/api/v1/rule-proposals/{proposal_id}/reject", summary="Reject a rule proposal (a person)")
async def reject_proposal(proposal_id: str, body: RejectIn, request: Request, session: SessionDep) -> dict[str, Any]:
    row = await rc.get_proposal(session, proposal_id, lock=True)
    if row.status != "proposed":
        raise _not_pending(row)
    await _decide(session, row, {"action": "rejected", "by": body.rejected_by,
                                 "at": rc.now_iso(request.app.state.clock.now()), "reason": body.reason}, "rejected")
    await session.commit()
    return {"proposal_id": row.id, "status": "rejected", "destination_id": row.destination_id}


# ------------------------------------------------------------------ confirm page


@router.get("/ui/rules/{proposal_id}", response_class=HTMLResponse, include_in_schema=False)
async def proposal_page(proposal_id: str, session: SessionDep) -> HTMLResponse:
    """The confirm page. It shows public rules text only; the confirm/reject buttons call the API with the bearer
    token the person types in (it is kept in this tab's sessionStorage, never in the URL)."""
    from app.ui import render

    row = await rc.get_proposal(session, proposal_id)
    dest = await service.get_destination(session, row.destination_id)
    current = await service.confirmed_rules(session, dest.id)
    nonce = secrets.token_urlsafe(16)
    html = render("rules_proposal.html", proposal=rc.proposal_out(row), destination=dest,
                  current=service.rules_out(current), nonce=nonce)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; "
           "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
    return HTMLResponse(html, headers={"Content-Security-Policy": csp, "X-Robots-Tag": "noindex",
                                       "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
