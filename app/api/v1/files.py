"""Files out (IS7): ``GET /api/v1/files/{key}?exp=<unix>&sig=<hmac>`` streams an S3 object through the API.

The link itself is the credential (HMAC over key + expiry, see ``app.storage.links``), so no bearer token is
needed and the link works from the host through the one published API port. Bad signature → 403,
expired → 410, missing object → 404.

Only types that can not run script in the browser are shown inline (images, video, PDF, plain text, JSON,
markdown, YAML). Anything else, e.g. an ``text/html`` file whose type a caller chose, is sent as an attachment,
so a stored file can never execute as a page on the API's origin.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.storage.links import verify_download

router = APIRouter(prefix="/api/v1/files", tags=["files"])
INLINE_PREFIXES = ("image/png", "image/jpeg", "image/gif", "image/webp", "video/", "application/pdf", "text/plain",
                   "text/markdown", "application/json", "application/yaml")


def disposition(content_type: str | None) -> str:
    kind = (content_type or "").split(";")[0].strip().lower()
    return "inline" if kind.startswith(INLINE_PREFIXES) else "attachment"


@router.get("/{key:path}", summary="Download a stored file with a signed link",
            responses={200: {"content": {"application/octet-stream": {}}}, 403: {}, 404: {}, 410: {}})
async def download(key: str, request: Request, exp: str = Query(...), sig: str = Query(...)) -> StreamingResponse:
    verify_download(key, exp, sig, request.app.state.settings, request.app.state.clock.now())
    info, chunks = await request.app.state.storage.open_stream(key)
    name = key.rsplit("/", 1)[-1].replace('"', "")
    headers = {"Content-Length": str(info.size), "Content-Disposition": f'{disposition(info.content_type)}; filename="{name}"',
               "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
    return StreamingResponse(chunks, media_type=info.content_type, headers=headers)
