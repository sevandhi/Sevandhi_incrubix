"""structlog JSON logging with redaction and correlation ids (IS8).

Every log line is one JSON object: ``timestamp``, ``level``, ``event``, plus ``correlation_id`` and
``job_id`` when bound in the context. The redaction processor runs last before rendering, so no
secret reaches stdout even if a caller logs a whole request. Standard-library loggers (uvicorn,
alembic) are routed through the same processors. httpx/httpcore header logging is switched off.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from typing import Any

import structlog

from app.reliability.redact import redact, secret_registry

CORRELATION_HEADER = "X-Correlation-Id"
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def redaction_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: mask secret keys, live secret values and token patterns everywhere."""
    if "exception" in event_dict and isinstance(event_dict["exception"], str):
        event_dict["exception"] = redact(event_dict["exception"])
    return redact(event_dict)


def _logger_key(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    if "logger_name" in event_dict:
        event_dict["logger"] = event_dict.pop("logger_name")
    return event_dict


def _shared_processors(stdlib: bool = False) -> list[Any]:
    processors: list[Any] = [structlog.contextvars.merge_contextvars, structlog.stdlib.add_log_level, _logger_key]
    if stdlib:  # records from logging.getLogger(...) carry their logger name
        processors.append(structlog.stdlib.add_logger_name)
    return processors + [
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.format_exc_info,
    ]


class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stdout`` is now (stays correct if stdout is swapped, e.g. by test capture)."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def configure_logging(level: str = "INFO", json_logs: bool = True, secrets: list[str] | None = None) -> None:
    """Configure structlog + stdlib logging once per process. Safe to call again (tests)."""
    if secrets:
        secret_registry.add_many(secrets, pinned=True)  # settings secrets are never evicted
    renderer: Any = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[*_shared_processors(), redaction_processor, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),  # resolves sys.stdout per logger, not once
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(stdlib=True),
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, redaction_processor, renderer],
    )
    handler = _StdoutHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    # uvicorn's access line includes the query string (tokens); CorrelationIdMiddleware logs requests instead.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # Header logging off: httpx/httpcore print request headers (tokens) at DEBUG.
    for name in ("httpx", "httpcore", "botocore", "boto3", "urllib3", "s3transfer", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None, **bound: Any) -> Any:
    """A lazy logger: safe at import time, it picks up ``configure_logging`` (JSON + redaction) when used."""
    if name:
        bound = {"logger_name": name, **bound}  # "logger" itself is reserved by structlog.get_logger
    return structlog.get_logger(**bound)


def clean_correlation_id(value: str | None) -> str:
    """Accept a caller's id only if it is short and plain; otherwise make a new one (no header injection)."""
    if value and _CORRELATION_RE.match(value):
        return value
    return uuid.uuid4().hex


_access_log = get_logger("app.access")


class CorrelationIdMiddleware:
    """Pure ASGI middleware: read or create ``X-Correlation-Id``, bind it for logs, echo it back."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for key, value in scope.get("headers", []):
            if key.decode("latin-1").lower() == CORRELATION_HEADER.lower():
                incoming = value.decode("latin-1")
                break
        correlation_id = clean_correlation_id(incoming)
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        status = {"code": 500}
        started = time.perf_counter()

        async def send_with_header(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((CORRELATION_HEADER.encode(), correlation_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            # Access log without the query string (it may carry tokens); uvicorn's own access log is off.
            if scope.get("path") not in ("/health", "/ready"):
                _access_log.info(
                    "http_request",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status=status["code"],
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            structlog.contextvars.clear_contextvars()
