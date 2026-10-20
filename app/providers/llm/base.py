"""Shared pieces of the LLM adapters: the provider interface, errors, the call record (``LLMMeta``) and
strict JSON parsing / validation.

A provider only turns chat messages into text. Parsing, schema validation, retries and fallback live in
``chain.py``, so every provider (hosted, local or the mock) is held to the same rules.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class LLMError(Exception):
    """One provider call failed. ``kind``: unavailable | timeout | rate_limited | http_error | bad_output."""

    def __init__(self, kind: str, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after_s = retry_after_s


class LLMUnavailableError(Exception):
    """No provider in the chain produced a valid answer (and the caller gave no deterministic mock)."""

    def __init__(self, message: str, meta: LLMMeta) -> None:
        super().__init__(message)
        self.meta = meta


@dataclass
class Completion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    data: Any = None  # the mock returns the object itself (no text round trip)


class Provider(ABC):
    """One chat provider. ``available()`` says whether it may be called at all (key present ...)."""

    name: str = "base"
    model: str = ""
    free_tier_terms: str = ""

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    @abstractmethod
    async def complete(self, messages: list[dict[str, str]], *, json_schema: dict[str, Any] | None,
                       max_tokens: int) -> Completion: ...


class Attempt(BaseModel):
    provider: str
    model: str
    outcome: str  # ok | skipped | unavailable | timeout | rate_limited | http_error | bad_output
    detail: str | None = None
    latency_ms: float | None = None


class LLMMeta(BaseModel):
    """What one ``complete_json`` call used. Goes into the job manifest (IS9) as a provider entry."""

    provider: str
    model: str
    fallback_used: bool = False
    attempts: list[Attempt] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0  # free tiers and local models only: always 0
    latency_ms: float = 0.0
    requested: str = "mock"  # LLM_PROVIDER as configured
    data_terms: str = ""

    def manifest_entry(self, task: str) -> dict[str, Any]:
        return {
            "name": "llm",
            "task": task,
            "provider": self.provider,
            "model": self.model,
            "version": self.model,
            "fallback_used": self.fallback_used,
            "attempts": [a.model_dump(exclude_none=True) for a in self.attempts],
            "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens, "total": self.total_tokens},
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "data_terms": self.data_terms,
        }


# ------------------------------------------------------------------ JSON text -> object

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S | re.I)


def parse_json_object(text: str) -> dict[str, Any]:
    """The one JSON object in a model's reply. Accepts a ```json fence around it; nothing else is repaired."""
    text = (text or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("bad_output", "the reply is not JSON") from None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            raise LLMError("bad_output", "the reply is not valid JSON") from None
    if not isinstance(value, dict):
        raise LLMError("bad_output", "the reply is JSON but not an object")
    return value


# ------------------------------------------------------------------ schemas: a Pydantic model or a JSON-schema dict


def schema_dict(schema: type[BaseModel] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(schema, dict):
        return schema
    return schema.model_json_schema()


def validate_object(schema: type[BaseModel] | dict[str, Any], value: Any) -> Any:
    """Validate ``value``; a Pydantic model class returns the model instance, a dict schema the checked dict.
    Raises ``LLMError("bad_output")`` with a short reason (never the value itself)."""
    if isinstance(schema, dict):
        problems = check_json_schema(schema, value)
        if problems:
            raise LLMError("bad_output", "schema: " + "; ".join(problems[:5]))
        return value
    try:
        if isinstance(value, schema):
            return value
        return schema.model_validate(value)
    except ValidationError as exc:
        reasons = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:5]]
        raise LLMError("bad_output", "schema: " + "; ".join(reasons)) from None


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def check_json_schema(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """A small strict subset of JSON Schema (type, enum, const, properties, required, additionalProperties,
    items, minimum/maximum, minLength/maxLength, anyOf). Enough for LLM output contracts; returns problems."""
    problems: list[str] = []
    if "anyOf" in schema:
        if all(check_json_schema(option, value, path) for option in schema["anyOf"]):
            problems.append(f"{path}: matches none of anyOf")
        return problems
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        ok = any(isinstance(value, _TYPES.get(k, ())) and not (k in ("integer", "number") and isinstance(value, bool))
                 for k in kinds)
        if not ok:
            return [f"{path}: expected {'/'.join(kinds)}"]
    if "enum" in schema and not any(v == value and isinstance(v, bool) == isinstance(value, bool) for v in schema["enum"]):
        problems.append(f"{path}: not one of the allowed values")
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}: wrong constant")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{path}.{name}: required")
        if schema.get("additionalProperties") is False:
            problems.extend(f"{path}.{name}: not allowed" for name in value if name not in props)
        for name, sub in props.items():
            if name in value:
                problems.extend(check_json_schema(sub, value[name], f"{path}.{name}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            problems.extend(check_json_schema(schema["items"], item, f"{path}[{i}]"))
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: above maximum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: too long")
    return problems
