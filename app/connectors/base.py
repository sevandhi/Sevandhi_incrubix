"""BaseConnector: the contract methods, already wrapped with every reliability step (plan §4).

A connector subclass implements only small *hooks* that talk to its platform:

    _create(ctx, rendition, destination, connection, marker) -> PublishResult     (capability: publish)
    _lookup(ctx, marker, destination, connection) -> PublishResult | None          (capability: publish)
    _get_status(ctx, platform_post_id, destination, connection) -> StatusResult    (capability: status)
    _delete(ctx, platform_post_id, destination, connection) -> DeleteResult        (capability: delete)
    _fetch_metrics(ctx, platform_post_id, destination, connection, since) -> MetricsResult  (metrics)
    _refresh_auth(ctx, connection, destination) -> RefreshAuthResult               (refresh_auth)
    _build_assist_pack(ctx, rendition, destination) -> AssistPack                  (assist)

``publish`` runs this pipeline:

    kill switch → stored result? (replay / 409) → spec validation → rate-limit state + token bucket
    → circuit breaker → CLAIM (one short transaction: rules gate + write-ahead PENDING intent)
    → [reconcile by marker] → platform call → error mapping → breaker / rate-limit bookkeeping
    → redaction (in the HTTP error handler)

* The stored-result check comes before the rules gate on purpose: a retry of a post that already
  succeeded must return the stored result, not be blocked by the frequency cap that post itself used up.
* No DB connection is held while a platform is called. The PENDING intent row is the lock:
  a concurrent duplicate sees it and waits for the result; a concurrent post to the same destination
  counts it in the frequency-cap history.
* Rules (design decision D9): inline ``destination.rules`` > Appendix A record fields on the
  destination > our catalogue (by destination_id or destination_key) > none. With none, the post is
  allowed after the spec checks, with the warning ``rules_source=none_supplied``.

The other methods resolve where the post lives from the destination, the connection's extra fields, or
our publish ledger, so Appendix A's ``get_status(platform_post_id, connection)`` works.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import Clock
from app.config import Settings
from app.contract.manifest import CAPABILITY_HOOKS, Manifest, Surface, check_spec_values
from app.contract.models import (
    AssistPack,
    Connection,
    DeleteResult,
    Destination,
    MetricsResult,
    PublishResult,
    RefreshAuthResult,
    Rendition,
    StatusResult,
    ValidateResult,
    Violation,
)
from app.db.tables import ConnectionRow, DestinationRow, PublishIntentRow, RuleSetRow, TestPostRow
from app.errors import ErrorCode, IbxError
from app.reliability import breaker, killswitch, ratelimit
from app.reliability.idempotency import (
    Action,
    IntentLedger,
    IntentState,
    classify,
    in_flight,
    lock_xact,
    marker_for,
    request_hash,
)
from app.reliability.redact import redact, secret_registry
from app.reliability.retry_after import fallback_reset, parse_reset
from app.reliability.secrets import decrypt_credentials
from app.rules.engine import evaluate
from app.rules.gate import apply_decision, promo_flag, resolve_ruleset
from app.rules.models import PublishEvent, RuleSet
from app.rules.spec import check_spec

T = TypeVar("T")

# Diagnostics for the current call (warnings, replayed, intent_state ...). The router creates a fresh dict
# per request and returns it under ``_meta`` when the caller sends ``X-IncuBrix-Verbose: 1``.
CALL_META: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("ibx_call_meta", default=None)

# Connectors used during the current job: {key: version}. The worker sets a fresh dict per job and lists
# exactly these in the job manifest (IS9).
USED_CONNECTORS: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("ibx_used_connectors", default=None)

# Async callables run when a breaker opens: (connector_key, scope). Recertification registers here.
CIRCUIT_OPEN_LISTENERS: list[Callable[[str, str], Awaitable[None]]] = []

_buckets = ratelimit.BucketRegistry()

# Intents that may already be (or are being) posted reserve their slot in the frequency-cap history.
_COUNTED_INTENTS = (IntentState.SUCCEEDED.value, IntentState.PENDING.value, IntentState.UNKNOWN.value)
_HISTORY_LIMIT = 200  # newest intents read per rules check (enough for any cap or ratio(n))


def note(**values: Any) -> None:
    """Add diagnostics to the current call's ``_meta``."""
    meta = CALL_META.get()
    if meta is not None:
        meta.update(values)


def warn(message: str) -> None:
    meta = CALL_META.get()
    if meta is not None:
        meta.setdefault("warnings", []).append(message)


@dataclass
class ConnectorContext:
    """Everything a connector hook may use. Built once per connector by the app."""

    manifest: Manifest
    settings: Settings
    http: httpx.AsyncClient
    clock: Clock
    log: Any
    db_session_factory: async_sessionmaker[AsyncSession] | None
    simulation: bool = False
    storage: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def normalise_instance(value: str) -> str:
    """``https://WWW.Forum.org/c/x/`` → ``forum.org/c/x``; non-URL values are lower-cased."""
    value = (value or "").strip()
    if "://" not in value:
        return value.lower().rstrip("/")
    parts = urlsplit(value)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{host}{parts.path.rstrip('/')}"


_TAG = re.compile(r"^(?P<raw>.+)~i[0-9a-f]{10}$")


def tagged_post_id(raw: str, instance: str) -> str:
    """The id we return when the platform's own id already exists on another instance of the same connector
    (mock tenants and fresh instances all start at 1): ``<raw>~i<10 hex of the instance>``. Same Appendix A shape
    (a string); ``platform_id`` gives the platform's own id back."""
    return f"{raw}~i{hashlib.sha256(normalise_instance(instance).encode()).hexdigest()[:10]}"


def platform_id(platform_post_id: str) -> str:
    """The platform's own id for an id we returned (``tagged_post_id`` undone; any other id unchanged)."""
    match = _TAG.match(platform_post_id or "")
    return match.group("raw") if match else platform_post_id


class BaseConnector:
    key: ClassVar[str] = ""
    manifest: ClassVar[Manifest]
    mock_route: ClassVar[str | None] = None  # route name on mock-platforms used in simulation (default: key)

    def __init__(self, ctx: ConnectorContext) -> None:
        self.ctx = ctx
        self.log = ctx.log

    # ================================================================== public contract methods

    async def validate(
        self, rendition: Rendition, destination: Destination, connection: Connection | None = None
    ) -> ValidateResult:
        await self._ensure_enabled()
        violations = self.spec_violations(rendition, destination)
        return ValidateResult(ok=not violations, violations=violations)

    async def publish(
        self, rendition: Rendition, destination: Destination, connection: Connection, idempotency_key: str
    ) -> PublishResult:
        await self._ensure_enabled()
        self._require("publish")
        if not idempotency_key:
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "idempotency_key is required for publish.")
        conn = await self._resolve_connection(connection)
        dest_key = self.destination_key(destination)
        req_hash = request_hash(rendition.model_dump(mode="json"), dest_key, connection.connection_id or "inline")
        marker = marker_for(idempotency_key)
        ledger = IntentLedger(self._factory())
        note(idempotency_key=idempotency_key, marker=marker)

        # 1) a finished attempt answers at once (no lock, no rules, no platform call)
        intent = await ledger.get(idempotency_key)
        action = classify(intent, req_hash)
        if action in (Action.REPLAY, Action.REPLAY_ERROR):
            return self._from_ledger(intent, action)

        # 2) checks that need no lock
        violations = self.spec_violations(rendition, destination)
        if violations:
            raise self._spec_error(violations)
        sim_dest, sim_conn = self.simulate(destination, conn)
        scope = self._scopes(sim_dest, sim_conn)
        await self._preflight(scope)

        # 3) claim: rules gate + PENDING intent in one short transaction (released before any platform call)
        claim = await self._claim(idempotency_key, req_hash, marker, rendition, destination, dest_key, conn)
        if claim.action in (Action.REPLAY, Action.REPLAY_ERROR):
            return self._from_ledger(claim.intent, claim.action)
        rendition = claim.rendition

        # 4) an earlier attempt may have created the post: find it by its marker before creating again
        if claim.action == Action.RECONCILE:
            found = await self._reconcile(ledger, idempotency_key, claim, marker, sim_dest, sim_conn, scope,
                                          instance=self.instance_value(destination))
            if found is not None:
                return found
            rendition = await self._gate_again(ledger, idempotency_key, rendition, destination, dest_key)

        # 5) the platform call
        try:
            result = await self._call(
                lambda: self._create(self.ctx, rendition, sim_dest, sim_conn, marker), scope, sim_conn, writing=True
            )
        except IbxError as err:
            state = self._intent_state_for(err)
            err.details.update({"intent_state": state.value, "idempotency_key": idempotency_key})
            if state == IntentState.UNKNOWN:
                if err.details.get("reason") == "response_schema_mismatch":
                    fields = ", ".join(err.details.get("fields") or []) or "unknown"
                    err.message = (
                        f"{self.key}: response_schema_mismatch - the platform's answer to the publish no longer has the "
                        f"expected field(s) ({fields}); the platform may have renamed a field. The post may have been "
                        "created: outcome unknown, safe to retry with the same idempotency_key."
                    )
                else:
                    err.message = (
                        f"{self.key}: the platform did not confirm the publish (timeout or server error after the "
                        "request was sent); outcome unknown, safe to retry with the same idempotency_key."
                    )
            await ledger.mark(idempotency_key, state, self.now(), error=redact(err.to_body()))
            note(intent_state=state.value)
            raise
        result = await self._succeed(idempotency_key, result, self.instance_value(destination))
        note(intent_state=IntentState.SUCCEEDED.value)
        return result

    async def get_status(
        self, platform_post_id: str, connection: Connection, destination: Destination | None = None
    ) -> StatusResult:
        return await self._simple(
            "status", connection, destination, lambda d, c: self._get_status(self.ctx, platform_id(platform_post_id), d, c),
            platform_post_id=platform_post_id,
        )

    async def delete(
        self, platform_post_id: str, connection: Connection, destination: Destination | None = None
    ) -> DeleteResult:
        return await self._simple(
            "delete", connection, destination, lambda d, c: self._delete(self.ctx, platform_id(platform_post_id), d, c),
            platform_post_id=platform_post_id,
        )

    async def fetch_metrics(
        self,
        platform_post_id: str,
        connection: Connection,
        since: datetime | None,
        destination: Destination | None = None,
    ) -> MetricsResult:
        return await self._simple(
            "metrics",
            connection,
            destination,
            lambda d, c: self._fetch_metrics(self.ctx, platform_id(platform_post_id), d, c, since),
            platform_post_id=platform_post_id,
        )

    async def refresh_auth(self, connection: Connection, destination: Destination | None = None) -> RefreshAuthResult:
        return await self._simple(
            "refresh_auth", connection, destination, lambda d, c: self._refresh_auth(self.ctx, c, d), allow_paused=True
        )

    async def build_assist_pack(
        self, rendition: Rendition, destination: Destination, *, enforce_rules: bool = True
    ) -> AssistPack:
        """The pack a person posts. The rules gate runs first, as for publish: a pack that breaks a
        rule is refused with the rule's 409, so no ready-to-post pack (or prefilled deep link) is ever built for it.
        ``enforce_rules=False`` is only for certification, whose neutral test pack is never posted."""
        await self._ensure_enabled()
        self._require("assist")
        violations = self.spec_violations(rendition, destination)
        if violations:
            raise self._spec_error(violations)
        if enforce_rules:
            rendition = await self._assist_rules_gate(rendition, destination)
        try:
            return await self._build_assist_pack(self.ctx, rendition, destination)
        except ValidationError as exc:
            raise self._schema_error(exc, writing=False) from exc

    # ================================================================== hooks (override what you list in capabilities)

    async def _create(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination, connection: Connection, marker: str
    ) -> PublishResult:
        raise NotImplementedError

    async def _lookup(
        self, ctx: ConnectorContext, marker: str, destination: Destination, connection: Connection
    ) -> PublishResult | None:
        raise NotImplementedError

    async def _get_status(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> StatusResult:
        raise NotImplementedError

    async def _delete(
        self, ctx: ConnectorContext, platform_post_id: str, destination: Destination, connection: Connection
    ) -> DeleteResult:
        raise NotImplementedError

    async def _fetch_metrics(
        self,
        ctx: ConnectorContext,
        platform_post_id: str,
        destination: Destination,
        connection: Connection,
        since: datetime | None,
    ) -> MetricsResult:
        raise NotImplementedError

    async def _refresh_auth(
        self, ctx: ConnectorContext, connection: Connection, destination: Destination
    ) -> RefreshAuthResult:
        raise NotImplementedError

    async def _build_assist_pack(
        self, ctx: ConnectorContext, rendition: Rendition, destination: Destination
    ) -> AssistPack:
        raise NotImplementedError

    @classmethod
    def implemented_capabilities(cls) -> set[str]:
        """Capabilities whose hooks this class overrides (the conformance kit compares this to the manifest)."""
        return {
            capability
            for capability, hooks in CAPABILITY_HOOKS.items()
            if all(getattr(cls, hook) is not getattr(BaseConnector, hook) for hook in hooks)
        }

    # ================================================================== helpers for connector authors

    def now(self) -> datetime:
        return self.ctx.clock.now()

    def instance_value(self, destination: Destination | None, connection: Connection | None = None) -> str:
        """The destination's ``instance_param`` value (e.g. base_url), from the destination or the connection."""
        param = self.manifest.instance_param
        candidates: list[Any] = []
        if destination is not None:
            candidates += [destination.instance.get(param), (destination.model_extra or {}).get(param), destination.url]
        if connection is not None:
            extra = connection.model_extra or {}
            instance = extra.get("instance")
            candidates += [extra.get(param), instance.get(param) if isinstance(instance, dict) else instance]
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise IbxError(
            ErrorCode.REQUEST_VALIDATION_FAILED,
            f"Connector '{self.key}' needs to know which {param} to call: send destination.instance.{param}, "
            f"connection.{param}, or a platform_post_id that this service published.",
            details={"errors": [{"loc": ["destination", "instance", param], "type": "missing", "msg": "Field required"}]},
        )

    def destination_key(self, destination: Destination) -> str:
        surface = destination.surface or (self.manifest.surfaces[0].key if self.manifest.surfaces else "")
        return f"{self.key}|{normalise_instance(self.instance_value(destination))}|{surface}"

    def surface_for(self, rendition: Rendition, destination: Destination) -> Surface:
        wanted = destination.surface or rendition.surface
        if not wanted:
            return self.manifest.surfaces[0]
        surface = self.manifest.surface(wanted)
        if surface is None:
            known = [s.key for s in self.manifest.surfaces]
            raise self._spec_error(
                [Violation(field="surface", rule="surface_supported", message=f"Unknown surface '{wanted}'.", limit=known, actual=wanted)]
            )
        return surface

    def spec_violations(self, rendition: Rendition, destination: Destination) -> list[Violation]:
        """All spec problems for this surface (the rules engine's ``check_spec``)."""
        surface = self.surface_for(rendition, destination)
        spec = {**surface.spec, "content_types": list(surface.content_types)}
        try:
            check_spec_values(destination.spec_overrides or {})
        except ValueError as exc:  # a caller's bad override is their 422, never our 500
            raise IbxError(
                ErrorCode.REQUEST_VALIDATION_FAILED, f"destination.spec_overrides is invalid: {exc}.",
                details={"errors": [{"loc": ["destination", "spec_overrides"], "type": "value_error", "msg": str(exc)}]},
            ) from None
        return check_spec(rendition, spec, destination.spec_overrides)

    def simulate(self, destination: Destination, connection: Connection) -> tuple[Destination, Connection]:
        """In SIMULATION_MODE, point the destination at mock-platforms and use ``sim-*`` credentials."""
        if not self._has_instance(destination, connection):
            return destination, connection
        mock_base = self.ctx.settings.MOCK_PLATFORM_URL.rstrip("/")
        original = self.instance_value(destination, connection)
        if original.startswith(mock_base):
            # A destination that already targets our mock-platforms container (e.g. the brief's "Forum 41" row)
            # gets a placeholder credential when it has none. Only in simulation or a dev/test run, never in a
            # demo/prod local-mode run.
            if not connection.credentials and (self.ctx.simulation or self.ctx.settings.APP_ENV in ("dev", "test")):
                connection = connection.model_copy(update={"credentials": self.sim_credentials(connection)})
            return destination, connection
        if not self.ctx.simulation:
            return destination, connection
        param = self.manifest.instance_param
        tenant = re.sub(r"[^a-z0-9]+", "-", normalise_instance(original)).strip("-")[:60] or "default"
        instance = {**destination.instance, param: f"{mock_base}/{self.mock_route or self.key}/{tenant}"}
        return (
            destination.model_copy(update={"instance": instance}),
            connection.model_copy(update={"credentials": self.sim_credentials(connection)}),
        )

    def sim_credentials(self, connection: Connection) -> dict[str, SecretStr]:
        """Placeholder credentials that our mock of this platform accepts (simulation only, never real).
        Override when the mock checks them, e.g. the webhook's signing secret or Ghost's Admin key format."""
        names = list(connection.credentials) or ["token"]
        return {name: SecretStr(f"sim-{name}") for name in names}

    def check(self, response: httpx.Response) -> httpx.Response:
        """Raise the mapped ``IbxError`` if the platform answered with an error status."""
        if response.is_error:
            raise self.map_error(response)
        return response

    def parse(self, response: httpx.Response, model: type[BaseModel]) -> Any:
        """``check`` + strict parse; a renamed/missing field raises ValidationError → schema mismatch."""
        self.check(response)
        return model.model_validate(response.json())

    def map_error(self, response: httpx.Response) -> IbxError:
        """Platform response → IbxError via ``manifest.error_map`` ("status:body_token" first, then "status")."""
        status = response.status_code
        try:
            text = response.text[:8000]
        except Exception:  # noqa: BLE001 - undecodable body
            text = ""
        try:
            body = response.json()
        except ValueError:
            body = None
        code: ErrorCode | None = None
        matched: str | None = None
        tokens = _error_tokens(body)
        body_keys = [(k, v) for k, v in self.manifest.error_map.items() if ":" in k and int(k.split(":", 1)[0]) == status]
        # 1) exact match on the JSON error fields (Lemmy {"error": "rate_limit_error"}, Slack {"error": "token_revoked"})
        for key, value in body_keys:
            if key.split(":", 1)[1] in tokens:
                code, matched = value, key
                break
        # 2) otherwise the token anywhere in the raw body (non-JSON or nested error formats)
        if code is None:
            for key, value in body_keys:
                if key.split(":", 1)[1] in text:
                    code, matched = value, key
                    break
        if code is None and str(status) in self.manifest.error_map:
            code, matched = self.manifest.error_map[str(status)], str(status)
        details: dict[str, Any] = {"platform_status": status, "platform_message": _platform_message(body, text)}
        if code is None:
            code = default_error_code(status)
            if status < 500 and status not in (401, 403, 408, 429):
                details["unmapped"] = True
                self.log.warning("unmapped_platform_error", connector=self.key, platform_status=status)
        else:
            details["error_map_key"] = matched
        if code in (ErrorCode.PLATFORM_RATE_LIMIT, ErrorCode.PLATFORM_QUOTA_EXHAUSTED):
            reset = parse_reset(response.headers, body, self.now()) or fallback_reset(
                self.ctx.settings.RATE_LIMIT_DEFAULT_RETRY_S, self.now(), "default"
            )
            details.update(reset.as_details())
        message = f"{self.key} answered HTTP {status}"
        if details["platform_message"]:
            message += f": {details['platform_message']}"
        return IbxError(code, message, details=details)

    # ================================================================== pipeline internals

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self.ctx.db_session_factory is None:
            raise IbxError(ErrorCode.DEPENDENCY_UNAVAILABLE, "The database is not configured.")
        return self.ctx.db_session_factory

    async def _ensure_enabled(self) -> None:
        used = USED_CONNECTORS.get()
        if used is not None:
            used[self.key] = self.manifest.version
        if self.ctx.db_session_factory is None:
            await killswitch.ensure_enabled(None, self.ctx.settings, self.key)
            return
        async with self.ctx.db_session_factory() as session:
            await killswitch.ensure_enabled(session, self.ctx.settings, self.key)

    def _require(self, capability: str) -> None:
        if capability not in self.manifest.capabilities:
            raise IbxError(
                ErrorCode.METHOD_NOT_SUPPORTED,
                f"Connector '{self.key}' does not support '{capability}'.",
                details={"connector_key": self.key, "capabilities": list(self.manifest.capabilities)},
            )

    async def _resolve_connection(self, connection: Connection | None, *, allow_paused: bool = False) -> Connection:
        """Inline credentials, or load + decrypt ``connection_id`` from our store. Paused connections fail fast."""
        if connection is None:
            return Connection(connector_key=self.key, auth_type="none")
        if connection.credentials:
            secret_registry.add_many(v.get_secret_value() for v in connection.credentials.values())
            return connection
        if not connection.connection_id or self.ctx.db_session_factory is None:
            return connection
        async with self.ctx.db_session_factory() as session:
            row = await session.get(ConnectionRow, connection.connection_id)
        if row is None or row.connector_key != self.key:
            raise IbxError(ErrorCode.CONNECTION_NOT_FOUND, f"Unknown connection '{connection.connection_id}' for '{self.key}'.")
        if row.status != "active" and not allow_paused:
            raise IbxError(
                ErrorCode.CONNECTION_AUTH_EXPIRED,
                f"Connection {row.id} is {row.status}; the owner must reconnect before posting.",
                details={"connection_id": row.id, "connection_state": row.status, "action": "reconnect_required"},
            )
        credentials = decrypt_credentials(self.ctx.settings.fernet_key(), row.secret_enc)
        return connection.model_copy(
            update={
                "credentials": {k: SecretStr(v) for k, v in credentials.items()},
                "auth_type": row.auth_type,
                "expires_at": row.expires_at,
            }
        )

    async def _simple(
        self,
        capability: str,
        connection: Connection,
        destination: Destination | None,
        hook: Callable[[Destination, Connection], Awaitable[T]],
        *,
        platform_post_id: str | None = None,
        allow_paused: bool = False,
    ) -> T:
        """Pipeline for the non-publish methods: kill switch → capability → connection → where the post lives
        → limits → breaker → call."""
        await self._ensure_enabled()
        self._require(capability)
        conn = await self._resolve_connection(connection, allow_paused=allow_paused)
        dest = destination or Destination(connector_key=self.key)
        if not self._has_instance(dest, conn) and platform_post_id:
            instance = await self._instance_from_ledger(platform_post_id, conn)
            if instance:
                dest = dest.model_copy(update={"instance": {**dest.instance, self.manifest.instance_param: instance}})
        dest, conn = self.simulate(dest, conn)
        scope = self._scopes(dest, conn)
        await self._preflight(scope)
        return await self._call(lambda: hook(dest, conn), scope, conn)

    async def _instance_from_ledger(self, platform_post_id: str, connection: Connection) -> str | None:
        """The instance a post was published to, from our publish ledger (Appendix A's get_status/delete
        carry no destination). An id that exists on two instances is returned tagged (``tagged_post_id``),
        so this is unambiguous for new posts. For older rows with the same id on several instances, the caller's
        connection (connection_id or inline-credentials fingerprint) decides, newest post first. Only when the
        connection does not tell them apart (an anonymous connection, or none of its posts) is it a clear 422."""
        if self.ctx.db_session_factory is None:
            return None
        stmt = (
            select(PublishIntentRow.instance, PublishIntentRow.connection_ref)
            .where(
                PublishIntentRow.connector_key == self.key,
                PublishIntentRow.state == IntentState.SUCCEEDED.value,
                PublishIntentRow.instance.is_not(None),
                PublishIntentRow.result["platform_post_id"].astext == platform_post_id,
            )
            .order_by(PublishIntentRow.updated_at.desc())
        )
        async with self.ctx.db_session_factory() as session:
            rows = (await session.execute(stmt)).all()
        if not rows:
            return None
        instances = {normalise_instance(row.instance) for row in rows}
        if len(instances) == 1:
            return rows[0].instance
        ref = self._connection_ref(connection)
        if ref != "anon":
            mine = [row for row in rows if row.connection_ref == ref]
            if mine:
                return mine[0].instance  # newest post of this connection
        param = self.manifest.instance_param
        raise IbxError(
            ErrorCode.REQUEST_VALIDATION_FAILED,
            f"platform_post_id '{platform_post_id}' exists on {len(instances)} {self.key} instances and the connection "
            f"does not tell them apart; send connection.{param} or destination.instance.{param} to choose one.",
            details={"platform_post_id": platform_post_id, "instances": len(instances)},
        )

    def _has_instance(self, destination: Destination, connection: Connection) -> bool:
        try:
            self.instance_value(destination, connection)
            return True
        except IbxError:
            return False

    def _scopes(self, destination: Destination, connection: Connection) -> _Scopes:
        try:
            instance = normalise_instance(self.instance_value(destination, connection))
        except IbxError:
            instance = "*"
        return _Scopes(breaker=f"{self.key}|{instance}", rate=f"{self.key}|{self._connection_ref(connection)}|{instance}")

    @staticmethod
    def _connection_ref(connection: Connection) -> str:
        """A stable, non-secret name for a connection: its id, or a short hash of inline credentials."""
        if connection.connection_id:
            return connection.connection_id
        if connection.credentials:
            joined = "|".join(f"{k}={v.get_secret_value()}" for k, v in sorted(connection.credentials.items()))
            return "inline-" + hashlib.sha256(joined.encode()).hexdigest()[:16]
        return "anon"

    async def _preflight(self, scope: _Scopes) -> None:
        """Short-circuit on a known rate-limit block, the local token bucket, or an open breaker."""
        factory = self._factory()
        now = self.now()
        async with factory() as session:
            blocked = await ratelimit.blocked_until(session, scope.rate, now)
        if blocked is not None:
            seconds = max(1, int((blocked.blocked_until - now).total_seconds()) + 1)
            raise IbxError(
                ErrorCode(blocked.code or ErrorCode.PLATFORM_RATE_LIMIT.value),
                f"{self.key} is rate limited until {blocked.blocked_until.isoformat()}; not calling the platform.",
                details={
                    "retry_at": blocked.blocked_until.isoformat().replace("+00:00", "Z"),
                    "retry_after_s": seconds,
                    "reset_source": blocked.source,
                    "short_circuit": True,
                },
            )
        limit = self.manifest.rate_limits.per_connection
        if limit is not None:
            waited = 0.0
            while (wait := _buckets.take(scope.rate, limit.requests, limit.per_seconds, self.now().timestamp())) > 0:
                if waited + wait > self.ctx.settings.RATE_LIMIT_MAX_WAIT_S:
                    raise IbxError(
                        ErrorCode.PLATFORM_RATE_LIMIT,
                        f"Local rate limit for {self.key} ({limit.requests}/{limit.per_seconds:g}s) reached.",
                        details={"retry_after_s": int(wait) + 1, "reset_source": "local_token_bucket"},
                    )
                await self.ctx.clock.sleep(wait)
                waited += wait
        async with factory() as session, session.begin():
            await breaker.check(session, scope.breaker, self.now(), probe_timeout_s=self.ctx.settings.PLATFORM_TIMEOUT_S * 3)

    async def _call(
        self, fn: Callable[[], Awaitable[T]], scope: _Scopes, connection: Connection, *, writing: bool = False
    ) -> T:
        """Run a hook, translate any failure into an IbxError, and update breaker / rate-limit state."""
        try:
            result = await fn()
        except Exception as exc:  # noqa: BLE001 - every failure is translated below
            err = self._translate(exc, writing=writing)
            status = err.details.get("platform_status")
            if writing and isinstance(status, int) and (status >= 500 or status == 408):
                # A 5xx/408 to a create may come after the platform saved the post (a proxy 502/504,
                # a crash after commit), so the outcome is unknown and the retry looks for the marker first.
                err.details["outcome"] = "unknown"
            await self._after_call(scope, connection, err)
            raise err from exc
        await self._after_call(scope, connection, None)
        return result

    def _translate(self, exc: Exception, *, writing: bool) -> IbxError:
        if isinstance(exc, IbxError):
            return exc
        if isinstance(exc, httpx.HTTPStatusError):
            return self.map_error(exc.response)
        if isinstance(exc, ValidationError):
            return self._schema_error(exc, writing=writing)
        if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
            return IbxError(
                ErrorCode.PLATFORM_UNAVAILABLE,
                f"{self.key}: could not connect to the platform ({type(exc).__name__}).",
                details={"reason": "connect_failed", "outcome": "not_sent"},
            )
        if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
            return IbxError(
                ErrorCode.PLATFORM_UNAVAILABLE,
                f"{self.key}: no answer within {self.ctx.settings.PLATFORM_TIMEOUT_S:g}s ({type(exc).__name__}).",
                details={"reason": "timeout", "outcome": "unknown" if writing else "failed"},
            )
        if isinstance(exc, NotImplementedError):
            return IbxError(ErrorCode.METHOD_NOT_SUPPORTED, f"Connector '{self.key}' does not implement this method.")
        self.log.error("connector_unexpected_error", connector=self.key, error=redact(exc))
        return IbxError(
            ErrorCode.PLATFORM_UNAVAILABLE,
            f"{self.key}: unexpected connector error ({type(exc).__name__}).",
            details={"reason": "connector_exception", "outcome": "unknown" if writing else "failed"},
        )

    def _schema_error(self, exc: ValidationError, *, writing: bool) -> IbxError:
        fields = [".".join(str(p) for p in e["loc"]) for e in exc.errors()][:20]
        return IbxError(
            ErrorCode.PLATFORM_UNAVAILABLE,
            f"{self.key}: the platform response did not match the expected schema (fields: {', '.join(fields)}).",
            details={
                "reason": "response_schema_mismatch",
                "fields": fields,
                "outcome": "unknown" if writing else "failed",
            },
        )

    async def _after_call(self, scope: _Scopes, connection: Connection, err: IbxError | None) -> None:
        factory = self._factory()
        now = self.now()
        unavailable = err is not None and counts_as_outage(err)
        async with factory() as session, session.begin():
            opened = await breaker.record(
                session,
                scope.breaker,
                self.key,
                unavailable=unavailable,
                now=now,
                threshold=self.ctx.settings.CIRCUIT_FAILS,
                open_s=self.ctx.settings.CIRCUIT_OPEN_S,
                error=err.message if err else None,
            )
            if err is not None and err.code in (ErrorCode.PLATFORM_RATE_LIMIT, ErrorCode.PLATFORM_QUOTA_EXHAUSTED):
                retry_at = err.details.get("retry_at")
                if isinstance(retry_at, str):
                    until = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
                    await ratelimit.block(session, scope.rate, self.key, until, err.details.get("reset_source") or "unknown", err.code.value)
            if err is not None and err.code == ErrorCode.CONNECTION_AUTH_EXPIRED and connection.connection_id:
                row = await session.get(ConnectionRow, connection.connection_id)
                if row is not None:
                    row.status = "paused_auth"
                    err.details.update({"connection_id": row.id, "connection_state": "paused", "action": "reconnect_required"})
        if opened:
            self.log.warning("circuit_opened", connector=self.key, scope=scope.breaker)
            for listener in CIRCUIT_OPEN_LISTENERS:
                try:
                    await listener(self.key, scope.breaker)
                except Exception as exc:  # noqa: BLE001 - a listener must not break the call path
                    self.log.error("circuit_listener_failed", error=redact(exc))

    @staticmethod
    def _intent_state_for(err: IbxError) -> IntentState:
        if err.details.get("outcome") == "unknown":
            return IntentState.UNKNOWN
        if err.code == ErrorCode.PLATFORM_CONTENT_REJECTED:
            return IntentState.FAILED  # the same content will be rejected again: replay the error
        return IntentState.RETRYABLE  # nothing was created (rate limit, auth, not sent)

    def _stored_error(self, intent: PublishIntentRow) -> IbxError:
        body = (intent.error or {}).get("error", {})
        try:
            code = ErrorCode(body.get("code"))
        except ValueError:
            code = ErrorCode.PLATFORM_CONTENT_REJECTED
        details = {**(body.get("details") or {}), "replayed": True}
        return IbxError(code, body.get("message"), details=details, retryable=body.get("retryable"))

    @staticmethod
    def _spec_error(violations: list[Violation]) -> IbxError:
        return IbxError(
            ErrorCode.DEST_SPEC_VIOLATION,
            f"Content breaks {len(violations)} destination limit(s): " + "; ".join(v.message for v in violations[:5]),
            details={"violations": [v.model_dump(mode="json") for v in violations]},
        )

    # ------------------------------------------------------------------ publish: claim, reconcile, rules

    async def _succeed(self, key: str, result: PublishResult, instance: str) -> PublishResult:
        """Mark the intent SUCCEEDED. If a post with the same platform id already exists on ANOTHER instance of this
        connector (mock tenants and fresh instances all start at 1), the id we return is tagged with the instance
        (``tagged_post_id``), so Appendix A's ``get_status/delete/fetch_metrics(platform_post_id, connection)``
        always finds the right instance (MH1). The check and the write share one transaction under a lock on the id,
        so two instances answering the same id at the same moment are still told apart. Hooks always receive the
        platform's own id (``platform_id``); the ledger keeps (connector, instance, connection, returned id)."""
        raw = result.platform_post_id
        mine = normalise_instance(instance)
        async with self._factory()() as session, session.begin():
            await lock_xact(session, f"post-id:{self.key}:{raw}", self.ctx.settings.IDEMPOTENCY_LOCK_WAIT_S)
            others = (await session.execute(select(PublishIntentRow.instance).where(
                PublishIntentRow.connector_key == self.key,
                PublishIntentRow.state == IntentState.SUCCEEDED.value,
                PublishIntentRow.idempotency_key != key,
                PublishIntentRow.result["platform_post_id"].astext == raw,
            ))).scalars().all()
            if any(normalise_instance(other or "") != mine for other in others):
                result = result.model_copy(update={"platform_post_id": tagged_post_id(raw, instance)})
                note(platform_id=raw)
            row = await session.get(PublishIntentRow, key, with_for_update=True)
            if row is not None:
                row.state, row.updated_at = IntentState.SUCCEEDED.value, self.now()
                row.result, row.error = result.model_dump(mode="json"), None
        return result

    def _from_ledger(self, intent: PublishIntentRow, action: Action) -> PublishResult:
        """Answer a finished attempt from the ledger: the stored result, or the same error again."""
        note(replayed=True, intent_state=intent.state)
        if action == Action.REPLAY_ERROR:
            raise self._stored_error(intent)
        return PublishResult.model_validate(intent.result)

    async def _claim(
        self,
        key: str,
        req_hash: str,
        marker: str,
        rendition: Rendition,
        destination: Destination,
        dest_key: str,
        connection: Connection,
    ) -> _Claim:
        """One short transaction, under advisory locks on this key and this destination: re-read the intent,
        run the rules gate for a new attempt, and write the PENDING row. Committing releases the locks, so no
        DB connection is held during the platform call. If another request is publishing this
        key right now (a fresh PENDING row), wait for its result instead of posting again."""
        settings = self.ctx.settings
        lease_s = settings.PLATFORM_TIMEOUT_S * 6  # longer than any create (plus its helper calls) can take
        deadline = time.monotonic() + settings.IDEMPOTENCY_LOCK_WAIT_S
        while True:
            now = self.now()
            async with self._factory()() as session, session.begin():
                await lock_xact(session, f"publish:{key}", settings.IDEMPOTENCY_LOCK_WAIT_S)
                await lock_xact(session, f"publish-dest:{dest_key}", settings.IDEMPOTENCY_LOCK_WAIT_S)
                intent = await session.get(PublishIntentRow, key)
                if not in_flight(intent, now, lease_s):
                    action = classify(intent, req_hash)
                    if action in (Action.REPLAY, Action.REPLAY_ERROR):
                        return _Claim(action, rendition, intent, None)
                    previous_at = intent.updated_at if intent is not None else None
                    if action != Action.RECONCILE:  # a reconcile may find the post; its rules ran before
                        rendition = await self._rules_gate(session, rendition, destination, dest_key, key)
                    if intent is None:
                        intent = PublishIntentRow(
                            idempotency_key=key, request_hash=req_hash, connector_key=self.key, destination_key=dest_key,
                            marker=marker, attempts=0, created_at=now, instance=self.instance_value(destination),
                            connection_ref=self._connection_ref(connection),
                        )
                        session.add(intent)
                    intent.state, intent.updated_at, intent.attempts = IntentState.PENDING.value, now, intent.attempts + 1
                    intent.is_promotional = promo_flag(rendition, _owner_domains(rendition))
                    return _Claim(action, rendition, intent, previous_at)
            if time.monotonic() > deadline:
                raise IbxError(
                    ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                    "Another request with this idempotency_key is still publishing; retry shortly.",
                    details={"idempotency_key": key, "intent_state": IntentState.PENDING.value},
                )
            await asyncio.sleep(0.1)

    async def _reconcile(
        self,
        ledger: IntentLedger,
        key: str,
        claim: _Claim,
        marker: str,
        destination: Destination,
        connection: Connection,
        scope: _Scopes,
        *,
        instance: str,
    ) -> PublishResult | None:
        """An earlier attempt's outcome is unknown: look for its marker on the platform. Returns the post if it
        exists; raises 503 if it is too early to say it does not; returns None when a new create is safe."""
        try:
            found = await self._call(lambda: self._lookup(self.ctx, marker, destination, connection), scope, connection)
        except IbxError:
            await ledger.mark(key, IntentState.UNKNOWN, claim.previous_at or self.now())  # still unknown
            raise
        if found is not None:
            found = await self._succeed(key, found, instance)
            note(reconciled=True, intent_state=IntentState.SUCCEEDED.value)
            warn("RECONCILED_EXISTING_POST: found the post from an earlier attempt by its marker")
            return found
        age = (self.now() - (claim.previous_at or self.now())).total_seconds()
        if age < self.ctx.settings.IDEMPOTENCY_SETTLE_S:
            await ledger.mark(key, IntentState.UNKNOWN, claim.previous_at or self.now())
            raise IbxError(
                ErrorCode.PLATFORM_UNAVAILABLE,
                "An earlier attempt's outcome is still unknown and the post is not visible yet; retry shortly.",
                details={"intent_state": IntentState.UNKNOWN.value, "idempotency_key": key,
                         "retry_after_s": int(self.ctx.settings.IDEMPOTENCY_SETTLE_S - age) + 1},
            )
        return None

    async def _gate_again(
        self, ledger: IntentLedger, key: str, rendition: Rendition, destination: Destination, dest_key: str
    ) -> Rendition:
        """After a reconcile proved nothing was posted, the next create is a new post: check the rules first."""
        try:
            async with self._factory()() as session, session.begin():
                await lock_xact(session, f"publish-dest:{dest_key}", self.ctx.settings.IDEMPOTENCY_LOCK_WAIT_S)
                return await self._rules_gate(session, rendition, destination, dest_key, key)
        except IbxError:
            await ledger.mark(key, IntentState.RETRYABLE, self.now())  # nothing was created
            raise

    async def _rules_gate(
        self, session: AsyncSession, rendition: Rendition, destination: Destination, dest_key: str, key: str
    ) -> Rendition:
        """Posting rules for this destination (decision D9, the rules gate). Block / reschedule raise the rule's 409;
        required changes (the AI disclosure) are applied and the changed rendition is returned."""
        ruleset, source = await self.resolve_rules(session, destination, dest_key)
        if ruleset is None:
            return rendition
        history = await self._history(session, dest_key, exclude_key=key)
        decision = evaluate(rendition, ruleset, history, self.now(), owner_domains=_owner_domains(rendition))
        for message in decision.warnings:
            warn(str(message))
        fixed = apply_decision(rendition, decision)
        if fixed is not rendition and (violations := self.spec_violations(fixed, destination)):
            raise self._spec_error(violations)  # e.g. the disclosure line pushed the body over the limit
        return fixed

    async def _assist_rules_gate(self, rendition: Rendition, destination: Destination) -> Rendition:
        """Rules for an assisted pack: the same check as publish, except that "no automated posting" is satisfied
        (a person posts the pack; that is why the destination is assisted). A pack is not a post, so it does
        not use up a frequency-cap slot; the cap is still checked against the destination's real posts."""
        dest_key = self.destination_key(destination)
        factory = self.ctx.db_session_factory
        if factory is None:
            ruleset, history = (await self.resolve_rules(None, destination, dest_key))[0], []
        else:
            async with factory() as session:
                ruleset = (await self.resolve_rules(session, destination, dest_key))[0]
                history = await self._history(session, dest_key) if ruleset is not None else []
        if ruleset is None:
            return rendition
        if not ruleset.automation_allowed:
            ruleset = ruleset.model_copy(update={"automation_allowed": True})
        decision = evaluate(rendition, ruleset, history, self.now(), owner_domains=_owner_domains(rendition))
        for message in decision.warnings:
            warn(str(message))
        fixed = apply_decision(rendition, decision)  # raises the rule's 409 (block / reschedule)
        if fixed is not rendition and (violations := self.spec_violations(fixed, destination)):
            raise self._spec_error(violations)
        return fixed

    async def resolve_rules(
        self, session: AsyncSession | None, destination: Destination, dest_key: str
    ) -> tuple[RuleSet | None, str]:
        """D9 order: inline ``destination.rules`` > Appendix A record fields > our catalogue's confirmed rules
        (by destination_id, else destination_key) > none. With none: allowed, warning rules_source=none_supplied.
        Invalid rules raise 422 REQUEST_VALIDATION_FAILED."""
        catalogue = await self._catalogue_rules(session, destination, dest_key) if session is not None else None
        ruleset, source = resolve_ruleset(destination, catalogue)
        if ruleset is None:
            source = "none_supplied"
            warn("rules_source=none_supplied: no posting rules were sent or are confirmed for this destination; "
                 "only the spec checks ran")
            self.log.warning("rules_source_none_supplied", connector=self.key, destination_key=dest_key)
        note(rules_source=source)
        return ruleset, source

    async def _catalogue_rules(self, session: AsyncSession, destination: Destination, dest_key: str) -> dict[str, Any] | None:
        """The newest confirmed rules in our catalogue for this destination (by id, else by destination_key)."""
        dest_id = destination.destination_id
        if dest_id is None:
            found = select(DestinationRow.id).where(DestinationRow.destination_key == dest_key)
            dest_id = (await session.execute(found)).scalar_one_or_none()
        if dest_id is None:
            return None
        stmt = (
            select(RuleSetRow.rules)
            .where(RuleSetRow.destination_id == dest_id, RuleSetRow.status == "confirmed")
            .order_by(RuleSetRow.version.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _history(self, session: AsyncSession, dest_key: str, *, exclude_key: str | None = None) -> list[PublishEvent]:
        """Earlier posts on this destination for the frequency cap and the promo ratio (newest first).
        Posted, being posted (PENDING) and maybe-posted (UNKNOWN) intents count; certification test posts never do."""
        stmt = (
            select(PublishIntentRow)
            .where(PublishIntentRow.destination_key == dest_key, PublishIntentRow.state.in_(_COUNTED_INTENTS),
                   PublishIntentRow.idempotency_key != (exclude_key or ""))
            .order_by(PublishIntentRow.updated_at.desc())
            .limit(_HISTORY_LIMIT)
        )
        rows = (await session.execute(stmt)).scalars().all()
        tests = (await session.execute(
            select(TestPostRow.platform_post_id, TestPostRow.run_id, DestinationRow.destination_key)
            .join(DestinationRow, DestinationRow.id == TestPostRow.destination_id, isouter=True)
            .where(TestPostRow.connector_key == self.key))).all()
        test_keys = {f"cert-{t.run_id}" for t in tests}  # the harness publishes with idempotency_key cert-<run_id>
        # a test post's id only identifies it on ITS destination: the same id on another instance is a real post (MH4)
        test_ids = {t.platform_post_id for t in tests if t.destination_key == dest_key}
        return [
            PublishEvent(published_at=row.updated_at, destination_key=dest_key, status="published",
                         is_promotional=row.is_promotional)
            for row in rows
            if row.idempotency_key not in test_keys and str((row.result or {}).get("platform_post_id")) not in test_ids
        ]


@dataclass
class _Claim:
    action: Action
    rendition: Rendition  # after the rules gate (e.g. with the AI disclosure inserted)
    intent: PublishIntentRow | None
    previous_at: datetime | None  # when the earlier attempt last changed (for the settle window)


def _owner_domains(rendition: Rendition) -> tuple[str, ...]:
    return tuple((rendition.model_extra or {}).get("owner_domains") or ())


@dataclass(frozen=True)
class _Scopes:
    breaker: str  # connector|instance
    rate: str  # connector|connection|instance


def default_error_code(status: int) -> ErrorCode:
    """What an HTTP status means when the manifest's error_map does not name it."""
    if status == 429:
        return ErrorCode.PLATFORM_RATE_LIMIT
    if status in (401, 403):
        return ErrorCode.CONNECTION_AUTH_EXPIRED  # e.g. a partner who revoked our access
    if status >= 500 or status == 408:
        return ErrorCode.PLATFORM_UNAVAILABLE
    return ErrorCode.PLATFORM_CONTENT_REJECTED  # any other 4xx: the platform refused it; a retry will not help


def counts_as_outage(err: IbxError) -> bool:
    """Only real outages feed the circuit breaker: 5xx/408, timeouts, connect errors and schema drift.
    A 4xx refusal of one post must never take the whole instance offline."""
    if err.code != ErrorCode.PLATFORM_UNAVAILABLE or "circuit" in err.details:
        return False
    status = err.details.get("platform_status")
    return status is None or status >= 500 or status == 408


def _error_tokens(body: Any) -> set[str]:
    """String values of the usual JSON error fields: error, error.code/type, error_type, code, errors[]."""
    tokens: set[str] = set()
    if not isinstance(body, dict):
        return tokens
    for key in ("error", "error_type", "code", "errors", "type"):
        value = body.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                item = item.get("code") or item.get("type") or item.get("error")
            if isinstance(item, str | int) and not isinstance(item, bool):
                tokens.add(str(item))
    return tokens


def _platform_message(body: Any, text: str) -> str:
    """A short human message from a platform error body (never the whole body)."""
    message: Any = None
    if isinstance(body, dict):
        for key in ("message", "error_description", "error", "errors", "error_type", "detail"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("type") or value.get("code")
            if isinstance(value, list):
                value = "; ".join(str(v.get("message", v)) if isinstance(v, dict) else str(v) for v in value[:3])
            if value:
                message = value
                break
    elif text:
        message = text
    return redact(str(message))[:300] if message else ""
