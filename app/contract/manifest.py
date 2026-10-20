"""Connector manifest: the Appendix A YAML, as a strict Pydantic model plus a generated JSON Schema.

Only the Appendix A keys are allowed (``extra="forbid"``). ``error_map`` keys are an HTTP status
(``"429"``) or a status plus a body token (``"400:rate_limit_error"``); values must be ``ErrorCode``
members. ``capabilities`` use the Appendix A words; ``validate`` is always available and is not listed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.contract.models import ContentType
from app.errors import ErrorCode

SCHEMA_PATH = Path(__file__).with_name("manifest.schema.json")

Capability = Literal["publish", "status", "delete", "metrics", "refresh_auth", "assist"]

# contract method -> the capability that must be listed for it (None = always available)
METHOD_CAPABILITY: dict[str, str | None] = {
    "validate": None,
    "publish": "publish",
    "get_status": "status",
    "delete": "delete",
    "fetch_metrics": "metrics",
    "refresh_auth": "refresh_auth",
    "build_assist_pack": "assist",
}
# URL aliases accepted by the router (Appendix A capability words, common variants)
METHOD_ALIASES: dict[str, str] = {
    "status": "get_status",
    "metrics": "fetch_metrics",
    "assist": "build_assist_pack",
    "assist_pack": "build_assist_pack",
}
# capability -> connector hooks that implement it
CAPABILITY_HOOKS: dict[str, tuple[str, ...]] = {
    "publish": ("_create", "_lookup"),
    "status": ("_get_status",),
    "delete": ("_delete",),
    "metrics": ("_fetch_metrics",),
    "refresh_auth": ("_refresh_auth",),
    "assist": ("_build_assist_pack",),
}

ERROR_MAP_KEY = re.compile(r"^[1-5][0-9]{2}(:[A-Za-z0-9_.\-]+)?$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


COUNT_UNITS = ("codepoints", "graphemes", "utf16", "bytes")


def check_spec_values(spec: dict[str, Any]) -> dict[str, Any]:
    """Values that would otherwise crash the spec check later: a known count_unit and a valid
    tag_pattern regex."""
    if spec.get("count_unit", "codepoints") not in COUNT_UNITS:
        raise ValueError(f"count_unit must be one of {', '.join(COUNT_UNITS)}")
    if spec.get("tag_pattern") is not None:
        try:
            re.compile(str(spec["tag_pattern"]))
        except re.error:
            raise ValueError("tag_pattern is not a valid regular expression") from None
    return spec


class Surface(_Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    content_types: list[ContentType] = Field(min_length=1)
    spec: dict[str, Any] = Field(default_factory=dict)

    @field_validator("spec")
    @classmethod
    def _spec_values(cls, spec: dict[str, Any]) -> dict[str, Any]:
        return check_spec_values(spec)


class RateLimit(_Strict):
    requests: int = Field(gt=0)
    per_seconds: float = Field(gt=0)


class RateLimits(_Strict):
    per_connection: RateLimit | None = None


class Manifest(_Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.\-]+)?$")
    tier: Literal["native", "protocol", "bridge", "feed", "assisted"]
    auth: Literal["oauth2", "api_key", "bot_token", "none"]
    surfaces: list[Surface] = Field(min_length=1)
    rate_limits: RateLimits = Field(default_factory=RateLimits)
    capabilities: list[Capability] = Field(default_factory=list)
    error_map: dict[str, ErrorCode] = Field(default_factory=dict)
    instance_param: str = Field(pattern=r"^[a-z][a-z0-9_]*$")

    @field_validator("error_map")
    @classmethod
    def _check_keys(cls, value: dict[str, ErrorCode]) -> dict[str, ErrorCode]:
        bad = [key for key in value if not ERROR_MAP_KEY.match(key)]
        if bad:
            raise ValueError(f"error_map keys must look like '429' or '400:body_token'; bad keys: {bad}")
        return value

    @field_validator("capabilities")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("capabilities must not repeat")
        return value

    def supports(self, method: str) -> bool:
        capability = METHOD_CAPABILITY.get(method)
        return method in METHOD_CAPABILITY and (capability is None or capability in self.capabilities)

    def surface(self, key: str) -> Surface | None:
        return next((s for s in self.surfaces if s.key == key), None)

    def public_dict(self) -> dict[str, Any]:
        """The manifest as served at ``GET /connector/v1/{key}/manifest`` (Appendix A keys only)."""
        return self.model_dump(mode="json")


def load_manifest(path: Path) -> Manifest:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Manifest.model_validate(data)


def manifest_json_schema() -> dict[str, Any]:
    schema = Manifest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://incubrix.local/schemas/connector-manifest.json"
    schema["title"] = "IncuBrix connector manifest (Appendix A)"
    return schema


def write_json_schema(path: Path = SCHEMA_PATH) -> None:
    path.write_text(json.dumps(manifest_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_json_schema()
    print(f"wrote {SCHEMA_PATH}")
