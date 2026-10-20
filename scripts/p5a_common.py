"""Helpers for the metrics evidence scripts (OUT2 metrics, OUT3 recertification) against a running compose stack."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
PERMISSIVE = {"frequency_cap": "20/P1D", "self_promo_allowed": "true", "link_policy": "allow", "disclosure_required": "false",
              "automation_allowed": "true", "language": "en", "category": "ai", "audience_tags": "ai;creators"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Stack:
    def __init__(self, api: str, token: str, compose_files: list[str], mock: str | None = None) -> None:
        self.api = api.rstrip("/")
        self.mock = mock.rstrip("/") if mock else None
        self.http = httpx.Client(base_url=self.api, timeout=120, headers={"Authorization": f"Bearer {token}"})
        self.compose = ["docker", "compose"] + [x for f in compose_files for x in ("-f", f)]

    def exec_api(self, script: Path, *args: str) -> dict[str, Any]:
        """Run a Python script inside the api container (stdin), return its one-line JSON output."""
        out = subprocess.run([*self.compose, "exec", "-T", "api", "python", "-", *args], input=script.read_bytes(),
                             capture_output=True, cwd=ROOT, check=True, env=os.environ.copy())
        return json.loads(out.stdout.decode().strip().splitlines()[-1])

    def exec_api_code(self, code: str) -> str:
        out = subprocess.run([*self.compose, "exec", "-T", "api", "python", "-c", code], capture_output=True, cwd=ROOT,
                             check=True, env=os.environ.copy())
        return out.stdout.decode()

    def ok(self, response: httpx.Response, *codes: int) -> dict[str, Any]:
        if response.status_code not in (codes or (200, 201, 202)):
            raise RuntimeError(f"{response.request.method} {response.request.url.path}: HTTP {response.status_code} {response.text[:500]}")
        return response.json()

    def add(self, **row: Any) -> str:
        data = self.ok(self.http.post("/api/v1/destinations", json=row))
        result = (data.get("results") or [data])[0]
        dest = result.get("destination") or result
        return dest["destination_id"]

    def job(self, job_id: str, timeout: float = 300) -> dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            job = self.ok(self.http.get(f"/api/v1/jobs/{job_id}"))
            if job["status"] in ("succeeded", "failed") or time.time() > deadline:
                return job
            time.sleep(1)

    def certify(self, destination_id: str, timeout: float = 300) -> dict[str, Any]:
        started = self.ok(self.http.post("/api/v1/certifications", json={"destination_id": destination_id}))
        self.job(started["job_id"], timeout)
        return self.ok(self.http.get(started["run_url"]))

    def run(self, run_id: str) -> dict[str, Any]:
        return self.ok(self.http.get(f"/api/v1/certifications/{run_id}"))

    def publish(self, destination_id: str, content: dict[str, Any], timeout: float = 300) -> dict[str, Any]:
        created = self.ok(self.http.post("/api/v1/publications", json={"destination_id": destination_id, "content": content}))
        pub_id = created["publication"]["publication_id"]
        deadline = time.time() + timeout
        while True:
            pub = self.ok(self.http.get(f"/api/v1/publications/{pub_id}"))
            if pub["state"] not in ("pending", "publishing") or time.time() > deadline:
                return pub
            time.sleep(1)

    def destination(self, destination_id: str) -> dict[str, Any]:
        return self.ok(self.http.get(f"/api/v1/destinations/{destination_id}"))
