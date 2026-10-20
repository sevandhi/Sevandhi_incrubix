"""Job handlers of the LLM provider package (the worker imports every ``handlers.py`` under ``app/``):

    rules.capture   fetch a destination's rules, propose posting rules with quotes, store the proposal (ADV2)
    tailoring.run   tailored renditions + diffs for one content item on several destinations (ADV4 API)

They live here because the job registry only loads modules named ``handlers.py`` and this is the LLM provider package.
"""

from __future__ import annotations

from typing import Any

from app.catalogue import rules_capture as rc
from app.catalogue import service
from app.errors import ErrorCode, IbxError
from app.jobs.context import JobContext
from app.jobs.registry import job_handler


@job_handler("rules.capture", max_attempts=2)
async def rules_capture_run(ctx: JobContext) -> dict[str, Any]:
    payload = ctx.payload
    async with ctx.session_factory() as session:
        dest = await service.get_destination(session, payload["destination_id"])
        await session.commit()
    await ctx.progress(10, "fetching rules")
    with ctx.stage("fetch"):
        gathered = await rc.gather_sources(dest, ctx.settings, None, rules_url=payload.get("rules_url"),
                                           pasted=payload.get("text"), use_platform_api=payload.get("use_platform_api", True))
    for warning in gathered.warnings:
        ctx.warn(warning)
    if not gathered.text.strip():
        raise IbxError(ErrorCode.INPUT_FETCH_FAILED, "No rules text could be read for this destination (no sidebar, no "
                       "reachable rules_url). Paste the rules text with {\"text\": ...} instead.", retryable=False,
                       details={"warnings": gathered.issues})
    page_sha = rc._sha(gathered.text.encode())
    text_key = await ctx.put_artifact("rules_page_text.txt", gathered.text)
    await ctx.progress(40, "proposing rules")
    with ctx.stage("propose"):
        extraction, meta = await rc.propose(gathered.text)
    ctx.llm_tokens += meta.total_tokens
    ctx.manifest_extra.setdefault("providers", []).append(meta.manifest_entry("rules_capture"))
    ctx.manifest_extra["ai_generated"] = meta.provider != "mock"
    ctx.manifest_extra["rights"] = {"content_owner": None, "licence": None,
                                    "source_urls": [d.url for d in gathered.docs if d.url], "ai_generated": False,
                                    "media_produced_by_service": []}
    if meta.fallback_used:
        ctx.warn(f"LLM fallback used: answered by {meta.provider}")
    rules, fields, flags = rc.build_proposal(extraction, gathered.docs, destination_language=dest.language,
                                             rules_url=payload.get("rules_url") or dest.rules_url,
                                             from_llm=meta.provider != "mock")
    captured_at = rc.now_iso(ctx.clock.now())
    evidence: list[dict[str, Any]] = [
        *fields,
        *[d.evidence() for d in gathered.docs],
        {"type": "llm", **meta.manifest_entry("rules_capture")},
        {"type": "capture", "job_id": ctx.job_id, "captured_at": captured_at, "flags": flags,
         "warnings": gathered.warnings, "issues": gathered.issues, "origin": "rules_capture", "page_sha256": page_sha, "text_chars": len(gathered.text), "text_key": text_key,
         "requested_by": payload.get("requested_by")},
    ]
    async with ctx.session_factory() as session, session.begin():
        fresh = await service.get_destination(session, dest.id)
        row = await rc.store_proposal(session, fresh, rules=rules, evidence=evidence, page_sha256=page_sha,
                                      source="llm" if meta.provider != "mock" else "page")
        proposal_id = row.id
    await ctx.put_artifact("proposal.json", {"proposal_id": proposal_id, "rules": rules, "fields": fields, "flags": flags})
    verified = sum(1 for f in fields if f["status"] == "verified")
    return {"proposal_id": proposal_id, "destination_id": dest.id, "status": "proposed",
            "ui_url": f"/ui/rules/{proposal_id}", "fields_verified": verified, "fields_defaulted": len(fields) - verified,
            "flags": flags, "warnings": [*gathered.issues, *rc.field_warnings(fields)], "provider": meta.provider, "model": meta.model, "fallback_used": meta.fallback_used,
            "rules_page_sha256": page_sha}


@job_handler("tailoring.run", max_attempts=2)
async def tailoring_run(ctx: JobContext) -> dict[str, Any]:
    from app.certification.publications import ruleset_for
    from app.contract.registry import get_registry
    from app.tailoring import tailor

    registry = get_registry()
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    async with ctx.session_factory() as session:
        content = await service.get_content(session, ctx.payload["content_id"])
        loaded = []
        for dest_id in ctx.payload["destination_ids"]:
            try:
                dest = await service.get_destination(session, dest_id)
            except IbxError as err:
                errors.append({"destination_id": dest_id, "code": err.code.value, "message": err.message})
                continue
            loaded.append((dest, await service.confirmed_rules(session, dest.id)))
        await session.commit()
    with ctx.stage("tailor"):
        for dest, rules_row in loaded:
            manifest = registry.manifest(dest.connector_key)
            if manifest is None:
                errors.append({"destination_id": dest.id, "code": ErrorCode.CONNECTOR_NOT_FOUND.value,
                               "message": "Unknown connector."})
                continue
            surface = manifest.surface(dest.surface) or manifest.surfaces[0]
            assisted = manifest.tier == "assisted"
            ruleset = ruleset_for(dest, rules_row, assisted=assisted)
            rendition = service.rendition_for(content, dest.surface, ctx.settings)
            contract_dest = service.contract_destination(dest, rules_row.rules if rules_row else None)
            try:
                result = tailor(rendition, contract_dest, {**surface.spec, "content_types": list(surface.content_types)},
                                ruleset)
            except IbxError as err:
                errors.append({"destination_id": dest.id, "code": err.code.value, "message": err.message})
                continue
            items.append({
                "destination_id": dest.id, "destination_key": dest.destination_key, "connector_key": dest.connector_key,
                "surface": surface.key, "profile": result.profile, "ok": result.ok,
                "rules": {"source": "confirmed" if rules_row else "none",
                          "version": rules_row.version if rules_row else None},
                "rendition": result.rendition.model_dump(mode="json", exclude_none=True),
                "diff": result.diff, "warnings": result.warnings,
                "violations": [v.model_dump(mode="json") for v in result.violations],
            })
    ctx.manifest_extra["ai_generated"] = content.ai_generated
    output = {"content_id": content.id, "tailorings": items, "errors": errors}
    await ctx.put_artifact("tailorings.json", output)
    return {"content_id": content.id, "count": len(items), "errors": errors, "tailorings": items}
