"""Live demo unit tests: Supabase-style DATABASE_URLs, the in-process host alias, demo settings guard, the demo image +
render.yaml shape, the demo seed rows, and scripts/deploy_render.py (mocked APIs, secrets never printed)."""

from __future__ import annotations

import importlib.util
import io
import json
import socket
import stat
import sys
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from app.config import Settings
from app.db.session import engine_url_and_args, make_engine, sync_dsn
from app.demo.allinone import check_demo_settings, install_host_aliases, mock_endpoint, uninstall_host_aliases

ROOT = Path(__file__).resolve().parents[2]


def _load_deploy():
    spec = importlib.util.spec_from_file_location("deploy_render", ROOT / "scripts" / "deploy_render.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_render"] = module
    spec.loader.exec_module(module)
    return module


deploy = _load_deploy()


# ------------------------------------------------------------------ DATABASE_URL shapes


@pytest.mark.parametrize("url", [
    "postgresql://postgres.abc:p%40ss@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require",
    "postgres://postgres.abc:p%40ss@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require",
    "postgresql+asyncpg://postgres.abc:p%40ss@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?ssl=require",
])
def test_supabase_urls_become_asyncpg_with_ssl_connect_arg(url):
    out, args = engine_url_and_args(url)
    assert out.startswith("postgresql+asyncpg://postgres.abc:p%40ss@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres")
    assert "sslmode" not in out and "ssl=" not in out
    assert args == {"ssl": "require"}
    engine = make_engine(url, 1)  # no TypeError for sslmode, pool sized as asked
    assert engine.dialect.create_connect_args(engine.url)[1]["password"] == "p@ss"


def test_plain_url_unchanged_and_bad_sslmode_refused():
    url = "postgresql+asyncpg://ibx:pw@postgres:5432/ibx"
    assert engine_url_and_args(url) == (url, {})
    with pytest.raises(ValueError, match="sslmode"):
        engine_url_and_args("postgresql://u:p@h/db?sslmode=bogus")


def test_listen_dsn_keeps_libpq_sslmode():
    assert sync_dsn("postgresql+asyncpg://u:p@h:5432/db?ssl=require") == "postgresql://u:p@h:5432/db?sslmode=require"
    assert sync_dsn("postgresql://u:p@h:5432/db?sslmode=require") == "postgresql://u:p@h:5432/db?sslmode=require"


# ------------------------------------------------------------------ all-in-one wiring


def test_host_alias_maps_only_the_named_host():
    try:
        install_host_aliases({"mock-platforms": "127.0.0.1"})
        assert socket.getaddrinfo("mock-platforms", 8080)[0][4][0] == "127.0.0.1"
        assert socket.getaddrinfo("MOCK-PLATFORMS.", 8080)[0][4][0] == "127.0.0.1"
        assert socket.getaddrinfo("127.0.0.1", 1)[0][4][0] == "127.0.0.1"
    finally:
        uninstall_host_aliases()
    with pytest.raises(OSError):
        socket.getaddrinfo("mock-platforms.invalid", 8080)


def _settings(**kw):
    base = {"APP_ENV": "demo", "API_TOKEN": "a-real-demo-token", "S3_ACCESS_KEY": "k", "S3_SECRET_KEY": "s",
            "DATABASE_URL": "postgresql://u:pw@h/db", "SIMULATION_MODE": True}
    return Settings(_env_file=None, **{**base, **kw})


def test_demo_refuses_without_simulation_or_in_prod():
    check_demo_settings(_settings())
    assert mock_endpoint(_settings()) == ("mock-platforms", 8080)
    with pytest.raises(RuntimeError, match="SIMULATION_MODE"):
        check_demo_settings(_settings(SIMULATION_MODE=False))
    with pytest.raises(RuntimeError, match="prod"):
        check_demo_settings(_settings(APP_ENV="prod"))
    with pytest.raises(RuntimeError, match="MOCK_PLATFORM_URL"):
        check_demo_settings(_settings(MOCK_PLATFORM_URL="https://mock-platforms"))


def test_demo_image_and_start_script():
    dockerfile = (ROOT / "Dockerfile.demo").read_text()
    for needle in ("SIMULATION_MODE=true", "APP_ENV=demo", "MOCK_PLATFORM_URL=http://mock-platforms:8080", "USER ibx",
                   'CMD ["/srv/scripts/demo_start.sh"]', "COPY seed ./seed"):
        assert needle in dockerfile
    for name in ("API_TOKEN=", "S3_SECRET_KEY=", "DATABASE_URL=", "GEMINI_API_KEY="):
        assert name not in dockerfile  # secrets only from the host's environment
    start = (ROOT / "scripts" / "demo_start.sh").read_text()
    assert "--loop asyncio" in start and '--port "$PORT"' in start and "app.demo.allinone:create_demo_app" in start
    assert "RENDER_EXTERNAL_URL" in start
    assert (ROOT / "scripts" / "demo_start.sh").stat().st_mode & stat.S_IXUSR


def test_render_blueprint_is_free_docker_with_generated_and_typed_secrets():
    doc = yaml.safe_load((ROOT / "render.yaml").read_text())
    (svc,) = doc["services"]
    assert (svc["type"], svc["runtime"], svc["plan"]) == ("web", "docker", "free")
    assert svc["dockerfilePath"] == "./Dockerfile.demo" and svc["healthCheckPath"] == "/health"
    env = {e["key"]: e for e in svc["envVars"]}
    assert env["API_TOKEN"].get("generateValue") is True
    for key in ("DATABASE_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "GEMINI_API_KEY", "S3_ENDPOINT"):
        assert env[key].get("sync") is False and "value" not in env[key]
    assert env["SIMULATION_MODE"]["value"] == "true" and env["APP_ENV"]["value"] == "demo"
    assert (ROOT / "deploy" / "render" / "DEPLOY.md").exists()


def test_demo_rows_cover_hard_cases_and_every_connector():
    from app.catalogue.models import DestinationIn
    from app.contract.registry import get_registry
    from app.demo.seed import demo_keys, demo_rows

    rows = demo_rows()
    names = {r["name"] for r in rows}
    assert {"dest-weekly-cap-1", "dest-no-promo", "dest-ai-disclosure-es", "dest-partner-revocable", "dest-strict-limits"} <= names
    registry = get_registry()
    covered = {DestinationIn.model_validate(r).connector_key for r in rows}
    assert set(registry.keys()) <= covered, f"no demo row for {set(registry.keys()) - covered}"
    assert len(demo_keys(registry, rows)) == len(rows)  # every row is a distinct destination


# ------------------------------------------------------------------ scripts/deploy_render.py

SECRETS = {"RENDER_API_KEY": "rnd_SECRET_render_123", "SUPABASE_ACCESS_TOKEN": "sbp_SECRET_supabase_456",
           "SUPABASE_DB_PASSWORD": "db/pass@word SECRET", "SUPABASE_S3_ACCESS_KEY": "s3key_SECRET_789",
           "SUPABASE_S3_SECRET_KEY": "s3secret_SECRET_abc", "GEMINI_API_KEY": "gem_SECRET_def"}
REF = "abcdefghijklmnopqrst"


def _env_file(tmp_path: Path, **extra: str) -> Path:
    path = tmp_path / ".env"
    path.write_text("# test\nLOG_LEVEL=INFO\n" + "".join(f"{k}={v}\n" for k, v in {**SECRETS, **extra}.items()))
    return path


def _no_secret_in(text: str) -> None:
    for name, value in SECRETS.items():
        assert value not in text, name
    assert "db%2Fpass%40word%20SECRET" not in text


def test_dry_run_plans_every_call_without_network_or_secrets(tmp_path, monkeypatch):
    out = io.StringIO()
    cfg = deploy.Config(_env_file(tmp_path), environ={})
    d = deploy.Deployer(cfg, dry_run=True, out=out)
    with respx.mock(assert_all_called=False) as router:  # any real request would fail the test
        for step in (d.step_supabase, d.check_db, d.check_storage, d.step_render, d.wait_ready):
            step()
        assert not router.calls
    text = out.getvalue()
    _no_secret_in(text)
    assert "<secret:SUPABASE_DB_PASSWORD>" in text and "<secret:GEMINI_API_KEY>" in text
    planned = [(c["method"], c["url"]) for c in d.calls]
    assert ("POST", "https://api.supabase.com/v1/projects") in planned
    assert ("POST", "https://api.render.com/v1/services") in planned
    assert "DEMO_API_TOKEN" not in cfg.env_file.read_text()  # a dry run writes nothing


def test_full_flow_against_mocked_apis(tmp_path):
    env_path = _env_file(tmp_path)
    out = io.StringIO()
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.supabase.com/v1/organizations").respond(200, json=[{"id": "x", "slug": "org-one", "name": "Org"}])
        router.get("https://api.supabase.com/v1/projects").respond(200, json=[])
        create = router.post("https://api.supabase.com/v1/projects").respond(
            201, json={"id": REF, "ref": REF, "name": deploy.PROJECT_NAME, "region": "ap-southeast-1"})
        router.get(f"https://api.supabase.com/v1/projects/{REF}").mock(side_effect=[
            httpx.Response(200, json={"ref": REF, "status": "COMING_UP"}),
            httpx.Response(200, json={"ref": REF, "status": "ACTIVE_HEALTHY"})])
        router.get(f"https://api.supabase.com/v1/projects/{REF}/config/storage").respond(
            200, json={"features": {"s3Protocol": {"enabled": True}}})
        router.get(f"https://api.supabase.com/v1/projects/{REF}/config/database/pooler").respond(200, json=[{
            "identifier": REF, "database_type": "PRIMARY", "is_using_scram_auth": True, "db_user": f"postgres.{REF}",
            "db_host": "aws-1-ap-southeast-1.pooler.supabase.com", "db_port": 6543, "db_name": "postgres",
            "connection_string": "x", "connectionString": "x", "default_pool_size": 15, "max_client_conn": 200,
            "pool_mode": "transaction"}])
        router.get("https://api.render.com/v1/owners").respond(200, json=[{"owner": {"id": "tea-1", "name": "me"}, "cursor": "c"}])
        router.get("https://api.render.com/v1/services").respond(200, json=[])
        service = router.post("https://api.render.com/v1/services").respond(201, json={
            "service": {"id": "srv-1", "name": "ibx-destination-network-demo",
                        "serviceDetails": {"url": "https://ibx-destination-network-demo.onrender.com"}}, "deployId": "dep-1"})
        d = deploy.Deployer(deploy.Config(env_path, environ={}), out=out, poll_s=0)
        d.step_supabase()
        d.step_render()

    _no_secret_in(out.getvalue())
    body = json.loads(create.calls[0].request.content)
    assert body["organization_slug"] == "org-one" and body["db_pass"] == SECRETS["SUPABASE_DB_PASSWORD"]
    assert body["region_selection"] == {"type": "specific", "code": "ap-southeast-1"}
    for call in router.calls:
        auth = call.request.headers["Authorization"]
        assert auth in (f"Bearer {SECRETS['RENDER_API_KEY']}", f"Bearer {SECRETS['SUPABASE_ACCESS_TOKEN']}")

    saved = deploy.read_env_file(env_path)
    assert saved["SUPABASE_PROJECT_REF"] == REF and saved["DEMO_URL"] == "https://ibx-destination-network-demo.onrender.com"
    assert saved["DEMO_DATABASE_URL"] == (f"postgresql://postgres.{REF}:db%2Fpass%40word%20SECRET@"
                                          "aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require")
    assert len(saved["DEMO_API_TOKEN"]) >= 40 and saved["LOG_LEVEL"] == "INFO"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    sent = json.loads(service.calls[0].request.content)
    assert sent["serviceDetails"]["plan"] == "free" and sent["serviceDetails"]["runtime"] == "docker"
    assert sent["serviceDetails"]["envSpecificDetails"]["dockerfilePath"] == "./Dockerfile.demo"
    env = {e["key"]: e for e in sent["envVars"]}
    assert env["API_TOKEN"]["value"] == saved["DEMO_API_TOKEN"]
    assert env["DATABASE_URL"]["value"] == saved["DEMO_DATABASE_URL"]
    assert env["S3_ENDPOINT"]["value"] == f"https://{REF}.storage.supabase.co/storage/v1/s3"
    assert env["S3_ACCESS_KEY"]["value"] == SECRETS["SUPABASE_S3_ACCESS_KEY"]
    assert env["CALLBACK_SECRET"] == {"key": "CALLBACK_SECRET", "generateValue": True}
    assert env["SIMULATION_MODE"]["value"] == "true"


def test_existing_service_is_updated_not_duplicated(tmp_path):
    env_path = _env_file(tmp_path, SUPABASE_PROJECT_REF=REF, SUPABASE_REGION="ap-southeast-1", DEMO_API_TOKEN="tok_SECRET_x",
                         DEMO_DATABASE_URL="postgresql://u:SECRETpw@h:5432/postgres?sslmode=require")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.render.com/v1/owners").respond(200, json=[{"owner": {"id": "tea-1"}, "cursor": "c"}])
        router.get("https://api.render.com/v1/services").respond(200, json=[{"service": {"id": "srv-9", "name": "ibx-destination-network-demo"}}])
        put = router.put("https://api.render.com/v1/services/srv-9/env-vars").respond(200, json=[])
        router.post("https://api.render.com/v1/services/srv-9/deploys").respond(201, json={"id": "dep-2"})
        out = io.StringIO()
        deploy.Deployer(deploy.Config(env_path, environ={}), out=out).step_render()
    assert "tok_SECRET_x" not in out.getvalue() and "SECRETpw" not in out.getvalue()
    assert {e["key"] for e in json.loads(put.calls[0].request.content)} >= {"API_TOKEN", "DATABASE_URL", "S3_SECRET_KEY"}


def test_api_errors_are_masked(tmp_path):
    env_path = _env_file(tmp_path)
    with respx.mock() as router:
        router.get("https://api.supabase.com/v1/organizations").respond(401, text=f"bad token {SECRETS['SUPABASE_ACCESS_TOKEN']}")
        out = io.StringIO()
        with pytest.raises(deploy.DeployError) as err:
            deploy.Deployer(deploy.Config(env_path, environ={}), out=out).step_supabase()
    assert SECRETS["SUPABASE_ACCESS_TOKEN"] not in str(err.value)
    assert "HTTP 401" in str(err.value)
