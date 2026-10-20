"""Auto-discovered /api/v1 routers.

Every module in this package (name not starting with ``_``) that exports ``router`` (an APIRouter)
is included by ``app.main`` as-is, so modules declare their full paths (``/api/v1/...``).
A module that fails to import is reported by ``GET /ready`` instead of taking the API down.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter


def discover_routers() -> tuple[list[tuple[str, APIRouter]], dict[str, str]]:
    """Returns ([(module name, router)], {module name: import error})."""
    routers: list[tuple[str, APIRouter]] = []
    errors: dict[str, str] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # noqa: BLE001 - reported via /ready
            errors[info.name] = f"{type(exc).__name__}: {exc}"
            continue
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append((info.name, router))
    return routers, errors
