"""Error codes and the error envelope shared by every endpoint and connector.

The first eight codes are the IncuBrix shared connector codes (Appendix A); the rest
are service-level codes in the same UPPER_SNAKE_CASE style. The README error table is
generated from ``CATALOGUE`` so the docs can never drift from the code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # --- IncuBrix shared connector codes (Appendix A) ---
    DEST_SPEC_VIOLATION = "DEST_SPEC_VIOLATION"
    DEST_RULE_FREQUENCY_CAP = "DEST_RULE_FREQUENCY_CAP"
    CONNECTION_AUTH_EXPIRED = "CONNECTION_AUTH_EXPIRED"
    PLATFORM_RATE_LIMIT = "PLATFORM_RATE_LIMIT"
    PLATFORM_QUOTA_EXHAUSTED = "PLATFORM_QUOTA_EXHAUSTED"
    PLATFORM_CONTENT_REJECTED = "PLATFORM_CONTENT_REJECTED"
    PLATFORM_UNAVAILABLE = "PLATFORM_UNAVAILABLE"
    CONNECTOR_DISABLED = "CONNECTOR_DISABLED"
    # --- Posting-rule codes (same style, documented in README) ---
    DEST_RULE_SELF_PROMO = "DEST_RULE_SELF_PROMO"
    DEST_RULE_LINK_POLICY = "DEST_RULE_LINK_POLICY"
    DEST_RULE_DISCLOSURE = "DEST_RULE_DISCLOSURE"
    DEST_RULE_LANGUAGE = "DEST_RULE_LANGUAGE"
    DEST_RULE_AUTOMATION_NOT_ALLOWED = "DEST_RULE_AUTOMATION_NOT_ALLOWED"
    DEST_RULE_BLOCKED_TERM = "DEST_RULE_BLOCKED_TERM"
    DEST_RULES_UNCONFIRMED = "DEST_RULES_UNCONFIRMED"
    DESTINATION_EXCLUDED = "DESTINATION_EXCLUDED"
    # --- Service-level codes ---
    METHOD_NOT_SUPPORTED = "METHOD_NOT_SUPPORTED"
    CONNECTOR_NOT_FOUND = "CONNECTOR_NOT_FOUND"
    DESTINATION_NOT_FOUND = "DESTINATION_NOT_FOUND"
    CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"
    NOT_FOUND = "NOT_FOUND"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    REQUEST_VALIDATION_FAILED = "REQUEST_VALIDATION_FAILED"
    INPUT_FETCH_FAILED = "INPUT_FETCH_FAILED"
    UNAUTHORIZED = "UNAUTHORIZED"
    API_RATE_LIMITED = "API_RATE_LIMITED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    SIMULATION_ONLY = "SIMULATION_ONLY"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    HTTP_METHOD_NOT_ALLOWED = "HTTP_METHOD_NOT_ALLOWED"  # 405 on any route
    DESTINATION_NOT_CERTIFIED = "DESTINATION_NOT_CERTIFIED"


@dataclass(frozen=True)
class CodeInfo:
    http_status: int
    retryable: bool
    retry_policy: str  # never | later | after_reconnect | backoff | next_window
    meaning: str


CATALOGUE: dict[ErrorCode, CodeInfo] = {
    ErrorCode.DEST_SPEC_VIOLATION: CodeInfo(422, False, "never", "Content breaks the destination's format limits; field-level reasons in details.violations."),
    ErrorCode.DEST_RULE_FREQUENCY_CAP: CodeInfo(409, True, "later", "Frequency cap reached; reschedule at details.retry_at."),
    ErrorCode.CONNECTION_AUTH_EXPIRED: CodeInfo(424, False, "after_reconnect", "Token expired or revoked; the connection is paused until the owner reconnects."),
    ErrorCode.PLATFORM_RATE_LIMIT: CodeInfo(429, True, "backoff", "Platform rate limit; back off until details.retry_at (from the platform's reset time)."),
    ErrorCode.PLATFORM_QUOTA_EXHAUSTED: CodeInfo(429, True, "next_window", "Daily quota used up; reschedule to the next window."),
    ErrorCode.PLATFORM_CONTENT_REJECTED: CodeInfo(422, False, "never", "Platform refused the content; its message is in details.platform_message."),
    ErrorCode.PLATFORM_UNAVAILABLE: CodeInfo(503, True, "backoff", "Platform error or timeout; retry. Calls stop after 5 consecutive failures (circuit open)."),
    ErrorCode.CONNECTOR_DISABLED: CodeInfo(503, False, "never", "Kill switch is on for this connector."),
    ErrorCode.DEST_RULE_SELF_PROMO: CodeInfo(409, False, "never", "Destination does not allow self-promotion; details name the signal that fired."),
    ErrorCode.DEST_RULE_LINK_POLICY: CodeInfo(409, False, "never", "Links break the destination's link policy."),
    ErrorCode.DEST_RULE_DISCLOSURE: CodeInfo(409, False, "never", "AI-content disclosure is required but could not be placed."),
    ErrorCode.DEST_RULE_LANGUAGE: CodeInfo(409, False, "never", "Content language does not match the destination's language policy."),
    ErrorCode.DEST_RULE_AUTOMATION_NOT_ALLOWED: CodeInfo(409, False, "never", "Destination forbids automated posting; use the assisted connector."),
    ErrorCode.DEST_RULE_BLOCKED_TERM: CodeInfo(409, False, "never", "Content contains a term the destination blocks."),
    ErrorCode.DEST_RULES_UNCONFIRMED: CodeInfo(409, False, "never", "Destination rules are not confirmed by a person yet; only certification posts to own instances are allowed."),
    ErrorCode.DESTINATION_EXCLUDED: CodeInfo(409, False, "never", "Destination failed certification and is excluded until it passes again."),
    ErrorCode.METHOD_NOT_SUPPORTED: CodeInfo(404, False, "never", "The connector does not list this method in its manifest capabilities."),
    ErrorCode.CONNECTOR_NOT_FOUND: CodeInfo(404, False, "never", "Unknown connector key."),
    ErrorCode.DESTINATION_NOT_FOUND: CodeInfo(404, False, "never", "Unknown destination."),
    ErrorCode.CONNECTION_NOT_FOUND: CodeInfo(404, False, "never", "Unknown connection."),
    ErrorCode.NOT_FOUND: CodeInfo(404, False, "never", "Resource not found."),
    ErrorCode.JOB_NOT_FOUND: CodeInfo(404, False, "never", "Unknown job id."),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: CodeInfo(409, False, "never", "The Idempotency-Key was already used with a different request body."),
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: CodeInfo(409, True, "backoff", "A request with this Idempotency-Key is still being processed."),
    ErrorCode.REQUEST_VALIDATION_FAILED: CodeInfo(422, False, "never", "The request body or parameters are invalid; see details.errors (input values are never echoed)."),
    ErrorCode.INPUT_FETCH_FAILED: CodeInfo(422, True, "backoff", "An input URL could not be downloaded."),
    ErrorCode.UNAUTHORIZED: CodeInfo(401, False, "never", "Missing or invalid API token."),
    ErrorCode.API_RATE_LIMITED: CodeInfo(429, True, "backoff", "This service's own rate limit was hit."),
    ErrorCode.STORAGE_UNAVAILABLE: CodeInfo(503, True, "backoff", "Object storage is unreachable."),
    ErrorCode.DEPENDENCY_UNAVAILABLE: CodeInfo(503, True, "backoff", "A required dependency (database, storage, queue) is unavailable."),
    ErrorCode.SIMULATION_ONLY: CodeInfo(403, False, "never", "This endpoint only works in simulation/demo mode."),
    ErrorCode.INTERNAL_ERROR: CodeInfo(500, False, "never", "Unexpected error (a bug, not a passing fault); the correlation id is in the logs."),
    ErrorCode.HTTP_METHOD_NOT_ALLOWED: CodeInfo(405, False, "never", "This HTTP method is not allowed on this path."),
    ErrorCode.DESTINATION_NOT_CERTIFIED: CodeInfo(409, False, "never", "Destination is not certified yet (level L0); certify it first (POST /api/v1/certifications). Certification test posts are the only posts allowed before that."),
}


class IbxError(Exception):
    """An error that renders as {"error": {"code", "message", "retryable", "details"}}."""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
    ) -> None:
        info = CATALOGUE[code]
        self.code = code
        self.message = message or info.meaning
        self.retryable = info.retryable if retryable is None else retryable
        self.http_status = http_status or info.http_status
        self.details = {"retry_policy": info.retry_policy, **(details or {})}
        super().__init__(f"{code.value}: {self.message}")

    def to_body(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            }
        }


def error_table_markdown() -> str:
    """Markdown table of every code, used to generate the README section."""
    rows = ["| Code | HTTP | Retryable | Retry policy | Meaning |", "|---|---|---|---|---|"]
    for code, info in CATALOGUE.items():
        rows.append(
            f"| `{code.value}` | {info.http_status} | {'yes' if info.retryable else 'no'} | {info.retry_policy} | {info.meaning} |"
        )
    return "\n".join(rows)
