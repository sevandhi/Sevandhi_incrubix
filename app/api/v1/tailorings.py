"""Tailoring API (ADV4): ``POST /api/v1/tailorings`` → 202 ``{job_id}``.

The job (``tailoring.run``) returns, per destination, the tailored rendition (``tailor``: forum discussion-first
framing, canonical URL for articles, word-boundary truncation, hashtag and mention style), a field-by-field diff,
warnings and any spec violations left. It uses the destination's CONFIRMED rules only (never a proposal) and
publishes nothing. ``GET /api/v1/jobs/{job_id}`` has the result; ``result.links.tailorings`` the JSON file.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.catalogue import service
from app.catalogue.models import ContentIn
from app.db.session import SessionDep
from app.jobs import queue

router = APIRouter(tags=["tailoring"])
MAX_DESTINATIONS = 100


class TailoringIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_id: str | None = None
    content: ContentIn | None = Field(default=None, description="Inline content (stored as a content item).")
    destination_ids: list[str] = Field(min_length=1, max_length=MAX_DESTINATIONS)
    callback_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)

    @model_validator(mode="after")
    def _one_content(self) -> TailoringIn:
        if (self.content_id is None) == (self.content is None):
            raise ValueError("send exactly one of content_id or content")
        return self


@router.post("/api/v1/tailorings", status_code=202, summary="Tailor one content item for several destinations")
async def create_tailoring(body: TailoringIn, request: Request, session: SessionDep) -> JSONResponse:
    if body.content is not None:
        content = await service.create_content(session, body.content, storage=request.app.state.storage)
    else:
        content = await service.get_content(session, body.content_id or "")
    destination_ids = list(dict.fromkeys(body.destination_ids))
    job = await queue.enqueue(session, "tailoring.run", {"content_id": content.id, "destination_ids": destination_ids},
                              now=request.app.state.clock.now(), callback_url=body.callback_url,
                              correlation_id=getattr(request.state, "correlation_id", None),
                              idempotency_key=request.headers.get("Idempotency-Key"), max_attempts=2)
    await session.commit()
    url = f"/api/v1/jobs/{job.id}"
    return JSONResponse({"job_id": job.id, "status": "queued", "status_url": url, "content_id": content.id,
                         "destinations": len(destination_ids)}, status_code=202, headers={"Location": url})
