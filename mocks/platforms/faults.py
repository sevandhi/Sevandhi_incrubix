"""Fault injection for mock platforms (hard cases on demand).

Fault strings:
    timeout_after_create[:secs]  run the handler (the post IS created), then hang before answering
    create_then_<status>         run the handler (the post IS created), then answer <status> anyway
                                 (e.g. create_then_502: a proxy error after the platform committed)
    429:<secs>                   429 with ``Retry-After: <secs>``
    <status>                     that status with a JSON error body, e.g. 401, 422, 500, 503
    <status>:<token>             that status with ``{"error": "<token>"}``, e.g. 400:rate_limit_error
    rename_field:a->b            run the handler, then rename key ``a`` to ``b`` in the JSON response

They arrive per request in the ``X-Sim-Fault`` header, or are queued with
``POST /_faults {platform?, tenant, method, fault, count}`` (count -1 = until cleared).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

DEFAULT_HANG_S = 30.0

# contract method words map to the HTTP method the platform sees
METHOD_ALIASES = {"publish": "POST", "create": "POST", "get_status": "GET", "status": "GET", "delete": "DELETE",
                  "fetch_metrics": "GET", "metrics": "GET", "any": "*"}

_FAULT_RE = re.compile(r"^(timeout_after_create(:\d+(\.\d+)?)?|create_then_[1-5]\d\d|[1-5]\d\d(:[A-Za-z0-9_.\-]+)?|rename_field:[A-Za-z0-9_.]+->[A-Za-z0-9_.]+)$")


@dataclass(frozen=True)
class Fault:
    kind: str  # hang | status | after_create | rename
    status: int = 0
    arg: str | None = None
    hang_s: float = DEFAULT_HANG_S
    rename_from: str | None = None
    rename_to: str | None = None


def parse_fault(spec: str) -> Fault:
    spec = spec.strip()
    if not _FAULT_RE.match(spec):
        raise ValueError(f"unknown fault '{spec}'")
    if spec.startswith("timeout_after_create"):
        _, _, secs = spec.partition(":")
        return Fault("hang", hang_s=float(secs) if secs else DEFAULT_HANG_S)
    if spec.startswith("create_then_"):
        return Fault("after_create", status=int(spec.removeprefix("create_then_")))
    if spec.startswith("rename_field:"):
        source, target = spec.removeprefix("rename_field:").split("->", 1)
        return Fault("rename", rename_from=source, rename_to=target)
    status, _, arg = spec.partition(":")
    return Fault("status", status=int(status), arg=arg or None)


def status_response_parts(fault: Fault) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Body and headers for a ``status`` fault."""
    headers: dict[str, str] = {}
    if fault.status == 429:
        headers["Retry-After"] = fault.arg or "30"
        return 429, {"error": "rate_limited", "message": "Too many requests"}, headers
    if fault.arg:
        return fault.status, {"error": fault.arg, "message": f"simulated {fault.arg}"}, headers
    messages = {401: "invalid or expired token", 403: "forbidden", 404: "not found",
                422: "content rejected by platform", 500: "internal server error", 503: "service unavailable"}
    return fault.status, {"error": f"http_{fault.status}", "message": messages.get(fault.status, "simulated error")}, headers


def rename_key(data: Any, source: str, target: str) -> Any:
    """Rename ``source`` to ``target`` everywhere in a JSON value (dotted paths rename the leaf)."""
    leaf = source.split(".")[-1]
    new_leaf = target.split(".")[-1]
    if isinstance(data, dict):
        return {(new_leaf if k == leaf else k): rename_key(v, source, target) for k, v in data.items()}
    if isinstance(data, list):
        return [rename_key(v, source, target) for v in data]
    return data


@dataclass
class _Queued:
    platform: str
    tenant: str
    method: str
    fault: str
    remaining: int  # -1 = forever
    path: str | None = None


class FaultRegistry:
    def __init__(self) -> None:
        self._items: list[_Queued] = []
        self._lock = threading.Lock()

    def add(self, *, tenant: str, method: str, fault: str, count: int = 1, platform: str = "*", path: str | None = None) -> None:
        parse_fault(fault)  # validate now, not at use time
        http_method = METHOD_ALIASES.get(method.lower(), method.upper())
        with self._lock:
            self._items.append(_Queued(platform or "*", tenant or "*", http_method, fault, count, path))

    def take(self, platform: str, tenant: str, method: str, path: str) -> str | None:
        """The next queued fault matching this request (and use one of its count)."""
        with self._lock:
            for item in self._items:
                if item.platform not in ("*", platform) or item.tenant not in ("*", tenant):
                    continue
                if item.method not in ("*", method.upper()):
                    continue
                if item.path and item.path not in path:
                    continue
                if item.remaining > 0:
                    item.remaining -= 1
                    if item.remaining == 0:
                        self._items.remove(item)
                return item.fault
        return None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [vars(item).copy() for item in self._items]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


faults = FaultRegistry()
