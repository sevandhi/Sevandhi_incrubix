"""Network health (plan §16): every destination's trust level, last certification, failure streak, rules version
and yield, plus totals. Served by ``GET /api/v1/network/health`` and drawn by ``/ui/network``.

* failure streak: consecutive DEMOTED certification runs, newest first, up to the last PASSED (rescheduled,
  inconclusive and not-certifiable-here runs say nothing about the destination and are skipped);
* rules: the newest rule-set version and its status (``confirmed``, ``needs_review`` after rules drift, ...);
* yield: from the newest metrics snapshot of each post (``metrics_snapshots``): totals per normalised key (null when
  the platform exposes none of them) and engagement per measured post.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.contract.registry import Registry
from app.db.tables import AlertRow, CertificationRunRow, DestinationRow, MetricsSnapshotRow, PublicationRow, RuleSetRow
from app.metrics import normalize as nm

RUNS_PER_DESTINATION = 20
NEUTRAL_STATES = {"RESCHEDULED", "INCONCLUSIVE", "FAILED_PRECONDITION"}
CERTIFIED = ("L1", "L2", "L3", "L4")


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def failure_streak(states_newest_first: list[str]) -> int:
    streak = 0
    for state in states_newest_first:
        if state == "DEMOTED":
            streak += 1
        elif state == "PASSED":
            break
    return streak


def yield_of(snapshots: list[MetricsSnapshotRow], published: int) -> dict[str, Any]:
    totals: dict[str, Any] = dict.fromkeys(nm.KEYS)
    for snap in snapshots:
        for key in nm.KEYS:
            value = (snap.metrics or {}).get(key)
            if isinstance(value, int | float):
                totals[key] = (totals[key] or 0) + value
    engagement = [e for e in (nm.engagement(s.metrics or {}) for s in snapshots) if e is not None]
    return {"published_posts": published, "posts_measured": len(snapshots), "totals": totals,
            "engagement_per_post": round(sum(engagement) / len(engagement), 3) if engagement else None,
            "last_measured_at": _iso(max((s.captured_at for s in snapshots), default=None))}


async def network_health(session: AsyncSession, settings: Settings, registry: Registry, now: datetime, *,
                         destination_id: str | None = None, status: str | None = None, connector_key: str | None = None,
                         limit: int = 500) -> dict[str, Any]:
    stmt = select(DestinationRow).order_by(DestinationRow.name, DestinationRow.id).limit(limit)
    if destination_id:
        stmt = stmt.where(DestinationRow.id == destination_id)
    if status:
        stmt = stmt.where(DestinationRow.status == status)
    if connector_key:
        stmt = stmt.where(DestinationRow.connector_key == connector_key)
    dests = list((await session.execute(stmt)).scalars())
    ids = [d.id for d in dests]

    runs: dict[str, list[Any]] = defaultdict(list)
    alerts: dict[str, list[str]] = defaultdict(list)
    rules: dict[str, list[RuleSetRow]] = defaultdict(list)
    snaps: dict[str, list[MetricsSnapshotRow]] = defaultdict(list)
    published: dict[str, int] = {}
    if ids:
        rn = func.row_number().over(partition_by=CertificationRunRow.destination_id,
                                    order_by=CertificationRunRow.created_at.desc()).label("rn")
        sub = select(CertificationRunRow.id, CertificationRunRow.destination_id, CertificationRunRow.trigger,
                     CertificationRunRow.state, CertificationRunRow.failure_code, CertificationRunRow.created_at,
                     CertificationRunRow.finished_at, rn).where(CertificationRunRow.destination_id.in_(ids)).subquery()
        for row in (await session.execute(select(sub).where(sub.c.rn <= RUNS_PER_DESTINATION).order_by(sub.c.destination_id, sub.c.rn))).all():
            runs[row.destination_id].append(row)
        for row in (await session.execute(select(AlertRow.destination_id, AlertRow.kind)
                                          .where(AlertRow.destination_id.in_(ids), AlertRow.status == "open"))).all():
            alerts[row.destination_id].append(row.kind)
        for row in (await session.execute(select(RuleSetRow).where(RuleSetRow.destination_id.in_(ids))
                                          .order_by(RuleSetRow.version.desc()))).scalars():
            rules[row.destination_id].append(row)
        latest = (select(MetricsSnapshotRow).where(MetricsSnapshotRow.destination_id.in_(ids))
                  .ext(distinct_on(MetricsSnapshotRow.destination_id, MetricsSnapshotRow.platform_post_id))
                  .order_by(MetricsSnapshotRow.destination_id, MetricsSnapshotRow.platform_post_id,
                            MetricsSnapshotRow.captured_at.desc(), MetricsSnapshotRow.id.desc()))
        for snap in (await session.execute(latest)).scalars():
            snaps[snap.destination_id or ""].append(snap)
        for row in (await session.execute(select(PublicationRow.destination_id, func.count())
                                          .where(PublicationRow.destination_id.in_(ids), PublicationRow.state == "published",
                                                 PublicationRow.is_test.is_(False))
                                          .group_by(PublicationRow.destination_id))).all():
            published[row[0]] = int(row[1])

    recert_after = timedelta(days=settings.RECERT_AFTER_DAYS)
    items: list[dict[str, Any]] = []
    for d in dests:
        history = runs.get(d.id, [])
        last = history[0] if history else None
        passed = next((r for r in history if r.state == "PASSED"), None)
        due_at = d.last_certified_at + recert_after if d.last_certified_at and d.level in CERTIFIED else None
        sets = rules.get(d.id, [])
        newest = sets[0] if sets else None
        confirmed = next((r for r in sets if r.status == "confirmed"), None)
        watch = (d.extra or {}).get("rules_watch") or {}
        manifest = registry.manifest(d.connector_key)
        items.append({
            "destination_id": d.id, "name": d.name, "connector_key": d.connector_key, "url": d.url,
            "trust_level": d.level, "status": d.status, "excluded_reason": d.excluded_reason,
            "last_certified_at": _iso(d.last_certified_at), "recert_due_at": _iso(due_at),
            "recert_overdue": bool(due_at and now >= due_at and d.status == "active"),
            "last_run": None if last is None else {"run_id": last.id, "trigger": last.trigger, "state": last.state,
                                                    "failure_code": last.failure_code, "finished_at": _iso(last.finished_at)},
            "last_passed_at": _iso(passed.finished_at) if passed else None,
            "failure_streak": failure_streak([r.state for r in history if r.state not in NEUTRAL_STATES]),
            "rules": {"version": newest.version if newest else None, "status": newest.status if newest else "none",
                      "confirmed_version": confirmed.version if confirmed else None,
                      "source": newest.source if newest else None,
                      "watch": {k: watch.get(k) for k in ("baseline_sha256", "baseline_at", "last_checked_at", "drift") if k in watch}
                      or None},
            "metrics_supported": nm.supported(d.connector_key) if manifest and "metrics" in manifest.capabilities else [],
            "yield": yield_of(snaps.get(d.id, []), published.get(d.id, 0)),
            "open_alerts": sorted(alerts.get(d.id, [])),
        })
    by_level: dict[str, int] = defaultdict(int)
    by_status: dict[str, int] = defaultdict(int)
    for item in items:
        by_level[item["trust_level"]] += 1
        by_status[item["status"]] += 1
    summary = {
        "destinations": len(items), "by_level": dict(sorted(by_level.items())), "by_status": dict(sorted(by_status.items())),
        "recert_overdue": sum(1 for i in items if i["recert_overdue"]),
        "failing": sum(1 for i in items if i["failure_streak"] > 0),
        "rules_need_review": sum(1 for i in items if i["rules"]["status"] == "needs_review"),
        "open_alerts": sum(len(i["open_alerts"]) for i in items),
        "posts_measured": sum(i["yield"]["posts_measured"] for i in items),
    }
    return {"generated_at": _iso(now), "recert_after_days": settings.RECERT_AFTER_DAYS, "summary": summary, "destinations": items}
