"""Network side of the catalogue import (ADV1): polite fetching, robots.txt, reachability and platform detection.

``Fetcher`` is the only way the importer touches the network. It is the shared
``app.reliability.polite_fetch.PoliteFetcher`` (rules capture uses the same one) with the importer's user agent:

* every hop of every request (redirects are followed by hand, at most 5) is checked by the one SSRF guard,
  ``app.reliability.safe_fetch.check_url``: only public http(s) addresses, never internal services, and our own
  compose hosts only in simulation or dev/test runs;
* a global limit of 50 requests in flight and at most 4 per site, a 5 s timeout per request, bodies capped at 256 KiB;
* public sites also get at least ``public_min_interval_s`` (1 s) between two requests (the research etiquette);
* robots.txt is fetched once per site and respected for every URL (RFC 9309: 4xx = no rules, 5xx or no answer =
  everything disallowed);
* reachability is ``HEAD`` first, then ``GET`` when HEAD is refused (405/501/other 4xx) or when the page is needed.

A *site* is scheme + host + port. On our own mock-platforms host, the first two path segments are part of the site
(``/internet/<site>``), because each mock tenant stands for a separate web site there.

Platform detection (plan §7): a hint from the home page (``generator`` / ``og:platform`` meta) is probed first,
then Discourse ``/about.json``, Lemmy ``/api/v3/site``, PeerTube ``/api/v1/config`` and Forem
``/api/articles?per_page=1``; Ghost is recognised from its generator meta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings
from app.reliability.polite_fetch import MAX_REDIRECTS, Page, PoliteFetcher, Robots

__all__ = ["MAX_REDIRECTS", "Fetcher", "Page", "Robots", "detect_platform", "page_meta", "platform_hint"]

USER_AGENT = "IncuBrix-catalogue-import/0.1 (+read-only reachability check)"
GLOBAL_LIMIT = 50
PER_SITE_LIMIT = 4
TIMEOUT_S = 5.0
MAX_BODY = 256 * 1024

# platform -> (probe path, test on the JSON)
PROBES: list[tuple[str, str]] = [
    ("discourse", "/about.json"),
    ("lemmy", "/api/v3/site"),
    ("peertube", "/api/v1/config"),
    ("forem", "/api/articles?per_page=1"),
]


def _is_discourse(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("about"), dict)


def _is_lemmy(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("site_view"), dict)


def _is_peertube(data: Any) -> bool:
    return isinstance(data, dict) and "serverVersion" in data and isinstance(data.get("instance"), dict)


def _is_forem(data: Any) -> bool:
    return isinstance(data, list) and bool(data) and isinstance(data[0], dict) and data[0].get("type_of") == "article"


PROBE_TESTS = {"discourse": _is_discourse, "lemmy": _is_lemmy, "peertube": _is_peertube, "forem": _is_forem}


class Fetcher(PoliteFetcher):
    """The importer's fetcher: the shared ``PoliteFetcher`` with the importer's user agent and limits."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        global_limit: int = GLOBAL_LIMIT,
        per_site: int = PER_SITE_LIMIT,
        timeout_s: float = TIMEOUT_S,
        public_min_interval_s: float = 1.0,
    ) -> None:
        super().__init__(settings, user_agent=USER_AGENT, client=client, global_limit=global_limit, per_site=per_site,
                         timeout_s=timeout_s, public_min_interval_s=public_min_interval_s, max_body=MAX_BODY)


# ------------------------------------------------------------------ page meta and detection

_META = re.compile(r"<meta\s+[^>]*>", re.I | re.S)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HTML_LANG = re.compile(r"<html[^>]*\blang\s*=\s*[\"']?([A-Za-z-]+)", re.I)
_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


@dataclass
class PageMeta:
    title: str | None = None
    description: str | None = None
    lang: str | None = None
    generator: str | None = None
    og_platform: str | None = None
    text: str = ""


def page_meta(html: str) -> PageMeta:
    """Title, description, ``<html lang>``, generator and a short visible-text sample (no HTML library needed)."""
    from html import unescape

    meta = PageMeta()
    match = _TITLE.search(html)
    if match:
        meta.title = unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:300] or None
    match = _HTML_LANG.search(html)
    if match:
        meta.lang = match.group(1)
    for tag in _META.findall(html):
        attrs = {m.group(1).lower(): unescape(m.group(3) if m.group(3) is not None else m.group(4) or "") for m in _ATTR.finditer(tag)}
        key = (attrs.get("name") or attrs.get("property") or "").lower()
        content = attrs.get("content", "").strip()
        if key in ("description", "og:description") and content and not meta.description:
            meta.description = content[:500]
        elif key == "generator" and content:
            meta.generator = content[:200]
        elif key == "og:platform" and content:
            meta.og_platform = content[:100]
    body = _TAGS.sub(" ", html)
    meta.text = unescape(re.sub(r"\s+", " ", body)).strip()[:2000]
    return meta


def platform_hint(meta: PageMeta | None) -> str | None:
    if meta is None:
        return None
    generator = (meta.generator or "").lower()
    if generator.startswith("discourse"):
        return "discourse"
    if generator.startswith("ghost"):
        return "ghost"
    if (meta.og_platform or "").lower() == "peertube":
        return "peertube"
    return None


@dataclass
class Detection:
    platform: str | None
    evidence: str | None = None
    probe_data: dict[str, Any] = field(default_factory=dict)  # platform -> JSON of a successful probe (reused by enrichment)
    probes_tried: list[str] = field(default_factory=list)


async def detect_platform(fetcher: Fetcher, url: str, meta: PageMeta | None) -> Detection:
    """Which platform serves ``url``: hint first, then the probes in plan order. ``platform`` None = not detected."""
    root = fetcher.site_root(url)
    hint = platform_hint(meta)
    if hint == "ghost":
        return Detection("ghost", evidence=f"generator meta '{meta.generator if meta else ''}'")
    order = [p for p in PROBES if p[0] == hint] + [p for p in PROBES if p[0] != hint]
    result = Detection(None)
    for platform, path in order:
        result.probes_tried.append(platform)
        page = await fetcher.get(root + path)
        if page.error == "TIMEOUT":
            continue
        if page.ok:
            data = page.json()
            if PROBE_TESTS[platform](data):
                result.platform = platform
                result.evidence = f"GET {path} answered like {platform}"
                result.probe_data[platform] = data
                return result
    if hint:
        result.platform = hint
        result.evidence = f"page meta says {hint}; no probe confirmed it"
    return result
