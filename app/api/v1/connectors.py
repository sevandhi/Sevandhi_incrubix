"""Connector catalogue and the runtime kill switch (plan §4.3)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionDep
from app.errors import ErrorCode, IbxError
from app.reliability import killswitch

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])


class KillSwitchIn(BaseModel):
    disabled: bool
    reason: str | None = Field(default=None, max_length=500)


async def _describe(request: Request, session: AsyncSession, key: str) -> dict[str, Any]:
    manifest = request.app.state.registry.manifest(key)
    reason = await killswitch.disabled_reason(session, request.app.state.settings, key)
    return {
        "key": key,
        "version": manifest.version,
        "tier": manifest.tier,
        "auth": manifest.auth,
        "capabilities": list(manifest.capabilities),
        "instance_param": manifest.instance_param,
        "surfaces": [s.key for s in manifest.surfaces],
        "disabled": reason is not None,
        "disabled_reason": reason,
    }


@router.get("", summary="Registered connectors with their kill-switch state")
async def list_connectors(request: Request, session: SessionDep) -> dict[str, Any]:
    registry = request.app.state.registry
    return {
        "connectors": [await _describe(request, session, key) for key in registry.keys()],
        "registry_errors": registry.errors,
    }


@router.put("/{key}/kill-switch", summary="Turn a connector off (or on) at runtime")
async def set_kill_switch(
    key: str, body: KillSwitchIn, request: Request, session: SessionDep
) -> dict[str, Any]:
    if request.app.state.registry.manifest(key) is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, "Unknown connector key.")
    await killswitch.set_flag(session, key, body.disabled, body.reason)
    await session.commit()
    return await _describe(request, session, key)
