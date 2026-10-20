"""Destinations from catalogue rows, confirmed rules, connections and content.

Adding a destination is one catalogue row (Must-have 3, plan §3.3):

1. look up the connector's manifest and its ``instance_param`` (``base_url``, ``community_url`` ...);
2. the row's URL (cleaned, scheme kept) becomes ``instance.<instance_param>``, unless the row gives it;
   ``destination_key = <connector>|<normalised instance>|<surface>`` (the same key the connector's
   publish log and frequency cap use);
3. bind the connection (by id or ``connection_ref``; resolved lazily if it does not exist yet);
4. the row's rule columns become a *confirmed* rule set only when the row says they are an operator's own
   (``rules_status=confirmed`` and ``rules_source=manual``); otherwise they become a *proposed* rule set that a person
   confirms on the confirm page (``/ui/rules/{id}``) before anything is posted (D6). Rows that do not say:
   ``default_rules_status`` decides (the catalogue import: proposed; ``POST /api/v1/destinations``: confirmed/manual);
5. the destination starts at level ``L0``; certification raises it.

No restart and no code change: the connector registry and the multi-tenant mocks already know the type.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import SecretStr, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.models import ConnectionIn, ContentIn, DestinationIn, DestinationPatch, RulesIn
from app.catalogue.normalize import check_url_syntax, clean_instance_url
from app.connectors.base import normalise_instance
from app.contract.models import Connection, Destination, Media, Rendition
from app.contract.registry import Registry
from app.db.tables import (
    CertificationRunRow,
    ConnectionRow,
    ContentItemRow,
    DestinationRow,
    RuleSetRow,
    new_id,
)
from app.errors import ErrorCode, IbxError
from app.reliability.secrets import decrypt_credentials, encrypt_credentials

LEVELS = ("L0", "L1", "L2", "L3", "L4")


def _validation(loc: list[str], msg: str) -> IbxError:
    return IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, msg, details={"errors": [{"loc": loc, "type": "value_error", "msg": msg}]})


def _rules_flat(rules: dict[str, Any]) -> dict[str, Any]:
    """The Appendix A record's flat rule fields, from a (validated) RuleSet."""
    from app.rules.models import RuleSet

    rs = RuleSet.model_validate(rules)
    cap = f"{rs.frequency_cap.max_posts}/{rs.frequency_cap.window}" if rs.frequency_cap else None
    link = rs.link_policy if rs.link_policy != "allowlist" else "allowlist:" + ",".join(rs.link_allowlist)
    return {
        "frequency_cap": cap,
        "self_promo_allowed": rs.self_promo != "disallowed",
        "link_policy": link,
        "disclosure_required": rs.disclosure.required,
        "automation_allowed": rs.automation_allowed,
    }


def check_rules(rules: dict[str, Any], loc: str = "rules") -> dict[str, Any]:
    """Validate a rules dict with the rules engine's RuleSet; 422 with the field errors if it does not parse."""
    from app.rules.models import RuleSet

    try:
        RuleSet.model_validate(rules)
    except (ValidationError, ValueError) as exc:
        errors = exc.errors() if isinstance(exc, ValidationError) else [{"loc": [], "msg": str(exc)}]
        raise IbxError(
            ErrorCode.REQUEST_VALIDATION_FAILED,
            "The posting rules could not be parsed.",
            details={"errors": [{"loc": [loc, *map(str, e.get("loc", []))], "type": "value_error", "msg": str(e.get("msg"))[:200]}
                                for e in errors]},
        ) from None
    return rules


# ------------------------------------------------------------------ connections


async def find_connection(session: AsyncSession, connector_key: str, ref: str | None) -> ConnectionRow | None:
    """A connection by id, or by the ``ref`` name given when it was created (same connector only)."""
    if not ref:
        return None
    row = await session.get(ConnectionRow, ref)
    if row is None:
        stmt = select(ConnectionRow).where(ConnectionRow.meta["ref"].astext == ref, ConnectionRow.connector_key == connector_key)
        row = (await session.execute(stmt.order_by(ConnectionRow.created_at.desc()).limit(1))).scalar_one_or_none()
    if row is not None and row.connector_key != connector_key:
        raise IbxError(ErrorCode.CONNECTION_NOT_FOUND, f"Connection '{ref}' belongs to '{row.connector_key}', not '{connector_key}'.")
    return row


async def create_connection(session: AsyncSession, registry: Registry, body: ConnectionIn, fernet_key: bytes) -> ConnectionRow:
    key = body.connector_key.strip().lower()
    if registry.manifest(key) is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, "Unknown connector key.", details={"known": registry.keys()})
    row = ConnectionRow(
        id=new_id("conn"),
        connector_key=key,
        auth_type=body.auth_type,
        secret_enc=encrypt_credentials(fernet_key, body.credentials) if body.credentials else None,
        expires_at=body.expires_at,
        status="active",
        account_handle=body.account_handle,
        meta={"ref": body.ref, "credential_names": sorted(body.credentials), "instance": body.instance},
    )
    session.add(row)
    await session.flush()
    return row


def connection_out(row: ConnectionRow) -> dict[str, Any]:
    """Public view: never a credential value, only the names of the stored fields."""
    meta = row.meta or {}
    return {
        "connection_id": row.id,
        "connector_key": row.connector_key,
        "auth_type": row.auth_type,
        "status": row.status,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "account_handle": row.account_handle,
        "ref": meta.get("ref"),
        "credential_names": meta.get("credential_names", []),
        "instance": meta.get("instance") or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def contract_connection(row: ConnectionRow | None, connector_key: str, auth: str, fernet_key: bytes | None = None) -> Connection:
    """The contract ``Connection`` for a stored row (``connection_id`` only: the connector decrypts it itself).

    Without a stored connection (simulation mode, or an auth-less connector) an empty one is used; in
    simulation the base connector fills ``sim-*`` credentials.
    """
    if row is None:
        return Connection(connector_key=connector_key, auth_type=auth if auth in ("oauth2", "api_key", "bot_token", "none") else None)
    extra = dict((row.meta or {}).get("instance") or {})
    conn = Connection(connection_id=row.id, connector_key=row.connector_key, auth_type=row.auth_type, **extra)
    if fernet_key is not None and row.secret_enc:
        creds = decrypt_credentials(fernet_key, row.secret_enc)
        conn = conn.model_copy(update={"credentials": {k: SecretStr(v) for k, v in creds.items()}})
    return conn


# ------------------------------------------------------------------ destinations


def instance_for(manifest: Any, row: DestinationIn) -> dict[str, Any]:
    param = manifest.instance_param
    instance = dict(row.instance)
    value = instance.get(param)
    if isinstance(value, str) and value.strip():
        if "://" in value:
            instance[param] = clean_instance_url(value)
    elif param.endswith("url"):
        instance[param] = clean_instance_url(row.url)
    else:
        raise _validation(["instance", param], f"instance.{param} is required for connector '{manifest.key}' "
                          f"(add a column instance_{param}).")
    return instance


def destination_key(connector_key: str, instance_value: str, surface: str) -> str:
    return f"{connector_key}|{normalise_instance(instance_value)}|{surface}"


async def create_destination(
    session: AsyncSession, registry: Registry, row: DestinationIn, *, now: datetime, confirmed_by: str = "catalogue_row",
    default_rules_status: str = "confirmed",
) -> tuple[DestinationRow, bool, list[str]]:
    """Create one destination from one catalogue row. Returns (row, created, warnings).

    The same row twice gives the existing destination (``created`` False), never a duplicate.
    """
    warnings: list[str] = []
    manifest = registry.manifest(row.connector_key)
    if manifest is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, "Unknown connector key.",
                       details={"known": registry.keys(), "registry_errors": registry.errors})
    surface = row.surface or manifest.surfaces[0].key
    if manifest.surface(surface) is None:
        raise _validation(["surface"], f"Connector '{manifest.key}' has no surface '{surface}' "
                          f"(known: {', '.join(s.key for s in manifest.surfaces)}).")
    try:  # a malformed URL is the caller's error: 422 with the reason, never a 500 or a stored bad row
        _, normalized = check_url_syntax(row.url)
    except ValueError as exc:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"The url is not valid: {exc}.",
                       details={"errors": [{"loc": ["url"], "type": "url_invalid", "msg": str(exc)}]}) from None
    instance = instance_for(manifest, row)
    dest_key = destination_key(manifest.key, instance[manifest.instance_param], surface)

    existing = (await session.execute(select(DestinationRow).where(DestinationRow.destination_key == dest_key))).scalar_one_or_none()
    if existing is not None:
        return existing, False, ["DUPLICATE: a destination with this connector, instance and surface already exists"]

    rules = row.rules_dict()
    if rules is not None:
        check_rules(rules)
    conn_ref = row.connection_id or row.connection_ref
    conn = await find_connection(session, manifest.key, conn_ref)
    if conn_ref and conn is None:
        warnings.append(f"CONNECTION_REF_UNRESOLVED: no connection '{conn_ref}' yet; it is bound when one with that ref exists")

    known = set(DestinationIn.model_fields)
    extra = {k: v for k, v in (row.model_extra or {}).items() if k not in known}
    if conn_ref:
        extra["connection_ref"] = conn_ref
    origin = rules_origin(row, default_rules_status)
    if rules is not None:
        extra["rules_origin"] = origin
    flat = _rules_flat(rules) if rules is not None else {}
    dest = DestinationRow(
        id=new_id("dest"),
        destination_key=dest_key,
        connector_key=manifest.key,
        name=row.name.strip(),
        url=row.url.strip(),
        normalized_url=normalized,
        platform_key=row.platform_key or manifest.key,
        surface=surface,
        category=row.category,
        audience_tags=row.audience_tags,
        language=row.language,
        geo=row.geo,
        est_audience=row.est_audience,
        rules_url=row.rules_url,
        frequency_cap=flat.get("frequency_cap"),
        self_promo_allowed=flat.get("self_promo_allowed"),
        link_policy=flat.get("link_policy"),
        disclosure_required=flat.get("disclosure_required"),
        automation_allowed=flat.get("automation_allowed"),
        relevance_score=row.relevance_score,
        relevance_reason=row.relevance_reason,
        level="L0",
        instance=instance,
        connection_id=conn.id if conn else None,
        spec_overrides=row.spec_overrides,
        status="active",
        extra=extra,
    )
    session.add(dest)
    await session.flush()
    if rules is not None and origin["status"] == "confirmed":
        session.add(RuleSetRow(
            id=new_id("rules"), destination_id=dest.id, version=1, rules=rules, status="confirmed", source="manual",
            evidence=[{"type": "catalogue_row", "value": "rules entered in the catalogue row by an operator",
                       "rules_source": origin["source"]}],
            confirmed_by=confirmed_by, confirmed_at=now,
        ))
    elif rules is not None:
        proposal = _row_proposal(dest, row, rules, origin, now=now, requested_by=confirmed_by)
        session.add(proposal)
        dest.extra = {**extra, "rules_proposal_id": proposal.id}
        warnings.append(f"RULES_PROPOSED: the row's rules (rules_source={origin['source']}) are stored as proposal "
                        f"{proposal.id}; a person confirms them at /ui/rules/{proposal.id} before anything is posted")
    else:
        warnings.append("RULES_UNCONFIRMED: the row states no posting rules; publications are blocked until "
                        "PUT /api/v1/destinations/{id}/rules confirms them (certification still works on own instances)")
    await session.flush()
    return dest, True, warnings


def rules_origin(row: DestinationIn, default_rules_status: str = "confirmed") -> dict[str, Any]:
    """Whether a row's rules are confirmed on creation: only an operator's own rules are, i.e.
    ``rules_status=confirmed`` together with ``rules_source=manual``. A row that says neither takes
    ``default_rules_status`` (with source ``manual`` for "confirmed", ``import`` for "proposed")."""
    asked = row.rules_status or default_rules_status
    source = row.rules_source or ("manual" if asked == "confirmed" and default_rules_status == "confirmed" else "import")
    confirmed = asked == "confirmed" and source == "manual"
    return {"status": "confirmed" if confirmed else "proposed", "source": source,
            "confirmation_refused": asked == "confirmed" and not confirmed}


def _row_proposal(dest: DestinationRow, row: DestinationIn, rules: dict[str, Any], origin: dict[str, Any], *,
                  now: datetime, requested_by: str) -> RuleSetRow:
    """The row's rules as a ``proposed`` rule set (shown on the confirm page; never used for posting)."""
    from app.catalogue import rules_capture as rc

    proposed, fields, flags, issues = rc.proposal_from_row(
        rules, destination_language=dest.language, rules_url=row.rules_url, rules_source=origin["source"],
        quotes=row.rules_quotes, refused_confirmation=origin["confirmation_refused"])
    evidence: list[dict[str, Any]] = [
        *fields,
        {"type": "source", "kind": f"catalogue_row:{origin['source']}", "url": row.rules_url, "sha256": None, "bytes": None,
         "text_chars": None, "note": "rule columns of the catalogue row"},
        {"type": "capture", "origin": "catalogue_row", "job_id": None, "captured_at": rc.now_iso(now), "flags": flags,
         "issues": issues, "warnings": [f"{i['code']}: {i['message']}" for i in issues], "rules_source": origin["source"],
         "row_rules": rules, "requested_by": requested_by},
    ]
    return RuleSetRow(id=new_id("rprop"), destination_id=dest.id, version=1, rules=proposed, status="proposed",
                      source=origin["source"][:32], evidence=evidence)


async def pending_proposal(session: AsyncSession, destination_id: str) -> RuleSetRow | None:
    stmt = (select(RuleSetRow).where(RuleSetRow.destination_id == destination_id, RuleSetRow.status == "proposed",
                                     RuleSetRow.id.like("rprop_%"))
            .order_by(RuleSetRow.version.desc()).limit(1))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_destination(session: AsyncSession, destination_id: str, *, lock: bool = False) -> DestinationRow:
    dest = await session.get(DestinationRow, destination_id, with_for_update=lock)
    if dest is None:
        raise IbxError(ErrorCode.DESTINATION_NOT_FOUND, f"Unknown destination '{destination_id}'.")
    return dest


async def confirmed_rules(session: AsyncSession, destination_id: str) -> RuleSetRow | None:
    stmt = (
        select(RuleSetRow)
        .where(RuleSetRow.destination_id == destination_id, RuleSetRow.status == "confirmed")
        .order_by(RuleSetRow.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def put_rules(session: AsyncSession, dest: DestinationRow, body: RulesIn, *, now: datetime) -> RuleSetRow:
    """Confirm a new rules version; earlier confirmed versions become ``superseded``."""
    check_rules(body.rules)
    latest = (await session.execute(select(func.max(RuleSetRow.version)).where(RuleSetRow.destination_id == dest.id))).scalar() or 0
    for old in (await session.execute(select(RuleSetRow).where(
            RuleSetRow.destination_id == dest.id, RuleSetRow.status == "confirmed"))).scalars():
        old.status = "superseded"
    row = RuleSetRow(id=new_id("rules"), destination_id=dest.id, version=latest + 1, rules=body.rules, status="confirmed",
                     source=body.source, evidence=body.evidence, confirmed_by=body.confirmed_by, confirmed_at=now)
    session.add(row)
    for key, value in _rules_flat(body.rules).items():
        setattr(dest, key, value)
    await session.flush()
    return row


async def patch_destination(session: AsyncSession, registry: Registry, dest: DestinationRow, body: DestinationPatch) -> DestinationRow:
    data = body.model_dump(exclude_unset=True)
    manifest = registry.manifest(dest.connector_key)
    if "instance" in data:
        instance = dict(data.pop("instance") or {})
        param = manifest.instance_param if manifest else None
        if param and param in instance and normalise_instance(str(instance[param])) != normalise_instance(str(dest.instance.get(param, ""))):
            raise _validation(["instance", param], f"instance.{param} identifies the destination and can not change; add a new row instead.")
        instance.pop(param, None)
        dest.instance = {**dest.instance, **instance}
    ref = data.pop("connection_ref", None) or data.pop("connection_id", None)
    if ref is not None:
        conn = await find_connection(session, dest.connector_key, ref)
        if conn is None:
            raise IbxError(ErrorCode.CONNECTION_NOT_FOUND, f"Unknown connection '{ref}' for '{dest.connector_key}'.")
        dest.connection_id = conn.id
        dest.extra = {**(dest.extra or {}), "connection_ref": ref}
    if data.get("status") == "active":
        data.setdefault("excluded_reason", None)
    for key, value in data.items():
        setattr(dest, key, value)
    await session.flush()
    return dest


async def destination_connection(session: AsyncSession, dest: DestinationRow) -> ConnectionRow | None:
    """The bound connection; binds lazily when the row's ``connection_ref`` now exists."""
    if dest.connection_id:
        return await session.get(ConnectionRow, dest.connection_id)
    ref = (dest.extra or {}).get("connection_ref")
    conn = await find_connection(session, dest.connector_key, ref) if ref else None
    if conn is not None:
        dest.connection_id = conn.id
    return conn


def contract_destination(dest: DestinationRow, rules: dict[str, Any] | None) -> Destination:
    return Destination(
        destination_id=dest.id,
        connector_key=dest.connector_key,
        platform_key=dest.platform_key,
        surface=dest.surface,
        name=dest.name,
        url=dest.url,
        instance=dict(dest.instance or {}),
        language=dest.language,
        spec_overrides=dest.spec_overrides,
        rules=rules,
    )


async def destination_out(session: AsyncSession, dest: DestinationRow, *, with_rules: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "destination_id": dest.id,
        "destination_key": dest.destination_key,
        "name": dest.name,
        "url": dest.url,
        "normalized_url": dest.normalized_url,
        "platform_key": dest.platform_key,
        "surface": dest.surface,
        "connector_key": dest.connector_key,
        "category": dest.category,
        "audience_tags": dest.audience_tags or [],
        "language": dest.language,
        "geo": dest.geo,
        "est_audience": dest.est_audience,
        "rules_url": dest.rules_url,
        "frequency_cap": dest.frequency_cap,
        "self_promo_allowed": dest.self_promo_allowed,
        "link_policy": dest.link_policy,
        "disclosure_required": dest.disclosure_required,
        "automation_allowed": dest.automation_allowed,
        "relevance_score": dest.relevance_score,
        "relevance_reason": dest.relevance_reason,
        "level": dest.level,
        "last_certified_at": dest.last_certified_at.isoformat() if dest.last_certified_at else None,
        "instance": dest.instance or {},
        "connection_id": dest.connection_id,
        "connection_ref": (dest.extra or {}).get("connection_ref"),
        "spec_overrides": dest.spec_overrides,
        "status": dest.status,
        "excluded_reason": dest.excluded_reason,
        "created_at": dest.created_at.isoformat() if dest.created_at else None,
    }
    if with_rules:
        rules = await confirmed_rules(session, dest.id)
        out["rules"] = rules_out(rules)
        pending = await pending_proposal(session, dest.id)
        out["rules_status"] = "confirmed" if rules is not None else ("proposed" if pending is not None else "none")
        out["rules_proposal_id"] = pending.id if pending is not None else None
        last = (await session.execute(
            select(CertificationRunRow).where(CertificationRunRow.destination_id == dest.id)
            .order_by(CertificationRunRow.created_at.desc()).limit(1))).scalar_one_or_none()
        out["last_certification"] = (
            {"run_id": last.id, "state": last.state, "failure_code": last.failure_code} if last is not None else None
        )
    return out


def rules_out(rules: RuleSetRow | None) -> dict[str, Any] | None:
    if rules is None:
        return None
    return {
        "rules_id": rules.id,
        "version": rules.version,
        "status": rules.status,
        "source": rules.source,
        "rules": rules.rules,
        "confirmed_by": rules.confirmed_by,
        "confirmed_at": rules.confirmed_at.isoformat() if rules.confirmed_at else None,
    }


async def list_destinations(
    session: AsyncSession, *, connector_key: str | None, level: str | None, status: str | None, q: str | None,
    limit: int, offset: int,
) -> tuple[list[DestinationRow], int]:
    stmt = select(DestinationRow)
    if connector_key:
        stmt = stmt.where(DestinationRow.connector_key == connector_key)
    if level:
        stmt = stmt.where(DestinationRow.level == level)
    if status:
        stmt = stmt.where(DestinationRow.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(DestinationRow.name.ilike(like), DestinationRow.normalized_url.ilike(like)))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    rows = (await session.execute(stmt.order_by(DestinationRow.created_at, DestinationRow.id).limit(limit).offset(offset))).scalars().all()
    return list(rows), int(total)


# ------------------------------------------------------------------ content


def media_key(media_id: str) -> str:
    return f"media/{media_id}"


async def create_content(session: AsyncSession, body: ContentIn, storage: Any = None) -> ContentItemRow:
    media = []
    for n, item in enumerate(body.media):
        entry = item.model_dump(exclude_none=True)
        if item.media_id:
            info = await storage.stat(media_key(item.media_id)) if storage is not None else None
            if info is None:
                raise _validation(["media", str(n), "media_id"], f"Unknown media_id '{item.media_id}' (upload it with POST /api/v1/media).")
            entry.update({"key": media_key(item.media_id), "mime": item.mime or info.content_type, "bytes": info.size})
        elif not item.url:
            raise _validation(["media", str(n)], "each media item needs a url or a media_id")
        media.append(entry)
    row = ContentItemRow(
        id=new_id("content"),
        title=body.title,
        body=body.body,
        canonical_url=body.canonical_url,
        media=media,
        tags=body.tags,
        language=body.language,
        is_promotional=body.is_promotional,
        ai_generated=body.ai_generated,
        owner_domains=body.owner_domains,
        extra={"content_type": body.content_type, "body_format": body.body_format, "link_url": body.link_url,
               "mentions": body.mentions},
    )
    session.add(row)
    await session.flush()
    return row


async def get_content(session: AsyncSession, content_id: str) -> ContentItemRow:
    row = await session.get(ContentItemRow, content_id)
    if row is None:
        raise IbxError(ErrorCode.NOT_FOUND, f"Unknown content '{content_id}'.")
    return row


def content_out(row: ContentItemRow) -> dict[str, Any]:
    extra = row.extra or {}
    return {
        "content_id": row.id,
        "title": row.title,
        "body": row.body,
        "content_type": extra.get("content_type", "text"),
        "body_format": extra.get("body_format", "markdown"),
        "canonical_url": row.canonical_url,
        "link_url": extra.get("link_url"),
        "media": row.media or [],
        "tags": row.tags or [],
        "mentions": extra.get("mentions") or [],
        "language": row.language,
        "is_promotional": row.is_promotional,
        "ai_generated": row.ai_generated,
        "owner_domains": row.owner_domains or [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def rendition_for(content: ContentItemRow, surface: str | None, settings: Any = None) -> Rendition:
    """The contract Rendition for one content item; uploaded media get a fresh signed download link."""
    from app.storage.links import signed_download_url

    extra = content.extra or {}
    media = []
    for m in content.media or []:
        item = {k: v for k, v in m.items() if k not in ("media_id", "key")}
        if m.get("key") and settings is not None:
            item["url"] = signed_download_url(m["key"], settings, expires_s=max(settings.FILE_LINK_EXPIRY_S, 86400))
        if item.get("url"):
            media.append(Media.model_validate(item))
    return Rendition(
        rendition_id=f"{content.id}:{surface or 'default'}",
        content_id=content.id,
        surface=surface,
        content_type=extra.get("content_type") or "text",
        title=content.title,
        body=content.body or "",
        body_format=extra.get("body_format") or "markdown",
        canonical_url=content.canonical_url,
        link_url=extra.get("link_url"),
        media=media,
        tags=list(content.tags or []),
        mentions=list(extra.get("mentions") or []),
        language=content.language,
        is_promotional=content.is_promotional,
        ai_generated=content.ai_generated,
        owner_domains=list(content.owner_domains or []),
    )
