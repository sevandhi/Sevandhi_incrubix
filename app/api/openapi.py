"""Make the generated OpenAPI document say what the service really does (IS2).

FastAPI's defaults describe a 422 as ``HTTPValidationError`` (``{"detail": [...]}``) and know nothing about
our Bearer auth, rate limit or idempotency layer. ``install_openapi(app)`` post-processes the document once:

* every 4xx/5xx response uses our ``ErrorEnvelope`` (``{"error": {code, message, retryable, details}}``);
* the ``bearerAuth`` scheme, and on every protected route the security requirement plus 401, 429 and 500;
* every POST under /api/v1 (except the connector contract) documents ``Idempotency-Key``, the
  ``Idempotent-Replayed`` response header and the 409 IDEMPOTENCY_KEY_REUSED / IDEMPOTENCY_IN_PROGRESS.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

ENVELOPE = {"$ref": "#/components/schemas/ErrorEnvelope"}
STANDARD = {
    "401": "Missing or invalid bearer token (UNAUTHORIZED)",
    "429": "This service's rate limit was hit (API_RATE_LIMITED); see Retry-After",
    "500": "Unexpected error (INTERNAL_ERROR), with details.correlation_id",
}
IDEMPOTENCY_HEADER = {
    "name": "Idempotency-Key", "in": "header", "required": False,
    "description": "Same key + same body → the stored response (no second job). A missing key is generated and echoed.",
    "schema": {"type": "string", "maxLength": 255, "pattern": "^[A-Za-z0-9._:\\-]{1,255}$"},
}


def _error_response(description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": ENVELOPE}}}


def _is_protected(path: str) -> bool:
    return path.startswith(("/api/v1/", "/connector/v1/")) and not path.startswith("/api/v1/files/")


def _is_contract(path: str) -> bool:
    return path.startswith(("/connector/v1/", "/api/v1/connector/"))


def fix_spec(spec: dict[str, Any]) -> dict[str, Any]:
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    spec["components"]["securitySchemes"] = {"bearerAuth": {"type": "http", "scheme": "bearer"}}
    for path, item in spec.get("paths", {}).items():
        for method, op in item.items():
            responses = op.setdefault("responses", {})
            for status, response in responses.items():
                if str(status).startswith(("4", "5")):
                    response["content"] = {"application/json": {"schema": ENVELOPE}}
                    response.setdefault("description", "Error")
            if not _is_protected(path):
                continue
            op["security"] = [{"bearerAuth": []}]
            for status, description in STANDARD.items():
                responses.setdefault(status, _error_response(description))
            if method == "post" and not _is_contract(path):
                params = op.setdefault("parameters", [])
                if not any(p.get("name") == "Idempotency-Key" for p in params):
                    params.append(IDEMPOTENCY_HEADER)
                responses.setdefault("409", _error_response("IDEMPOTENCY_KEY_REUSED or IDEMPOTENCY_IN_PROGRESS"))
                for status, response in responses.items():
                    if str(status).startswith("2"):
                        response.setdefault("headers", {})["Idempotent-Replayed"] = {
                            "description": "true when this is the stored response of an earlier request",
                            "schema": {"type": "string"},
                        }
    return spec


def install_openapi(app: FastAPI) -> None:
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = fix_spec(generate())
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
