"""Network health (plan §16) and alerts.

    GET /api/v1/network/health   every destination: trust level, last certification, recert due date, failure streak,
                                 rules version/status (drift), yield (metrics) and open alerts; plus totals
                                 (?destination_id= &status= &connector_key= &limit=)
    GET /api/v1/network/alerts   alerts, newest first (?status=open|resolved &kind= &destination_id=)
    GET /ui/network              the page (it calls the API with the bearer token the person types in)
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.db.session import SessionDep
from app.db.tables import AlertRow
from app.recert.health import network_health

router = APIRouter(tags=["network"])


@router.get("/api/v1/network/health", summary="Trust, certification, failure streak, rules and yield per destination")
async def get_network_health(request: Request, session: SessionDep, destination_id: str | None = None,
                             status: str | None = Query(None, pattern="^(active|excluded)$"),
                             connector_key: str | None = None, limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
    state = request.app.state
    return await network_health(session, state.settings, state.registry, state.clock.now(), destination_id=destination_id,
                                status=status, connector_key=connector_key, limit=limit)


@router.get("/api/v1/network/alerts", summary="Alerts (certification failures, rules drift, ...) with their deliveries")
async def get_alerts(session: SessionDep, status: str | None = Query(None, pattern="^(open|resolved)$"), kind: str | None = None,
                     destination_id: str | None = None, limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    stmt = select(AlertRow).order_by(AlertRow.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(AlertRow.status == status)
    if kind:
        stmt = stmt.where(AlertRow.kind == kind)
    if destination_id:
        stmt = stmt.where(AlertRow.destination_id == destination_id)
    items = [{"alert_id": a.id, "kind": a.kind, "status": a.status, "destination_id": a.destination_id, "body": a.body,
              "delivered_to": a.delivered_to, "created_at": a.created_at.isoformat().replace("+00:00", "Z")}
             for a in (await session.execute(stmt)).scalars()]
    return {"items": items, "count": len(items)}


@router.get("/ui/network", response_class=HTMLResponse, include_in_schema=False)
async def network_page() -> HTMLResponse:
    """A static page: no data is rendered on the server; the browser fetches /api/v1/network/health with the token the
    person types in (kept in this tab's sessionStorage, never in the URL)."""
    from app.ui import render

    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; "
           "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
    return HTMLResponse(render("network.html", nonce=nonce),
                        headers={"Content-Security-Policy": csp, "X-Robots-Tag": "noindex", "Cache-Control": "no-store",
                                 "Referrer-Policy": "no-referrer"})
