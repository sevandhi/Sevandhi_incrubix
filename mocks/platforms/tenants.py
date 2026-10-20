"""In-memory, auto-provisioned tenant state for mock platforms.

A tenant is the second path segment: ``/discourse/forum-41/...`` is platform ``discourse``, tenant
``forum-41``. The first request to a new tenant creates its state, so "add a forum" needs no setup
(Must-have 3: one connector, many destinations).
"""

from __future__ import annotations

import threading
from typing import Any


class TenantStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def state(self, platform: str, tenant: str) -> dict[str, Any]:
        """The mutable state dict for one tenant (created on first use)."""
        with self._lock:
            key = (platform, tenant)
            if key not in self._data:
                self._data[key] = {"posts": {}, "next_id": 1, "revoked_tokens": set(), "extra": {}}
            return self._data[key]

    def next_id(self, platform: str, tenant: str) -> int:
        st = self.state(platform, tenant)
        with self._lock:
            value = st["next_id"]
            st["next_id"] = value + 1
            return value

    def tenants(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(self._data)

    def reset(self) -> None:
        with self._lock:
            self._data.clear()


store = TenantStore()
