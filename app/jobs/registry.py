"""Job handler registry: ``@job_handler("kind")`` maps a job kind to an async function.

    @job_handler("certification.run")
    async def run(ctx: JobContext) -> dict:
        await ctx.progress(50, "published test post")
        return {"certified": True}

``public=True`` lets ``POST /api/v1/jobs`` enqueue the kind directly (only for safe demo/self-check kinds).
Handlers live in modules named ``handlers.py`` anywhere under ``app/``; ``load_handlers()`` imports them.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HandlerFn = Callable[[Any], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class HandlerSpec:
    kind: str
    fn: HandlerFn
    public: bool = False
    max_attempts: int | None = None


HANDLERS: dict[str, HandlerSpec] = {}


def job_handler(kind: str, *, public: bool = False, max_attempts: int | None = None) -> Callable[[HandlerFn], HandlerFn]:
    def decorator(fn: HandlerFn) -> HandlerFn:
        HANDLERS[kind] = HandlerSpec(kind, fn, public, max_attempts)
        return fn

    return decorator


def load_handlers() -> list[str]:
    """Import every ``app/**/handlers.py`` so their ``@job_handler`` decorators run. Returns module names."""
    root = Path(__file__).resolve().parent.parent  # app/
    loaded = []
    for path in sorted(root.rglob("handlers.py")):
        if "_template" in path.parts:
            continue
        module = "app." + ".".join(path.relative_to(root).with_suffix("").parts)
        importlib.import_module(module)
        loaded.append(module)
    return loaded
