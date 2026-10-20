"""Unit regression tests that need no database (error codes, default secrets, run manifest, batch runner,
OpenAPI responses, spec values, SSRF, local-mode decisions). Each was written before its fix and seen failing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr

from app.config import Settings
from app.connectors.base import ConnectorContext, counts_as_outage, default_error_code
from app.contract.models import Connection, Destination
from app.errors import ErrorCode, IbxError
from tests.conftest import ROOT, TEST_TOKEN


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"APP_ENV": "test", "API_TOKEN": TEST_TOKEN, "MOCK_PLATFORM_URL": "http://mock-platforms:8080"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def connector(key: str, cfg: Settings) -> Any:
    from app.clock import FakeClock
    from app.contract.registry import get_registry
    from app.logging import get_logger

    registry = get_registry()
    ctx = ConnectorContext(manifest=registry.manifest(key), settings=cfg, http=httpx.AsyncClient(), clock=FakeClock(),
                           log=get_logger("test"), db_session_factory=None, simulation=cfg.SIMULATION_MODE)
    return registry.build(key, ctx)


# ------------------------------------------------------------------ error codes


def test_internal_error_is_not_retryable():
    assert IbxError(ErrorCode.INTERNAL_ERROR).retryable is False


@pytest.mark.parametrize(("status", "code"), [
    (400, ErrorCode.PLATFORM_CONTENT_REJECTED), (404, ErrorCode.PLATFORM_CONTENT_REJECTED),
    (409, ErrorCode.PLATFORM_CONTENT_REJECTED), (413, ErrorCode.PLATFORM_CONTENT_REJECTED),
    (401, ErrorCode.CONNECTION_AUTH_EXPIRED), (403, ErrorCode.CONNECTION_AUTH_EXPIRED),
    (429, ErrorCode.PLATFORM_RATE_LIMIT), (408, ErrorCode.PLATFORM_UNAVAILABLE),
    (500, ErrorCode.PLATFORM_UNAVAILABLE), (504, ErrorCode.PLATFORM_UNAVAILABLE),
])
def test_default_codes_for_unmapped_statuses(status, code):
    assert default_error_code(status) == code


def test_only_outages_count_for_the_breaker():
    assert counts_as_outage(IbxError(ErrorCode.PLATFORM_UNAVAILABLE, details={"platform_status": 502}))
    assert counts_as_outage(IbxError(ErrorCode.PLATFORM_UNAVAILABLE, details={"reason": "timeout"}))
    assert not counts_as_outage(IbxError(ErrorCode.PLATFORM_UNAVAILABLE, details={"platform_status": 400}))
    assert not counts_as_outage(IbxError(ErrorCode.PLATFORM_CONTENT_REJECTED, details={"platform_status": 400}))


async def test_pool_timeout_is_503_dependency_unavailable():
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from app.api.errors import _unhandled_handler

    class _Req:
        class state:  # noqa: N801
            correlation_id = "c1"

        class url:  # noqa: N801
            path = "/x"

    response = await _unhandled_handler(_Req(), PoolTimeout("QueuePool limit reached"))
    assert response.status_code == 503
    error = json.loads(response.body)["error"]
    assert error["code"] == "DEPENDENCY_UNAVAILABLE" and error["retryable"] is True


# ------------------------------------------------------------------ local-mode decisions


def test_placeholder_credentials_only_in_simulation_or_dev():
    dest = Destination(connector_key="discourse", instance={"base_url": "http://mock-platforms:8080/discourse/forum-41"})
    bare = Connection(connector_key="discourse")
    for env, sim, expected in (("test", False, True), ("dev", False, True), ("demo", True, True),
                               ("demo", False, False), ("prod", False, False)):
        _, conn = connector("discourse", settings(APP_ENV=env, SIMULATION_MODE=sim)).simulate(dest, bare)
        assert bool(conn.credentials) is expected, (env, sim)
    other = Destination(connector_key="discourse", instance={"base_url": "https://forum.example.org"})
    _, conn = connector("discourse", settings(APP_ENV="test")).simulate(other, bare)
    assert not conn.credentials  # never for a host that is not our mock container


def test_assisted_simulation_keeps_a_working_submit_page():
    dest = Destination(connector_key="assisted", instance={"submit_url": "https://www.reddit.com/r/selfhosted/submit"})
    sim_dest, _ = connector("assisted", settings(SIMULATION_MODE=True)).simulate(dest, Connection(connector_key="assisted"))
    url = sim_dest.instance["submit_url"]
    assert url.startswith("http://mock-platforms:8080/assist/") and url.endswith("/submit")


# ------------------------------------------------------------------ no default secrets outside dev/test


def test_demo_and_prod_refuse_dev_default_secrets():
    from app.config import ensure_safe_secrets

    for env in ("demo", "prod"):
        with pytest.raises(RuntimeError, match="API_TOKEN"):
            ensure_safe_secrets(Settings(_env_file=None, APP_ENV=env))
    ensure_safe_secrets(Settings(_env_file=None, APP_ENV="dev"))  # a clean machine still starts
    ensure_safe_secrets(Settings(
        _env_file=None, APP_ENV="demo", API_TOKEN="x" * 40, S3_ACCESS_KEY="a" * 20, S3_SECRET_KEY="b" * 40,
        DATABASE_URL="postgresql+asyncpg://ibx:generated-pw-123@postgres:5432/ibx",
    ))


def test_create_app_fails_fast_in_demo_with_defaults():
    from app.main import create_app

    with pytest.raises(RuntimeError, match="default"):
        create_app(Settings(_env_file=None, APP_ENV="demo"))


# ------------------------------------------------------------------ SSRF guard


async def test_ssrf_private_and_metadata_hosts_are_blocked_in_prod():
    from app.reliability.safe_fetch import check_url

    prod = settings(APP_ENV="prod")
    for url in ("http://127.0.0.1:8088/x", "http://169.254.169.254/latest/meta-data", "http://10.1.2.3/a",
                "http://metadata.google.internal/x", "http://[::1]/x", "file:///etc/passwd"):
        with pytest.raises(IbxError) as err:
            await check_url(url, prod)
        assert err.value.code == ErrorCode.REQUEST_VALIDATION_FAILED and err.value.retryable is False


async def test_ssrf_local_mode_allows_only_our_compose_hosts():
    from app.reliability.safe_fetch import check_url

    local = settings(APP_ENV="dev")
    await check_url("http://mock-platforms:8080/assist/x/media/a.png", local)  # our own container
    await check_url("http://127.0.0.1:9/x", local)
    with pytest.raises(IbxError):
        await check_url("http://10.1.2.3/a", local)  # private but not ours
    with pytest.raises(IbxError):
        await check_url("http://169.254.169.254/latest", local)


async def test_ssrf_redirect_hop_to_metadata_is_blocked_and_limits_apply():
    from app.reliability.safe_fetch import fetch_url

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * 100, headers={"Content-Type": "image/png"})
        return httpx.Response(200, content=b"<html>", headers={"Content-Type": "text/html"})

    local = settings(APP_ENV="dev")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IbxError):
            await fetch_url("http://127.0.0.1/redirect", local, client=client, max_bytes=1000)
        with pytest.raises(IbxError) as too_big:
            await fetch_url("http://127.0.0.1/big", local, client=client, max_bytes=10)
        assert too_big.value.http_status == 413
        with pytest.raises(IbxError) as wrong_type:
            await fetch_url("http://127.0.0.1/page", local, client=client, max_bytes=1000, allowed_mime=["image/png"])
        assert wrong_type.value.http_status == 415
        ok = await fetch_url("http://127.0.0.1/big", local, client=client, max_bytes=1000, allowed_mime=["image/png"])
        assert ok.data == b"x" * 100 and ok.mime == "image/png"


async def test_ssrf_callbacks_to_internal_hosts_are_refused_in_prod():
    from app.jobs.callbacks import deliver

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        outcome = await deliver(client, "http://postgres:5432/", {"job_id": "j"}, b"k", settings=settings(APP_ENV="prod"))
    assert outcome.startswith("rejected:")


# ------------------------------------------------------------------ manifest lists what was used


def test_manifest_lists_only_used_connectors_and_providers_and_has_rights():
    from app.jobs.context import JobContext, build_manifest
    from app.logging import get_logger

    ctx = JobContext(job_id="j1", kind="publication.run", payload={}, attempt=1, settings=settings(), clock=None,
                     log=get_logger("t"), session_factory=None, storage=None)
    ctx.used_connectors["lemmy"] = "1.0.0"
    ctx.manifest_extra["rights"] = {"content_owner": "creator", "licence": "CC-BY-4.0", "ai_generated": True}
    now = datetime(2026, 10, 17, tzinfo=UTC)
    manifest = build_manifest(ctx, status="succeeded", started_at=now, finished_at=now,
                              connectors={"lemmy": "1.0.0", "ghost": "1.0.0", "devto": "1.0.0"})
    assert manifest["connectors"] == {"lemmy": "1.0.0"}
    assert manifest["providers"] == []  # no LLM was called
    assert manifest["rights"]["licence"] == "CC-BY-4.0" and manifest["rights"]["ai_generated"] is True
    assert manifest["cost"]["total_usd"] == 0.0


# ------------------------------------------------------------------ batch runner follows jobs


def test_batch_runner_polls_jobs_and_downloads_links(tmp_path: Path):
    from app.batch.runner import run_cases

    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "job_1", "status": "queued", "status_url": "/api/v1/jobs/job_1"})
        if request.url.path == "/api/v1/jobs/job_1":
            polls["n"] += 1
            if polls["n"] < 2:
                return httpx.Response(200, json={"job_id": "job_1", "status": "running", "progress": 50})
            return httpx.Response(200, json={"job_id": "job_1", "status": "succeeded", "progress": 100,
                                             "result": {"links": {"manifest": "http://api.test/api/v1/files/jobs/job_1/manifest.json?sig=s"}}})
        if request.url.path.startswith("/api/v1/files/"):
            return httpx.Response(200, json={"job_id": "job_1", "kind": "selfcheck"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api.test")
    results = run_cases([{"name": "selfcheck", "method": "POST", "path": "/api/v1/jobs", "body": {"kind": "selfcheck"},
                          "expect": {"status": 202, "job_status": "succeeded"}}], tmp_path, client, poll_interval_s=0)
    assert results[0].passed and results[0].job_status == "succeeded"
    folder = tmp_path / results[0].folder
    assert json.loads((folder / "job.json").read_text())["status"] == "succeeded"
    assert json.loads((folder / "outputs" / "manifest.json").read_text())["job_id"] == "job_1"


def test_batch_runner_parallel_case(tmp_path: Path):
    from app.batch.runner import run_cases

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})), base_url="http://t")
    results = run_cases([{"name": "dup", "method": "POST", "path": "/x", "parallel": 4}], tmp_path, client)
    assert len(results) == 4 and all(r.status == 200 for r in results)


def test_sample_cases_ship_with_the_repo():
    from app.batch.runner import load_cases

    cases = load_cases(ROOT / "test_pack" / "inputs" / "sample")
    names = " ".join(c["name"] for c in cases).lower()
    assert len(cases) >= 5
    for hard_case in ("frequency cap", "self-promo", "spec", "timeout", "duplicate"):
        assert hard_case in names, hard_case


# ------------------------------------------------------------------ openapi documents the real responses


def _spec() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))


def test_every_error_response_uses_the_error_envelope():
    spec = _spec()
    assert "HTTPValidationError" not in json.dumps(spec)
    for path, item in spec["paths"].items():
        for method, op in item.items():
            for status, response in op.get("responses", {}).items():
                if str(status).startswith(("4", "5")):
                    ref = json.dumps(response)
                    assert "ErrorEnvelope" in ref, (method, path, status)


def test_bearer_security_and_standard_errors_on_protected_routes():
    spec = _spec()
    assert spec["components"]["securitySchemes"]["bearerAuth"] == {"type": "http", "scheme": "bearer"}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            protected = path.startswith(("/api/v1/", "/connector/v1/")) and not path.startswith("/api/v1/files/")
            if protected:
                assert op.get("security") == [{"bearerAuth": []}], (method, path)
                for status in ("401", "429", "500"):
                    assert status in op["responses"], (method, path, status)
    assert "503" in spec["paths"]["/ready"]["get"]["responses"]
    assert "404" in spec["paths"]["/api/v1/jobs/{job_id}"]["get"]["responses"]


async def test_real_error_responses_match_the_documented_envelope(app_factory, pg_url):
    """Conformance: the bodies the service really returns validate against the committed schema."""
    from app.api.errors import ErrorEnvelope
    from tests.conftest import api_client

    schema = _spec()["components"]["schemas"]["ErrorEnvelope"]
    assert schema["required"] == ["error"]
    app = await app_factory(pg_url=pg_url)
    async with api_client(app) as c, api_client(app, token=None) as anon:
        responses = [
            await anon.get("/api/v1/connectors"),  # 401
            await c.get("/api/v1/jobs/job_nope"),  # 404
            await c.post("/api/v1/jobs", json={"kind": 5}),  # 422
            await c.post("/api/v1/jobs", content=b"{nope", headers={"Content-Type": "application/json"}),  # 422
            await c.delete("/health"),  # 405
        ]
    assert [r.status_code for r in responses] == [401, 404, 422, 422, 405]
    for response in responses:
        ErrorEnvelope.model_validate(response.json())
        assert set(response.json()["error"]) == {"code", "message", "retryable", "details"}


# ------------------------------------------------------------------ no input echoed in messages


async def test_error_messages_do_not_echo_input(app_factory, pg_url):
    from tests.conftest import api_client

    app = await app_factory(pg_url=pg_url)
    canary = "zz-canary-echo-42"
    async with api_client(app) as c:
        bodies = [
            (await c.post("/api/v1/jobs", json={"kind": canary})).json(),
            (await c.post(f"/connector/v1/{canary}/publish", json={})).json(),
            (await c.get(f"/api/v1/jobs/{canary}")).json(),
        ]
    for body in bodies:
        assert canary not in body["error"]["message"], body


def test_image_tag_is_per_compose_project():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    for name in ("api", "worker"):
        image = compose["services"][name].get("image", "")
        assert "ibx-destination-network:dev" not in image


def test_secret_value_type_stays_secret():
    assert isinstance(settings().API_TOKEN, SecretStr)


# ------------------------------------------------------------------ bad spec values are 422 / load errors


def test_bad_surface_spec_is_refused_when_the_manifest_loads():
    from pydantic import ValidationError

    from app.contract.manifest import Surface

    for spec in ({"count_unit": "words"}, {"tag_pattern": "[unclosed"}):
        with pytest.raises(ValidationError):
            Surface(key="post", content_types=["text"], spec=spec)


def test_bad_spec_overrides_from_a_caller_are_422_not_500():
    from app.contract.models import Rendition

    discourse = connector("discourse", settings())
    dest = Destination(connector_key="discourse", instance={"base_url": "https://f.example.org"},
                       spec_overrides={"count_unit": "words"})
    with pytest.raises(IbxError) as err:
        discourse.spec_violations(Rendition(title="A title that is long enough", body="x" * 30), dest)
    assert err.value.code == ErrorCode.REQUEST_VALIDATION_FAILED and err.value.retryable is False
