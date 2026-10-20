"""ISO-8601 durations (``P7D``, ``PT12H``, ``P1W``) used by frequency caps.

Only the subset that posting rules need is supported. Years and months have no fixed length,
so ``P1M`` is read as 30 days and ``P1Y`` as 365 days (documented in docs/RULES.md).
"""

from __future__ import annotations

import re
from datetime import timedelta

_DURATION = re.compile(
    r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

# Friendly words accepted in catalogue rows ("1/week").
_WORDS = {"hour": "PT1H", "day": "P1D", "week": "P7D", "month": "P30D", "year": "P365D"}


def parse_duration(value: str) -> timedelta:
    """Parse an ISO-8601 duration. Raises ValueError for anything else, or for a zero duration."""
    text = value.strip().upper()
    text = _WORDS.get(text.lower(), text)
    match = _DURATION.match(text)
    if not match or text in ("P", "PT"):
        raise ValueError(f"not an ISO-8601 duration: {value!r} (expected e.g. P7D or PT12H)")
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    delta = timedelta(
        days=parts["years"] * 365 + parts["months"] * 30 + parts["weeks"] * 7 + parts["days"],
        hours=parts["hours"],
        minutes=parts["minutes"],
        seconds=parts["seconds"],
    )
    if delta <= timedelta(0):
        raise ValueError(f"duration must be positive: {value!r}")
    return delta


def normalize_duration(value: str) -> str:
    """Validate and return the canonical upper-case form (``"week"`` becomes ``"P7D"``)."""
    parse_duration(value)
    text = value.strip()
    return _WORDS.get(text.lower(), text.upper())
