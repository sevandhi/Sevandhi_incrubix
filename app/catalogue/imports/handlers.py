"""Job ``catalogue.import`` (ADV1): run one catalogue import and store its row-by-row report.

Payload: ``{"import_id", "source": {"kind": "upload", "key"} | {"kind": "url", "url"}, "options": {...}}``.
The ``csv_url`` is downloaded here through ``app.reliability.safe_fetch.fetch_url`` (the SSRF guard).
"""

from __future__ import annotations

from typing import Any

from app.catalogue.importer import MAX_BYTES, Importer, parse_csv, report_csv
from app.contract.registry import get_registry
from app.db.tables import CatalogueImportRow
from app.errors import IbxError
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.reliability.safe_fetch import fetch_url


async def _update(ctx: JobContext, import_id: str, **values: Any) -> None:
    async with ctx.session_factory() as session, session.begin():
        row = await session.get(CatalogueImportRow, import_id, with_for_update=True)
        if row is None:
            return
        for key, value in values.items():
            setattr(row, key, value)


@job_handler("catalogue.import", max_attempts=1)
async def catalogue_import(ctx: JobContext) -> dict[str, Any]:
    import_id = ctx.payload["import_id"]
    source = ctx.payload.get("source") or {}
    options = ctx.payload.get("options") or {}
    await _update(ctx, import_id, status="running")
    try:
        with ctx.stage("read_csv"):
            if source.get("kind") == "url":
                fetched = await fetch_url(source["url"], ctx.settings, max_bytes=MAX_BYTES)
                data = fetched.data
            else:
                data = await ctx.storage.get_bytes(source["key"])
            rows = parse_csv(data)
        await _update(ctx, import_id, rows_total=len(rows))

        async def progress(percent: int, note: str) -> None:
            await ctx.progress(percent, note)

        importer = Importer(ctx.settings, get_registry(), session_factory=ctx.session_factory, progress=progress,
                            now=ctx.clock.now(), timeout_s=float(options.get("timeout_s") or 5.0),
                            import_unreachable=bool(options.get("import_unreachable")),
                            relevance_llm=bool(options.get("relevance_llm")), import_id=import_id)
        with ctx.stage("import"):
            report = await importer.run(rows, dry_run=bool(options.get("dry_run")))
    except IbxError as err:
        await _update(ctx, import_id, status="failed", report=err.to_body())
        raise
    summary = report["summary"]
    await _update(ctx, import_id, status="succeeded", rows_total=summary["rows"], imported=summary["imported"],
                  duplicates=summary["duplicate"], invalid=summary["invalid"], unreachable=summary["unreachable"],
                  report=report)
    await ctx.put_artifact("report.csv", report_csv(report), "text/csv; charset=utf-8")
    await ctx.put_artifact("summary.json", {k: v for k, v in report.items() if k != "rows"})
    if report.get("llm_used"):
        ctx.llm_tokens += int(report.get("llm_tokens") or 0)
        for name in report.get("llm_providers") or []:
            provider, _, model = name.partition(":")
            ctx.manifest_extra.setdefault("providers", []).append(
                {"name": "llm", "task": "relevance", "provider": provider, "model": model, "version": model,
                 "calls": report["llm_used"], "cost_usd": 0.0})
    for code, count in summary["codes"].items():
        if code in ("RULES_UNPARSED", "CONNECTOR_FALLBACK", "PLATFORM_UNDETECTED", "LANGUAGE_MISMATCH", "ROBOTS_DISALLOWED"):
            ctx.warn(f"{count} row(s) {code}")
    return {"import_id": import_id, "summary": summary, "timings": report["timings"],
            "network_requests": report["network_requests"], "language_detector": report["language_detector"],
            "report_url": f"/api/v1/catalogue/imports/{import_id}/report"}
