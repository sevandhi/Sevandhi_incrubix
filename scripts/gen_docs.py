"""Regenerate the parts of the docs that are copied from code, so they can never drift.

    uv run python scripts/gen_docs.py            # rewrite every generated block in README.md and docs/*.md
    uv run python scripts/gen_docs.py --check    # exit 1 if any block is out of date (a test runs this)
    uv run python scripts/gen_docs.py --print settings   # print one block to stdout

A generated block sits between two markers in any Markdown file:

    <!-- BEGIN GENERATED: errors -->
    ...
    <!-- END GENERATED: errors -->

Blocks:
- ``errors``            every error code, from ``app.errors.error_table_markdown()`` (CATALOGUE)
- ``settings``          every setting with its default and comment, from ``app/config.py`` (Settings) and
                        ``app/providers/llm/config.py`` (LLMSettings); secret defaults are never printed
- ``connectors``        one row per connector manifest (tier, auth, instance_param, capabilities, surfaces)
- ``connector_errors``  every manifest ``error_map`` entry, per connector
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
BLOCK = re.compile(r"(<!-- BEGIN GENERATED: (?P<name>[a-z_]+) -->\n)(?P<body>.*?)(<!-- END GENERATED: (?P=name) -->)", re.S)
SETTING_LINE = re.compile(r"^    (?P<name>[A-Z][A-Z0-9_]*): [^=]+?(?:= (?P<default>.*?))?\s*(?:#\s*(?P<comment>.*))?$")
SECTION_LINE = re.compile(r"^    # --- (?P<title>.+?)(?: ---.*)?$")


def _import_app() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------------------------- errors
def gen_errors() -> str:
    _import_app()
    from app.errors import error_table_markdown

    return error_table_markdown()


# -------------------------------------------------------------------------------------------- settings
def _source_notes(path: Path) -> dict[str, tuple[str, str]]:
    """name -> (section, comment) read from the settings class source (comments are not in the model)."""
    notes: dict[str, tuple[str, str]] = {}
    section = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        sec = SECTION_LINE.match(line)
        if sec:
            section = sec.group("title").strip().rstrip(".")
            continue
        m = SETTING_LINE.match(line)
        if m:
            comment = (m.group("comment") or "").strip()
            comment = re.sub(r"\bnoqa: \S+( - )?", "", comment).strip(" -")
            notes[m.group("name")] = (section, comment)
    return notes


def _env_example_notes() -> dict[str, str]:
    """name -> the comment lines directly above it in .env.example (fallback when the code has no comment)."""
    notes: dict[str, str] = {}
    pending: list[str] = []
    path = ROOT / ".env.example"
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        stripped = line.strip()
        if stripped.startswith("# ---") or not stripped:
            pending = []
        elif stripped.startswith("#"):
            pending.append(stripped.lstrip("# ").strip())
        elif "=" in stripped:
            if pending:
                notes[stripped.split("=", 1)[0].strip()] = " ".join(pending)
            pending = []
    return notes


def _fmt_default(value: Any, secret: bool) -> str:
    if secret:
        return "*(secret; empty)*" if value is None else "*(secret; dev placeholder)*"
    if value is None:
        return "*(empty)*"
    if isinstance(value, list):
        return "`" + ",".join(str(v) for v in value) + "`" if value else "*(empty)*"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    text = re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", str(value))  # never print a password inside a URL
    return f"`{text}`"


def _settings_rows(cls: Any, source: Path, owner: str) -> list[str]:
    from pydantic import SecretStr

    notes = _source_notes(source)
    env_notes = _env_example_notes()
    rows = []
    for name, field in cls.model_fields.items():
        annotation = str(field.annotation)
        secret = "SecretStr" in annotation or field.annotation is SecretStr
        default = field.get_default(call_default_factory=True)
        section, comment = notes.get(name, ("", ""))
        comment = comment or env_notes.get(name, "")
        if get_origin(field.annotation) is Literal:
            comment = f"one of: {', '.join(get_args(field.annotation))}. {comment}".strip()
        section = re.sub(r"\s*\(.*?\)", "", section).strip()
        section = section.split(". ")[0].rstrip(".")
        comment = comment.replace("|", "\\|")
        rows.append(f"| `{name}` | {_fmt_default(default, secret)} | {section or owner} | {comment} |")
    return rows


def gen_settings() -> str:
    _import_app()
    from app.config import Settings
    from app.providers.llm.config import LLMSettings

    header = ["| Setting | Default | Group | Notes |", "|---|---|---|---|"]
    main = _settings_rows(Settings, ROOT / "app/config.py", "service")
    seen = set(Settings.model_fields)
    llm_cls = type("_LLMOnly", (), {})  # filter LLMSettings fields that Settings already lists
    llm_cls.model_fields = {k: v for k, v in LLMSettings.model_fields.items() if k not in seen}
    llm = _settings_rows(llm_cls, ROOT / "app/providers/llm/config.py", "LLM providers")
    return "\n".join(header + main + llm)


# ------------------------------------------------------------------------------------------ connectors
def _manifests() -> list[Any]:
    _import_app()
    from app.contract.manifest import load_manifest

    found = []
    for path in sorted((ROOT / "app/connectors").glob("*/manifest.yaml")):
        if path.parent.name.startswith(("_", ".")):
            continue
        found.append(load_manifest(path))
    return found


def gen_connectors() -> str:
    rows = ["| Connector | Tier | Auth | instance_param | Capabilities | Surfaces |", "|---|---|---|---|---|---|"]
    for m in _manifests():
        surfaces = ", ".join(s.key for s in m.surfaces)
        caps = ", ".join(m.capabilities) or "-"
        rows.append(f"| `{m.key}` | {m.tier} | {m.auth} | `{m.instance_param}` | {caps} | {surfaces} |")
    return "\n".join(rows)


def gen_connector_errors() -> str:
    rows = ["| Connector | Platform answer (`status` or `status:body_token`) | Shared code |", "|---|---|---|"]
    for m in _manifests():
        if not m.error_map:
            rows.append(f"| `{m.key}` | *(none: base defaults only)* | |")
            continue
        for key, code in m.error_map.items():
            rows.append(f"| `{m.key}` | `{key}` | `{code.value}` |")
    return "\n".join(rows)


GENERATORS = {
    "errors": gen_errors,
    "settings": gen_settings,
    "connectors": gen_connectors,
    "connector_errors": gen_connector_errors,
}


def render(text: str, cache: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in GENERATORS:
            raise SystemExit(f"unknown generated block '{name}'")
        if name not in cache:
            cache[name] = GENERATORS[name]().rstrip("\n") + "\n"
        return match.group(1) + cache[name] + match.group(4)

    return BLOCK.sub(replace, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if a generated block is stale")
    parser.add_argument("--print", dest="block", choices=sorted(GENERATORS), help="print one block and exit")
    args = parser.parse_args(argv)
    if args.block:
        print(GENERATORS[args.block]())
        return 0
    cache: dict[str, str] = {}
    stale = []
    for path in TARGETS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        new = render(text, cache)
        if new != text:
            stale.append(path.relative_to(ROOT))
            if not args.check:
                path.write_text(new, encoding="utf-8")
    if args.check:
        if stale:
            print("generated doc blocks are out of date in: " + ", ".join(map(str, stale))
                  + " (run: uv run python scripts/gen_docs.py)", file=sys.stderr)
            return 1
        print("generated doc blocks are up to date")
        return 0
    print("updated: " + (", ".join(map(str, stale)) if stale else "nothing (already up to date)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
