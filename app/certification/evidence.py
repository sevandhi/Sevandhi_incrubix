"""Certification evidence pack (plan §6): ``certifications/{destination_id}/{run_id}/`` in S3.

    run.json                    state timeline, durations, harness version, redacted requests/responses
    steps/NN_<STEP>.json        one file per step
    manifest.snapshot.yaml      the connector manifest used
    deletion_confirmation.json  how the test post was removed and how that was confirmed
    summary.md                  a short human summary
    live.png, after_delete.png  screenshots from the shooter (compose profile ``evidence``), when it runs
    screenshots.json            what each picture is (a real page, or a labelled API-response render) or why
                                there is none

Everything passes through the log redactor first, so a credential can not end up in evidence.
``scripts/export_cert.py`` copies a pack to ``evidence/certification/<connector>/<destination>/<run_id>/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml

from app.reliability.redact import redact

HARNESS_VERSION = "1.0.0"


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


@dataclass
class StepRecord:
    n: int
    step: str
    outcome: str  # ok | failed | skipped
    started_at: datetime
    duration_ms: float
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return redact({
            "n": self.n,
            "step": self.step,
            "outcome": self.outcome,
            "started_at": iso(self.started_at),
            "duration_ms": round(self.duration_ms, 1),
            "detail": self.detail,
        })


@dataclass
class EvidencePack:
    destination_id: str
    run_id: str
    storage: Any
    steps: list[StepRecord] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # name -> storage key

    @property
    def prefix(self) -> str:
        return f"certifications/{self.destination_id}/{self.run_id}/"

    def state(self, state: str, at: datetime, note: str | None = None) -> None:
        self.timeline.append({"state": state, "at": iso(at), **({"note": note} if note else {})})

    async def put(self, name: str, data: Any, content_type: str | None = None) -> str:
        if isinstance(data, dict | list):
            payload = json.dumps(redact(data), indent=2, default=str, ensure_ascii=False).encode()
            content_type = content_type or "application/json"
        else:
            payload = redact(str(data)).encode()
            content_type = content_type or "text/plain; charset=utf-8"
        key = self.prefix + name
        await self.storage.put_bytes(key, payload, content_type)
        self.files[name] = key
        return key

    async def put_binary(self, name: str, data: bytes, content_type: str) -> str:
        """Store bytes as they are (a screenshot PNG); text evidence always goes through ``put`` (redacted)."""
        key = self.prefix + name
        await self.storage.put_bytes(key, data, content_type)
        self.files[name] = key
        return key

    async def add_step(self, record: StepRecord) -> None:
        self.steps.append(record)
        await self.put(f"steps/{record.n:02d}_{record.step}.json", record.as_dict())

    async def manifest_snapshot(self, manifest: Any) -> None:
        await self.put("manifest.snapshot.yaml", yaml.safe_dump(manifest.public_dict(), sort_keys=False), "application/yaml")
