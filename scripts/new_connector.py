"""Scaffold a new connector from ``app/connectors/_template``.

    uv run python scripts/new_connector.py writefreely

creates
    app/connectors/<key>/{__init__.py, manifest.yaml, connector.py}
    mocks/platforms/routes/<key>.py          (a generic mock with fault injection + counters)
    tests/integration/test_<key>_connector.py

The connector is registered automatically (no central list). Edit the manifest and hooks to match
the real platform, then run ``uv run pytest tests/contract tests/integration/test_<key>_connector.py``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "app" / "connectors" / "_template"
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def scaffold(key: str, root: Path = ROOT, *, package_dir: Path | None = None, force: bool = False) -> list[Path]:
    """Write the files for ``key`` under ``root``; returns the paths written."""
    if not KEY_RE.match(key):
        raise ValueError("key must be lower-case letters, digits or '_' and start with a letter")
    connector_dir = package_dir or root / "app" / "connectors" / key
    targets = {
        connector_dir / "__init__.py": f'"""{key} connector."""\n',
        connector_dir / "manifest.yaml": (TEMPLATE / "manifest.yaml").read_text(),
        connector_dir / "connector.py": (TEMPLATE / "connector.py").read_text(),
        root / "mocks" / "platforms" / "routes" / f"{key}.py": (TEMPLATE / "mock_route.py.tmpl").read_text(),
        root / "tests" / "integration" / f"test_{key}_connector.py": (TEMPLATE / "test_connector.py.tmpl").read_text(),
    }
    existing = [p for p in targets if p.exists()]
    if existing and not force:
        raise FileExistsError(f"refusing to overwrite: {', '.join(str(p) for p in existing)}")
    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.replace("__KEY__", key), encoding="utf-8")
    return list(targets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("key", help="connector key, e.g. writefreely")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)
    try:
        written = scaffold(args.key, force=args.force)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
