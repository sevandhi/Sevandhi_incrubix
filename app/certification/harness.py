"""Certification harness: the state machine of plan §6, run as the ``certification.run`` job.

    QUEUED → PREFLIGHT → VALIDATE → PUBLISH → CONFIRM_LIVE → STATUS → [METRICS] → DELETE → CONFIRM_DELETED → PASSED
    any failure → FAILED → CLEANUP (best-effort delete, by id or by marker) → DEMOTED (L0, excluded, alert)

Two failures say nothing about the destination, so they never demote it:
* a refusal at PREFLIGHT because the host is not one of our own instances (local mode) → FAILED_PRECONDITION:
  nothing was sent, the destination keeps its level and status;
* a platform rate limit / quota, or an open circuit breaker, in any step → CLEANUP → RESCHEDULED: a new run is
  queued at the platform's reset time (``CERT_RESCHEDULE_MAX`` times in a row at most, then INCONCLUSIVE with an
  alert); the level is kept.
A platform without a delete API (Dev.to) names its last step CONFIRM_REMOVED and its file removal_confirmation.json.

Assisted connectors run PREFLIGHT → VALIDATE → BUILD_PACK → CONFIRM_PACK → DELETE (the pack's stored files)
→ CONFIRM_DELETED (links dead) → PASSED (L1).

Safety (Must-have 5):
* test posts only go to own instances: outside simulation the destination host must be in ``CERT_OWN_HOSTS``
  (local Lemmy, Ghost, Discourse, mock-platforms); real catalogue rows are refused in PREFLIGHT;
* every test post is written to ``test_posts`` the moment it exists, with ``expires_at = created + TEST_POST_TTL_S``;
  the harness deletes it within seconds, and the sweeper deletes anything a crashed run left behind;
* the certification post uses fixed, neutral text written by us (not AI-generated) and its own permissive rule
  set, so a destination's frequency cap is never consumed by tests (test posts are excluded from the publish log).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalogue import service
from app.catalogue.normalize import host_of
from app.certification.evidence import HARNESS_VERSION, EvidencePack, StepRecord, iso
from app.certification.levels import ORDER, level_for
from app.certification.levels import delete_mode_name as _delete_mode
from app.certification.runtime import connector_for
from app.clock import Clock
from app.config import Settings
from app.connectors.base import CALL_META, BaseConnector
from app.contract.models import Connection, Destination, Rendition
from app.contract.registry import Registry, get_registry
from app.db.tables import AlertRow, CertificationRunRow, DestinationRow, LevelHistoryRow, TestPostRow
from app.errors import ErrorCode, IbxError
from app.jobs import queue
from app.logging import get_logger
from app.reliability import killswitch
from app.reliability.idempotency import advisory_lock, marker_for
from app.reliability.redact import redact
from app.storage.links import key_from_download_url, verify_download

log = get_logger("app.certification")

# Rules for the certification post only: neutral fixed text on our own instance. The destination's real
# rules still govern every real publication.
# frequency_cap is stated as None on purpose: inline rules are merged over the destination's record and catalogue
# rules (rules.gate.resolve_ruleset), so without it the test post inherited the real cap and a recertification of a
# destination posted to this week failed with DEST_RULE_FREQUENCY_CAP and demoted it (found by the recertification tests).
CERT_RULES: dict[str, Any] = {"automation_allowed": True, "self_promo": "allowed", "link_policy": "any", "frequency_cap": None,
                              "disclosure": {"required": False}, "language_policy": "any", "blocked_terms": [],
                              "source": "manual"}
POLL_INTERVAL_S = 1.0
TERMINAL_STATES = ("PASSED", "DEMOTED", "FAILED_PRECONDITION", "RESCHEDULED", "INCONCLUSIVE")
TRANSIENT_CODES = ("PLATFORM_RATE_LIMIT", "PLATFORM_QUOTA_EXHAUSTED")
# Evidence screenshots: which picture is taken after which confirmed step.
SHOT_AFTER = {"CONFIRM_LIVE": "live", "CONFIRM_PACK": "live", "CONFIRM_DELETED": "after_delete", "CONFIRM_REMOVED": "after_delete"}
API_RENDER_LABEL = "API response render (not a screenshot of a web page): this destination shows no web page here"
# Connectors whose live page can not be fetched on our own instance, so the platform API confirms the post
# instead (the step records which method was used). Lemmy: with federation off (every start after the first
# bootstrap) the backend serves no ActivityPub page, so ap_id answers 404 although the post exists
# (measured on the local instance); the API (GET /api/v3/post) answers 200.
LIVE_BY_API = {"lemmy": "local Lemmy with federation off serves no page at ap_id (404); the platform API confirms the post"}


class StepFailed(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass
class RunState:
    run_id: str
    destination: DestinationRow
    connector: BaseConnector
    contract_dest: Destination
    connection: Connection
    token: str
    idempotency_key: str
    marker: str
    platform_post_id: str | None = None
    live_url: str | None = None
    test_post_id: int | None = None
    deleted: bool = False
    delete_mode: str | None = None
    removal_mode: str | None = None
    platform_delete_mode: str | None = None
    metrics_ok: bool = False
    pack_keys: list[str] = field(default_factory=list)
    pack_marker: str | None = None
    notes: list[str] = field(default_factory=list)


def _failure_details(failure: StepFailed) -> dict[str, Any]:
    """The platform error's ``details`` inside a step failure (directly, or under ``error`` for PUBLISH)."""
    detail = failure.detail or {}
    return detail.get("details") or (detail.get("error") or {}).get("details") or {}


def _failure_cause(failure: StepFailed) -> tuple[str | None, list[str], str]:
    """(reason, fields, message) for the run result and the alert. A renamed/missing response field is named as
    ``response_schema_mismatch`` with its fields (HC5), never as a timeout or an unknown outcome."""
    details = _failure_details(failure)
    reason = details.get("reason") if isinstance(details.get("reason"), str) else None
    fields = [str(f) for f in details.get("fields") or []][:20]
    if reason == "response_schema_mismatch":
        return reason, fields, (f"response_schema_mismatch: the platform's answer no longer has the expected field(s) "
                                f"{', '.join(fields) or '?'}; the platform may have renamed a field in its API")
    return reason, fields, failure.message


def _transient(failure: StepFailed) -> bool:
    """Rate limit, quota, or an open circuit breaker: says nothing about the destination itself."""
    return failure.code in TRANSIENT_CODES or (failure.code == "PLATFORM_UNAVAILABLE" and "circuit" in _failure_details(failure))


def _retry_delay(failure: StepFailed, now: Any, cap_s: int) -> int:
    """Seconds until the platform's reset time (from retry_at / retry_after_s), between 1 and ``cap_s``."""
    details = _failure_details(failure)
    seconds = details.get("retry_after_s")
    retry_at = details.get("retry_at")
    if isinstance(retry_at, str):
        try:
            seconds = (datetime.fromisoformat(retry_at.replace("Z", "+00:00")) - now).total_seconds()
        except ValueError:
            pass
    if not isinstance(seconds, int | float):
        seconds = 60
    return int(min(max(1, seconds + 1), cap_s))


def _error_detail(err: Exception) -> dict[str, Any]:
    if isinstance(err, IbxError):
        return redact(err.to_body()["error"])
    return {"code": "INTERNAL_ERROR", "message": redact(f"{type(err).__name__}: {err}")}


class Harness:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        storage: Any,
        clock: Clock,
        registry: Registry | None = None,
        http: httpx.AsyncClient | None = None,
        progress: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.factory = session_factory
        self.storage = storage
        self.clock = clock
        self.registry = registry or get_registry()
        self.http = http
        self.progress = progress
        self._pack: Any = None
        self._shots: dict[str, dict[str, Any]] = {}
        self.reschedule_attempt = 0

    # ------------------------------------------------------------------ entry point

    async def run(self, run_id: str, *, reschedule_attempt: int = 0) -> dict[str, Any]:
        self.reschedule_attempt = reschedule_attempt
        async with self.factory() as session:
            run = await session.get(CertificationRunRow, run_id)
            if run is None:
                raise IbxError(ErrorCode.NOT_FOUND, f"Unknown certification run '{run_id}'.")
            if run.state in TERMINAL_STATES:
                return dict(run.result or {})
            destination_id = run.destination_id
        async with advisory_lock(self.factory, f"cert:{destination_id}", max(self.settings.IDEMPOTENCY_LOCK_WAIT_S, 60)):
            return await self._run_locked(run_id, destination_id)

    async def _run_locked(self, run_id: str, destination_id: str) -> dict[str, Any]:
        started = self.clock.now()
        t0 = time.perf_counter()
        self._shots = {}
        pack = EvidencePack(destination_id, run_id, self.storage)
        pack.state("QUEUED", started)
        async with self.factory() as session, session.begin():
            run = await session.get(CertificationRunRow, run_id, with_for_update=True)
            run.state, run.started_at, run.evidence_prefix = "PREFLIGHT", started, pack.prefix
            dest = await service.get_destination(session, destination_id)
            conn_row = await service.destination_connection(session, dest)
            level_before = dest.level
            dest_snapshot = await service.destination_out(session, dest, with_rules=True)
        manifest = self.registry.manifest(dest.connector_key)
        connector = None
        if manifest is not None:
            connector = connector_for(dest.connector_key, settings=self.settings, session_factory=self.factory,
                                      storage=self.storage, clock=self.clock, registry=self.registry)
            await pack.manifest_snapshot(manifest)
            if getattr(connector, "live_check_by_api", None):  # the connector says its page cannot prove live/deleted
                LIVE_BY_API.setdefault(dest.connector_key, connector.live_check_by_api)
        assisted = manifest is not None and (manifest.tier == "assisted" or "assist" in manifest.capabilities and "publish" not in manifest.capabilities)
        rules = (dest_snapshot.get("rules") or {}).get("rules")
        key = f"cert-{run_id}"
        state = RunState(
            run_id=run_id, destination=dest, connector=connector,  # type: ignore[arg-type]
            contract_dest=service.contract_destination(dest, rules if assisted else CERT_RULES),
            connection=service.contract_connection(conn_row, dest.connector_key, manifest.auth if manifest else "none"),
            token=f"ibxcert{run_id[-10:]}", idempotency_key=key, marker=marker_for(key),
        )

        plan: list[tuple[str, Callable[[RunState], Awaitable[dict[str, Any]]]]] = [("PREFLIGHT", self.preflight), ("VALIDATE", self.validate)]
        if assisted:
            plan += [("BUILD_PACK", self.build_pack), ("CONFIRM_PACK", self.confirm_pack),
                     ("DELETE", self.delete_pack), ("CONFIRM_DELETED", self.confirm_pack_deleted)]
        else:
            plan += [("PUBLISH", self.publish), ("CONFIRM_LIVE", self.confirm_live), ("STATUS", self.status)]
            if manifest is not None and "metrics" in manifest.capabilities:
                plan.append(("METRICS", self.metrics))
            # no delete API (Dev.to): the post is unpublished, so the last step is named for what it proves
            confirm = "CONFIRM_DELETED" if manifest is None or "delete" in manifest.capabilities else "CONFIRM_REMOVED"
            plan += [("DELETE", self.delete), (confirm, self.confirm_deleted)]

        failure: StepFailed | None = None
        failed_step: str | None = None
        for n, (name, fn) in enumerate(plan, start=1):
            await self._set_state(run_id, name, pack)
            if self.progress:
                await self.progress(int(n * 90 / (len(plan) + 1)), name)
            step_start = self.clock.now()
            t = time.perf_counter()
            try:
                if connector is None and name != "PREFLIGHT":
                    raise StepFailed("CONNECTOR_NOT_FOUND", "connector is not registered")
                detail = await fn(state)
                await pack.add_step(StepRecord(n, name, "ok", step_start, (time.perf_counter() - t) * 1000, detail))
                if name in SHOT_AFTER:
                    await self._screenshot(SHOT_AFTER[name], state, detail, pack)
            except StepFailed as exc:
                failure, failed_step = exc, name
            except IbxError as exc:
                failure, failed_step = StepFailed(exc.code.value, exc.message, _error_detail(exc)), name
            except Exception as exc:  # noqa: BLE001 - a harness bug fails the run (and demotes), never hangs it
                log.error("certification_step_crashed", run_id=run_id, step=name, error=redact(exc))
                failure, failed_step = StepFailed("INTERNAL_ERROR", f"{type(exc).__name__} in {name}"), name
            if failure is not None:
                await pack.add_step(StepRecord(n, name, "failed", step_start, (time.perf_counter() - t) * 1000,
                                               {"code": failure.code, "message": failure.message, **failure.detail}))
                break

        if failure is None:
            return await self._passed(run_id, state, pack, manifest, level_before, started, t0)
        if failed_step == "PREFLIGHT" and failure.code == "DEST_NOT_OWN_INSTANCE":
            return await self._not_certifiable_here(run_id, state, pack, level_before, failure, started, t0)
        if _transient(failure):
            return await self._reschedule(run_id, state, pack, level_before, failed_step or "?", failure, started, t0, len(plan) + 1)
        return await self._failed(run_id, state, pack, manifest, level_before, failed_step or "?", failure, started, t0, len(plan) + 1)

    # ------------------------------------------------------------------ outcome

    async def _passed(self, run_id: str, st: RunState, pack: EvidencePack, manifest: Any, level_before: str,
                      started: Any, t0: float) -> dict[str, Any]:
        now = self.clock.now()
        level, reason = level_for(manifest, delete_mode=st.delete_mode, metrics_ok=st.metrics_ok)
        pack.state("PASSED", now, reason)
        async with self.factory() as session, session.begin():
            dest = await session.get(DestinationRow, st.destination.id, with_for_update=True)
            dest.level, dest.last_certified_at, dest.status, dest.excluded_reason = level, now, "active", None
            if level != level_before:
                session.add(LevelHistoryRow(destination_id=dest.id, level_before=level_before, level_after=level,
                                            reason=f"certification {run_id} passed: {reason}", run_id=run_id))
            await session.execute(
                update(AlertRow).where(AlertRow.destination_id == dest.id, AlertRow.kind == "certification_failed",
                                       AlertRow.status == "open").values(status="resolved"))
        result = {"run_id": run_id, "destination_id": st.destination.id, "connector_key": st.destination.connector_key,
                  "passed": True, "state": "PASSED", "level_before": level_before, "level_after": level,
                  "level_reason": reason, "delete_mode": st.delete_mode,
                  "platform_delete_mode": st.platform_delete_mode or st.removal_mode, "platform_post_id": st.platform_post_id,
                  "live_url": st.live_url, "notes": st.notes}
        return await self._finish(run_id, pack, result, "PASSED", None, started, t0)

    async def _failed(self, run_id: str, st: RunState, pack: EvidencePack, manifest: Any, level_before: str,
                      step: str, failure: StepFailed, started: Any, t0: float, n: int) -> dict[str, Any]:
        pack.state("FAILED", self.clock.now(), f"{step}: {failure.code}")
        await self._set_state(run_id, "CLEANUP", pack)
        t = time.perf_counter()
        cleanup_start = self.clock.now()
        cleanup = await self.cleanup(st)
        await pack.add_step(StepRecord(n, "CLEANUP", "ok" if cleanup.get("ok") else "failed", cleanup_start,
                                       (time.perf_counter() - t) * 1000, cleanup))
        now = self.clock.now()
        cause, fields, message = _failure_cause(failure)
        reason = f"certification {run_id} failed at {step}: {failure.code}: {message}"[:1000]
        async with self.factory() as session, session.begin():
            dest = await session.get(DestinationRow, st.destination.id, with_for_update=True)
            dest.level, dest.status, dest.excluded_reason = "L0", "excluded", reason
            if level_before != "L0":
                session.add(LevelHistoryRow(destination_id=dest.id, level_before=level_before, level_after="L0",
                                            reason=reason, run_id=run_id))
            else:
                session.add(LevelHistoryRow(destination_id=dest.id, level_before="L0", level_after="L0",
                                            reason=reason + " (excluded)", run_id=run_id))
            session.add(AlertRow(kind="certification_failed", destination_id=dest.id, connection_id=dest.connection_id,
                                 body=redact({"run_id": run_id, "destination": dest.name, "connector_key": dest.connector_key,
                                              "step": step, "code": failure.code, "message": message,
                                              "reason": cause, "fields": fields,
                                              "action": "destination demoted to L0 and excluded until it passes again",
                                              "cleanup": cleanup}),
                                 status="open", delivered_to=["log"]))
        log.warning("certification_failed_destination_demoted", run_id=run_id, destination_id=st.destination.id,
                    step=step, code=failure.code)
        pack.state("DEMOTED", now, "level L0, status excluded, alert opened")
        result = {"run_id": run_id, "destination_id": st.destination.id, "connector_key": st.destination.connector_key,
                  "passed": False, "state": "DEMOTED", "failed_step": step, "failure_code": failure.code,
                  "failure_message": message, "failure_reason": cause, "failure_fields": fields,
                  "level_before": level_before, "level_after": "L0",
                  "excluded": True, "cleanup": cleanup, "platform_post_id": st.platform_post_id, "notes": st.notes}
        return await self._finish(run_id, pack, result, "DEMOTED", failure.code, started, t0)

    async def _not_certifiable_here(self, run_id: str, st: RunState, pack: EvidencePack, level_before: str,
                                    failure: StepFailed, started: Any, t0: float) -> dict[str, Any]:
        """A real host in local mode: nothing was sent and nothing is known about the destination, so it keeps its
        level and status. Certify it in simulation mode, or point it at an own instance."""
        pack.state("FAILED_PRECONDITION", self.clock.now(), "not certifiable here: no request was sent")
        log.info("certification_not_certifiable_here", run_id=run_id, destination_id=st.destination.id, code=failure.code)
        result = {"run_id": run_id, "destination_id": st.destination.id, "connector_key": st.destination.connector_key,
                  "passed": False, "state": "FAILED_PRECONDITION", "failed_step": "PREFLIGHT", "failure_code": failure.code,
                  "failure_message": failure.message, "level_before": level_before, "level_after": level_before,
                  "excluded": False, "notes": [*st.notes, "nothing was sent; the destination keeps its level and status"]}
        return await self._finish(run_id, pack, result, "FAILED_PRECONDITION", failure.code, started, t0)

    async def _reschedule(self, run_id: str, st: RunState, pack: EvidencePack, level_before: str, step: str,
                          failure: StepFailed, started: Any, t0: float, n: int) -> dict[str, Any]:
        """A rate limit (or an open breaker) is about the platform's load, not the destination's health: clean up,
        keep the level, and certify again at the reset time."""
        await self._set_state(run_id, "CLEANUP", pack)
        t, cleanup_start = time.perf_counter(), self.clock.now()
        cleanup = await self.cleanup(st)
        await pack.add_step(StepRecord(n, "CLEANUP", "ok" if cleanup.get("ok") else "failed", cleanup_start,
                                       (time.perf_counter() - t) * 1000, cleanup))
        now = self.clock.now()
        result = {"run_id": run_id, "destination_id": st.destination.id, "connector_key": st.destination.connector_key,
                  "passed": False, "failed_step": step, "failure_code": failure.code, "failure_message": failure.message,
                  "level_before": level_before, "level_after": level_before, "excluded": False, "cleanup": cleanup,
                  "platform_post_id": st.platform_post_id, "reschedule_attempt": self.reschedule_attempt, "notes": st.notes}
        if self.reschedule_attempt >= self.settings.CERT_RESCHEDULE_MAX:
            async with self.factory() as session, session.begin():
                session.add(AlertRow(kind="certification_inconclusive", destination_id=st.destination.id,
                                     connection_id=st.destination.connection_id, status="open", delivered_to=["log"],
                                     body=redact({"run_id": run_id, "step": step, "code": failure.code, "message": failure.message,
                                                  "action": "level kept; certify again later"})))
            pack.state("INCONCLUSIVE", now, f"{failure.code} {self.reschedule_attempt + 1} times in a row; level kept")
            return await self._finish(run_id, pack, {**result, "state": "INCONCLUSIVE"}, "INCONCLUSIVE", failure.code, started, t0)
        retry_at = now + timedelta(seconds=_retry_delay(failure, now, self.settings.CERT_RESCHEDULE_MAX_DELAY_S))
        async with self.factory() as session, session.begin():
            nxt = await create_run(session, st.destination.id, "rate_limit_retry")
            job = await queue.enqueue(session, "certification.run", {"run_id": nxt.id, "reschedule_attempt": self.reschedule_attempt + 1},
                                      now=now, run_after=retry_at)
            nxt.job_id = job.id
        log.info("certification_rescheduled", run_id=run_id, next_run_id=nxt.id, code=failure.code, retry_at=iso(retry_at))
        pack.state("RESCHEDULED", now, f"{failure.code}: next run {nxt.id} at {iso(retry_at)}; level kept")
        result = {**result, "state": "RESCHEDULED", "next_run_id": nxt.id, "retry_at": iso(retry_at)}
        return await self._finish(run_id, pack, result, "RESCHEDULED", failure.code, started, t0)

    async def _finish(self, run_id: str, pack: EvidencePack, result: dict[str, Any], state: str, code: str | None,
                      started: Any, t0: float) -> dict[str, Any]:
        finished = self.clock.now()
        duration_ms = int((time.perf_counter() - t0) * 1000)
        run_doc = {
            "run_id": run_id, "harness_version": HARNESS_VERSION, "destination_id": pack.destination_id,
            "simulation": self.settings.SIMULATION_MODE, "started_at": iso(started), "finished_at": iso(finished),
            "duration_ms": duration_ms, "timeline": pack.timeline, "steps": [s.as_dict() for s in pack.steps], "result": result,
        }
        confirm = next((s for s in reversed(pack.steps) if s.step in ("CONFIRM_DELETED", "CONFIRM_REMOVED", "CLEANUP")), None)
        if confirm is not None:
            removed_only = any(s.step == "CONFIRM_REMOVED" for s in pack.steps)
            await pack.put("removal_confirmation.json" if removed_only else "deletion_confirmation.json",
                           {"run_id": run_id, "delete_mode": result.get("delete_mode"), "deleted": not removed_only and
                            result.get("delete_mode") in ("deleted", "soft_deleted", "purged", "pack_revoked"),
                            "step": confirm.as_dict()})
        if self._shots:
            await pack.put("screenshots.json", {"run_id": run_id, "simulation": self.settings.SIMULATION_MODE, **self._shots})
        await pack.put("run.json", run_doc)
        await pack.put("summary.md", _summary(run_doc), "text/markdown; charset=utf-8")
        result = {**result, "evidence_prefix": pack.prefix, "evidence_files": dict(sorted(pack.files.items()))}
        async with self.factory() as session, session.begin():
            run = await session.get(CertificationRunRow, run_id, with_for_update=True)
            run.state, run.result, run.failure_code = state, redact(result), code
            run.steps = [s.as_dict() for s in pack.steps]
            run.finished_at, run.duration_ms = finished, duration_ms
        return result

    async def _set_state(self, run_id: str, name: str, pack: EvidencePack) -> None:
        pack.state(name, self.clock.now())
        async with self.factory() as session, session.begin():
            await session.execute(update(CertificationRunRow).where(CertificationRunRow.id == run_id).values(
                state=name, steps=[s.as_dict() for s in pack.steps]))

    # ------------------------------------------------------------------ helpers

    def _own_host(self, url: str | None) -> bool:
        host = host_of(url or "")
        if not host:
            return False
        mock_host = host_of(self.settings.MOCK_PLATFORM_URL)
        # our own API's public host is an own instance too (the MRSS feed lives there; on a PaaS it is e.g.
        # <name>.onrender.com, not one of the compose names in CERT_OWN_HOSTS)
        own_api = host_of(self.settings.PUBLIC_BASE_URL)
        return host in set(self.settings.CERT_OWN_HOSTS) | {mock_host, own_api}

    def _surface_spec(self, st: RunState) -> dict[str, Any]:
        surface = st.connector.manifest.surface(st.destination.surface) or st.connector.manifest.surfaces[0]
        return {**surface.spec, **(st.destination.spec_overrides or {}), "content_types": list(surface.content_types)}

    def _rendition(self, st: RunState, *, oversize: bool = False) -> Rendition:
        spec = self._surface_spec(st)
        title = f"IncuBrix certification test {st.token}"
        removal = ("delete", "is deleted within 10 minutes") if "delete" in st.connector.manifest.capabilities else \
            ("removal", "is unpublished (taken off the public site) within 10 minutes")
        body = (f"Automated certification test post {st.token} from the IncuBrix destination network. "
                f"It checks publish, live URL, status and {removal[0]} on our own test instance, and {removal[1]}.")
        tmax, bmax = spec.get("title_max_chars"), spec.get("body_max_chars")
        if isinstance(tmax, int) and len(title) > tmax:
            title = title[: max(1, tmax - len(st.token) - 1)].rstrip() + " " + st.token if tmax > len(st.token) + 2 else st.token[:tmax]
        if isinstance(bmax, int) and len(body) > bmax:
            body = (st.token + " " + body)[:bmax]
        if oversize:
            if isinstance(tmax, int):
                title = "T" * (tmax + 25)
            elif isinstance(bmax, int):
                body = "B" * (bmax + 25)
        types = spec.get("content_types") or ["text"]
        ctype = "text" if "text" in types else types[0]
        link = "https://example.com/" if ctype == "link" else None
        return Rendition(rendition_id=f"cert-{st.run_id}", surface=st.destination.surface, content_type=ctype,
                         title=title, body=body, body_format="markdown", link_url=link, tags=[], language="en",
                         is_promotional=False, ai_generated=False)

    async def _get(self, url: str) -> httpx.Response:
        if self.http is not None:
            return await self.http.get(url, follow_redirects=True, timeout=self.settings.PLATFORM_TIMEOUT_S)
        async with httpx.AsyncClient(follow_redirects=True, timeout=self.settings.PLATFORM_TIMEOUT_S) as client:
            return await client.get(url)

    async def _poll(self, url: str, ok: Callable[[httpx.Response], bool]) -> tuple[bool, list[dict[str, Any]]]:
        deadline = self.clock.now() + timedelta(seconds=self.settings.CERT_LIVE_TIMEOUT_S)
        attempts: list[dict[str, Any]] = []
        while True:
            try:
                response = await self._get(url)
                attempts.append({"at": iso(self.clock.now()), "status": response.status_code, "bytes": len(response.content)})
                if ok(response):
                    return True, attempts
            except httpx.HTTPError as exc:
                attempts.append({"at": iso(self.clock.now()), "error": type(exc).__name__})
            if self.clock.now() >= deadline:
                return False, attempts
            await self.clock.sleep(POLL_INTERVAL_S)

    async def _record_test_post(self, st: RunState) -> None:
        now = self.clock.now()
        async with self.factory() as session, session.begin():
            row = TestPostRow(connector_key=st.destination.connector_key, destination_id=st.destination.id,
                              platform_post_id=st.platform_post_id or f"marker:{st.marker}", live_url=st.live_url,
                              run_id=st.run_id, created_at=now, expires_at=now + timedelta(seconds=self.settings.TEST_POST_TTL_S))
            session.add(row)
            await session.flush()
            st.test_post_id = row.id

    async def _update_test_post(self, st: RunState) -> None:
        """The publish answered: replace the write-ahead ``marker:`` id with the real post id and URL."""
        async with self.factory() as session, session.begin():
            row = await session.get(TestPostRow, st.test_post_id, with_for_update=True)
            if row is not None:
                row.platform_post_id, row.live_url = st.platform_post_id or row.platform_post_id, st.live_url

    async def _mark_test_post(self, st: RunState, mode: str) -> None:
        if st.test_post_id is None:
            return
        async with self.factory() as session, session.begin():
            row = await session.get(TestPostRow, st.test_post_id, with_for_update=True)
            if row is not None and row.deleted_at is None:
                row.deleted_at, row.delete_mode = self.clock.now(), mode

    # ------------------------------------------------------------------ evidence screenshots

    def _page_url(self, url: str | None) -> str | None:
        """The URL a browser can open for this post: ``SCREENSHOT_URL_REWRITES`` ("from=>to" prefixes) map e.g. a
        Lemmy post id URL to lemmy-ui. None when the post has no page here (Lemmy without lemmy-ui)."""
        if not url:
            return None
        for rule in self.settings.SCREENSHOT_URL_REWRITES:
            source, _, target = rule.partition("=>")
            if source and target and url.lower().startswith(source):
                return target + url[len(source):]
        return url

    async def _screenshot(self, name: str, st: RunState, detail: dict[str, Any], pack: EvidencePack) -> None:
        """Take ``<name>.png`` with the shooter: the page when there is one, else a labelled render of the API
        response. Evidence only: any problem is recorded in screenshots.json and never fails the run."""
        if not self.settings.SCREENSHOT_URL:
            self._shots[name] = {"taken": False, "why": "no shooter: SCREENSHOT_URL is not set (start compose profile "
                                 "`evidence` and set SCREENSHOT_URL=http://shooter:3000)"}
            return
        assisted = bool(st.pack_keys)
        if assisted:
            page = st.live_url if name == "live" and self._own_host(st.live_url) else None
        elif st.destination.connector_key in LIVE_BY_API:
            rewritten = self._page_url(st.live_url)
            page = rewritten if rewritten != st.live_url else None  # the backend URL itself shows no page
        else:
            page = self._page_url(st.live_url)
        title = f"{st.destination.name}: {name.replace('_', ' ')} ({st.destination.connector_key}, run {st.run_id})"
        try:
            if page:
                request = {"url": page, "title": title}
                path = "/shoot"
            else:
                data = detail
                if not assisted and name == "live":
                    data = detail.get("status") or (await st.connector.get_status(
                        st.platform_post_id or "", st.connection, st.contract_dest)).model_dump(mode="json")
                elif not assisted:
                    data = detail.get("status_after") or detail
                request = {"title": title, "data": redact(data), "note": f"get_status / evidence of step for '{name}'"}
                path = "/render"
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(self.settings.SCREENSHOT_URL.rstrip("/") + path, json=request)
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";")[0] != "image/png":
                raise ValueError("the shooter did not answer a PNG")
            await pack.put_binary(f"{name}.png", response.content, "image/png")
            kind = response.headers.get("X-Shot-Kind", "page" if page else "api_render")
            status = response.headers.get("X-Shot-Status")
            label = (API_RENDER_LABEL if kind == "api_render" else
                     f"browser screenshot of {page} (HTTP {status})" + (" - a mock-platforms page (simulation mode)"
                                                                        if self.settings.SIMULATION_MODE else ""))
            self._shots[name] = {"taken": True, "file": f"{name}.png", "kind": kind, "url": page, "label": label,
                                 "http_status": int(status) if status and status.isdigit() else None,
                                 "content_type": response.headers.get("X-Shot-Content-Type"),
                                 "note": response.headers.get("X-Shot-Note"), "at": iso(self.clock.now())}
        except Exception as exc:  # noqa: BLE001 - evidence only: never fails or changes the certification
            log.warning("certification_screenshot_failed", run_id=st.run_id, shot=name, error=redact(exc))
            reason = ""
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    reason = f": {exc.response.json().get('error')}"
                except ValueError:
                    pass
            self._shots[name] = {"taken": False, "url": page, "error": f"{type(exc).__name__}{reason}",
                                 "why": "the shooter failed; the certification result is unaffected"}

    # ------------------------------------------------------------------ steps (native / protocol / feed)

    async def preflight(self, st: RunState) -> dict[str, Any]:
        key = st.destination.connector_key
        if key in self.registry.errors:
            raise StepFailed("CONNECTOR_NOT_FOUND", f"connector '{key}' failed to load: {self.registry.errors[key]}")
        if st.connector is None:
            raise StepFailed("CONNECTOR_NOT_FOUND", f"connector '{key}' is not registered")
        async with self.factory() as session:
            await killswitch.ensure_enabled(session, self.settings, key)
        param = st.connector.manifest.instance_param
        value = str(st.destination.instance.get(param) or "")
        sandbox = bool((st.destination.extra or {}).get("own_sandbox"))
        if self.settings.SIMULATION_MODE:
            target = "simulation: every call goes to mock-platforms"
        elif "://" in value:
            if not self._own_host(value):
                raise StepFailed("DEST_NOT_OWN_INSTANCE", f"refusing to post a test to '{host_of(value)}': certification only posts to "
                                 "own test instances (CERT_OWN_HOSTS) or in simulation mode", {"host": host_of(value)})
            target = f"own instance {host_of(value)}"
        elif sandbox:
            target = "destination flagged own_sandbox"
        else:
            raise StepFailed("DEST_NOT_OWN_INSTANCE", f"instance.{param} is not a URL; mark the destination own_sandbox to certify it")
        detail: dict[str, Any] = {"connector_key": key, "connector_version": st.connector.manifest.version,
                                  "tier": st.connector.manifest.tier, "capabilities": list(st.connector.manifest.capabilities),
                                  "target": target, "kill_switch": "off", "connection_id": st.connection.connection_id}
        async with self.factory() as session:
            rules = await service.confirmed_rules(session, st.destination.id)
        detail["rules_confirmed"] = rules is not None
        if rules is None:
            st.notes.append("rules not confirmed: only this certification post is allowed (own instance)")
        if "refresh_auth" in st.connector.manifest.capabilities and (st.connection.connection_id or st.connection.credentials):
            result = await st.connector.refresh_auth(st.connection, st.contract_dest)
            detail["auth_probe"] = {"method": "refresh_auth", "expires_at": iso(result.expires_at)}
        else:
            detail["auth_probe"] = "no refresh_auth capability: the PUBLISH step proves the credentials"
        return detail

    async def validate(self, st: RunState) -> dict[str, Any]:
        good = self._rendition(st)
        ok = await st.connector.validate(good, st.contract_dest, st.connection)
        if not ok.ok:
            raise StepFailed("DEST_SPEC_VIOLATION", "the certification rendition did not pass validate",
                             {"violations": [v.model_dump() for v in ok.violations]})
        detail: dict[str, Any] = {"good_rendition": {"title": good.title, "ok": True}}
        spec = self._surface_spec(st)
        if isinstance(spec.get("title_max_chars"), int) or isinstance(spec.get("body_max_chars"), int):
            bad = await st.connector.validate(self._rendition(st, oversize=True), st.contract_dest, st.connection)
            if bad.ok:
                raise StepFailed("DEST_SPEC_VIOLATION", "an over-long rendition was accepted; spec checks are not enforced")
            detail["over_long_rendition"] = {"ok": False, "violations": [v.model_dump() for v in bad.violations]}
        else:
            detail["over_long_rendition"] = "skipped: the surface declares no length limits"
        return detail

    async def publish(self, st: RunState) -> dict[str, Any]:
        rendition = self._rendition(st)
        await self._record_test_post(st)  # write-ahead: "marker:<marker>" BEFORE the call, so the sweeper always sees it
        try:
            result = await st.connector.publish(rendition, st.contract_dest, st.connection, st.idempotency_key)
        except IbxError as err:
            raise StepFailed(err.code.value, err.message, {"error": _error_detail(err), "idempotency_key": st.idempotency_key}) from err
        st.platform_post_id, st.live_url = result.platform_post_id, result.live_url
        await self._update_test_post(st)
        if result.status != "published":
            raise StepFailed("PLATFORM_CONTENT_REJECTED", f"the test post is '{result.status}', not published",
                             {"result": result.model_dump(mode="json")})
        return {"request": {"rendition": rendition.model_dump(mode="json"), "idempotency_key": st.idempotency_key,
                            "marker": st.marker},
                "response": result.model_dump(mode="json"), "test_post_id": st.test_post_id,
                "expires_after_s": self.settings.TEST_POST_TTL_S}

    async def confirm_live(self, st: RunState) -> dict[str, Any]:
        if not st.live_url:
            raise StepFailed("PLATFORM_UNAVAILABLE", "publish returned no live_url")
        if not self._own_host(st.live_url):
            raise StepFailed("DEST_NOT_OWN_INSTANCE", f"live URL host '{host_of(st.live_url)}' is not an own instance; not fetched")

        def shows_post(r: httpx.Response) -> bool:
            return r.status_code == 200 and (st.token in r.text or st.marker in r.text or st.marker in str(r.url))

        api_reason = LIVE_BY_API.get(st.destination.connector_key)
        if api_reason is not None:
            attempts: list[dict[str, Any]] = []
            try:
                response = await self._get(st.live_url)
                attempts.append({"at": iso(self.clock.now()), "status": response.status_code})
                if shows_post(response):
                    return {"method": "http_get_live_url", "live_url": st.live_url, "http_status": 200, "marker_found": True,
                            "attempts": attempts}
            except httpx.HTTPError as exc:
                attempts.append({"at": iso(self.clock.now()), "error": type(exc).__name__})
            status = await st.connector.get_status(st.platform_post_id or "", st.connection, st.contract_dest)
            if status.status != "published":
                raise StepFailed("PLATFORM_UNAVAILABLE", f"the platform API says '{status.status}', not published",
                                 {"live_url_attempts": attempts})
            return {"method": "platform_api_get_status", "why": api_reason, "live_url": st.live_url,
                    "status": status.model_dump(mode="json"), "live_url_attempts": attempts}

        found, attempts = await self._poll(st.live_url, shows_post)
        if not found:
            raise StepFailed("PLATFORM_UNAVAILABLE", "the live URL did not show the test post (200 + marker) in time",
                             {"live_url": st.live_url, "attempts": attempts})
        return {"method": "http_get_live_url", "live_url": st.live_url, "http_status": 200, "marker_found": True, "attempts": attempts}

    async def status(self, st: RunState) -> dict[str, Any]:
        result = await st.connector.get_status(st.platform_post_id or "", st.connection, st.contract_dest)
        if result.status != "published":
            raise StepFailed("PLATFORM_UNAVAILABLE", f"get_status says '{result.status}', expected 'published'",
                             {"response": result.model_dump(mode="json")})
        return {"response": result.model_dump(mode="json")}

    async def metrics(self, st: RunState) -> dict[str, Any]:
        result = await st.connector.fetch_metrics(st.platform_post_id or "", st.connection, None, st.contract_dest)
        values = result.model_dump(mode="json")
        st.metrics_ok = True
        return {"response": values, "exposed": sorted(k for k, v in values.items() if v is not None),
                "null_means": "the platform does not expose this metric (never a fake 0)"}

    async def delete(self, st: RunState) -> dict[str, Any]:
        if "delete" not in st.connector.manifest.capabilities:
            unpublish = getattr(st.connector, "unpublish_to_draft", None)
            if unpublish is None:
                raise StepFailed("METHOD_NOT_SUPPORTED", "the connector can not remove its test post (no delete capability)")
            # e.g. Dev.to: no delete API. The post is taken off the public site; it is never reported as deleted.
            record = await unpublish(st.platform_post_id or "", st.connection, st.contract_dest)
            st.deleted, st.removal_mode = False, str(record.get("removal_mode") or "unpublish_to_draft")
            detail = {"method": "unpublish_to_draft", "response": record,
                      "note": "the platform has no delete API: unpublished, not deleted"}
            if st.removal_mode == "admin_unpublish":
                detail["admin_path"] = ("admin_unpublish (PUT /api/articles/{id}/unpublish) needs an admin of the Forem "
                                        "instance: it applies only to our own Forem mock (instance.forem_admin: true). A normal "
                                        "dev.to account can not do this; it uses unpublish_to_draft.")
            return detail
        meta: dict[str, Any] = {}
        token = CALL_META.set(meta)
        try:
            result = await st.connector.delete(st.platform_post_id or "", st.connection, st.contract_dest)
        finally:
            CALL_META.reset(token)
        st.deleted = result.deleted
        platform_mode = meta.get("delete_mode") or getattr(st.connector, "delete_mode", None)
        st.platform_delete_mode = str(platform_mode) if platform_mode else None
        st.removal_mode = _delete_mode(platform_mode) if result.deleted else None
        return {"response": result.model_dump(mode="json"), "platform_delete_mode": platform_mode}

    async def confirm_deleted(self, st: RunState) -> dict[str, Any]:
        status = await st.connector.get_status(st.platform_post_id or "", st.connection, st.contract_dest)
        if status.status in ("deleted", "removed_by_platform") and st.deleted:
            mode = st.removal_mode or "deleted"  # soft_deleted (Discourse), purged (Lemmy), deleted (Ghost, webhook)
        elif not st.deleted and status.status in ("draft", "deleted", "removed_by_platform"):
            mode = st.removal_mode or "unpublish_to_draft"  # e.g. Dev.to: no delete API; never reported as deleted
        else:
            raise StepFailed("PLATFORM_UNAVAILABLE", f"after delete, get_status says '{status.status}' (deleted={st.deleted})",
                             {"status": status.model_dump(mode="json")})
        detail: dict[str, Any] = {"status_after": status.model_dump(mode="json"), "delete_mode": mode,
                                  "platform_delete_mode": st.platform_delete_mode, "method": "platform_api_get_status"}
        if st.destination.connector_key in LIVE_BY_API:
            detail["live_url_after"] = "not used: " + LIVE_BY_API[st.destination.connector_key]
        elif st.live_url and self._own_host(st.live_url):
            detail["method"] = "platform_api_get_status + http_get_live_url"
            gone, attempts = await self._poll(st.live_url, lambda r: r.status_code in (404, 410) or 400 <= r.status_code < 500
                                              and r.status_code not in (408, 429))
            if not gone:
                raise StepFailed("PLATFORM_UNAVAILABLE", "the live URL still answers after delete", {"attempts": attempts})
            detail["live_url_after"] = {"url": st.live_url, "http_status": attempts[-1].get("status"), "attempts": attempts}
        st.delete_mode = mode
        await self._mark_test_post(st, mode)
        return detail

    # ------------------------------------------------------------------ steps (assisted)

    async def build_pack(self, st: RunState) -> dict[str, Any]:
        rendition = self._rendition(st)
        marker_fn = getattr(sys.modules.get(type(st.connector).__module__), "pack_marker", None)
        if marker_fn is not None:
            st.pack_marker = marker_fn(rendition, st.connector.destination_key(st.contract_dest))
        # the neutral certification pack is never posted, so the destination's rules are listed (checked in
        # CONFIRM_PACK) but not enforced on it; every real pack goes through the rules gate
        pack = await st.connector.build_assist_pack(rendition, st.contract_dest, enforce_rules=False)
        st.notes.append(f"assist pack with {len(pack.files)} file(s)")
        st.live_url = pack.deep_link
        self._pack = pack
        return {"response": pack.model_dump(mode="json")}

    def _key_for(self, url: str) -> str | None:
        signed = key_from_download_url(url)
        if signed is not None:
            return signed[0]
        bucket = getattr(self.storage, "bucket", None) or self.settings.S3_BUCKET
        path = urlsplit(url).path.lstrip("/")
        if url.startswith("memory://"):
            return url.split("memory://", 1)[1].split("/", 1)[1].split("?", 1)[0]
        if path.startswith(f"{bucket}/"):
            return path[len(bucket) + 1:]
        return None

    async def confirm_pack(self, st: RunState) -> dict[str, Any]:
        pack = self._pack
        if not pack.caption.strip():
            raise StepFailed("DEST_SPEC_VIOLATION", "the pack has an empty caption")
        files = []
        for f in pack.files:
            key = self._key_for(f.url)
            if key is None:
                raise StepFailed("PLATFORM_UNAVAILABLE", f"pack file '{f.name}' is not one of our stored files", {"url": f.url})
            signed = key_from_download_url(f.url)
            if signed is not None:
                verify_download(key, signed[1], signed[2], self.settings, self.clock.now())
            info = await self.storage.stat(key)
            if info is None:
                raise StepFailed("PLATFORM_UNAVAILABLE", f"pack file '{f.name}' is not reachable", {"key": key})
            st.pack_keys.append(key)
            files.append({"name": f.name, "key": key, "bytes": info.size, "link": "signed link verified" if signed else "stored"})
        detail: dict[str, Any] = {"files": files, "caption_chars": len(pack.caption)}
        if self._own_host(pack.deep_link):
            ok, attempts = await self._poll(pack.deep_link, lambda r: r.status_code < 400)
            if not ok:
                raise StepFailed("PLATFORM_UNAVAILABLE", "the deep link (submit page) did not answer", {"attempts": attempts})
            detail["deep_link"] = {"url": pack.deep_link, "http_status": attempts[-1].get("status")}
        else:
            detail["deep_link"] = {"url": pack.deep_link, "checked": False, "why": "external site: no request is sent to real sites"}
        missing = _missing_rules(st.contract_dest.rules, pack.checklist)
        if missing:
            raise StepFailed("DEST_SPEC_VIOLATION", f"confirmed rules missing from the checklist: {', '.join(missing)}",
                             {"checklist": pack.checklist})
        detail["checklist"] = pack.checklist
        return detail

    async def _purge_pack(self, st: RunState) -> list[str]:
        purged: list[str] = []
        purge = getattr(st.connector, "purge_pack", None)
        if purge is not None and st.pack_marker:
            purged = list(await purge(st.pack_marker))  # every object of the pack, its index included
        for key in st.pack_keys:
            if key not in purged:
                await self.storage.delete(key)
                purged.append(key)
        return purged

    async def delete_pack(self, st: RunState) -> dict[str, Any]:
        return {"deleted_keys": await self._purge_pack(st), "pack_marker": st.pack_marker}

    async def confirm_pack_deleted(self, st: RunState) -> dict[str, Any]:
        still = [k for k in st.pack_keys if await self.storage.stat(k) is not None]
        if still:
            raise StepFailed("PLATFORM_UNAVAILABLE", "pack files still exist after delete", {"keys": still})
        st.delete_mode = "pack_revoked"
        return {"delete_mode": "pack_revoked", "links_now": "404 from /api/v1/files (object gone)", "keys": st.pack_keys}

    # ------------------------------------------------------------------ cleanup

    async def cleanup(self, st: RunState) -> dict[str, Any]:
        """Best-effort removal of anything the failed run created: by id, else by the marker."""
        if st.connector is None:
            return {"ok": True, "action": "nothing to clean (no connector)"}
        if st.pack_keys or st.pack_marker:
            try:
                keys = await self._purge_pack(st)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "action": "pack purge failed", "error": _error_detail(exc)}
            return {"ok": True, "action": "pack files deleted", "keys": keys}
        if st.deleted or st.delete_mode:
            return {"ok": True, "action": "test post already removed"}
        caps = st.connector.manifest.capabilities
        if "publish" not in caps:
            return {"ok": True, "action": "nothing published"}
        post_id = st.platform_post_id
        found_by = "id"
        if post_id is None:
            found_by = "marker"
            try:
                conn = await st.connector._resolve_connection(st.connection)
                dest, conn = st.connector.simulate(st.contract_dest, conn)
                found = await st.connector._lookup(st.connector.ctx, st.marker, dest, conn)
            except Exception as exc:  # noqa: BLE001 - e.g. the same schema drift that failed the run
                return {"ok": False, "action": "lookup by marker failed", "marker": st.marker, "error": _error_detail(exc)}
            if found is None:
                await self._mark_test_post(st, "not_created")  # the write-ahead row: nothing reached the platform
                return {"ok": True, "action": "no post found by marker: nothing was created", "marker": st.marker}
            post_id, st.platform_post_id, st.live_url = found.platform_post_id, found.platform_post_id, found.live_url
            if st.test_post_id is None:
                await self._record_test_post(st)
            else:
                await self._update_test_post(st)
        unpublish = getattr(st.connector, "unpublish_to_draft", None)
        if "delete" not in caps and unpublish is not None:
            try:
                record = await unpublish(post_id, st.connection, st.contract_dest)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "action": "unpublish failed; the sweeper retries", "platform_post_id": post_id, "error": _error_detail(exc)}
            mode = str(record.get("removal_mode") or "unpublish_to_draft")
            await self._mark_test_post(st, f"cleanup_{mode}")
            return {"ok": True, "action": mode, "found_by": found_by, "platform_post_id": post_id, "deleted": False}
        if "delete" not in caps:
            return {"ok": False, "action": "connector has no delete; the sweeper and an alert follow", "platform_post_id": post_id}
        meta: dict[str, Any] = {}
        token = CALL_META.set(meta)
        try:
            result = await st.connector.delete(post_id, st.connection, st.contract_dest)
        except Exception as exc:  # noqa: BLE001
            try:  # e.g. Discourse: deleting an already-deleted topic answers 422
                gone = (await st.connector.get_status(post_id, st.connection, st.contract_dest)).status in ("deleted", "removed_by_platform")
            except Exception:  # noqa: BLE001
                gone = False
            if gone:
                await self._mark_test_post(st, "cleanup_already_deleted")
                return {"ok": True, "action": "already deleted", "found_by": found_by, "platform_post_id": post_id}
            return {"ok": False, "action": "delete failed; the sweeper retries", "platform_post_id": post_id, "error": _error_detail(exc)}
        finally:
            CALL_META.reset(token)
        mode = _delete_mode(meta.get("delete_mode") or getattr(st.connector, "delete_mode", None))
        await self._mark_test_post(st, f"cleanup_{mode}" if result.deleted else "cleanup_attempted")
        return {"ok": bool(result.deleted), "action": "deleted", "found_by": found_by, "platform_post_id": post_id,
                "marker": st.marker}


_RULE_WORDS = {
    "frequency_cap": ("frequen", "per ", "/p", "week", "day", "cap", "once"),
    "self_promo": ("promot", "self-promo", "promo"),
    "link_policy": ("link",),
    "disclosure": ("disclos", "ai-generated", "ai generated", " ai "),
    "automation_allowed": ("automat", "manual", "by hand", "person"),
    "languages": ("language", "lang"),
    "blocked_terms": ("term", "word"),
}


def _missing_rules(rules: dict[str, Any] | None, checklist: list[str]) -> list[str]:
    """Confirmed rules (that restrict something) that no checklist line mentions."""
    if not rules:
        return []
    from app.rules.models import RuleSet

    rs = RuleSet.model_validate(rules)
    active = []
    if rs.frequency_cap:
        active.append("frequency_cap")
    if rs.self_promo != "allowed":
        active.append("self_promo")
    if rs.link_policy != "any":
        active.append("link_policy")
    if rs.disclosure.required:
        active.append("disclosure")
    if not rs.automation_allowed:
        active.append("automation_allowed")
    if rs.language_policy == "must_match":
        active.append("languages")
    if rs.blocked_terms:
        active.append("blocked_terms")
    text = " " + " ".join(checklist).lower() + " "
    return [name for name in active if not any(word in text for word in _RULE_WORDS[name])]


def _summary(doc: dict[str, Any]) -> str:
    result = doc["result"]
    lines = [f"# Certification {doc['run_id']}", "",
             f"- destination: `{doc['destination_id']}` ({result.get('connector_key')})",
             f"- outcome: **{result.get('state')}**" + (f" at {result.get('failed_step')} ({result.get('failure_code')})"
                                                        if not result.get("passed") else ""),
             f"- level: {result.get('level_before')} → {result.get('level_after')}",
             f"- delete mode: {result.get('delete_mode')}",
             f"- simulation: {doc['simulation']}", f"- started {doc['started_at']}, finished {doc['finished_at']} "
             f"({doc['duration_ms']} ms)", f"- harness version {doc['harness_version']}", "", "| # | step | outcome | ms |", "|---|---|---|---|"]
    for step in doc["steps"]:
        lines.append(f"| {step['n']} | {step['step']} | {step['outcome']} | {step['duration_ms']} |")
    return "\n".join(lines) + "\n"


async def create_run(session: AsyncSession, destination_id: str, trigger: str) -> CertificationRunRow:
    run = CertificationRunRow(destination_id=destination_id, trigger=trigger, state="QUEUED", steps=[])
    session.add(run)
    await session.flush()
    return run


async def latest_runs(session: AsyncSession, destination_id: str | None, limit: int) -> list[CertificationRunRow]:
    stmt = select(CertificationRunRow)
    if destination_id:
        stmt = stmt.where(CertificationRunRow.destination_id == destination_id)
    return list((await session.execute(stmt.order_by(CertificationRunRow.created_at.desc()).limit(limit))).scalars())


__all__ = ["CERT_RULES", "ORDER", "Harness", "StepFailed", "create_run", "latest_runs"]
