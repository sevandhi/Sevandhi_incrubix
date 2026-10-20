"""What a job handler receives (``JobContext``), the IS9 run manifest, and the public job view.

A handler reports progress, stores output files in S3 and adds warnings; the worker turns that into
``result.links`` (presigned on read) and a ``manifest.json`` written next to the outputs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import Clock
from app.config import Settings
from app.db.tables import JobRow
from app.jobs import queue

SHIPPED_LICENCE = {"service": "MIT", "note": "third-party servers run as unmodified separate containers; see SOURCES.md"}


@dataclass
class JobContext:
    job_id: str
    kind: str
    payload: dict[str, Any]
    attempt: int
    settings: Settings
    clock: Clock
    log: Any
    session_factory: async_sessionmaker[AsyncSession]
    storage: Any
    links: dict[str, str] = field(default_factory=dict)  # name -> storage key (presigned when read)
    warnings: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    manifest_extra: dict[str, Any] = field(default_factory=dict)  # providers, rights, ai_generated, disclosure ...
    used_connectors: dict[str, str] = field(default_factory=dict)  # {key: version}, filled by BaseConnector calls
    llm_tokens: int = 0

    async def progress(self, percent: int, note: str | None = None) -> None:
        async with self.session_factory() as session, session.begin():
            await queue.set_progress(session, self.job_id, percent)
        self.log.info("job_progress", progress=percent, note=note)

    async def put_artifact(self, name: str, data: bytes | str | dict[str, Any] | list[Any], content_type: str | None = None) -> str:
        """Store an output file under ``jobs/<id>/<name>`` and list it in ``result.links``."""
        if isinstance(data, dict | list):
            data, content_type = json.dumps(data, indent=2, default=str).encode(), content_type or "application/json"
        elif isinstance(data, str):
            data, content_type = data.encode(), content_type or "text/plain; charset=utf-8"
        key = f"jobs/{self.job_id}/{name}"
        await self.storage.put_bytes(key, data, content_type or "application/octet-stream")
        self.links[name.rsplit(".", 1)[0]] = key
        return key

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time a stage for the manifest: ``with ctx.stage("publish"): ...``."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.timings_ms[name] = round((time.perf_counter() - start) * 1000, 1)


def build_manifest(ctx: JobContext, *, status: str, started_at: datetime, finished_at: datetime, connectors: dict[str, str]) -> dict[str, Any]:
    """The IS9 run manifest. Only what this job really used is listed: the connectors it called
    (``connectors`` gives their versions), the AI providers a handler recorded, and the content's rights.
    Settings come from an allow-list snapshot, never ``model_dump()``."""
    settings = ctx.settings
    price = settings.LLM_PRICE_PER_1K_TOKENS_USD
    llm_usd = round(ctx.llm_tokens / 1000 * price, 6)
    return {
        "job_id": ctx.job_id,
        "kind": ctx.kind,
        "status": status,
        "attempt": ctx.attempt,
        "service": {"name": "ibx-destination-network", "version": settings.SERVICE_VERSION, "git_sha": settings.GIT_SHA},
        "connectors": {key: connectors.get(key, version) for key, version in sorted(ctx.used_connectors.items())},
        "providers": ctx.manifest_extra.get("providers", []),  # e.g. [{"name": "llm", "provider", "model", "version"}]
        "settings": settings.safe_snapshot(),
        "seed": settings.SEED,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "timings_ms": ctx.timings_ms,
        "cost": {
            "llm_tokens": ctx.llm_tokens,
            "llm_price_per_1k_tokens_usd": price,
            "llm_usd": llm_usd,
            "platform_api_usd": 0.0,
            "total_usd": llm_usd,
        },
        "warnings": ctx.warnings,
        "licence": SHIPPED_LICENCE,
        "rights": ctx.manifest_extra.get("rights", {"content_owner": None, "licence": None, "source_urls": [],
                                                    "ai_generated": None}),
        "ai_generated": bool(ctx.manifest_extra.get("ai_generated", False)),
        "disclosure": ctx.manifest_extra.get("disclosure"),
        "simulation": settings.SIMULATION_MODE,
    }


async def public_job(job: JobRow, storage: Any, settings: Settings | None = None) -> dict[str, Any]:
    """``GET /api/v1/jobs/{id}`` shape: {job_id, kind, status, progress, result:{links,...}, error, ...}.

    With ``settings``, links are signed API download links (``/api/v1/files/...``, IS7) that work through
    the one published API port; without, they fall back to S3 presigned URLs.
    """
    result = dict(job.result) if job.result else None
    if result is not None and storage is not None:
        from app.storage.links import signed_download_url

        links = {}
        for name, key in (result.get("links") or {}).items():
            if str(key).startswith(("http://", "https://")):
                links[name] = key
            elif settings is not None:
                links[name] = signed_download_url(str(key), settings)
            else:
                links[name] = await storage.presign(key)
        result["links"] = links
    return {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "result": result,
        "error": job.error,
        "attempts": job.attempts,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
