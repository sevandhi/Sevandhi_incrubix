"""Server-rendered pages (Jinja2) for the human steps, e.g. confirming captured posting rules.

Autoescaping is always on: the pages show text fetched from third-party rules pages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_ENV = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True,
                   undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


def render(name: str, **context: Any) -> str:
    return _ENV.get_template(name).render(**context)
