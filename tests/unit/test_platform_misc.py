"""Config (IS6), manifest model + JSON Schema, HMAC callback signing, scaffold, intent helpers, mock faults."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from pydantic import ValidationError

from app.config import Settings
from app.contract.manifest import SCHEMA_PATH, Manifest, load_manifest, manifest_json_schema
from app.jobs import callbacks
from app.reliability.idempotency import Action, IntentState, classify, marker_for, request_hash
from tests.conftest import ROOT

TEMPLATE_MANIFEST = ROOT / "app" / "connectors" / "_template" / "manifest.yaml"


# ------------------------------------------------------------------ IS6 configuration


def test_disabled_connectors_csv_and_safe_snapshot(make_settings):
    settings = make_settings(DISABLED_CONNECTORS=" DevTo, lemmy ,", API_TOKEN="tok-very-secret-1", S3_SECRET_KEY="s3-very-secret")
    assert settings.DISABLED_CONNECTORS == ["devto", "lemmy"]
    snapshot = json.dumps(settings.safe_snapshot(), default=str)
    assert "tok-very-secret-1" not in snapshot and "s3-very-secret" not in snapshot and "API_TOKEN" not in snapshot
    assert "tok-very-secret-1" in settings.secret_values()


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILS", "7")
    monkeypatch.setenv("DISABLED_CONNECTORS", "ghost")
    settings = Settings(_env_file=None)
    assert settings.CIRCUIT_FAILS == 7 and settings.DISABLED_CONNECTORS == ["ghost"]


def test_fernet_key_is_stable_and_valid(make_settings):
    from cryptography.fernet import Fernet

    a, b = make_settings().fernet_key(), make_settings().fernet_key()
    assert a == b
    Fernet(a)  # a valid key


def test_secret_roundtrip_registers_values(make_settings):
    from app.reliability.redact import redact_text, secret_registry
    from app.reliability.secrets import decrypt_credentials, encrypt_credentials

    key = make_settings().fernet_key()
    token = encrypt_credentials(key, {"api_key": "roundtrip-secret-1"})
    secret_registry.clear()
    assert decrypt_credentials(key, token) == {"api_key": "roundtrip-secret-1"}
    assert "roundtrip-secret-1" not in redact_text("x roundtrip-secret-1 y")


def test_every_setting_is_listed_in_env_example():
    listed = {line.split("=", 1)[0].strip() for line in (ROOT / ".env.example").read_text().splitlines() if "=" in line and not line.startswith("#")}
    missing = set(Settings.model_fields) - listed
    assert not missing, f".env.example is missing: {sorted(missing)}"


FORBIDDEN = [re.compile(r"\bsk-[A-Za-z0-9]{8,}"), re.compile(r"/home/"), re.compile(r"https?://localhost"),
             re.compile(r"https?://127\.0\.0\.1:\d+")]
ALLOWED_FILES = {"app/config.py", "app/reliability/redact.py"}


def test_no_hardcoded_paths_keys_or_hosts_in_app():
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        offenders += [f"{rel}: {p.pattern}" for p in FORBIDDEN if p.search(text)]
    assert not offenders, offenders


# ------------------------------------------------------------------ manifest


def test_template_manifest_is_valid():
    manifest = Manifest.model_validate(yaml.safe_load(TEMPLATE_MANIFEST.read_text().replace("__KEY__", "sample")))
    assert manifest.supports("validate") and manifest.supports("get_status") and not manifest.supports("refresh_auth")


def test_appendix_a_example_manifest_is_valid():
    data = {
        "key": "discourse", "version": "1.0.0", "tier": "native", "auth": "api_key",
        "surfaces": [{"key": "topic", "content_types": ["text", "image", "video_link"],
                      "spec": {"title_max_chars": 255, "body_max_chars": 32000, "links_allowed": True}}],
        "rate_limits": {"per_connection": {"requests": 60, "per_seconds": 60}},
        "capabilities": ["publish", "status", "delete", "metrics"],
        "error_map": {"429": "PLATFORM_RATE_LIMIT", "422": "PLATFORM_CONTENT_REJECTED", "401": "CONNECTION_AUTH_EXPIRED",
                      "400:rate_limit_error": "PLATFORM_RATE_LIMIT"},
        "instance_param": "base_url",
    }
    assert Manifest.model_validate(data).key == "discourse"


@pytest.mark.parametrize(
    "patch",
    [
        {"extra_key": 1},
        {"error_map": {"4xx": "PLATFORM_RATE_LIMIT"}},
        {"error_map": {"429": "NOT_A_CODE"}},
        {"capabilities": ["publish", "teleport"]},
        {"capabilities": ["publish", "publish"]},
        {"tier": "magic"},
        {"version": "1.0"},
        {"surfaces": []},
    ],
)
def test_invalid_manifests_are_rejected(patch):
    data = yaml.safe_load(TEMPLATE_MANIFEST.read_text().replace("__KEY__", "sample"))
    data.update(patch)
    with pytest.raises(ValidationError):
        Manifest.model_validate(data)


def test_committed_json_schema_is_up_to_date():
    committed = json.loads(SCHEMA_PATH.read_text())
    assert committed == json.loads(json.dumps(manifest_json_schema())), "run: uv run python -m app.contract.manifest"


def test_load_manifest_from_file(tmp_path: Path):
    path = tmp_path / "manifest.yaml"
    path.write_text(TEMPLATE_MANIFEST.read_text().replace("__KEY__", "abc"))
    assert load_manifest(path).key == "abc"


# ------------------------------------------------------------------ HMAC callbacks


def test_sign_and_verify():
    body = callbacks.encode({"job_id": "job_1", "status": "succeeded"})
    signature = callbacks.sign(b"secret", body)  # t=<ts>,v1=<hex> (timestamped)
    assert signature.startswith("t=") and ",v1=" in signature and len(signature.split("v1=")[1]) == 64
    assert callbacks.verify(b"secret", body, signature)
    assert not callbacks.verify(b"secret", body + b" ", signature)
    assert not callbacks.verify(b"other", body, signature)
    assert not callbacks.verify(b"secret", body, None)


async def test_deliver_retries_5xx_then_succeeds():
    with respx.mock() as router:
        route = router.post("https://cb.example/x").mock(side_effect=[httpx.Response(503), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            outcome = await callbacks.deliver(client, "https://cb.example/x", {"job_id": "j"}, b"k", attempts=3)
    assert outcome == "delivered" and route.call_count == 2


async def test_deliver_rejects_non_http_urls():
    async with httpx.AsyncClient() as client:
        assert await callbacks.deliver(client, "file:///etc/passwd", {}, b"k") == "rejected:bad_url"


# ------------------------------------------------------------------ intent helpers


def test_marker_is_stable_short_and_key_specific():
    assert marker_for("K1") == marker_for("K1") and marker_for("K1") != marker_for("K2")
    assert re.fullmatch(r"ibx-[a-z2-7]{12}", marker_for("K1"))


def test_request_hash_ignores_key_order():
    assert request_hash({"a": 1, "b": 2}, "d", "c") == request_hash({"b": 2, "a": 1}, "d", "c")
    assert request_hash({"a": 1}, "d", "c") != request_hash({"a": 2}, "d", "c")


def test_classify_states():
    class Row:
        def __init__(self, state: str, request_hash: str = "h") -> None:
            self.state, self.request_hash, self.idempotency_key = state, request_hash, "k"

    assert classify(None, "h") == Action.NEW
    assert classify(Row(IntentState.SUCCEEDED), "h") == Action.REPLAY
    assert classify(Row(IntentState.FAILED), "h") == Action.REPLAY_ERROR
    assert classify(Row(IntentState.RETRYABLE), "h") == Action.RETRY
    assert classify(Row(IntentState.PENDING), "h") == Action.RECONCILE
    assert classify(Row(IntentState.UNKNOWN), "h") == Action.RECONCILE
    from app.errors import IbxError

    with pytest.raises(IbxError) as exc:
        classify(Row(IntentState.SUCCEEDED, "other"), "h")
    assert exc.value.code.value == "IDEMPOTENCY_KEY_REUSED"


# ------------------------------------------------------------------ scaffold


def test_scaffold_writes_files_and_refuses_overwrite(tmp_path: Path):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from new_connector import scaffold

    written = scaffold("writefreely", root=tmp_path)
    names = {p.relative_to(tmp_path).as_posix() for p in written}
    assert names == {
        "app/connectors/writefreely/__init__.py", "app/connectors/writefreely/manifest.yaml",
        "app/connectors/writefreely/connector.py", "mocks/platforms/routes/writefreely.py",
        "tests/integration/test_writefreely_connector.py",
    }
    assert "__KEY__" not in (tmp_path / "app/connectors/writefreely/connector.py").read_text()
    with pytest.raises(FileExistsError):
        scaffold("writefreely", root=tmp_path)
    with pytest.raises(ValueError):
        scaffold("Bad-Key", root=tmp_path)
