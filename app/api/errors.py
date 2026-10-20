"""Exception handlers: every error leaves as ``{"error": {"code", "message", "retryable", "details"}}`` (IS5).

* ``IbxError`` renders with its catalogue status; rate-limit and cap errors also get ``Retry-After``.
* Request validation errors become ``REQUEST_VALIDATION_FAILED`` and list only ``loc``/``type``/``msg``:
  the submitted values are never echoed, because they may hold tokens (red-team #6).
* Unknown routes (404), wrong methods (405) and unhandled exceptions (500) use the same envelope.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.clock import get_clock
from app.errors import ErrorCode, IbxError
from app.logging import get_logger
from app.reliability.redact import redact

log = get_logger("app.errors")

_STATUS_TO_CODE = {
    400: ErrorCode.REQUEST_VALIDATION_FAILED,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.HTTP_METHOD_NOT_ALLOWED,
    413: ErrorCode.REQUEST_VALIDATION_FAILED,
    415: ErrorCode.REQUEST_VALIDATION_FAILED,
    422: ErrorCode.REQUEST_VALIDATION_FAILED,
    429: ErrorCode.API_RATE_LIMITED,
}


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool
    details: dict[str, Any]


class ErrorEnvelope(BaseModel):
    """The error shape of every endpoint (documented in OpenAPI)."""

    error: ErrorBody


def retry_after_seconds(details: dict[str, Any]) -> int | None:
    """Seconds until ``details.retry_at`` (or ``details.retry_after_s``), for the Retry-After header."""
    if details.get("retry_after_s") is not None:
        try:
            return max(0, math.ceil(float(details["retry_after_s"])))
        except (TypeError, ValueError):
            return None
    retry_at = details.get("retry_at")
    if isinstance(retry_at, str):
        try:
            retry_at = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(retry_at, datetime):
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0, math.ceil((retry_at - get_clock().now()).total_seconds()))
    return None


def error_response(exc: IbxError, extra_headers: dict[str, str] | None = None) -> JSONResponse:
    body = redact(exc.to_body())
    headers = dict(extra_headers or {})
    if exc.http_status in (409, 429, 503):
        seconds = retry_after_seconds(exc.details)
        if seconds is not None:
            headers["Retry-After"] = str(seconds)
    if exc.code == ErrorCode.UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(body, status_code=exc.http_status, headers=headers)


def validation_details(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep where and why, drop what (``input``/``ctx`` can contain secrets)."""
    out = []
    for err in errors[:50]:
        out.append(
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "type": err.get("type", "value_error"),
                "msg": redact(str(err.get("msg", "invalid value"))),
            }
        )
    return out


def validation_error(errors: list[dict[str, Any]], message: str = "The request is invalid.") -> IbxError:
    return IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, message, details={"errors": validation_details(errors)})


async def _ibx_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, IbxError)
    return error_response(exc)


async def _validation_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(validation_error(list(exc.errors())))


async def _http_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    message = {
        404: "No route matches this path.",
        405: "This HTTP method is not allowed on this path.",
    }.get(exc.status_code)
    if message is None and isinstance(exc.detail, str):
        message = redact(exc.detail)
    err = IbxError(code, message, http_status=exc.status_code)
    return error_response(err, dict(exc.headers or {}))


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    log.error("unhandled_exception", error=redact(exc), path=request.url.path, exc_info=False)
    if isinstance(exc, SQLAlchemyTimeoutError):  # the DB connection pool is exhausted: busy, not broken
        err = IbxError(ErrorCode.DEPENDENCY_UNAVAILABLE, "The database is busy; retry shortly.",
                       details={"correlation_id": correlation_id, "retry_after_s": 2})
    else:
        err = IbxError(ErrorCode.INTERNAL_ERROR, "Unexpected error.", details={"correlation_id": correlation_id})
    response = error_response(err)
    if correlation_id:
        # ServerErrorMiddleware sits outside our correlation middleware, so add the header here.
        response.headers["X-Correlation-Id"] = correlation_id
    return response


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(IbxError, _ibx_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
