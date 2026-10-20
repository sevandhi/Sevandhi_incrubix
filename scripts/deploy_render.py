"""Scripted live-demo deploy: Supabase (Postgres + Storage S3) + one Render free web service, through their APIs.

    uv run python scripts/deploy_render.py plan            # dry run: every planned API call, secrets masked, NO network
    uv run python scripts/deploy_render.py supabase        # org → project (create or reuse) → healthy → session-pooler URL
    uv run python scripts/deploy_render.py check-storage   # Supabase S3 keys work: bucket head/create + put/get/delete
    uv run python scripts/deploy_render.py check-db        # session pooler: SSL, session advisory lock, LISTEN/NOTIFY
    uv run python scripts/deploy_render.py render          # Render web service (create or update env) + deploy
    uv run python scripts/deploy_render.py wait            # poll https://<service>/ready, report the cold start
    uv run python scripts/deploy_render.py all             # supabase → check-db → check-storage → render → wait

Secrets are read ONLY from the environment or the git-ignored ``.env`` and are never printed (values are shown as
``<secret:NAME>``). Inputs: ``RENDER_API_KEY``, ``SUPABASE_ACCESS_TOKEN``, ``SUPABASE_DB_PASSWORD``,
``SUPABASE_S3_ACCESS_KEY`` + ``SUPABASE_S3_SECRET_KEY`` (Supabase has no public API that creates S3 keys: create one
pair in the dashboard, see deploy/render/DEPLOY.md), optional ``GEMINI_API_KEY``, ``SUPABASE_ORG_SLUG`` (needed only
with several organisations), ``RENDER_OWNER_ID`` (only with several workspaces), ``DEMO_GITHUB_REPO``
(default https://github.com/sevandhi/Sevandhi_incrubix), ``DEMO_BRANCH`` (default: the repo's default branch).
Outputs written back to ``.env`` (mode 600): ``SUPABASE_PROJECT_REF``, ``SUPABASE_REGION``, ``DEMO_DATABASE_URL``,
``DEMO_API_TOKEN`` (the reviewers' bearer token, generated here once), ``RENDER_SERVICE_ID``, ``DEMO_URL``.
Re-running is safe: an existing project / service with the same name is reused, never duplicated.

Service shape (name, region, plan, Dockerfile, health check, plain env values) comes from ``render.yaml``, so the
Blueprint path (dashboard) and this API path deploy the same thing. API shapes checked against the official OpenAPI
documents (api.supabase.com/api/v1-json, api-docs.render.com, fetched 2026-10-20).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
RENDER_API = "https://api.render.com/v1"
SUPABASE_API = "https://api.supabase.com/v1"
PROJECT_NAME = "ibx-destination-network-demo"
DEFAULT_REPO = "https://github.com/sevandhi/Sevandhi_incrubix"
# Render region → the nearest Supabase region code (keep the app and its database together).
SUPABASE_REGION_FOR = {"singapore": "ap-southeast-1", "frankfurt": "eu-central-1", "oregon": "us-west-1",
                       "ohio": "us-east-2", "virginia": "us-east-1"}
SECRET_NAMES = ("RENDER_API_KEY", "SUPABASE_ACCESS_TOKEN", "SUPABASE_DB_PASSWORD", "SUPABASE_S3_ACCESS_KEY",
                "SUPABASE_S3_SECRET_KEY", "GEMINI_API_KEY", "DEMO_API_TOKEN", "DEMO_DATABASE_URL")


class DeployError(RuntimeError):
    pass


# ------------------------------------------------------------------ .env (read + write back, never printed)


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env_values(updates: dict[str, str], path: Path = ENV_FILE) -> None:
    """Set KEY=VALUE lines in .env (replace or append), keeping everything else, file mode 600."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: set[str] = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    missing = [k for k in updates if k not in done]
    if missing:
        lines += ["", "# --- written by scripts/deploy_render.py (live demo; never commit) ---"]
        lines += [f"{k}={updates[k]}" for k in missing]
    old_umask = os.umask(0o077)
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    finally:
        os.umask(old_umask)
    path.chmod(0o600)


class Config:
    """Environment first, then .env. ``secret()`` values are only ever handed to HTTP clients, never printed."""

    def __init__(self, env_file: Path = ENV_FILE, environ: dict[str, str] | None = None) -> None:
        self.env_file = env_file
        self.file = read_env_file(env_file)
        self.environ = dict(os.environ if environ is None else environ)

    def get(self, name: str, default: str | None = None) -> str | None:
        value = self.environ.get(name) or self.file.get(name)
        return value if value else default

    def secret(self, name: str, *, required: bool = True) -> str | None:
        value = self.get(name)
        if required and not value:
            raise DeployError(f"{name} is not set (put it in the git-ignored .env; it is never printed).")
        return value

    def save(self, updates: dict[str, str]) -> None:
        write_env_values(updates, self.env_file)
        self.file.update(updates)


def mask(value: Any, secrets_by_value: dict[str, str]) -> Any:
    """Replace every known secret value (also inside strings) with ``<secret:NAME>``."""
    if isinstance(value, str):
        for raw, name in sorted(secrets_by_value.items(), key=lambda kv: -len(kv[0])):
            if raw and raw in value:
                value = value.replace(raw, f"<secret:{name}>")
        return value
    if isinstance(value, dict):
        return {k: mask(v, secrets_by_value) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v, secrets_by_value) for v in value]
    return value


# ------------------------------------------------------------------ the service shape (from render.yaml)


def blueprint(path: Path = ROOT / "render.yaml") -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    service = doc["services"][0]
    plain = {e["key"]: str(e["value"]) for e in service.get("envVars", []) if "value" in e}
    return {"name": service["name"], "region": service.get("region", "oregon"), "plan": service.get("plan", "free"),
            "dockerfilePath": service.get("dockerfilePath", "./Dockerfile"), "dockerContext": service.get("dockerContext", "."),
            "healthCheckPath": service.get("healthCheckPath", "/health"),
            "autoDeployTrigger": service.get("autoDeployTrigger", "off"), "plain_env": plain,
            "generated": [e["key"] for e in service.get("envVars", []) if e.get("generateValue")],
            "typed": [e["key"] for e in service.get("envVars", []) if e.get("sync") is False]}


def session_pooler_url(pooler: list[dict[str, Any]], password: str) -> str:
    """The Supavisor SESSION-mode URL (port 5432, ``sslmode=require``) from GET /v1/projects/{ref}/config/database/pooler.
    Session mode keeps one server connection per client: LISTEN/NOTIFY and session advisory locks work."""
    primary = [p for p in pooler if p.get("database_type", "PRIMARY") == "PRIMARY"] or pooler
    if not primary:
        raise DeployError("the project has no pooler configuration yet (try again in a minute)")
    p = primary[0]
    return (f"postgresql://{quote(p['db_user'], safe='')}:{quote(password, safe='')}@{p['db_host']}:5432/"
            f"{p.get('db_name') or 'postgres'}?sslmode=require")


def s3_endpoint(ref: str) -> str:
    return f"https://{ref}.storage.supabase.co/storage/v1/s3"


def render_env(bp: dict[str, Any], *, api_token: str, database_url: str, ref: str, region: str, s3_key: str,
               s3_secret: str, gemini: str | None) -> list[dict[str, Any]]:
    env: dict[str, str] = dict(bp["plain_env"])
    env.update({"API_TOKEN": api_token, "DATABASE_URL": database_url, "S3_ENDPOINT": s3_endpoint(ref),
                "S3_PUBLIC_ENDPOINT": s3_endpoint(ref), "S3_REGION": region, "S3_ACCESS_KEY": s3_key,
                "S3_SECRET_KEY": s3_secret})
    if gemini:
        env["GEMINI_API_KEY"] = gemini
    out: list[dict[str, Any]] = [{"key": k, "value": v} for k, v in env.items()]
    out += [{"key": k, "generateValue": True} for k in bp["generated"] if k not in env]
    return out


def create_service_body(bp: dict[str, Any], *, owner_id: str, repo: str, branch: str | None,
                        env_vars: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "web_service", "name": bp["name"], "ownerId": owner_id, "repo": repo,
        "autoDeployTrigger": bp["autoDeployTrigger"],
        "serviceDetails": {"runtime": "docker", "plan": bp["plan"], "region": bp["region"],
                           "healthCheckPath": bp["healthCheckPath"],
                           "envSpecificDetails": {"dockerfilePath": bp["dockerfilePath"], "dockerContext": bp["dockerContext"]}},
        "envVars": env_vars,
    }
    if branch:
        body["branch"] = branch
    return body


# ------------------------------------------------------------------ API calls


class Deployer:
    def __init__(self, cfg: Config, *, dry_run: bool = False, transport: httpx.BaseTransport | None = None,
                 out: Any = None, poll_s: float = 10.0) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.transport = transport
        self.out = out or sys.stdout
        self.poll_s = poll_s
        self.calls: list[dict[str, Any]] = []

    # -- output (always masked)
    def secrets_by_value(self) -> dict[str, str]:
        found = {}
        for name in SECRET_NAMES:
            value = self.cfg.get(name)
            if value:
                found[value] = name
        db = self.cfg.get("SUPABASE_DB_PASSWORD")
        if db:
            found[quote(db, safe="")] = "SUPABASE_DB_PASSWORD"
        return found

    def say(self, text: str) -> None:
        print(mask(text, self.secrets_by_value()), file=self.out, flush=True)

    def did(self, text: str) -> None:
        """An outcome line; in a dry run it says what WOULD happen."""
        self.say(("  (dry run) would have: " + text.strip()) if self.dry_run else text)

    def _request(self, api: str, method: str, path: str, *, token_name: str, json_body: Any = None,
                 params: dict[str, Any] | None = None, ok: tuple[int, ...] = (200, 201, 202, 204)) -> Any:
        record = {"method": method, "url": f"{api}{path}", "params": params, "json": json_body,
                  "auth": f"Bearer <secret:{token_name}>"}
        self.calls.append(mask(record, self.secrets_by_value()))
        self.say(f"  {method} {api}{path}" + (f" {json.dumps(params)}" if params else ""))
        if self.dry_run and json_body is not None:
            self.say("      body: " + json.dumps(mask(json_body, self.secrets_by_value()), ensure_ascii=False))
        if self.dry_run:
            return None
        token = self.cfg.secret(token_name)
        with httpx.Client(transport=self.transport, timeout=60) as client:
            response = client.request(method, f"{api}{path}", json=json_body, params=params,
                                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if response.status_code not in ok:
            detail = mask(response.text[:400], self.secrets_by_value())
            raise DeployError(f"{method} {path} answered HTTP {response.status_code}: {detail}")
        return response.json() if response.content else None

    def supabase(self, method: str, path: str, **kw: Any) -> Any:
        return self._request(SUPABASE_API, method, path, token_name="SUPABASE_ACCESS_TOKEN", **kw)

    def render(self, method: str, path: str, **kw: Any) -> Any:
        return self._request(RENDER_API, method, path, token_name="RENDER_API_KEY", **kw)

    # -- Supabase
    def step_supabase(self) -> dict[str, str]:
        bp = blueprint()
        region = SUPABASE_REGION_FOR.get(bp["region"], "ap-southeast-1")
        self.say(f"Supabase: project '{PROJECT_NAME}' in {region} (free organisation plan; smallest instance)")
        if not self.dry_run:
            self.cfg.secret("SUPABASE_ACCESS_TOKEN")
            self.cfg.secret("SUPABASE_DB_PASSWORD")
        orgs = self.supabase("GET", "/organizations") or [{"slug": "<org-slug>", "name": "(dry run)"}]
        slug = self.cfg.get("SUPABASE_ORG_SLUG")
        if not slug:
            if len(orgs) != 1:
                raise DeployError(f"{len(orgs)} Supabase organisations; set SUPABASE_ORG_SLUG to one of "
                                  f"{', '.join(o['slug'] for o in orgs)}")
            slug = orgs[0]["slug"]
        projects = self.supabase("GET", "/projects") or []
        # SUPABASE_PROJECT_NAME reuses a project the owner already created (e.g. the dashboard default project).
        wanted = self.cfg.get("SUPABASE_PROJECT_NAME") or PROJECT_NAME
        project = next((p for p in projects if p.get("name") == wanted), None)
        if project is None:
            body = {"name": PROJECT_NAME, "organization_slug": slug, "db_pass": self.cfg.get("SUPABASE_DB_PASSWORD") or
                    "<secret:SUPABASE_DB_PASSWORD>", "region_selection": {"type": "specific", "code": region}}
            project = self.supabase("POST", "/projects", json_body=body, ok=(200, 201)) or {"ref": "<project-ref>",
                                                                                            "region": region}
            self.did("  created the project (the database password is the one from .env; it cannot be read back later)")
        else:
            self.say(f"  reusing project {project['ref']} (status {project.get('status')})")
        ref = project["ref"]
        self._wait_project(ref)
        storage = self.supabase("GET", f"/projects/{ref}/config/storage")
        if storage is not None and not ((storage.get("features") or {}).get("s3Protocol") or {}).get("enabled", True):
            self.supabase("PATCH", f"/projects/{ref}/config/storage", json_body={"features": {"s3Protocol": {"enabled": True}}})
        pooler = self.pooler(ref)
        url = session_pooler_url(pooler, self.cfg.get("SUPABASE_DB_PASSWORD") or "<secret:SUPABASE_DB_PASSWORD>") \
            if pooler else "postgresql://postgres.<ref>:<secret:SUPABASE_DB_PASSWORD>@<pooler-host>:5432/postgres?sslmode=require"
        pool_size = (pooler[0].get("default_pool_size") if pooler else None)
        self.say(f"  session pooler: {mask(url, self.secrets_by_value())}  (default_pool_size={pool_size}; the demo uses ≤ 7)")
        actual_region = project.get("region") or region
        if not self.dry_run:
            self.cfg.save({"SUPABASE_PROJECT_REF": ref, "SUPABASE_REGION": actual_region, "DEMO_DATABASE_URL": url})
            self.say("  saved SUPABASE_PROJECT_REF, SUPABASE_REGION, DEMO_DATABASE_URL to .env")
        self.say(f"  S3 endpoint {s3_endpoint(ref)}  region {actual_region}")
        if not (self.cfg.get("SUPABASE_S3_ACCESS_KEY") and self.cfg.get("SUPABASE_S3_SECRET_KEY")):
            self.say(f"  NEXT (one manual step, no public API exists): https://supabase.com/dashboard/project/{ref}/storage/s3 → "
                     "'New access key' → put SUPABASE_S3_ACCESS_KEY and SUPABASE_S3_SECRET_KEY into .env")
        return {"ref": ref, "region": actual_region, "database_url": url}

    def _wait_project(self, ref: str, timeout_s: float = 900) -> None:
        if self.dry_run:
            self.say(f"  GET {SUPABASE_API}/projects/{ref}  (poll until status ACTIVE_HEALTHY)")
            return
        deadline = time.monotonic() + timeout_s
        while True:
            status = (self.supabase("GET", f"/projects/{ref}") or {}).get("status")
            if status == "ACTIVE_HEALTHY":
                return
            if status in ("INIT_FAILED", "REMOVED", "INACTIVE") or time.monotonic() > deadline:
                raise DeployError(f"Supabase project {ref} is {status} (paused projects are restored from the dashboard)")
            time.sleep(self.poll_s)

    def pooler(self, ref: str, attempts: int = 30) -> list[dict[str, Any]]:
        for _ in range(attempts):
            pooler = self.supabase("GET", f"/projects/{ref}/config/database/pooler")
            if self.dry_run or pooler:
                return pooler or []
            time.sleep(self.poll_s)
        raise DeployError("no pooler configuration after waiting")

    # -- Render
    def step_render(self) -> dict[str, str]:
        bp = blueprint()
        ref = self.cfg.get("SUPABASE_PROJECT_REF") or "<project-ref>"
        region = self.cfg.get("SUPABASE_REGION") or SUPABASE_REGION_FOR.get(bp["region"], "ap-southeast-1")
        if not self.dry_run:
            for name in ("RENDER_API_KEY", "DEMO_DATABASE_URL", "SUPABASE_S3_ACCESS_KEY", "SUPABASE_S3_SECRET_KEY"):
                self.cfg.secret(name)
        token = self.cfg.get("DEMO_API_TOKEN")
        if not token:
            token = secrets.token_urlsafe(32)
            if not self.dry_run:
                self.cfg.save({"DEMO_API_TOKEN": token})
                self.say("  generated DEMO_API_TOKEN (the reviewers' bearer token) and saved it to .env")
            else:
                token = "<secret:DEMO_API_TOKEN>"
        env_vars = render_env(bp, api_token=token, database_url=self.cfg.get("DEMO_DATABASE_URL") or "<secret:DEMO_DATABASE_URL>",
                              ref=ref, region=region, s3_key=self.cfg.get("SUPABASE_S3_ACCESS_KEY") or "<secret:SUPABASE_S3_ACCESS_KEY>",
                              s3_secret=self.cfg.get("SUPABASE_S3_SECRET_KEY") or "<secret:SUPABASE_S3_SECRET_KEY>",
                              gemini=self.cfg.get("GEMINI_API_KEY"))
        self.say(f"Render: web service '{bp['name']}' ({bp['plan']}, {bp['region']}, Docker {bp['dockerfilePath']}, "
                 f"health {bp['healthCheckPath']}, {len(env_vars)} env vars)")
        owners = self.render("GET", "/owners", params={"limit": 20}) or [{"owner": {"id": "<owner-id>", "name": "(dry run)"}}]
        owner_id = self.cfg.get("RENDER_OWNER_ID")
        if not owner_id:
            if len(owners) != 1:
                raise DeployError("several Render workspaces; set RENDER_OWNER_ID to one of "
                                  + ", ".join(f"{o['owner']['id']} ({o['owner'].get('name')})" for o in owners))
            owner_id = owners[0]["owner"]["id"]
        existing = self.render("GET", "/services", params={"name": bp["name"], "ownerId": owner_id, "limit": 20}) or []
        service = next((s["service"] for s in existing if s.get("service", {}).get("name") == bp["name"]), None)
        repo = self.cfg.get("DEMO_GITHUB_REPO", DEFAULT_REPO) or DEFAULT_REPO
        if service is None:
            body = create_service_body(bp, owner_id=owner_id, repo=repo, branch=self.cfg.get("DEMO_BRANCH"), env_vars=env_vars)
            created = self.render("POST", "/services", json_body=body, ok=(201,)) or {"service": {"id": "<service-id>"}}
            service = created["service"]
            self.did(f"  created service {service['id']} (its first deploy starts automatically)")
        else:
            self.say(f"  reusing service {service['id']}: replacing its env vars, then a manual deploy")
            self.render("PUT", f"/services/{service['id']}/env-vars", json_body=env_vars)
            self.render("POST", f"/services/{service['id']}/deploys", json_body={"clearCache": "do_not_clear"}, ok=(200, 201, 202))
        url = ((service.get("serviceDetails") or {}).get("url")) or f"https://{bp['name']}.onrender.com"
        if not self.dry_run:
            self.cfg.save({"RENDER_SERVICE_ID": service["id"], "DEMO_URL": url})
        self.did(f"  service URL: {url}  (saved RENDER_SERVICE_ID, DEMO_URL to .env)")
        return {"service_id": service["id"], "url": url}

    # -- checks
    def check_storage(self) -> None:
        import boto3
        from botocore.client import Config as BotoConfig

        ref = self.cfg.get("SUPABASE_PROJECT_REF")
        if self.dry_run or not ref:
            self.say(f"  S3 {s3_endpoint(ref or '<project-ref>')}: head/create bucket 'ibx', put/get/delete one probe object")
            return
        client = boto3.client("s3", endpoint_url=s3_endpoint(ref), region_name=self.cfg.get("SUPABASE_REGION"),
                              aws_access_key_id=self.cfg.secret("SUPABASE_S3_ACCESS_KEY"),
                              aws_secret_access_key=self.cfg.secret("SUPABASE_S3_SECRET_KEY"),
                              config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"},
                                                request_checksum_calculation="when_required",
                                                response_checksum_validation="when_required"))
        bucket = blueprint()["plain_env"].get("S3_BUCKET", "ibx")
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001 - missing bucket: create it (private)
            client.create_bucket(Bucket=bucket)
            self.say(f"  created private bucket '{bucket}'")
        key = f"deploy-check/{int(time.time())}.txt"
        client.put_object(Bucket=bucket, Key=key, Body=b"ibx deploy check", ContentType="text/plain")
        assert client.get_object(Bucket=bucket, Key=key)["Body"].read() == b"ibx deploy check"
        client.delete_object(Bucket=bucket, Key=key)
        self.say(f"  storage OK: bucket '{bucket}' head/put/get/delete through {s3_endpoint(ref)} (path-style)")

    def check_db(self) -> None:
        url = self.cfg.get("DEMO_DATABASE_URL")
        if self.dry_run or not url:
            self.say("  DB: connect with sslmode=require, SELECT version(), pg_try_advisory_lock, LISTEN/NOTIFY round trip")
            return
        import asyncio

        import asyncpg

        async def probe() -> str:
            conn = await asyncpg.connect(url)
            try:
                got: list[str] = []
                await conn.add_listener("ibx_deploy_check", lambda *a: got.append(a[-1]))
                assert await conn.fetchval("SELECT pg_try_advisory_lock(424242)") is True
                await conn.execute("SELECT pg_notify('ibx_deploy_check', 'ok')")
                for _ in range(50):
                    if got:
                        break
                    await asyncio.sleep(0.1)
                await conn.execute("SELECT pg_advisory_unlock(424242)")
                ssl = await conn.fetchval("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
                version = await conn.fetchval("SHOW server_version")
                if got != ["ok"]:
                    raise DeployError("LISTEN/NOTIFY did not round-trip: is this the transaction pooler (6543)?")
                return f"Postgres {version}, ssl={ssl}, advisory lock + LISTEN/NOTIFY OK (session mode)"
            finally:
                await conn.close()

        self.say("  DB " + asyncio.run(probe()))

    def wait_ready(self, timeout_s: float = 900) -> float | None:
        url = (self.cfg.get("DEMO_URL") or "").rstrip("/")
        if self.dry_run or not url:
            self.say("  GET <DEMO_URL>/ready until 200 (first build ~5-10 min); then the token check on /api/v1/connectors")
            return None
        started = time.monotonic()
        with httpx.Client(timeout=60, transport=self.transport) as client:
            while time.monotonic() - started < timeout_s:
                try:
                    response = client.get(f"{url}/ready")
                    if response.status_code == 200:
                        took = time.monotonic() - started
                        auth = client.get(f"{url}/api/v1/connectors",
                                          headers={"Authorization": f"Bearer {self.cfg.secret('DEMO_API_TOKEN')}"})
                        self.say(f"  /ready 200 after {took:.1f} s; /api/v1/connectors with the token: HTTP {auth.status_code}")
                        return took
                except httpx.HTTPError:
                    pass
                time.sleep(self.poll_s)
        raise DeployError(f"{url}/ready was not 200 within {timeout_s:.0f} s (see the Render deploy logs)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["plan", "supabase", "check-storage", "check-db", "render", "wait", "all"])
    parser.add_argument("--dry-run", action="store_true", help="print the planned calls (secrets masked); no network")
    args = parser.parse_args(argv)
    dry = args.dry_run or args.command == "plan"
    deployer = Deployer(Config(), dry_run=dry)
    try:
        if args.command in ("plan", "all"):
            deployer.step_supabase()
            deployer.check_db()
            deployer.check_storage()
            deployer.step_render()
            deployer.wait_ready()
        elif args.command == "supabase":
            deployer.step_supabase()
        elif args.command == "check-storage":
            deployer.check_storage()
        elif args.command == "check-db":
            deployer.check_db()
        elif args.command == "render":
            deployer.step_render()
        elif args.command == "wait":
            deployer.wait_ready()
    except DeployError as exc:
        deployer.say(f"STOPPED: {exc}")
        return 1
    if dry:
        deployer.say(f"dry run: {len(deployer.calls)} API calls planned, nothing was sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
