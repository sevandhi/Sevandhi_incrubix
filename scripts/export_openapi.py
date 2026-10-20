"""Export the API's OpenAPI 3.1 document to ``openapi.yaml`` (IS2).

    uv run python scripts/export_openapi.py            # write openapi.yaml
    uv run python scripts/export_openapi.py --check    # exit 1 if openapi.yaml is out of date

The document is built from the app with fixed settings, so it does not depend on the local ``.env``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "openapi.yaml"


def build_spec() -> dict:
    sys.path.insert(0, str(ROOT))
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(_env_file=None, SERVICE_VERSION="0.1.0"))
    return app.openapi()


def render(spec: dict) -> str:
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render(build_spec())
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("openapi.yaml is out of date: run uv run python scripts/export_openapi.py", file=sys.stderr)
            return 1
        print("openapi.yaml is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
