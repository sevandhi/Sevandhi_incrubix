"""Content items: ``POST /api/v1/content`` (201) and ``GET /api/v1/content/{id}``.

Media may reference an uploaded file by ``media_id`` (from ``POST /api/v1/media``); the publication then
hands connectors a fresh signed download link for it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.catalogue import service
from app.catalogue.models import ContentIn
from app.db.session import SessionDep

router = APIRouter(prefix="/api/v1/content", tags=["content"])


@router.post("", status_code=201, summary="Store a content item")
async def create_content(body: ContentIn, request: Request, session: SessionDep) -> JSONResponse:
    row = await service.create_content(session, body, storage=request.app.state.storage)
    await session.refresh(row)
    out = service.content_out(row)
    await session.commit()
    return JSONResponse(out, status_code=201, headers={"Location": f"/api/v1/content/{row.id}"})


@router.get("/{content_id}", summary="One content item")
async def get_content(content_id: str, session: SessionDep) -> dict[str, Any]:
    return service.content_out(await service.get_content(session, content_id))
