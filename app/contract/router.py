"""HTTP surface of the connector contract (Appendix A, Must-have 1).

    POST /connector/v1/{key}/{method}         one route per method, typed request bodies (OpenAPI)
    POST /api/v1/connector/{key}/{method}     the same, mirrored under /api/v1 (IS point 2)
    GET  /connector/v1/{key}/manifest         the connector's manifest (Appendix A keys)

* Aliases ``status``, ``metrics`` and ``assist`` are accepted for get_status, fetch_metrics, build_assist_pack.
* Unknown connector → 404 CONNECTOR_NOT_FOUND; a method not in ``capabilities`` (or unknown) → 404
  METHOD_NOT_SUPPORTED. These checks run before the body is validated.
* Success bodies contain exactly the Appendix A keys. ``X-IncuBrix-Verbose: 1`` adds ``_meta``.
* publish: the body ``idempotency_key`` is authoritative; a different ``Idempotency-Key`` header → 409.
  With neither, a key is generated and echoed in the ``Idempotency-Key`` response header.

This module deliberately has no ``from __future__ import annotations``: FastAPI reads the request
models from the endpoint signatures built in ``_make_endpoint``.
"""

import contextvars
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.errors import ErrorEnvelope
from app.connectors.base import CALL_META, BaseConnector, ConnectorContext
from app.contract.manifest import METHOD_ALIASES, METHOD_CAPABILITY
from app.contract.models import (
    AssistPack,
    AssistPackRequest,
    DeleteResult,
    MetricsRequest,
    MetricsResult,
    PostRefRequest,
    PublishRequest,
    PublishResult,
    RefreshAuthRequest,
    RefreshAuthResult,
    StatusResult,
    ValidateRequest,
    ValidateResult,
)
from app.errors import ErrorCode, IbxError
from app.jobs.context import SHIPPED_LICENCE
from app.logging import get_logger

PREFIXES = ("/connector/v1", "/api/v1/connector")
VERBOSE_HEADER = "X-IncuBrix-Verbose"

# Fault string from the caller's X-Sim-Fault header, forwarded to mock-platforms only (sim tools on).
SIM_FAULT: contextvars.ContextVar[str | None] = contextvars.ContextVar("ibx_sim_fault", default=None)

METHODS: dict[str, tuple[type[BaseModel], type[BaseModel], str]] = {
    "validate": (ValidateRequest, ValidateResult, "Check a rendition against the destination's limits."),
    "publish": (PublishRequest, PublishResult, "Publish a rendition (idempotent per idempotency_key)."),
    "get_status": (PostRefRequest, StatusResult, "Status and live URL of a published post."),
    "delete": (PostRefRequest, DeleteResult, "Delete a published post."),
    "fetch_metrics": (MetricsRequest, MetricsResult, "Normalised metrics; null = not exposed by the platform."),
    "refresh_auth": (RefreshAuthRequest, RefreshAuthResult, "Refresh the connection's credentials."),
    "build_assist_pack": (AssistPackRequest, AssistPack, "Files, caption and checklist for a person to post."),
}
_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope} for status in (404, 409, 422, 424, 429, 503)
}

log = get_logger("app.contract")
router = APIRouter(tags=["connector contract"])


def get_connector(request: Request, key: str) -> BaseConnector:
    """The app's connector instance for ``key`` (built once, bound to the app's HTTP client and DB)."""
    app = request.app
    registry = app.state.registry
    if registry.manifest(key) is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, "Unknown connector key.", details={"known": registry.keys()})
    cache: dict[str, BaseConnector] = app.state.connectors
    if key not in cache:
        settings = app.state.settings
        ctx = ConnectorContext(
            manifest=registry.manifest(key),
            settings=settings,
            http=app.state.http,
            clock=app.state.clock,
            log=get_logger(f"connector.{key}", connector=key),
            db_session_factory=app.state.session_factory,
            simulation=settings.SIMULATION_MODE,
            storage=app.state.storage,
        )
        cache[key] = registry.build(key, ctx)
    return cache[key]


def _connector_dependency(method: str) -> Callable[..., Any]:
    async def dependency(request: Request, key: str) -> BaseConnector:
        connector = get_connector(request, key)
        if not connector.manifest.supports(method):
            raise IbxError(
                ErrorCode.METHOD_NOT_SUPPORTED,
                f"Connector '{key}' does not support '{method}' (capabilities: {', '.join(connector.manifest.capabilities) or 'none'}).",
                details={"connector_key": key, "method": method, "capabilities": list(connector.manifest.capabilities)},
            )
        return connector  # the kill switch is checked once, inside every BaseConnector method

    return dependency


async def _dispatch(connector: BaseConnector, method: str, body: Any, idempotency_key: str | None) -> BaseModel:
    if method == "validate":
        return await connector.validate(body.rendition, body.destination, body.connection)
    if method == "publish":
        return await connector.publish(body.rendition, body.destination, body.connection, idempotency_key or "")
    if method == "get_status":
        return await connector.get_status(body.platform_post_id, body.connection, body.destination)
    if method == "delete":
        return await connector.delete(body.platform_post_id, body.connection, body.destination)
    if method == "fetch_metrics":
        return await connector.fetch_metrics(body.platform_post_id, body.connection, body.since, body.destination)
    if method == "refresh_auth":
        return await connector.refresh_auth(body.connection, body.destination)
    return await connector.build_assist_pack(body.rendition, body.destination)


def _publish_key(body: PublishRequest, header_key: str | None) -> tuple[str, bool]:
    """Resolve the publish idempotency key. Returns (key, generated)."""
    if body.idempotency_key and header_key and body.idempotency_key != header_key:
        raise IbxError(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            "The Idempotency-Key header and the body idempotency_key differ; send one key.",
            details={"reason": "header_body_mismatch"},
        )
    key = body.idempotency_key or header_key
    if key:
        return key, False
    return f"gen-{uuid.uuid4().hex}", True


def _make_endpoint(method: str) -> Callable[..., Any]:
    request_model = METHODS[method][0]

    async def endpoint(
        request: Request,
        key: str,
        body: Annotated[request_model, Body()],  # type: ignore[valid-type]
        connector: Annotated[BaseConnector, Depends(_connector_dependency(method))],
        verbose: Annotated[str | None, Header(alias=VERBOSE_HEADER, include_in_schema=False)] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        sim_fault: Annotated[str | None, Header(alias="X-Sim-Fault", include_in_schema=False)] = None,
    ) -> JSONResponse:
        headers: dict[str, str] = {}
        effective_key = None
        meta: dict[str, Any] = {}
        if method == "publish":
            effective_key, generated = _publish_key(body, idempotency_key)
            headers["Idempotency-Key"] = effective_key
            if generated:
                headers["X-IncuBrix-Warning"] = "IDEMPOTENCY_KEY_GENERATED"
                meta["warnings"] = ["IDEMPOTENCY_KEY_GENERATED: no idempotency_key was sent; a retry will not be deduplicated"]
                log.warning("idempotency_key_generated", connector=key, method=method)
        meta_token = CALL_META.set(meta)
        fault_token = SIM_FAULT.set(sim_fault if sim_fault and request.app.state.settings.sim_tools_enabled else None)
        try:
            result = await _dispatch(connector, method, body, effective_key)
        finally:
            CALL_META.reset(meta_token)
            SIM_FAULT.reset(fault_token)
        payload = result.model_dump(mode="json")
        if meta.get("replayed") or meta.get("reconciled"):
            headers["Idempotent-Replayed"] = "true"
        if verbose == "1":
            payload["_meta"] = {
                "connector": key,
                "connector_version": connector.manifest.version,
                "method": method,
                "correlation_id": getattr(request.state, "correlation_id", None),
                "simulation": connector.ctx.simulation,
                "manifest": _call_manifest(connector, body),
                **meta,
            }
        return JSONResponse(payload, headers=headers)

    endpoint.__name__ = f"contract_{method}"
    return endpoint


def _call_manifest(connector: BaseConnector, body: Any) -> dict[str, Any]:
    """IS9 manifest for one synchronous call (sent with ``X-IncuBrix-Verbose: 1``): what was used, rights, cost."""
    rendition = getattr(body, "rendition", None)
    rights = None
    if rendition is not None:
        urls = [rendition.canonical_url, rendition.link_url, *[m.url for m in rendition.media]]
        rights = {"ai_generated": rendition.ai_generated, "source_urls": [u for u in urls if u],
                  "licence": (rendition.model_extra or {}).get("licence"), "media_produced_by_service": []}
    return {
        "connectors": {connector.key: connector.manifest.version},
        "providers": [],  # no AI provider is called by a contract method
        "settings": connector.ctx.settings.safe_snapshot(),
        "seed": connector.ctx.settings.SEED,
        "cost": {"total_usd": 0.0, "platform_api_usd": 0.0},
        "licence": SHIPPED_LICENCE,
        "rights": rights,
    }


async def manifest_endpoint(request: Request, key: str) -> dict[str, Any]:
    manifest = request.app.state.registry.manifest(key)
    if manifest is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, "Unknown connector key.")
    return manifest.public_dict()


async def unknown_method(request: Request, key: str, method: str) -> None:
    if request.app.state.registry.manifest(key) is None:
        raise IbxError(ErrorCode.CONNECTOR_NOT_FOUND, "Unknown connector key.")
    raise IbxError(
        ErrorCode.METHOD_NOT_SUPPORTED,
        f"Not a contract method. Methods: {', '.join(METHOD_CAPABILITY)}.",
        details={"method": method, "methods": list(METHOD_CAPABILITY)},
    )


for _prefix in PREFIXES:
    _mirror = _prefix != PREFIXES[0]
    for _method, (_req, _res, _summary) in METHODS.items():
        router.add_api_route(
            f"{_prefix}/{{key}}/{_method}",
            _make_endpoint(_method),
            methods=["POST"],
            response_model=_res,
            responses=_ERRORS,
            summary=_summary + (" (mirror)" if _mirror else ""),
            operation_id=f"{'api_' if _mirror else ''}contract_{_method}",
        )
    for _alias, _target in METHOD_ALIASES.items():
        router.add_api_route(
            f"{_prefix}/{{key}}/{_alias}", _make_endpoint(_target), methods=["POST"], include_in_schema=False
        )
    router.add_api_route(
        f"{_prefix}/{{key}}/manifest",
        manifest_endpoint,
        methods=["GET"],
        summary="The connector's manifest" + (" (mirror)" if _mirror else ""),
        operation_id=f"{'api_' if _mirror else ''}contract_manifest",
        responses={404: {"model": ErrorEnvelope}},
    )
    router.add_api_route(
        f"{_prefix}/{{key}}/{{method}}", unknown_method, methods=["POST"], include_in_schema=False
    )
