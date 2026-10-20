"""URL normalisation for the catalogue (dedupe key) and for instance values (what connectors call).

    normalize_url("HTTP://WWW.Forum.Example.org:443/c/AI/?utm_source=x#top") -> "https://forum.example.org/c/AI"
    clean_instance_url("http://mock-platforms:8080/discourse/forum-41/")       -> "http://mock-platforms:8080/discourse/forum-41"

``normalize_url`` is the canonical form used to detect duplicates: lower-case host, IDNA (punycode),
``www.`` stripped, default port dropped, trailing slash, fragment and ``utm_*`` parameters removed, and
the scheme made ``https`` (the same site over http and https is one destination).
``clean_instance_url`` applies the same cleaning but keeps the scheme and port, because a connector must
call the URL that really answers (local instances and mocks speak plain http).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.errors import ErrorCode, IbxError

_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


def _idna(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    if not host:
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        # a label longer than 63 chars or an invalid character: keep the lower-cased text
        return host


def _split(url: str):  # noqa: ANN202
    text = (url or "").strip()
    if not text:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "url is required.",
                       details={"errors": [{"loc": ["url"], "type": "missing", "msg": "Field required"}]})
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "url must be an http(s) URL with a host.",
                       details={"errors": [{"loc": ["url"], "type": "url_parsing", "msg": "Not an http(s) URL"}]})
    return parts


def _clean(url: str, *, keep_scheme: bool) -> str:
    parts = _split(url)
    scheme = parts.scheme.lower() if keep_scheme else "https"
    host = _idna(parts.hostname or "")
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    if port is not None and (not keep_scheme and port in (80, 443) or _DEFAULT_PORTS.get(scheme) == port):
        port = None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path or ""
    while path.endswith("/"):
        path = path[:-1]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS]
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def normalize_url(url: str) -> str:
    """Canonical https form for de-duplication (see module docstring)."""
    return _clean(url, keep_scheme=False)


def clean_instance_url(url: str) -> str:
    """Same cleaning as ``normalize_url`` but the scheme (and a non-default port) are kept."""
    return _clean(url, keep_scheme=True)


def host_of(url: str) -> str:
    """Lower-case host (IDNA) of a URL, or '' if it has none."""
    try:
        return _idna(urlsplit(url if "://" in url else "https://" + url).hostname or "")
    except ValueError:
        return ""


def check_url_syntax(url: str) -> tuple[str, str]:
    """(fetch URL with scheme kept, normalised dedupe URL). Raises ValueError with a short reason (never the URL itself).

    Used by the catalogue import and by every catalogue row (``service.create_destination``)."""
    text = (url or "").strip()
    if not text:
        raise ValueError("url is empty")
    if re.match(r"^[a-z][a-z0-9+.-]*:", text, re.I) and not re.match(r"^https?://", text, re.I):
        raise ValueError("only http(s) URLs are accepted")
    if any(ch.isspace() for ch in text):
        raise ValueError("the URL contains spaces")
    try:
        parts = urlsplit(text if "://" in text else "https://" + text.lstrip("/"))
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as exc:
        raise ValueError(f"the URL can not be parsed ({exc})") from None
    if parts.username or parts.password:
        raise ValueError("credentials in the URL (user:pass@) are not accepted")
    host = (parts.hostname or "").rstrip(".")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower() if host else ""
    except UnicodeError:
        raise ValueError("the host name is not valid") from None
    is_ip = re.fullmatch(r"[\d.]+|[0-9a-f:]+", ascii_host) is not None
    if not ascii_host or not (is_ip or _HOST.match(ascii_host)):
        raise ValueError("the URL has no valid host")
    try:
        return clean_instance_url(text), normalize_url(text)
    except (IbxError, ValueError) as exc:
        raise ValueError(getattr(exc, "message", None) or str(exc)) from None
