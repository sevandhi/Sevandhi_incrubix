"""Secret redaction for logs, error envelopes and evidence.

Three layers, applied to strings anywhere inside nested dicts/lists/tuples and exception text:
1. key names that look secret (``token``, ``password``, ``authorization`` ...) → the whole value is masked;
2. every live secret value registered in the ``SecretRegistry`` (filled from settings and on every decrypt);
3. well-known token shapes: ``Bearer x``, JWTs (``eyJ...``), Slack ``xox?-``, ``sk-`` keys, and a value after
   ``Authorization:``. After the plain words ``ghost`` / ``token`` only a token-shaped value (16+ characters with a
   digit or punctuation) is masked, so "ghost answered HTTP 401" stays readable.

The registry is bounded: settings secrets are pinned; inline credentials are kept in
least-recently-used order, and each call re-registers the credentials it uses, so active ones are never evicted.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

MASK = "***"

SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[-_]?key|authorization|cookie|jwt|credential|private[-_]?key|signature)",
    re.IGNORECASE,
)
# Keys that match the pattern above but only ever hold non-secret metadata.
SAFE_KEYS = frozenset({"idempotency_key", "secret_set", "token_type", "has_token", "connector_key"})

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?[A-Za-z]+\s+)[^\s\"',;}]{6,}"), r"\1" + MASK),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=:-]{6,}"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(ghost|token)\s+(?=[A-Za-z0-9._~+/=:-]*[0-9._~+/=:-])[A-Za-z0-9._~+/=:-]{16,}"), r"\1 " + MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), MASK),  # JWT
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{6,}"), MASK),  # Slack tokens
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), MASK),  # OpenAI-style keys
    (re.compile(r"(?i)([?&](?:api[-_]?key|token|access_token|key|signature|sig)=)[^&\s\"']+"), r"\1" + MASK),
    (re.compile(r"(?i)(://[^:/\s@]+:)[^@/\s]+(@)"), r"\1" + MASK + r"\2"),  # user:password@host
]


class SecretRegistry:
    """Live secret values to scrub. Thread-safe; values shorter than 4 chars are ignored (too noisy).
    At most ``max_values`` unpinned values are kept (least recently added/used is dropped first)."""

    def __init__(self, max_values: int = 2000) -> None:
        self._pinned: set[str] = set()
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._max = max_values
        self._lock = threading.Lock()
        self._pattern: re.Pattern[str] | None = None

    def add(self, *values: str | None, pinned: bool = False) -> None:
        with self._lock:
            for value in values:
                if not value or len(value) < 4 or value in self._pinned:
                    continue
                if pinned:
                    self._recent.pop(value, None)
                    self._pinned.add(value)
                    self._pattern = None
                elif value in self._recent:
                    self._recent.move_to_end(value)  # still in use: keep it
                else:
                    self._recent[value] = None
                    while len(self._recent) > self._max:
                        self._recent.popitem(last=False)
                    self._pattern = None

    def add_many(self, values: Iterable[str | None], *, pinned: bool = False) -> None:
        self.add(*values, pinned=pinned)

    def clear(self) -> None:
        with self._lock:
            self._pinned.clear()
            self._recent.clear()
            self._pattern = None

    def scrub(self, text: str) -> str:
        with self._lock:
            if not self._pinned and not self._recent:
                return text
            if self._pattern is None:
                ordered = sorted(self._pinned | set(self._recent), key=len, reverse=True)  # longest first
                self._pattern = re.compile("|".join(re.escape(v) for v in ordered))
            pattern = self._pattern
        return pattern.sub(MASK, text)

    def __contains__(self, value: str) -> bool:
        return value in self._pinned or value in self._recent

    def __len__(self) -> int:
        return len(self._pinned) + len(self._recent)


secret_registry = SecretRegistry()


def redact_text(text: str) -> str:
    text = secret_registry.scrub(text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower() not in SAFE_KEYS and bool(SECRET_KEY_RE.search(key))


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a redacted copy of ``value`` (dicts, lists, tuples, strings, exceptions)."""
    if _depth > 20:
        return MASK
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if is_secret_key(key) and item not in (None, "", [], {}) and not isinstance(item, bool):
                out[key] = MASK
            else:
                out[key] = redact(item, _depth + 1)
        return out
    if isinstance(value, list):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, _depth + 1) for item in value)
    if isinstance(value, BaseException):
        return redact_text(f"{type(value).__name__}: {value}")
    if hasattr(value, "get_secret_value"):  # pydantic SecretStr / SecretBytes
        return MASK
    return value
