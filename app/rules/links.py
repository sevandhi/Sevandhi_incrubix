"""Finding links in a rendition and matching them against domains.

A "link" is anything a reader could click: ``link_url``, URLs in the title or body (markdown, HTML,
Slack ``<url|text>`` or bare ``https://...``), ``www.`` addresses and bare domains such as
``example.com/page`` (many forums auto-link those). ``canonical_url`` is metadata and media URLs are
attachments, so neither counts as a link in the post.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

_SCHEME_OR_WWW = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)[^\s<>\"'`|\]\[]+")
_BARE_TLDS = (
    "com|net|org|io|ai|dev|co|app|me|ly|gl|gg|tv|xyz|link|site|blog|info|biz|in|uk|de|fr|es|pt|br|jp|us|ca|au|"
    "be|to|cc|so|is|it|nl|eu|ch|at|se|no|fi|pl|ru|cn|news|page|club|online|store|shop|tech|video"
)
_BARE_DOMAIN = re.compile(
    rf"(?i)(?<![\w@/.:-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{_BARE_TLDS})(?:/[^\s<>\"'`|\]\[]*)?(?![\w@-])"
)

SHORTENER_HOSTS = frozenset(
    """bit.ly bitly.com tinyurl.com t.co goo.gl ow.ly is.gd v.gd buff.ly rebrand.ly cutt.ly shorturl.at rb.gy t.ly
    tiny.cc bl.ink lnkd.in s.id dlvr.it trib.al fb.me adf.ly shorte.st cli.gs x.co po.st qr.ae soo.gd
    amzn.to amzn.eu tr.im su.pr j.mp short.io shorturl.com""".split()
)


@dataclass(frozen=True)
class Link:
    field: str  # "title", "body" or "link_url"
    url: str  # as written (trailing punctuation removed)

    @property
    def host(self) -> str:
        return url_host(self.url)


def _clean(url: str) -> str:
    url = url.rstrip(".,;:!?'\"…")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(".,;:!?'\"…")
    return url


def find_urls(text: str | None) -> list[str]:
    """Every URL-like string in free text, in order of appearance, without duplicates."""
    if not text:
        return []
    found: list[tuple[int, str]] = []
    covered: list[tuple[int, int]] = []
    for match in _SCHEME_OR_WWW.finditer(text):
        found.append((match.start(), _clean(match.group())))
        covered.append(match.span())
    for match in _BARE_DOMAIN.finditer(text):
        if not any(start <= match.start() < end for start, end in covered):
            found.append((match.start(), _clean(match.group())))
    out: list[str] = []
    for _, url in sorted(found):
        if url and url not in out:
            out.append(url)
    return out


def extract_links(rendition) -> list[Link]:
    """All clickable links in a rendition (title, body, link_url)."""
    links: list[Link] = []
    for field in ("title", "body"):
        links += [Link(field, url) for url in find_urls(getattr(rendition, field, None))]
    if rendition.link_url:
        links.append(Link("link_url", rendition.link_url.strip()))
    return links


def split_url(url: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (host without www., lower-case path, query pairs). Works for scheme-less URLs too."""
    text = url.strip()
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "", "", []
    if host.startswith("www."):
        host = host[4:]
    return host, parts.path.lower(), parse_qsl(parts.query, keep_blank_values=True)


def url_host(url: str) -> str:
    return split_url(url)[0]


def matches_domain(url: str, entry: str) -> bool:
    """True if ``url`` is on ``entry``'s domain (or a sub-domain). An entry with a path,
    like ``youtube.com/@creator``, also requires the URL path to start with that path."""
    host, path, _ = split_url(url)
    entry_host, entry_path, _ = split_url(entry)
    if not host or not entry_host:
        return False
    if host != entry_host and not host.endswith("." + entry_host):
        return False
    entry_path = entry_path.rstrip("/")
    return not entry_path or path == entry_path or path.startswith(entry_path + "/")


def is_shortener(url: str) -> bool:
    return url_host(url) in SHORTENER_HOSTS
