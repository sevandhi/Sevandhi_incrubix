"""Apply a Decision's fixes to a rendition (used by the contract publish path and by tailoring)."""

from __future__ import annotations

from app.contract.models import Rendition
from app.rules.disclosure import insert_disclosure
from app.rules.models import Fix


def apply_fixes(rendition: Rendition, fixes: list[Fix]) -> Rendition:
    """Return a new Rendition with every fix applied. Unknown fix kinds raise, never pass silently."""
    body = rendition.body
    for fix in fixes:
        if fix.kind == "insert_disclosure":
            body = insert_disclosure(body, fix.text, fix.placement, rendition.body_format)
        else:  # pragma: no cover - Fix.kind is a Literal, this guards future kinds
            raise ValueError(f"unknown fix kind {fix.kind!r}")
    return rendition.model_copy(update={"body": body})
