"""Shared helpers for the local-instance bootstrap and the fixture recorder.

Standard library only (no pip install inside the one-shot container, so the bootstrap works offline on a
clean machine once the images are pulled). Nothing in here prints secret values.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

REDACTED = "***REDACTED***"

# Header names we keep in recorded fixtures (lower-case). Everything else is dropped.
KEEP_HEADERS = {
    "content-type",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "location",
    "www-authenticate",
    "cache-control",
    "content-version",
    "x-cache-invalidate",
}
SECRET_KEYS = re.compile(r"(jwt|token|secret|password|passwd|api[-_]?key|authorization|cookie|session|private_key)", re.I)
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
BEARER_RE = re.compile(r"(Bearer|Ghost)\s+[A-Za-z0-9._~+/=-]{8,}")
GHOST_ADMIN_KEY_RE = re.compile(r"\b[0-9a-f]{24}:[0-9a-f]{64}\b")
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(msg: str) -> None:
    """Log a non-secret progress line to stderr."""
    print(f"[ibx-bootstrap {now_iso()}] {msg}", file=sys.stderr, flush=True)


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body_bytes: bytes
    elapsed_ms: int

    @property
    def text(self) -> str:
        return self.body_bytes.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body_bytes:
            return None
        try:
            return json.loads(self.body_bytes)
        except ValueError:
            return None


@dataclass
class Http:
    """Tiny urllib wrapper: JSON in/out, cookie jar, never raises on HTTP status, no redirects followed."""

    base_url: str
    default_headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
                return None

        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        absolute: bool = False,
    ) -> Response:
        url = path if absolute else f"{self.base_url}{path}"
        data = None
        hdrs = {"Accept": "application/json", "User-Agent": "ibx-bootstrap/1.0", **self.default_headers, **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        t0 = time.monotonic()
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read()
                status = resp.status
                rh = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            body = e.read()
            status = e.code
            rh = {k.lower(): v for k, v in (e.headers or {}).items()}
        return Response(status=status, headers=rh, body_bytes=body, elapsed_ms=int((time.monotonic() - t0) * 1000))


def wait_until(desc: str, fn, timeout_s: float = 300.0, interval_s: float = 2.0) -> Any:
    """Call fn() until it returns a truthy value or raises nothing; retry on connection errors."""
    deadline = time.monotonic() + timeout_s
    last: str = ""
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
            last = "not ready"
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last = type(e).__name__
        time.sleep(interval_s)
    raise RuntimeError(f"timed out waiting for {desc} ({last})")


# ---------------------------------------------------------------- Ghost Admin JWT (HS256)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def split_ghost_admin_key(admin_key: str) -> tuple[str, str]:
    """Ghost 6 admin keys are '<24-hex id>:<64-hex secret>'."""
    key_id, _, secret = admin_key.partition(":")
    if not key_id or not secret:
        raise ValueError("Ghost admin key must look like '<id>:<hexsecret>'")
    return key_id, secret


def ghost_admin_jwt(admin_key: str, *, iat: int | None = None, ttl_s: int = 300, secret_override: str | None = None) -> str:
    """Sign the short-lived Ghost Admin API token (aud /admin/, exp <= 5 min)."""
    key_id, secret = split_ghost_admin_key(admin_key)
    if secret_override is not None:
        secret = secret_override
    iat = int(time.time()) if iat is None else iat
    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    payload = {"iat": iat, "exp": iat + ttl_s, "aud": "/admin/"}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    sig = hmac.new(bytes.fromhex(secret), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


# ---------------------------------------------------------------- redaction


class Redactor:
    """Redacts known secret values, secret-looking keys and token patterns from nested data."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = sorted({s for s in (secrets or []) if s and len(s) >= 6}, key=len, reverse=True)

    def add(self, *values: str | None) -> None:
        for v in values:
            if v and len(v) >= 6 and v not in self.secrets:
                self.secrets.append(v)
        self.secrets.sort(key=len, reverse=True)

    def text(self, s: str) -> str:
        for v in self.secrets:
            s = s.replace(v, REDACTED)
        s = JWT_RE.sub(REDACTED, s)
        s = BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
        s = GHOST_ADMIN_KEY_RE.sub(REDACTED, s)
        s = PEM_RE.sub(REDACTED, s)
        return s

    def data(self, obj: Any, key: str | None = None) -> Any:
        if isinstance(obj, dict):
            return {k: self.data(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.data(v, key) for v in obj]
        if isinstance(obj, str):
            if key and SECRET_KEYS.search(key) and obj and not _is_harmless_value(key, obj):
                return REDACTED
            return self.text(obj)
        return obj


def _is_harmless_value(key: str, value: str) -> bool:
    # Error codes like {"error": "incorrect_login"} are not secrets, and neither are known non-secret labels.
    if key.lower() in {"token_type", "type"}:
        return True
    return False


def write_json_secure(path: str, data: Any) -> None:
    """Write JSON atomically. Mode 0600 + chown when BOOTSTRAP_OUT_UID is set, else 0644 (dev-only volume)."""
    uid = os.environ.get("BOOTSTRAP_OUT_UID")
    gid = os.environ.get("BOOTSTRAP_OUT_GID", uid or "")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    if uid:
        os.chown(tmp, int(uid), int(gid))
        os.chmod(tmp, 0o600)
    else:
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
