"""Publications (``/api/v1/publications``): rules gate with slot reservation → tailor → connector.publish.

State machine: ``pending → publishing → published``; ``rescheduled`` (frequency cap, with ``scheduled_for`` =
``retry_at``, re-checked when that time comes); ``blocked`` (a rule says no, with the reason and code);
``failed``; ``awaiting_human`` (assisted pack built, or the platform holds the post for review); ``deleted``;
for assisted posts also ``removed_by_platform`` (the community removed it) and ``abandoned`` (nobody posted it).

Frequency cap under concurrency (plan §5): the cap is evaluated and the slot reserved while holding a
transaction-scoped advisory lock on ``pubslot:<destination_key>``. The reservation is the publication row
itself (state ``pending``/``publishing`` with its time), which the next post's evaluation counts. So two
concurrent posts to a "1 per week" destination give one ``pending`` and one ``rescheduled``.

The publish log (history) for a destination = its non-test publications that count (pending, publishing,
published, awaiting_human, deleted) + successful contract publishes on the same ``destination_key`` that are
not publications and not certification test posts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue import service
from app.contract.models import Rendition
from app.db.session import advisory_key
from app.db.tables import (
    ContentItemRow,
    DestinationRow,
    PublicationRow,
    PublishIntentRow,
    RuleSetRow,
    TestPostRow,
)
from app.errors import ErrorCode, IbxError
from app.rules.models import Decision, PublishEvent, RuleSet

COUNTED_STATES = ("pending", "publishing", "published", "awaiting_human", "deleted", "removed_by_platform")
FINAL_STATES = ("published", "deleted", "failed", "awaiting_human", "removed_by_platform", "abandoned")


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def intent_key(publication_id: str) -> str:
    return f"pub-{publication_id}"


async def lock_destination_slot(session: AsyncSession, destination_key: str) -> None:
    """Serialise cap evaluation + reservation per destination until this transaction ends."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_key(f"pubslot:{destination_key}")})


async def publish_history(session: AsyncSession, dest: DestinationRow, *, exclude_publication: str | None = None) -> list[PublishEvent]:
    events: list[PublishEvent] = []
    stmt = (
        select(PublicationRow, ContentItemRow.is_promotional)
        .join(ContentItemRow, ContentItemRow.id == PublicationRow.content_id, isouter=True)
        .where(PublicationRow.destination_id == dest.id, PublicationRow.is_test.is_(False),
               PublicationRow.state.in_(COUNTED_STATES))
    )
    pub_keys: set[str] = set()
    for pub, promo in (await session.execute(stmt)).all():
        pub_keys.add(intent_key(pub.id))
        if pub.id == exclude_publication:
            continue
        when = pub.published_at or pub.scheduled_for or pub.created_at
        events.append(PublishEvent(published_at=when, destination_key=dest.destination_key, status="published",
                                   is_promotional=(pub.extra or {}).get("is_promotional", promo)))
    tests = (await session.execute(select(TestPostRow.platform_post_id, TestPostRow.run_id)
                                   .where(TestPostRow.connector_key == dest.connector_key))).all()
    tests_here = (await session.execute(select(TestPostRow.platform_post_id)
                                        .where(TestPostRow.connector_key == dest.connector_key,
                                               TestPostRow.destination_id == dest.id))).scalars()
    # a test post's id only identifies it on ITS destination: the same id on another instance is a real post (MH4)
    test_ids = set(tests_here)
    pub_keys |= {f"cert-{t.run_id}" for t in tests}  # certification intents never count
    intents = (await session.execute(select(PublishIntentRow).where(
        PublishIntentRow.destination_key == dest.destination_key,
        PublishIntentRow.state.in_(("SUCCEEDED", "PENDING", "UNKNOWN"))))).scalars()  # same slots as the contract gate
    for intent in intents:
        if intent.idempotency_key in pub_keys or str((intent.result or {}).get("platform_post_id")) in test_ids:
            continue
        events.append(PublishEvent(published_at=intent.updated_at, destination_key=dest.destination_key, status="published"))
    return events


def ruleset_for(dest: DestinationRow, rules: RuleSetRow | None, *, assisted: bool) -> RuleSet | None:
    if rules is None:
        return None
    ruleset = RuleSet.for_destination({"rules": rules.rules, "language": dest.language})
    if assisted and ruleset is not None and not ruleset.automation_allowed:
        # a person posts the assisted pack: "no automation" is exactly why this destination is assisted
        ruleset = ruleset.model_copy(update={"automation_allowed": True})
    return ruleset


def evaluate(rendition: Rendition, ruleset: RuleSet | None, history: list[PublishEvent], now: datetime,
             owner_domains: list[str]) -> Decision:
    from app.rules.engine import evaluate as rules_evaluate

    return rules_evaluate(rendition, ruleset, history, now, owner_domains=owner_domains)


def decision_doc(decision: Decision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "code": decision.code.value if decision.code else None,
        "reasons": decision.reasons,
        "retry_at": iso(decision.retry_at),
        "fixes": [f.model_dump(mode="json") for f in decision.fixes],
        "warnings": decision.warnings,
        "evidence": decision.evidence,
        "failed_checks": decision.details.get("failed_checks", []),
    }


def blocked_error(pub: PublicationRow) -> IbxError:
    stored = (pub.error or {}).get("error") or {}
    try:
        code = ErrorCode(stored.get("code"))
    except ValueError:
        code = ErrorCode.DEST_RULES_UNCONFIRMED
    details = {**(stored.get("details") or {}), "publication_id": pub.id, "state": pub.state}
    return IbxError(code, stored.get("message"), details=details, retryable=False)


def apply_decision(pub: PublicationRow, decision: Decision, *, at: datetime) -> None:
    """Set the publication's state from a rules decision (inside the slot lock)."""
    pub.rule_decision = decision_doc(decision)
    extra = dict(pub.extra or {})
    extra["reasons"] = decision.reasons
    if decision.action == "block":
        err = decision.to_error() or IbxError(ErrorCode.DEST_RULES_UNCONFIRMED)
        err.details.update({"decision": "block", "publication_id": pub.id})
        pub.state, pub.error = "blocked", err.to_body()
        extra["reason"] = decision.reasons[0] if decision.reasons else err.message
    elif decision.action == "reschedule":
        pub.state, pub.scheduled_for = "rescheduled", decision.retry_at
        err = decision.to_error()
        pub.error = err.to_body() if err is not None else None
        extra["reason"] = decision.reasons[0] if decision.reasons else "frequency cap"
    else:
        pub.state, pub.scheduled_for, pub.error = "pending", at, None
        extra["reason"] = None
    pub.extra = extra


def not_certified(dest: DestinationRow) -> bool:
    """Below L1: never certified (or demoted). Only certification test posts may go there."""
    return dest.level == "L0"


def block_for_destination(pub: PublicationRow, dest: DestinationRow) -> None:
    if dest.status == "excluded":
        err = IbxError(ErrorCode.DESTINATION_EXCLUDED, f"Destination '{dest.name}' is excluded: {dest.excluded_reason or 'failed certification'}.",
                       details={"destination_id": dest.id, "publication_id": pub.id})
    else:
        err = IbxError(ErrorCode.DESTINATION_NOT_CERTIFIED,
                       f"Destination '{dest.name}' is not certified yet (level {dest.level}): certify it first with "
                       "POST /api/v1/certifications {destination_id}.",
                       details={"destination_id": dest.id, "publication_id": pub.id, "level": dest.level})
    pub.state, pub.error = "blocked", err.to_body()
    pub.rule_decision = {"action": "block", "code": err.code.value, "reasons": [err.message]}
    pub.extra = {**(pub.extra or {}), "reason": err.message, "reasons": [err.message]}


async def publication_out(pub: PublicationRow, settings: Any) -> dict[str, Any]:
    from app.storage.links import signed_download_url

    extra = dict(pub.extra or {})
    decision = pub.rule_decision or {}
    code = decision.get("code") or ((pub.error or {}).get("error") or {}).get("code")
    pack = extra.get("assist_pack")
    if pack and settings is not None:
        pack = {**pack, "links": {name: signed_download_url(key, settings) for name, key in (pack.get("keys") or {}).items()}}
    return {
        "publication_id": pub.id,
        "destination_id": pub.destination_id,
        "content_id": pub.content_id,
        "state": pub.state,
        "reason": extra.get("reason"),
        "reasons": extra.get("reasons", []),
        "code": code if pub.state in ("blocked", "rescheduled", "failed", "removed_by_platform") else None,
        "outcome": extra.get("outcome"),
        "retry_at": iso(pub.scheduled_for) if pub.state == "rescheduled" else None,
        "scheduled_for": iso(pub.scheduled_for),
        "platform_post_id": pub.platform_post_id,
        "live_url": pub.live_url,
        "attempts": pub.attempts,
        "rule_decision": decision or None,
        "tailoring": extra.get("tailoring"),
        "assist_pack": pack,
        "removal": extra.get("removal"),
        "job_ids": extra.get("job_ids", []),
        "error": pub.error,
        "published_at": iso(pub.published_at),
        "created_at": iso(pub.created_at),
        "updated_at": iso(pub.updated_at),
    }


async def get_publication(session: AsyncSession, publication_id: str, *, lock: bool = False) -> PublicationRow:
    pub = await session.get(PublicationRow, publication_id, with_for_update=lock)
    if pub is None:
        raise IbxError(ErrorCode.NOT_FOUND, f"Unknown publication '{publication_id}'.")
    return pub


def add_job(pub: PublicationRow, job_id: str) -> None:
    extra = dict(pub.extra or {})
    extra["job_ids"] = [*extra.get("job_ids", []), job_id]
    pub.extra = extra


__all__ = [
    "COUNTED_STATES",
    "add_job",
    "apply_decision",
    "block_for_destination",
    "blocked_error",
    "decision_doc",
    "evaluate",
    "get_publication",
    "intent_key",
    "lock_destination_slot",
    "not_certified",
    "publication_out",
    "publish_history",
    "ruleset_for",
    "service",
]
