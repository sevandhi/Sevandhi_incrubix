"""SQLAlchemy 2 models for every table in plan §2.3.

Every table (catalogue, certification, metrics ...) is defined here, so the Alembic history stays linear.
A change that needs a new column adds a new migration revision.
All timestamps are timezone-aware UTC. ``JSONB`` holds the semi-structured parts.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id(prefix: str) -> str:
    """Readable random ids such as ``job_3f2a...``."""
    return f"{prefix}_{uuid.uuid4().hex}"


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# ------------------------------------------------------------------ connections and destinations


class ConnectionRow(Base):
    __tablename__ = "connections"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("conn"))
    connector_key: Mapped[str] = mapped_column(String(64), index=True)
    auth_type: Mapped[str] = mapped_column(String(32), default="api_key")
    secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # Fernet token of the credentials JSON
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | paused_auth | revoked
    account_handle: Mapped[str | None] = mapped_column(String(255))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class DestinationRow(Base):
    __tablename__ = "destinations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("dest"))
    destination_key: Mapped[str] = mapped_column(String(512), unique=True)
    connector_key: Mapped[str] = mapped_column(String(64), index=True)
    # Appendix A destination record fields
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text, index=True)
    platform_key: Mapped[str] = mapped_column(String(64))
    surface: Mapped[str] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(128))
    audience_tags: Mapped[list[Any]] = mapped_column(default=list)
    language: Mapped[str | None] = mapped_column(String(16))
    geo: Mapped[str | None] = mapped_column(String(64))
    est_audience: Mapped[int | None] = mapped_column(BigInteger)
    rules_url: Mapped[str | None] = mapped_column(Text)
    frequency_cap: Mapped[str | None] = mapped_column(String(64))
    self_promo_allowed: Mapped[bool | None] = mapped_column(Boolean)
    link_policy: Mapped[str | None] = mapped_column(String(64))
    disclosure_required: Mapped[bool | None] = mapped_column(Boolean)
    automation_allowed: Mapped[bool | None] = mapped_column(Boolean)
    relevance_score: Mapped[int | None] = mapped_column(Integer)
    relevance_reason: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(4), default="L0")
    last_certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # service fields
    instance: Mapped[dict[str, Any]] = mapped_column(default=dict)
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("connections.id", ondelete="SET NULL"))
    spec_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | excluded
    excluded_reason: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class RuleSetRow(Base):
    __tablename__ = "rule_sets"
    __table_args__ = (UniqueConstraint("destination_id", "version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rules"))
    destination_id: Mapped[str] = mapped_column(ForeignKey("destinations.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    rules: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed | confirmed | superseded
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual | llm | default_conservative ...
    evidence: Mapped[list[Any]] = mapped_column(default=list)
    rules_page_sha256: Mapped[str | None] = mapped_column(String(64))
    confirmed_by: Mapped[str | None] = mapped_column(String(255))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


# ------------------------------------------------------------------ content and publishing


class ContentItemRow(Base):
    __tablename__ = "content_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("content"))
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    canonical_url: Mapped[str | None] = mapped_column(Text)
    media: Mapped[list[Any]] = mapped_column(default=list)
    tags: Mapped[list[Any]] = mapped_column(default=list)
    language: Mapped[str | None] = mapped_column(String(16))
    is_promotional: Mapped[bool | None] = mapped_column(Boolean)
    ai_generated: Mapped[bool | None] = mapped_column(Boolean)
    owner_domains: Mapped[list[Any]] = mapped_column(default=list)
    # rendition fields without their own column (link_url, content_type, body_format, mentions); rev 0002
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    created_at: Mapped[datetime] = _created()


class PublicationRow(Base):
    __tablename__ = "publications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("pub"))
    content_id: Mapped[str | None] = mapped_column(ForeignKey("content_items.id", ondelete="SET NULL"), index=True)
    destination_id: Mapped[str] = mapped_column(ForeignKey("destinations.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rule_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    platform_post_id: Mapped[str | None] = mapped_column(String(255))
    live_url: Mapped[str | None] = mapped_column(Text)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # job ids, tailoring diff/warnings, assist pack, reasons (rev 0002)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class PublishIntentRow(Base):
    """Write-ahead ledger for contract ``publish`` (plan §4.1). One row per idempotency key."""

    __tablename__ = "publish_intents"
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    connector_key: Mapped[str] = mapped_column(String(64), index=True)
    destination_key: Mapped[str] = mapped_column(String(512), index=True)
    marker: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(16))  # PENDING | SUCCEEDED | FAILED | RETRYABLE | UNKNOWN
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # from the injectable clock
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    instance: Mapped[str | None] = mapped_column(Text)  # instance_param value (status/delete without destination)
    connection_ref: Mapped[str | None] = mapped_column(String(64))  # connection id or hash of inline credentials
    is_promotional: Mapped[bool | None] = mapped_column(Boolean)  # promo detector verdict (self_promo ratio)


# ------------------------------------------------------------------ jobs and HTTP idempotency (IS3, IS4)


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("job"))
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | succeeded | failed
    progress: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    callback_url: Mapped[str | None] = mapped_column(Text)
    callback_status: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _updated()


class IdempotencyKeyRow(Base):
    """Stored HTTP response per (scope, Idempotency-Key). Written in the same transaction as the job row."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(255))
    key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str | None] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[bytes] = mapped_column(LargeBinary)
    response_headers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ScheduledTaskRow(Base):
    """Periodic tasks run by the worker's leader scheduler."""

    __tablename__ = "scheduled_tasks"
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    every_s: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_job_id: Mapped[str | None] = mapped_column(String(64))


# ------------------------------------------------------------------ certification, levels, alerts


class CertificationRunRow(Base):
    __tablename__ = "certification_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("cert"))
    destination_id: Mapped[str] = mapped_column(ForeignKey("destinations.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual")  # manual | scheduled | self_heal
    state: Mapped[str] = mapped_column(String(32), default="queued")
    steps: Mapped[list[Any]] = mapped_column(default=list)
    evidence_prefix: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class LevelHistoryRow(Base):
    __tablename__ = "level_history"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    destination_id: Mapped[str] = mapped_column(ForeignKey("destinations.id", ondelete="CASCADE"), index=True)
    level_before: Mapped[str | None] = mapped_column(String(4))
    level_after: Mapped[str] = mapped_column(String(4))
    reason: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class AlertRow(Base):
    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("alert"))
    kind: Mapped[str] = mapped_column(String(64), index=True)
    destination_id: Mapped[str | None] = mapped_column(String(64), index=True)
    connection_id: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(32), default="open")
    delivered_to: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _created()


# ------------------------------------------------------------------ reliability state (shared by api and worker)


class CircuitStateRow(Base):
    __tablename__ = "circuit_state"
    scope: Mapped[str] = mapped_column(String(512), primary_key=True)  # connector_key|instance
    connector_key: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(16), default="closed")  # closed | open | half_open
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _updated()


class RateLimitStateRow(Base):
    __tablename__ = "rate_limit_state"
    scope: Mapped[str] = mapped_column(String(512), primary_key=True)  # connector_key|connection|instance
    connector_key: Mapped[str] = mapped_column(String(64), index=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(String(64))
    code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = _updated()


class ConnectorFlagRow(Base):
    """Runtime kill switch (plan §4.3)."""

    __tablename__ = "connector_flags"
    connector_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _updated()


class TestPostRow(Base):
    """Sweeper audit for certification test posts (MH5)."""

    __tablename__ = "test_posts"
    __test__ = False  # not a pytest class
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    connector_key: Mapped[str] = mapped_column(String(64))
    destination_id: Mapped[str | None] = mapped_column(String(64), index=True)
    platform_post_id: Mapped[str] = mapped_column(String(255))
    live_url: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_mode: Mapped[str | None] = mapped_column(String(32))


# ------------------------------------------------------------------ catalogue, metrics, labels


class CatalogueImportRow(Base):
    __tablename__ = "catalogue_imports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("import"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    source_name: Mapped[str | None] = mapped_column(String(255))
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    imported: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    invalid: Mapped[int] = mapped_column(Integer, default=0)
    unreachable: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    job_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class MetricsSnapshotRow(Base):
    __tablename__ = "metrics_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    connector_key: Mapped[str] = mapped_column(String(64))
    destination_id: Mapped[str | None] = mapped_column(String(64), index=True)
    publication_id: Mapped[str | None] = mapped_column(String(64), index=True)
    platform_post_id: Mapped[str] = mapped_column(String(255))
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LabelRow(Base):
    __tablename__ = "labels"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    destination_id: Mapped[str] = mapped_column(String(64), index=True)
    content_id: Mapped[str | None] = mapped_column(String(64))
    query: Mapped[str | None] = mapped_column(Text)
    label: Mapped[float] = mapped_column(Float)
    labeler: Mapped[str] = mapped_column(String(64), default="human")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
