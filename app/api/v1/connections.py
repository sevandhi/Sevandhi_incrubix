"""Connections: credentials for one account on one platform, Fernet-encrypted at rest and never returned.

    POST /api/v1/connections                {connector_key, auth_type, credentials{}, expires_at?, account_handle?, ref?}
    GET  /api/v1/connections                list (credential *names* only)
    GET  /api/v1/connections/{id}
    POST /api/v1/connections/{id}/refresh   the connector's refresh_auth (if it has that capability)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.catalogue import service
from app.catalogue.models import ConnectionIn
from app.contract.models import Connection
from app.contract.router import get_connector
from app.db.session import SessionDep
from app.db.tables import ConnectionRow
from app.errors import ErrorCode, IbxError

router = APIRouter(prefix="/api/v1/connections", tags=["connections"])


async def _get(session: Any, connection_id: str) -> ConnectionRow:
    row = await session.get(ConnectionRow, connection_id)
    if row is None:
        raise IbxError(ErrorCode.CONNECTION_NOT_FOUND, f"Unknown connection '{connection_id}'.")
    return row


@router.post("", status_code=201, summary="Store a connection (credentials encrypted, never returned)")
async def create_connection(body: ConnectionIn, request: Request, session: SessionDep) -> JSONResponse:
    settings = request.app.state.settings
    row = await service.create_connection(session, request.app.state.registry, body, settings.fernet_key())
    await session.refresh(row)
    out = service.connection_out(row)
    await session.commit()
    return JSONResponse(out, status_code=201, headers={"Location": f"/api/v1/connections/{row.id}"})


@router.get("", summary="List connections (no secrets)")
async def list_connections(session: SessionDep, connector_key: str | None = None,
                           limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    stmt = select(ConnectionRow)
    if connector_key:
        stmt = stmt.where(ConnectionRow.connector_key == connector_key)
    rows = (await session.execute(stmt.order_by(ConnectionRow.created_at).limit(limit))).scalars().all()
    return {"connections": [service.connection_out(r) for r in rows]}


@router.get("/{connection_id}", summary="One connection (no secrets)")
async def get_connection(connection_id: str, session: SessionDep) -> dict[str, Any]:
    return service.connection_out(await _get(session, connection_id))


@router.post("/{connection_id}/refresh", summary="Refresh the connection's credentials via the connector")
async def refresh_connection(connection_id: str, request: Request, session: SessionDep) -> dict[str, Any]:
    row = await _get(session, connection_id)
    connector = get_connector(request, row.connector_key)
    if "refresh_auth" not in connector.manifest.capabilities:
        raise IbxError(ErrorCode.METHOD_NOT_SUPPORTED, f"Connector '{row.connector_key}' has no refresh_auth.")
    result = await connector.refresh_auth(Connection(connection_id=row.id, connector_key=row.connector_key))
    row = await session.get(ConnectionRow, connection_id, populate_existing=True)
    row.expires_at = result.expires_at
    await session.flush()
    await session.refresh(row)
    out = service.connection_out(row)
    await session.commit()
    return {**out, "refreshed": True}
