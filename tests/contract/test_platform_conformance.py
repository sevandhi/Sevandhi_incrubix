"""Conformance kit: every registered connector (plus the template-generated ``sample``) against Appendix A.

Parametrised over the registry, so a connector dropped into ``app/connectors/<key>/`` is checked
automatically: manifest schema, capabilities == implemented hooks, error-map codes, exact output keys,
404 for unsupported methods, and the error envelope. No database or network needed.
"""

from __future__ import annotations

import httpx
import pytest
import yaml

from app.contract.manifest import CAPABILITY_HOOKS, METHOD_CAPABILITY, Manifest
from app.contract.models import (
    AssistPack,
    DeleteResult,
    MetricsResult,
    PublishResult,
    RefreshAuthResult,
    StatusResult,
    ValidateResult,
)
from app.contract.registry import Registry, discover
from app.errors import ErrorCode
from app.main import create_app
from app.storage import MemoryStorage
from tests.conftest import TEST_TOKEN

APPENDIX_MANIFEST_KEYS = {"key", "version", "tier", "auth", "surfaces", "rate_limits", "capabilities", "error_map", "instance_param"}
APPENDIX_OUTPUT_KEYS = {
    ValidateResult: {"ok", "violations"},
    PublishResult: {"platform_post_id", "live_url", "status"},
    StatusResult: {"status", "live_url"},
    DeleteResult: {"deleted"},
    MetricsResult: {"views", "likes", "comments", "shares", "saves", "watch_time_s", "clicks"},
    RefreshAuthResult: {"expires_at"},
    AssistPack: {"files", "caption", "deep_link", "checklist"},
}

REAL = discover()


def test_registry_loaded_without_errors():
    assert REAL.errors == {}, REAL.errors


@pytest.mark.parametrize("model, keys", APPENDIX_OUTPUT_KEYS.items(), ids=lambda v: getattr(v, "__name__", ""))
def test_output_models_have_exactly_the_appendix_keys(model, keys):
    assert set(model.model_fields) == keys
    assert model.model_config.get("extra") == "forbid"


@pytest.fixture(scope="module", params=["real", "sample"])
def registry(request, sample_registry) -> Registry:
    return REAL if request.param == "real" else sample_registry


def _cases(registry: Registry) -> list[str]:
    return registry.keys()


def test_each_connector_conforms(registry: Registry):
    for key in _cases(registry):
        check_connector(registry, key)


def check_connector(registry: Registry, key: str) -> None:
    manifest = registry.manifest(key)
    cls = registry.classes[key]
    # manifest: Appendix A keys only, re-validates, codes exist
    assert set(manifest.model_dump()) == APPENDIX_MANIFEST_KEYS
    Manifest.model_validate(yaml.safe_load(yaml.safe_dump(manifest.model_dump(mode="json"))))
    assert all(code in ErrorCode for code in manifest.error_map.values())
    # capabilities listed == hooks implemented
    assert cls.implemented_capabilities() == set(manifest.capabilities), (
        f"{key}: manifest lists {sorted(manifest.capabilities)} but hooks implement {sorted(cls.implemented_capabilities())}"
    )
    assert set(manifest.capabilities) <= set(CAPABILITY_HOOKS)
    assert cls.key == key == manifest.key


async def test_http_surface_conforms(registry: Registry, make_settings):
    app = create_app(make_settings(), storage=MemoryStorage(), registry=registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        for key in registry.keys():
            manifest = registry.manifest(key)
            served = await c.get(f"/connector/v1/{key}/manifest")
            assert served.status_code == 200 and set(served.json()) == APPENDIX_MANIFEST_KEYS
            for method, capability in METHOD_CAPABILITY.items():
                if capability is not None and capability not in manifest.capabilities:
                    response = await c.post(f"/connector/v1/{key}/{method}", json={})
                    assert response.status_code == 404, (key, method)
                    assert response.json()["error"]["code"] == "METHOD_NOT_SUPPORTED"
            # validate works for every connector and returns exactly {ok, violations}
            surface = manifest.surfaces[0]
            body = {
                "rendition": {"title": "Conformance", "body": "Hello", "content_type": surface.content_types[0]},
                "destination": {"connector_key": key, "surface": surface.key,
                                "instance": {manifest.instance_param: "http://conformance.invalid/x"}},
            }
            validated = await c.post(f"/connector/v1/{key}/validate", json=body)
            assert validated.status_code == 200, validated.text
            assert set(validated.json()) == {"ok", "violations"}
            # error envelope on a bad body
            bad = await c.post(f"/connector/v1/{key}/validate", json={"rendition": 1})
            assert bad.status_code == 422
            assert set(bad.json()["error"]) == {"code", "message", "retryable", "details"}
