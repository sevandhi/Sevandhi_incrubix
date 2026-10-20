"""Job handlers: certification runs, the test-post sweeper, publications and their deletion."""

from __future__ import annotations

import sys
from typing import Any

from sqlalchemy import select

from app.catalogue import service
from app.certification import publications as pubs
from app.certification.harness import Harness
from app.certification.runtime import connector_for
from app.certification.sweeper import sweep
from app.config import get_settings
from app.contract.registry import get_registry
from app.db.tables import CertificationRunRow, PublicationRow
from app.errors import ErrorCode, IbxError
from app.jobs import queue
from app.jobs.context import JobContext
from app.jobs.registry import job_handler
from app.jobs.scheduler import periodic

RULE_BLOCK_CODES = {c for c in ErrorCode if c.value.startswith("DEST_RULE")} | {
    ErrorCode.DEST_RULES_UNCONFIRMED, ErrorCode.DESTINATION_EXCLUDED, ErrorCode.DESTINATION_NOT_CERTIFIED}
RULE_BLOCK_CODES.discard(ErrorCode.DEST_RULE_FREQUENCY_CAP)


@job_handler("certification.run", max_attempts=1)
async def certification_run(ctx: JobContext) -> dict[str, Any]:
    harness = Harness(settings=ctx.settings, session_factory=ctx.session_factory, storage=ctx.storage, clock=ctx.clock,
                      progress=ctx.progress)
    with ctx.stage("certification"):
        result = await harness.run(ctx.payload["run_id"], reschedule_attempt=int(ctx.payload.get("reschedule_attempt") or 0))
    for name, key in (result.get("evidence_files") or {}).items():
        ctx.links[name.replace("/", "_").rsplit(".", 1)[0]] = key
    if not result.get("passed"):
        outcome = {"RESCHEDULED": f"rescheduled to {result.get('retry_at')} (level kept)",
                   "INCONCLUSIVE": "inconclusive (level kept)",
                   "FAILED_PRECONDITION": "not certifiable here (level kept)"}.get(str(result.get("state")), "destination demoted")
        ctx.warn(f"certification failed at {result.get('failed_step')}: {result.get('failure_code')}; {outcome}")
    return {k: v for k, v in result.items() if k != "evidence_files"}


@job_handler("certification.sweep", max_attempts=1)
async def certification_sweep(ctx: JobContext) -> dict[str, Any]:
    return await sweep(settings=ctx.settings, session_factory=ctx.session_factory, storage=ctx.storage, clock=ctx.clock)


periodic("certification.sweep", "certification.sweep", every_s=max(10, get_settings().SWEEPER_EVERY_S))


# ------------------------------------------------------------------ publications


async def _load(ctx: JobContext, pub_id: str) -> tuple[Any, ...]:
    async with ctx.session_factory() as session:
        pub = await pubs.get_publication(session, pub_id)
        dest = await service.get_destination(session, pub.destination_id)
        content = await service.get_content(session, pub.content_id) if pub.content_id else None
        rules = await service.confirmed_rules(session, dest.id)
        conn_row = await service.destination_connection(session, dest)
        await session.commit()
    return pub, dest, content, rules, conn_row


async def _set(ctx: JobContext, pub_id: str, **values: Any) -> PublicationRow:
    async with ctx.session_factory() as session, session.begin():
        pub = await session.get(PublicationRow, pub_id, with_for_update=True)
        extra = values.pop("extra", None)
        for key, value in values.items():
            setattr(pub, key, value)
        if extra:
            pub.extra = {**(pub.extra or {}), **extra}
        return pub


async def _result(ctx: JobContext, pub_id: str) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        pub = await pubs.get_publication(session, pub_id)
        out = await pubs.publication_out(pub, ctx.settings)
    await ctx.put_artifact("publication.json", out)
    return {"publication": out}


async def _reschedule(ctx: JobContext, pub_id: str, retry_at: Any) -> None:
    async with ctx.session_factory() as session, session.begin():
        pub = await session.get(PublicationRow, pub_id, with_for_update=True)
        job = await queue.enqueue(session, "publication.run", {"publication_id": pub_id}, now=ctx.clock.now(), run_after=retry_at)
        pubs.add_job(pub, job.id)


@job_handler("publication.run", max_attempts=5)
async def publication_run(ctx: JobContext) -> dict[str, Any]:
    from app.rules.fixes import apply_fixes
    from app.tailoring import tailor

    pub_id = ctx.payload["publication_id"]
    pub, dest, content, rules, conn_row = await _load(ctx, pub_id)
    if pub.state in pubs.FINAL_STATES:
        return await _result(ctx, pub_id)
    if pub.state == "blocked":
        raise pubs.blocked_error(pub)
    registry = get_registry()
    manifest = registry.manifest(dest.connector_key)
    if manifest is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, f"Unknown connector '{dest.connector_key}'.", retryable=False)
    assisted = "assist" in manifest.capabilities and "publish" not in manifest.capabilities or manifest.tier == "assisted"
    rendition = service.rendition_for(content, dest.surface, ctx.settings)
    ruleset = pubs.ruleset_for(dest, rules, assisted=assisted)
    now = ctx.clock.now()

    # 1) rules gate + slot reservation, under the per-destination lock
    async with ctx.session_factory() as session, session.begin():
        await pubs.lock_destination_slot(session, dest.destination_key)
        row = await session.get(PublicationRow, pub_id, with_for_update=True)
        fresh = await session.get(type(dest), dest.id)
        if fresh.status == "excluded" or pubs.not_certified(fresh):  # demoted since the POST: never post
            pubs.block_for_destination(row, fresh)
        else:
            history = await pubs.publish_history(session, fresh, exclude_publication=pub_id)
            decision = pubs.evaluate(rendition, ruleset, history, now, list(content.owner_domains or []))
            pubs.apply_decision(row, decision, at=now)
            if row.state == "pending":
                row.state, row.attempts = "publishing", row.attempts + 1
        state, retry_at = row.state, row.scheduled_for
        ctx.manifest_extra["ai_generated"] = content.ai_generated
        ctx.manifest_extra["rights"] = {  # IS9 rights of what is published
            "content_owner": (content.owner_domains or [None])[0],
            "licence": (content.extra or {}).get("licence"),  # as stated by the creator; None = not stated
            "source_urls": [u for u in [content.canonical_url, *[(m or {}).get("url") for m in content.media or []]] if u],
            "ai_generated": content.ai_generated,
            "media_produced_by_service": [],  # we only tailor text; the creator's files keep their own licence
        }
    if state == "blocked":
        async with ctx.session_factory() as session:
            raise pubs.blocked_error(await pubs.get_publication(session, pub_id))
    if state == "rescheduled":
        await _reschedule(ctx, pub_id, retry_at)
        ctx.warn(f"rescheduled to {retry_at.isoformat()} by the frequency cap")
        return await _result(ctx, pub_id)

    # 2) tailoring (and the decision's required changes, e.g. the AI disclosure)
    extra: dict[str, Any] = {}
    surface = manifest.surface(dest.surface) or manifest.surfaces[0]
    contract_dest = service.contract_destination(dest, dict(rules.rules) if rules else None)
    if pub.extra.get("tailor", True):
        tailored = tailor(rendition, contract_dest, {**surface.spec, "content_types": list(surface.content_types)}, ruleset)
        extra["tailoring"] = {"profile": tailored.profile, "diff": tailored.diff, "warnings": tailored.warnings}
        if tailored.violations:
            err = IbxError(ErrorCode.DEST_SPEC_VIOLATION, "The content still breaks the destination's limits after tailoring.",
                           details={"violations": [v.model_dump(mode="json") for v in tailored.violations]})
            await _set(ctx, pub_id, state="failed", error=err.to_body(), extra=extra)
            raise err
        rendition = tailored.rendition
    final = pubs.evaluate(rendition, ruleset, [], now, list(content.owner_domains or []))
    if final.action == "allow_with_changes":
        rendition = apply_fixes(rendition, final.fixes)
    ctx.manifest_extra["disclosure"] = {"required": bool(ruleset and ruleset.disclosure.required),
                                        "fixes_applied": [f.kind for f in final.fixes]}

    connector = connector_for(dest.connector_key, settings=ctx.settings, session_factory=ctx.session_factory,
                              storage=ctx.storage, clock=ctx.clock, registry=registry)
    connection = service.contract_connection(conn_row, dest.connector_key, manifest.auth)

    # 3a) assisted: build the pack, a person posts it
    if assisted:
        pack = await connector.build_assist_pack(rendition, contract_dest)
        doc = pack.model_dump(mode="json")
        key = await ctx.put_artifact("assist_pack.json", doc)
        marker_fn = getattr(sys.modules.get(type(connector).__module__), "pack_marker", None)
        marker = marker_fn(rendition, connector.destination_key(contract_dest)) if marker_fn else None
        extra["assist_pack"] = {**doc, "keys": {"assist_pack": key}, "marker": marker}
        await _set(ctx, pub_id, state="awaiting_human", extra={**extra, "reason": "assist pack ready; a person posts it"})
        return await _result(ctx, pub_id)

    # 3b) native: the connector's publish (idempotent per publication)
    try:
        result = await connector.publish(rendition, contract_dest, connection, pubs.intent_key(pub_id))
    except IbxError as err:
        await _handle_publish_error(ctx, pub_id, err, extra)
        if err.code == ErrorCode.DEST_RULE_FREQUENCY_CAP:
            return await _result(ctx, pub_id)
        raise
    state = {"published": "published", "deleted": "deleted"}.get(result.status, "awaiting_human")
    reason = None if state == "published" else f"the platform reports '{result.status}'; a person must act"
    await _set(ctx, pub_id, state=state, platform_post_id=result.platform_post_id, live_url=result.live_url,
               published_at=ctx.clock.now(), error=None, extra={**extra, "reason": reason})
    return await _result(ctx, pub_id)


async def _handle_publish_error(ctx: JobContext, pub_id: str, err: IbxError, extra: dict[str, Any]) -> None:
    body = err.to_body()
    if err.code == ErrorCode.DEST_RULE_FREQUENCY_CAP and err.details.get("retry_at"):
        from datetime import datetime

        retry_at = datetime.fromisoformat(str(err.details["retry_at"]).replace("Z", "+00:00"))
        await _set(ctx, pub_id, state="rescheduled", scheduled_for=retry_at, error=body,
                   extra={**extra, "reason": err.message})
        await _reschedule(ctx, pub_id, retry_at)
    elif err.code in RULE_BLOCK_CODES:
        await _set(ctx, pub_id, state="blocked", error=body, extra={**extra, "reason": err.message})
        err.retryable = False
    elif err.retryable and ctx.attempt < 5:
        await _set(ctx, pub_id, state="pending", error=body, extra={**extra, "reason": f"retrying: {err.code.value}"})
    else:
        await _set(ctx, pub_id, state="failed", error=body, extra={**extra, "reason": err.message})


@job_handler("publication.delete", max_attempts=5)
async def publication_delete(ctx: JobContext) -> dict[str, Any]:
    pub_id = ctx.payload["publication_id"]
    pub, dest, _content, _rules, conn_row = await _load(ctx, pub_id)
    if pub.state == "deleted":
        return await _result(ctx, pub_id)
    if not pub.platform_post_id:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"Publication '{pub_id}' has no platform post to delete (state {pub.state}).")
    connector = connector_for(dest.connector_key, settings=ctx.settings, session_factory=ctx.session_factory,
                              storage=ctx.storage, clock=ctx.clock)
    connection = service.contract_connection(conn_row, dest.connector_key, connector.manifest.auth)
    contract_dest = service.contract_destination(dest, None)
    result = await connector.delete(pub.platform_post_id, connection, contract_dest)
    if result.deleted:
        await _set(ctx, pub_id, state="deleted", extra={"removal": "deleted", "reason": "deleted on request"})
    else:
        status = await connector.get_status(pub.platform_post_id, connection, contract_dest)
        await _set(ctx, pub_id, extra={"removal": "unpublish_to_draft" if status.status == "draft" else f"not_deleted:{status.status}",
                                       "reason": f"the platform did not delete the post (status now '{status.status}')"})
        ctx.warn(f"delete returned deleted=false; status is '{status.status}'")
    return await _result(ctx, pub_id)


async def find_run(session: Any, run_id: str) -> CertificationRunRow:
    run = (await session.execute(select(CertificationRunRow).where(CertificationRunRow.id == run_id))).scalar_one_or_none()
    if run is None:
        raise IbxError(ErrorCode.NOT_FOUND, f"Unknown certification run '{run_id}'.")
    return run
