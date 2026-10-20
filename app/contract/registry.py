"""Connector registry: finds every ``app/connectors/<key>/`` package with ``manifest.yaml`` + ``connector.py``.

Adding a connector is "drop a folder in"; no central list to edit. Packages whose name starts with
``_`` (the template) are skipped. Problems are collected in ``errors`` instead of crashing the API,
and the conformance tests assert that ``errors`` is empty.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.contract.manifest import Manifest, load_manifest

if TYPE_CHECKING:
    from app.connectors.base import BaseConnector, ConnectorContext


@dataclass
class Registry:
    manifests: dict[str, Manifest] = field(default_factory=dict)
    classes: dict[str, type[BaseConnector]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def keys(self) -> list[str]:
        return sorted(self.manifests)

    def manifest(self, key: str) -> Manifest | None:
        return self.manifests.get(key)

    def add(self, manifest: Manifest, cls: type[BaseConnector]) -> None:
        cls.manifest = manifest
        self.manifests[manifest.key] = manifest
        self.classes[manifest.key] = cls

    def build(self, key: str, ctx: ConnectorContext) -> BaseConnector:
        """A new connector instance bound to ``ctx`` (the app caches one per key)."""
        return self.classes[key](ctx)


def discover(package: str = "app.connectors", path: Path | None = None) -> Registry:
    """Load every connector package under ``package`` (directory ``path``)."""
    from app.connectors.base import BaseConnector

    registry = Registry()
    root = path or Path(importlib.import_module(package).__file__ or "").parent
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        name = folder.name
        if name.startswith(("_", ".")) or not (folder / "manifest.yaml").exists():
            continue
        try:
            manifest = load_manifest(folder / "manifest.yaml")
            if manifest.key != name:
                raise ValueError(f"manifest key '{manifest.key}' must equal the folder name '{name}'")
            module = importlib.import_module(f"{package}.{name}.connector")
            cls = getattr(module, "Connector", None)
            if not (isinstance(cls, type) and issubclass(cls, BaseConnector)):
                raise TypeError("connector.py must export `Connector`, a BaseConnector subclass")
            if getattr(cls, "key", name) not in (name, ""):
                raise ValueError(f"Connector.key '{cls.key}' must equal '{name}'")
            cls.key = name
            registry.add(manifest, cls)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal: one bad connector must not take the API down
            registry.errors[name] = f"{type(exc).__name__}: {exc}"
    return registry


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = discover()
    return _registry


def set_registry(registry: Registry | None) -> None:
    """Replace the process registry (tests); ``None`` forces rediscovery on next use."""
    global _registry
    _registry = registry
