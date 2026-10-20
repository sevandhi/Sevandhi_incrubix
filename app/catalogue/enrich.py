"""Enrichment for imported destinations (ADV1): member counts and language.

Member counts come from the platform's own public API, read-only:

| platform  | request                                  | field                                  |
|-----------|------------------------------------------|----------------------------------------|
| discourse | GET <site>/about.json                    | about.stats.users_count (or user_count) |
| lemmy     | GET <site>/api/v3/community?name=<name>  | community_view.counts.subscribers      |
|           | (no community in the URL) /api/v3/site   | site_view.counts.users                 |
| peertube  | GET <site>/api/v1/server/stats           | totalUsers                             |

Forem tags, Ghost sites and plain web pages publish no member count; ``est_audience`` then stays as the row gave it
(or empty). A count is never estimated.

Language: the row's own ``language`` wins. Otherwise the page's title, description and text are classified with
lingua (``lingua-language-detector``, Apache-2.0) restricted to the languages below; when lingua is not installed
the rules engine's small built-in guesser is used and the result says so (``detector``).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from app.catalogue.detect import Fetcher
from app.rules.lang import normalize_lang

LINGUA_LANGS = ("en", "es", "de", "fr", "pt", "it", "nl", "ja", "zh", "ru", "ko", "pl", "sv", "da", "nb", "tr", "ar", "hi",
                "uk", "cs", "fi", "id", "vi")
MIN_TEXT = 12
MIN_CONFIDENCE = 0.5


@dataclass
class Enrichment:
    est_audience: int | None = None
    source: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def lemmy_community(url: str) -> str | None:
    """``/c/<name>`` (or ``/c/<name>@<host>``) in a Lemmy URL, else None."""
    match = re.search(r"/c/([^/?#]+)", urlsplit(url).path)
    return unquote(match.group(1)) if match else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


async def enrich(fetcher: Fetcher, platform: str | None, url: str, probe_data: dict[str, Any] | None = None) -> Enrichment:
    """Member count for ``url`` on ``platform``. Never raises: failures are in ``error``."""
    probe_data = probe_data or {}
    root = fetcher.site_root(url)
    if platform == "discourse":
        data = probe_data.get("discourse")
        if data is None:
            page = await fetcher.get(root + "/about.json")
            if not page.ok:
                return Enrichment(error=page.error or f"HTTP_{page.status}")
            data = page.json()
        stats = ((data or {}).get("about") or {}).get("stats") or {}
        count = _int(stats.get("users_count", stats.get("user_count")))
        return Enrichment(count, "discourse:about.json users_count",
                          {"active_users_30_days": _int(stats.get("active_users_30_days"))}, None if count is not None else "NO_COUNT")
    if platform == "lemmy":
        name = lemmy_community(url)
        if name:
            page = await fetcher.get(f"{root}/api/v3/community?name={quote(name, safe='@')}")
            if not page.ok:
                return Enrichment(error=page.error or f"HTTP_{page.status}")
            view = (page.json() or {}).get("community_view") or {}
            count = _int((view.get("counts") or {}).get("subscribers"))
            return Enrichment(count, "lemmy:/api/v3/community subscribers", {"community": name},
                              None if count is not None else "NO_COUNT")
        data = probe_data.get("lemmy")
        if data is None:
            page = await fetcher.get(root + "/api/v3/site")
            if not page.ok:
                return Enrichment(error=page.error or f"HTTP_{page.status}")
            data = page.json()
        count = _int((((data or {}).get("site_view") or {}).get("counts") or {}).get("users"))
        return Enrichment(count, "lemmy:/api/v3/site users", {}, None if count is not None else "NO_COUNT")
    if platform == "peertube":
        page = await fetcher.get(root + "/api/v1/server/stats")
        if not page.ok:
            return Enrichment(error=page.error or f"HTTP_{page.status}")
        data = page.json() or {}
        count = _int(data.get("totalUsers")) if isinstance(data, dict) else None
        return Enrichment(count, "peertube:/api/v1/server/stats totalUsers",
                          {"total_local_videos": _int(data.get("totalLocalVideos")) if isinstance(data, dict) else None},
                          None if count is not None else "NO_COUNT")
    return Enrichment(source=None, error=None)


# ------------------------------------------------------------------ language


class _Lingua:
    """A lazily built lingua detector (thread-safe; building loads the models once)."""

    _lock = threading.Lock()
    _detector: Any = None
    _failed = False

    @classmethod
    def get(cls) -> Any:
        if cls._detector is not None or cls._failed:
            return cls._detector
        with cls._lock:
            if cls._detector is None and not cls._failed:
                try:
                    from lingua import IsoCode639_1, Language, LanguageDetectorBuilder

                    langs = []
                    for code in LINGUA_LANGS:
                        try:
                            langs.append(Language.from_iso_code_639_1(getattr(IsoCode639_1, code.upper())))
                        except (AttributeError, ValueError):
                            continue
                    cls._detector = LanguageDetectorBuilder.from_languages(*langs).with_preloaded_language_models().build()
                except Exception:  # noqa: BLE001 - lingua missing or broken: fall back to the built-in guesser
                    cls._failed = True
        return cls._detector


def detector_name() -> str:
    return "lingua" if _Lingua.get() is not None else "builtin"


def warm_up() -> str:
    """Build the detector now (call it in a thread before the event loop is busy)."""
    return detector_name()


def detect_language(text: str) -> tuple[str | None, float | None, str]:
    """(language, confidence, detector) for ``text``; language None when unsure or the text is too short.

    Blocking (lingua runs native code): call it with ``asyncio.to_thread`` from async code.
    """
    sample = re.sub(r"\s+", " ", text or "").strip()[:1000]
    letters = sum(ch.isalpha() for ch in sample)
    if letters < MIN_TEXT:
        return None, None, detector_name()
    detector = _Lingua.get()
    if detector is not None:
        values = detector.compute_language_confidence_values(sample)
        if not values:
            return None, None, "lingua"
        top = values[0]
        code = normalize_lang(top.language.iso_code_639_1.name.lower())
        if code == "nb":
            code = "no"
        confidence = round(float(top.value), 3)
        return (code if confidence >= MIN_CONFIDENCE else None), confidence, "lingua"
    from app.rules.lang import detect_language as builtin

    code = builtin(sample)
    return code, None, "builtin"
