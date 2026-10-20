"""One polite, SSRF-guarded fetcher for every read of a third-party web page (catalogue import, rules capture).

``PoliteFetcher`` (the importer's ``Fetcher``, shared with rules capture):

* **SSRF guard on every hop.** Redirects are followed by hand (at most 5), and every hop is checked by
  ``app.reliability.safe_fetch.check_url``. Only public http(s) addresses are allowed; our own compose hosts only in a
  simulation or dev/test run.
* **robots.txt on every hop.** It is read once per site, for the fetcher's own ``user_agent``, and applied to every URL,
  redirect targets and platform-API URLs included (RFC 9309: 4xx = no rules; 5xx or no answer = everything disallowed).
* **The declared User-Agent on every request**, also when a shared ``httpx.AsyncClient`` is passed in. robots.txt is
  evaluated for the same agent string that is sent.
* **Politeness:** at most ``per_site`` requests in flight per site and ``global_limit`` overall. Public sites also get at
  least ``public_min_interval_s`` between two requests. A shared ``HostPacer`` makes that gap hold across fetchers
  (e.g. several capture jobs in one worker process). Our own local hosts are not paced.
* **Limits:** ``timeout_s`` per request; the body is capped at ``max_body`` bytes (``Page.truncated`` says so).

A *site* is scheme + host + port. On our own mock-platforms host, the first two path segments are part of the site
(``/internet/<site>``), because each mock tenant stands for a separate web site there.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.config import Settings
from app.errors import ErrorCode, IbxError
from app.reliability.safe_fetch import check_url, local_mode

MAX_REDIRECTS = 5


@dataclass
class Page:
    """One fetch result. ``error`` is a report code (TIMEOUT, DNS_FAILED, URL_NOT_ALLOWED, ROBOTS_DISALLOWED, ...) or None."""

    url: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str | None = None
    message: str | None = None
    elapsed_ms: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def mime(self) -> str | None:
        return self.headers.get("content-type", "").split(";")[0].strip().lower() or None

    def text(self) -> str:
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
        if match:
            charset = match.group(1)
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None


@dataclass
class Robots:
    allowed_all: bool = False
    disallow_all: bool = False
    parser: RobotFileParser | None = None
    note: str | None = None
    agent: str = "*"

    def allows(self, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allowed_all or self.parser is None:
            return True
        return self.parser.can_fetch(self.agent, url)


class HostPacer:
    """Next free request slot per site. Reserving a slot never awaits, so it is atomic under asyncio and needs no lock
    (a module-level pacer is safe to share between event loops, e.g. in tests)."""

    def __init__(self) -> None:
        self._next: dict[str, float] = {}

    def reserve(self, site: str, interval_s: float) -> float:
        """Seconds to wait before this request may be sent."""
        now = time.monotonic()
        start = max(now, self._next.get(site, 0.0))
        self._next[site] = start + interval_s
        return start - now


class PoliteFetcher:
    """Polite, SSRF-guarded HTTP (see the module docstring)."""

    def __init__(
        self,
        settings: Settings,
        *,
        user_agent: str,
        client: httpx.AsyncClient | None = None,
        global_limit: int = 50,
        per_site: int = 4,
        timeout_s: float = 5.0,
        public_min_interval_s: float = 1.0,
        max_body: int = 256 * 1024,
        pacer: HostPacer | None = None,
    ) -> None:
        self.settings = settings
        self.user_agent = user_agent
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(headers={"User-Agent": user_agent})
        self.timeout_s = timeout_s
        self.public_min_interval_s = public_min_interval_s
        self.max_body = max_body
        self.pacer = pacer or HostPacer()
        self._global = asyncio.Semaphore(global_limit)
        self._per_site_limit = per_site
        self._site_sems: dict[str, asyncio.Semaphore] = {}
        self._robots: dict[str, asyncio.Task[Robots]] = {}
        mock = urlsplit(settings.MOCK_PLATFORM_URL)
        self._mock_origin = f"{mock.scheme}://{mock.netloc}".lower()
        self.requests = 0

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def __aenter__(self) -> PoliteFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- sites

    def site_root(self, url: str) -> str:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        if origin == self._mock_origin:
            segments = [s for s in parts.path.split("/") if s][:2]
            return origin + "".join("/" + s for s in segments)
        return origin

    def _is_local(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return local_mode(self.settings) and host in self.settings.FETCH_LOCAL_HOSTS

    async def _polite_slot(self, site: str, url: str) -> None:
        if self.public_min_interval_s <= 0 or self._is_local(url):
            return
        wait = self.pacer.reserve(site, self.public_min_interval_s)
        if wait > 0:
            await asyncio.sleep(wait)

    # ---------------------------------------------------------------- robots

    async def robots(self, url: str) -> Robots:
        site = self.site_root(url)
        task = self._robots.get(site)
        if task is None:
            task = asyncio.ensure_future(self._load_robots(site))
            self._robots[site] = task
        return await task

    async def _load_robots(self, site: str) -> Robots:
        agent = self.user_agent
        page = await self.request("GET", site + "/robots.txt", robots_check=False)
        if page.error is not None:
            if page.error == "URL_NOT_ALLOWED":
                return Robots(disallow_all=True, note=page.error, agent=agent)
            return Robots(disallow_all=True, note=f"robots.txt unreachable ({page.error})", agent=agent)
        assert page.status is not None
        if 400 <= page.status < 500:
            return Robots(allowed_all=True, note=f"robots.txt {page.status}", agent=agent)
        if page.status >= 300:  # 5xx (and an unfollowed redirect): the site is treated as unreachable (RFC 9309)
            return Robots(disallow_all=True, note=f"robots.txt unreachable (HTTP {page.status})", agent=agent)
        parser = RobotFileParser()
        parser.parse(page.text().splitlines())
        return Robots(parser=parser, note="robots.txt allows", agent=agent)

    # ---------------------------------------------------------------- requests

    async def get(self, url: str) -> Page:
        return await self.request("GET", url)

    async def head_then_get(self, url: str) -> Page:
        """Reachability: HEAD, then GET when HEAD is refused or fails without a timeout."""
        page = await self.request("HEAD", url)
        if page.ok or page.error in ("TIMEOUT", "DNS_FAILED", "URL_NOT_ALLOWED", "ROBOTS_DISALLOWED", "ROBOTS_UNREACHABLE"):
            return page
        if page.status is not None and (page.status in (405, 501) or 400 <= page.status < 500) or page.error:
            return await self.request("GET", url)
        return page

    async def request(self, method: str, url: str, *, robots_check: bool = True) -> Page:
        start = time.perf_counter()
        current = url
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                try:
                    await check_url(current, self.settings)
                except IbxError as err:
                    code = "DNS_FAILED" if err.code == ErrorCode.INPUT_FETCH_FAILED else "URL_NOT_ALLOWED"
                    return Page(url=current, error=code, message=err.message, elapsed_ms=_ms(start))
                if robots_check:
                    verdict = await self.robots(current)
                    if not verdict.allows(current):
                        code = "ROBOTS_UNREACHABLE" if verdict.disallow_all and verdict.note and "unreachable" in verdict.note \
                            else "ROBOTS_DISALLOWED"
                        message = verdict.note if code == "ROBOTS_UNREACHABLE" else f"robots.txt disallows {current}"
                        return Page(url=current, error=code, message=message, elapsed_ms=_ms(start))
                site = self.site_root(current)
                sem = self._site_sems.setdefault(site, asyncio.Semaphore(self._per_site_limit))
                async with sem:
                    await self._polite_slot(site, current)
                    async with self._global:
                        self.requests += 1
                        status, headers, body, location, truncated = await asyncio.wait_for(
                            self._send(method, current), timeout=self.timeout_s)
                if location is not None and 300 <= status < 400:
                    current = urljoin(current, location)
                    continue
                return Page(url=current, status=status, headers=headers, body=body, elapsed_ms=_ms(start), truncated=truncated)
            return Page(url=current, error="TOO_MANY_REDIRECTS", message="more than 5 redirects", elapsed_ms=_ms(start))
        except (TimeoutError, httpx.TimeoutException):
            return Page(url=current, error="TIMEOUT", message=f"no answer within {self.timeout_s:g} s", elapsed_ms=_ms(start))
        except httpx.ConnectError:
            return Page(url=current, error="CONNECT_FAILED", message="connection refused or reset", elapsed_ms=_ms(start))
        except httpx.HTTPError as exc:
            return Page(url=current, error="HTTP_ERROR", message=type(exc).__name__, elapsed_ms=_ms(start))

    async def _send(self, method: str, url: str) -> tuple[int, dict[str, str], bytes, str | None, bool]:
        async with self.client.stream(method, url, follow_redirects=False, timeout=self.timeout_s,
                                      headers={"User-Agent": self.user_agent}) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            location = headers.get("location") if response.is_redirect else None
            chunks, size, truncated = [], 0, False
            if method != "HEAD" and location is None:
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= self.max_body:
                        truncated = True
                        break
            return response.status_code, headers, b"".join(chunks)[:self.max_body], location, truncated


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
