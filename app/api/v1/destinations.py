"""Destinations: one catalogue row = one destination (Must-have 3, plan §3.3).

    POST  /api/v1/destinations            one row as JSON, {"rows": [...]}, or text/csv (header + rows)
    GET   /api/v1/destinations            filter by connector_key, level, status, q; paginated
    GET   /api/v1/destinations/{id}       the Appendix A record + instance, confirmed rules, last certification
    PATCH /api/v1/destinations/{id}       descriptive fields, connection, spec overrides, status
    PUT   /api/v1/destinations/{id}/rules human-confirmed rules (new version; the old one is superseded)
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.catalogue import service
from app.catalogue.models import DestinationIn, DestinationPatch, RulesIn
from app.db.session import SessionDep
from app.errors import ErrorCode, IbxError

router = APIRouter(prefix="/api/v1/destinations", tags=["destinations"])
MAX_ROWS = 200

_ROW_SCHEMA = DestinationIn.model_json_schema()
_CREATE_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": {"anyOf": [_ROW_SCHEMA, {"type": "object", "properties": {
                "rows": {"type": "array", "items": _ROW_SCHEMA, "maxItems": MAX_ROWS}}, "required": ["rows"]}]}},
            "text/csv": {"schema": {"type": "string", "description": "CSV with a header row (catalogue columns)."}},
        },
    }
}


def _row_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": [str(p) for p in e["loc"]], "type": e["type"], "msg": e["msg"]} for e in exc.errors()]


async def _rows(request: Request) -> tuple[list[dict[str, Any]], bool]:
    raw = await request.body()
    ctype = request.headers.get("content-type", "").lower()
    if "csv" in ctype or ctype.startswith("text/plain"):
        text = raw.decode("utf-8-sig", errors="replace")
        header = text.split("\n", 1)[0]
        delimiter = max((",", ";", "\t"), key=header.count)  # quoting stays standard (RFC 4180 / Excel)
        rows = [dict(r) for r in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
        return rows, True
    try:
        data = json.loads(raw or b"null")
    except ValueError:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "The body must be JSON or text/csv.") from None
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return [r for r in data["rows"]], True
    if isinstance(data, dict):
        return [data], False
    if isinstance(data, list):
        return data, True
    raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "Send one destination object, {\"rows\": [...]} or CSV.")


@router.post("", status_code=201, summary="Add destinations from catalogue rows (JSON or CSV)", openapi_extra=_CREATE_BODY)
async def create_destinations(request: Request, session: SessionDep) -> JSONResponse:
    rows, many = await _rows(request)
    if not rows:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "No rows in the request.")
    if len(rows) > MAX_ROWS:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"At most {MAX_ROWS} rows per request; use /api/v1/catalogue/imports for more.")
    registry = request.app.state.registry
    now = request.app.state.clock.now()
    results = []
    for n, raw in enumerate(rows, start=1):
        try:
            row = DestinationIn.model_validate(raw)
        except ValidationError as exc:
            if not many:
                raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "The catalogue row is invalid.",
                               details={"errors": _row_errors(exc)}) from None
            results.append({"row": n, "status": "invalid", "code": ErrorCode.REQUEST_VALIDATION_FAILED.value,
                            "errors": _row_errors(exc)})
            continue
        try:
            async with session.begin_nested():
                dest, created, warnings = await service.create_destination(session, registry, row, now=now)
                await session.refresh(dest)
                out = await service.destination_out(session, dest)
        except IbxError as err:
            if not many:
                raise
            # a row the service rejects as malformed (e.g. a bad URL) is "invalid", like a schema error
            status = "invalid" if err.code == ErrorCode.REQUEST_VALIDATION_FAILED else "error"
            results.append({"row": n, "status": status, "code": err.code.value, "message": err.message})
            continue
        results.append({"row": n, "status": "created" if created else "duplicate", "destination_id": dest.id,
                        "warnings": warnings, "destination": out})
    await session.commit()
    if not many:
        item = results[0]
        body = {**item["destination"], "created": item["status"] == "created", "warnings": item["warnings"]}
        return JSONResponse(body, status_code=201 if body["created"] else 200,
                            headers={"Location": f"/api/v1/destinations/{item['destination_id']}"})
    created = sum(1 for r in results if r["status"] == "created")
    summary = {s: sum(1 for r in results if r["status"] == s) for s in ("created", "duplicate", "invalid", "error")}
    return JSONResponse({"summary": summary, "results": results}, status_code=201 if created else 200)


@router.get("", summary="List destinations")
async def list_destinations(
    session: SessionDep,
    connector_key: str | None = None,
    level: str | None = Query(default=None, pattern=r"^L[0-4]$"),
    status: str | None = Query(default=None, pattern=r"^(active|excluded)$"),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows, total = await service.list_destinations(session, connector_key=connector_key, level=level, status=status, q=q,
                                                  limit=limit, offset=offset)
    return {"total": total, "limit": limit, "offset": offset,
            "destinations": [await service.destination_out(session, d, with_rules=False) for d in rows]}


@router.get("/{destination_id}", summary="One destination with its confirmed rules and last certification")
async def get_destination(destination_id: str, session: SessionDep) -> dict[str, Any]:
    dest = await service.get_destination(session, destination_id)
    return await service.destination_out(session, dest)


@router.patch("/{destination_id}", summary="Update a destination")
async def patch_destination(destination_id: str, body: DestinationPatch, request: Request, session: SessionDep) -> dict[str, Any]:
    dest = await service.get_destination(session, destination_id, lock=True)
    await service.patch_destination(session, request.app.state.registry, dest, body)
    await session.refresh(dest)
    out = await service.destination_out(session, dest)
    await session.commit()
    return out


@router.put("/{destination_id}/rules", summary="Confirm the destination's posting rules (human-confirmed)")
async def put_rules(destination_id: str, body: RulesIn, request: Request, session: SessionDep) -> dict[str, Any]:
    dest = await service.get_destination(session, destination_id, lock=True)
    row = await service.put_rules(session, dest, body, now=request.app.state.clock.now())
    await session.refresh(row)
    out = {"destination_id": dest.id, "rules": service.rules_out(row)}
    await session.commit()
    return out
