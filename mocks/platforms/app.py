"""mock-platforms: a FastAPI app that hosts every mock platform.

* Each ``mocks/platforms/routes/<name>.py`` that exports ``router`` is mounted at ``/<name>``
  (files starting with ``_`` are skipped). Routes take ``/{tenant}/...``, so tenants are path prefixes.
* Every request to ``/<platform>/<tenant>/...`` is counted (``GET /_counts``) so tests can prove that a
  retry did not post twice.
* Faults come from the ``X-Sim-Fault`` header or the ``/_faults`` queue (see ``faults.py``).

Run: ``uv run uvicorn mocks.platforms.app:app --port 8080``
"""

from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
import re
import threading
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mocks.platforms import routes as routes_pkg
from mocks.platforms.faults import Fault, faults, parse_fault, rename_key, status_response_parts
from mocks.platforms.tenants import store

CONTROL_PREFIXES = ("_", "health", "docs", "openapi.json", "redoc")
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8,}|ibx-[a-z0-9]+)$")


class Counter:
    """Requests per platform / tenant / ``METHOD /path-template``."""

    def __init__(self) -> None:
        self._counts: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        self._lock = threading.Lock()

    def hit(self, platform: str, tenant: str, method: str, subpath: str) -> None:
        template = "/" + "/".join("{id}" if _ID_SEGMENT.match(seg) else seg for seg in subpath.split("/") if seg)
        with self._lock:
            self._counts[platform][tenant][f"{method} {template}"] += 1

    def snapshot(self, platform: str | None = None, tenant: str | None = None) -> dict[str, Any]:
        with self._lock:
            data = {p: {t: dict(c) for t, c in ts.items()} for p, ts in self._counts.items()}
        if platform:
            data = {platform: data.get(platform, {})}
        if tenant:
            data = {p: {tenant: ts.get(tenant, {})} for p, ts in data.items()}
        return data

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


counter = Counter()


class FaultIn(BaseModel):
    tenant: str = "*"
    method: str = "*"
    fault: str
    count: int = Field(default=1, ge=-1)
    platform: str = "*"
    path: str | None = None


def _split(path: str) -> tuple[str, str, str] | None:
    parts = path.lstrip("/").split("/", 2)
    if len(parts) < 2 or parts[0].startswith(CONTROL_PREFIXES) or parts[0] == "":
        return None
    return parts[0], parts[1], "/" + (parts[2] if len(parts) > 2 else "")


def create_app() -> FastAPI:
    app = FastAPI(title="IncuBrix mock platforms", version="1.0.0")
    mounted: list[str] = []
    for info in pkgutil.iter_modules(routes_pkg.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{routes_pkg.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router, prefix=f"/{info.name}")
            mounted.append(info.name)
    app.state.mounted = mounted

    @app.middleware("http")
    async def platform_middleware(request: Request, call_next: Any) -> Response:
        split = _split(request.url.path)
        if split is None:
            return await call_next(request)
        platform, tenant, subpath = split
        counter.hit(platform, tenant, request.method, subpath)
        spec = request.headers.get("X-Sim-Fault") or faults.take(platform, tenant, request.method, subpath)
        if not spec:
            return await call_next(request)
        try:
            fault = parse_fault(spec)
        except ValueError as exc:
            return JSONResponse({"error": "bad_fault", "message": str(exc)}, status_code=400)
        if fault.kind == "status":
            status, body, headers = status_response_parts(fault)
            return JSONResponse(body, status_code=status, headers=headers)
        response = await call_next(request)
        if fault.kind == "hang":
            await asyncio.sleep(fault.hang_s)  # the post exists; the client times out first
            return response
        if fault.kind == "after_create":  # the post exists, but the answer is an error (e.g. a 502 from a proxy)
            status, body, headers = status_response_parts(Fault("status", status=fault.status))
            return JSONResponse(body, status_code=status, headers=headers)
        raw = b"".join([chunk async for chunk in response.body_iterator])
        try:
            data = rename_key(json.loads(raw), fault.rename_from or "", fault.rename_to or "")
        except ValueError:
            return Response(raw, status_code=response.status_code, media_type=response.media_type)
        kept = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type")}
        return JSONResponse(data, status_code=response.status_code, headers=kept)  # keep e.g. Location, Retry-After

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "platforms": mounted}

    @app.post("/_faults", status_code=201)
    async def add_fault(fault: FaultIn) -> dict[str, Any]:
        try:
            faults.add(tenant=fault.tenant, method=fault.method, fault=fault.fault, count=fault.count,
                       platform=fault.platform, path=fault.path)
        except ValueError as exc:
            return JSONResponse({"error": "bad_fault", "message": str(exc)}, status_code=400)  # type: ignore[return-value]
        return {"queued": faults.list()}

    @app.get("/_faults")
    async def list_faults() -> dict[str, Any]:
        return {"queued": faults.list()}

    @app.delete("/_faults")
    async def clear_faults() -> dict[str, Any]:
        faults.clear()
        return {"queued": []}

    @app.get("/_counts")
    async def counts(platform: str | None = None, tenant: str | None = None) -> dict[str, Any]:
        return {"counts": counter.snapshot(platform, tenant)}

    @app.post("/_reset")
    async def reset() -> dict[str, Any]:
        faults.clear()
        counter.reset()
        store.reset()
        return {"reset": True}

    return app


app = create_app()
