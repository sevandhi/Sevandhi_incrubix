"""Catalogue import and relevance (ADV1, ADV3).

    POST /api/v1/catalogue/imports               multipart ``file`` | text/csv body | JSON {"csv_url"} → 202 {job_id, import_id}
    GET  /api/v1/catalogue/imports               recent imports
    GET  /api/v1/catalogue/imports/{id}          status, progress, counts, timings
    GET  /api/v1/catalogue/imports/{id}/report   the row-by-row report (text/csv; ?format=json for JSON)
    POST /api/v1/catalogue/relevance             score rows (not stored) or stored destinations (updated)

Options (form fields, query parameters or JSON keys): ``dry_run`` (validate, map, normalise, de-duplicate and score
only: no network, nothing written), ``import_unreachable``, ``relevance_llm`` (the optional LLM ±1 pass).
A ``csv_url`` is checked by the SSRF guard now and downloaded by the job through the same guard.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.catalogue import relevance as rel
from app.catalogue.importer import MAX_BYTES, parse_csv, report_csv
from app.db.session import SessionDep
from app.db.tables import CatalogueImportRow, DestinationRow, JobRow, new_id
from app.errors import ErrorCode, IbxError
from app.jobs import queue
from app.reliability.safe_fetch import check_url

router = APIRouter(prefix="/api/v1/catalogue", tags=["catalogue"])
_TRUE = {"1", "true", "yes", "on"}


class ImportUrlIn(BaseModel):
    csv_url: str = Field(pattern=r"^https?://", max_length=2000)
    dry_run: bool = False
    import_unreachable: bool = False
    relevance_llm: bool = False
    callback_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)


_IMPORT_BODY = {"requestBody": {"required": True, "content": {
    "multipart/form-data": {"schema": {"type": "object", "properties": {
        "file": {"type": "string", "format": "binary"}, "dry_run": {"type": "boolean"},
        "import_unreachable": {"type": "boolean"}, "relevance_llm": {"type": "boolean"}}, "required": ["file"]}},
    "text/csv": {"schema": {"type": "string", "description": "CSV with a header row (catalogue columns)."}},
    "application/json": {"schema": ImportUrlIn.model_json_schema()},
}}}


def _flag(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in _TRUE)


def _iso(moment: Any) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


@router.post("/imports", status_code=202, summary="Import a catalogue CSV (validate, dedupe, reachability, enrich, score)",
             openapi_extra=_IMPORT_BODY)
async def create_import(request: Request, session: SessionDep) -> JSONResponse:
    state = request.app.state
    ctype = request.headers.get("content-type", "").lower()
    params = dict(request.query_params)
    callback_url = None
    data: bytes | None = None
    source_name = None
    if ctype.startswith("multipart/form-data"):
        form = await request.form(max_files=1)
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "multipart upload needs a 'file' field.",
                           details={"errors": [{"loc": ["body", "file"], "type": "missing", "msg": "Field required"}]})
        data = await upload.read(MAX_BYTES + 1)
        source_name = upload.filename
        params.update({k: v for k, v in form.items() if isinstance(v, str)})
    elif "csv" in ctype or ctype.startswith("text/plain"):
        data = await request.body()
        source_name = "request-body.csv"
    else:
        try:
            body = ImportUrlIn.model_validate_json(await request.body() or b"{}")
        except ValueError:
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "Send multipart 'file', a text/csv body or JSON {\"csv_url\": ...}.",
                           details={"errors": [{"loc": ["body", "csv_url"], "type": "value_error", "msg": "http(s) URL required"}]}
                           ) from None
        await check_url(body.csv_url, state.settings)
        params.update({k: str(v) for k, v in body.model_dump(exclude={"csv_url", "callback_url"}).items()})
        callback_url = body.callback_url
        source_name = body.csv_url[:255]
    options = {"dry_run": _flag(params.get("dry_run")), "import_unreachable": _flag(params.get("import_unreachable")),
               "relevance_llm": _flag(params.get("relevance_llm"))}
    import_id = new_id("import")
    rows_total = 0
    if data is not None:
        if len(data) > MAX_BYTES:
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"The CSV is larger than {MAX_BYTES} bytes.", http_status=413)
        rows_total = len(parse_csv(data))  # an unreadable file is a 422 now, not a failed job later
        key = f"imports/{import_id}/source.csv"
        await state.storage.put_bytes(key, data, "text/csv")
        source = {"kind": "upload", "key": key}
    else:
        source = {"kind": "url", "url": source_name}
    record = CatalogueImportRow(id=import_id, status="queued", source_name=(source_name or "")[:255], rows_total=rows_total,
                                report={"options": options})
    session.add(record)
    job = await queue.enqueue(session, "catalogue.import", {"import_id": import_id, "source": source, "options": options},
                              now=state.clock.now(), callback_url=callback_url,
                              correlation_id=getattr(request.state, "correlation_id", None),
                              idempotency_key=request.headers.get("Idempotency-Key"), max_attempts=1)
    record.job_id = job.id
    await session.commit()
    url = f"/api/v1/jobs/{job.id}"
    return JSONResponse({"job_id": job.id, "import_id": import_id, "status": "queued", "status_url": url,
                         "import_url": f"/api/v1/catalogue/imports/{import_id}",
                         "report_url": f"/api/v1/catalogue/imports/{import_id}/report", "options": options,
                         "rows_total": rows_total or None}, status_code=202, headers={"Location": url})


async def _get(session: Any, import_id: str) -> CatalogueImportRow:
    row = await session.get(CatalogueImportRow, import_id)
    if row is None:
        raise IbxError(ErrorCode.NOT_FOUND, "Unknown catalogue import.")
    return row


async def _out(session: Any, row: CatalogueImportRow) -> dict[str, Any]:
    job = await session.get(JobRow, row.job_id) if row.job_id else None
    report = row.report or {}
    return {
        "import_id": row.id, "status": row.status, "source_name": row.source_name, "job_id": row.job_id,
        "progress": job.progress if job is not None else None,
        "options": report.get("options"), "dry_run": report.get("dry_run"),
        "counts": {"rows": row.rows_total, "imported": row.imported, "duplicates": row.duplicates, "invalid": row.invalid,
                   "unreachable": row.unreachable},
        "summary": report.get("summary"), "timings": report.get("timings"), "network_requests": report.get("network_requests"),
        "language_detector": report.get("language_detector"), "error": report.get("error"),
        "report_url": f"/api/v1/catalogue/imports/{row.id}/report", "created_at": _iso(row.created_at),
    }


@router.get("/imports", summary="Recent catalogue imports")
async def list_imports(session: SessionDep, limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    rows = (await session.execute(select(CatalogueImportRow).order_by(CatalogueImportRow.created_at.desc()).limit(limit))).scalars()
    return {"imports": [await _out(session, r) for r in rows]}


@router.get("/imports/{import_id}", summary="One catalogue import: status, progress and counts")
async def get_import(import_id: str, session: SessionDep) -> dict[str, Any]:
    return await _out(session, await _get(session, import_id))


@router.get("/imports/{import_id}/report", summary="The row-by-row import report (CSV, or JSON with ?format=json)",
            responses={200: {"content": {"text/csv": {}, "application/json": {}}}})
async def get_report(import_id: str, session: SessionDep, format: str = Query(default="csv", pattern="^(csv|json)$")) -> Response:
    row = await _get(session, import_id)
    report = row.report or {}
    if row.status not in ("succeeded",) or "rows" not in report:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"The report is not ready (import status: {row.status}).",
                       http_status=409, retryable=row.status in ("queued", "running"), details={"status": row.status})
    if format == "json":
        return JSONResponse({"import_id": row.id, **report})
    return Response(report_csv(report), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{row.id}_report.csv"'})


class RelevanceRowIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=128)
    audience_tags: list[str] = Field(default_factory=list, max_length=50)
    description: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=2000)


class RelevanceIn(BaseModel):
    rows: list[RelevanceRowIn] = Field(default_factory=list, max_length=500, description="Score these rows (nothing is stored).")
    destination_ids: list[str] = Field(default_factory=list, max_length=500,
                                       description="Score these destinations and store relevance_score/relevance_reason.")
    llm: bool = Field(default=False, description="Also run the optional LLM ±1 pass (free providers; lexicon-only fallback).")
    overwrite: bool = Field(default=False, description="Replace a score that is already stored.")


@router.post("/relevance", summary="Relevance 1-5 with a reason (lexicon, optional LLM ±1)")
async def score_relevance(body: RelevanceIn, session: SessionDep) -> dict[str, Any]:
    if not body.rows and not body.destination_ids:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "Send rows or destination_ids.")
    results: list[dict[str, Any]] = []
    for n, item in enumerate(body.rows, start=1):
        res = await rel.score(rel.RelevanceInput(name=item.name, category=item.category, audience_tags=item.audience_tags,
                                                 description=item.description, url=item.url), use_llm=body.llm)
        results.append({"row": n, "relevance_score": res.score, "relevance_reason": res.reason, "method": res.method,
                        "lexicon_score": res.lexicon_score, "llm": res.llm or None})
    for dest_id in body.destination_ids:
        dest = await session.get(DestinationRow, dest_id, with_for_update=True)
        if dest is None:
            results.append({"destination_id": dest_id, "error": "DESTINATION_NOT_FOUND"})
            continue
        if dest.relevance_score is not None and not body.overwrite:
            results.append({"destination_id": dest_id, "relevance_score": dest.relevance_score,
                            "relevance_reason": dest.relevance_reason, "stored": False, "why": "already scored (overwrite=false)"})
            continue
        page = ((dest.extra or {}).get("import") or {}).get("page") or {}
        res = await rel.score(rel.RelevanceInput(name=dest.name, category=dest.category, audience_tags=list(dest.audience_tags or []),
                                                 description=page.get("description"), title=page.get("title"), url=dest.url),
                              use_llm=body.llm)
        dest.relevance_score, dest.relevance_reason = res.score, res.reason
        results.append({"destination_id": dest_id, "relevance_score": res.score, "relevance_reason": res.reason,
                        "method": res.method, "lexicon_score": res.lexicon_score, "llm": res.llm or None, "stored": True})
    await session.commit()
    return {"results": results}
