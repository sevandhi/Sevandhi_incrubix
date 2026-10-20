"""The README / docs tables copied from code (errors, settings, connector manifests) must match the code."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _gen_docs():
    spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "scripts" / "gen_docs.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_generated_doc_blocks_are_up_to_date(capsys):
    assert _gen_docs().main(["--check"]) == 0, capsys.readouterr().err


def test_readme_lists_every_error_code_and_setting():
    from app.config import Settings
    from app.errors import ErrorCode

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert all(f"`{code.value}`" in readme for code in ErrorCode)
    assert all(f"`{name}`" in readme for name in Settings.model_fields)


def test_settings_table_never_prints_a_secret_default():
    table = _gen_docs().gen_settings()
    for placeholder in ("ibx-dev-token", "ibx-dev-access", "ibx-dev-secret", "ibx-dev-password"):
        assert placeholder not in table
