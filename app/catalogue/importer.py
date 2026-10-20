"""Catalogue import (ADV1): a CSV of destinations → validated, de-duplicated, checked, enriched, scored, inserted.

    rows = parse_csv(data)                                   # utf-8-sig, sniffed delimiter
    report = await Importer(settings, registry, ...).run(rows, dry_run=False)

Stages (every row gets exactly one line in the report, in file order):

1. **Validate** each row with ``CatalogueRow`` (the plan's Pydantic DestinationRow: the ``DestinationIn``
   spelling with ``connector_key`` optional), check the URL (http(s), a real host, no ``user:pass@``) and normalise it
   (``normalize_url``). The connector comes from ``connector_key``, else from ``platform_key``, else from detection.
   Surface aliases are mapped (the seed's Lemmy ``community`` is the manifest's ``post``, Discourse ``forum`` is
   ``topic``...); an unknown surface is a row error, never a crash. Rule columns that the rules engine can not
   parse are kept as text in ``extra.rules_text`` and the row is imported with rules *unconfirmed* and a warning.
   Rules that parse become a **proposal** (``rules_status`` column ``proposed``, shown at ``/ui/rules/{id}``), unless the
   row says ``rules_status=confirmed`` and ``rules_source=manual`` (an operator's own rules, confirmed on import).
2. **Network** (skipped in a dry run), once per distinct normalised URL: robots.txt, reachability (a GET of the page
   when its text is needed for language, platform hints or relevance; otherwise HEAD, then GET when HEAD is refused), platform detection by probe, member counts, page language (lingua).
   See ``app.catalogue.detect`` for the limits (50 in flight, 4 per site, 5 s, SSRF guard on every hop).
3. **De-duplicate** on ``(platform_key, normalized_url)``, within the file (the first row wins) and against the
   destinations already in the database.
4. **Relevance** 1-5 with a reason (``app.catalogue.relevance``), unless the row gives a score.
5. **Insert** in chunks of 50 rows, one transaction per chunk and a savepoint per row, through the same
   ``service.create_destination`` that ``POST /api/v1/destinations`` uses. Progress is reported every 50 rows.

Report statuses: ``ok`` | ``warning`` (imported, with warnings) | ``duplicate`` | ``invalid`` | ``unreachable``
(not imported unless ``import_unreachable``). In a dry run the same statuses say what *would* happen and nothing is
written.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalogue import relevance as rel
from app.catalogue.detect import Fetcher, PageMeta, detect_platform, page_meta
from app.catalogue.enrich import detect_language, enrich, warm_up
from app.catalogue.models import RULE_COLUMNS, DestinationIn
from app.catalogue.normalize import check_url_syntax
from app.config import Settings
from app.contract.registry import Registry
from app.db.tables import DestinationRow
from app.errors import IbxError
from app.rules.lang import normalize_langs

MAX_ROWS = 10_000
MAX_BYTES = 10 * 1024 * 1024
CHUNK = 50
REPORT_COLUMNS = ["row_no", "status", "code", "message", "destination_id", "name", "url", "normalized_url", "platform_key",
                  "connector_key", "surface", "language", "est_audience", "relevance_score", "relevance_reason", "http_status",
                  "duplicate_of_row", "rules_status", "rules_proposal_id"]

# Platform spellings -> canonical platform_key
PLATFORM_ALIASES = {"dev.to": "forem", "devto": "forem", "dev": "forem", "lemmy": "lemmy", "discourse": "discourse",
                    "ghost": "ghost", "peertube": "peertube", "forem": "forem", "slack": "slack", "mrss": "mrss"}
# platform_key -> connector_key (when the row gives no connector_key)
PLATFORM_CONNECTOR = {"discourse": "discourse", "lemmy": "lemmy", "forem": "devto", "ghost": "ghost", "peertube": "peertube",
                      "slack": "slack", "mrss": "mrss", "websub": "mrss"}
# connector -> {alias: [canonical surfaces to try, in order]}; the manifest decides which one exists.
SURFACE_ALIASES: dict[str, dict[str, list[str]]] = {
    "lemmy": {"community": ["post"], "communities": ["post"], "post": ["post"], "link": ["post"]},
    "discourse": {"forum": ["topic"], "category": ["topic"], "topic": ["topic"], "thread": ["topic"]},
    "devto": {"tag": ["article"], "article": ["article"], "post": ["article"], "blog": ["article"]},
    "ghost": {"publication": ["article", "post"], "blog": ["article", "post"], "article": ["article", "post"],
              "post": ["article", "post"], "newsletter": ["newsletter", "article"]},
    "webhook": {"ping-api": ["post"], "api": ["post"], "webhook": ["post"], "post": ["post"]},
    "assisted": {a: ["post"] for a in ("directory", "launch", "link-aggregator", "curated-list", "community", "newsletter",
                                       "blog-platform", "marketplace", "tips", "video-platform", "forum", "submission",
                                       "listing", "post")},
    "peertube": {"instance": ["video", "upload", "post"], "channel": ["video", "upload", "post"], "video": ["video", "upload"]},
    "slack": {"workspace": ["message", "channel", "post"], "channel": ["message", "channel", "post"],
              "message": ["message", "post"]},
    "mrss": {"feed-ingest": ["item", "feed", "video"], "feed": ["item", "feed", "video"], "item": ["item", "feed"]},
}
# A row's rule columns are a proposal for a person to confirm, unless the row says rules_status=confirmed together with
# rules_source=manual (an operator typed them in). Researched or captured rules are never confirmed by an import (D6).
DEFAULT_RULES_STATUS = "proposed"
UNREACHABLE = {"TIMEOUT", "DNS_FAILED", "CONNECT_FAILED", "HTTP_ERROR", "TOO_MANY_REDIRECTS", "ROBOTS_UNREACHABLE"}


class CatalogueRow(DestinationIn):
    """One CSV row (plan ADV1 ``DestinationRow``): ``DestinationIn`` with ``connector_key`` optional and DB limits."""

    connector_key: str | None = Field(default=None, max_length=64)  # type: ignore[assignment]
    platform_key: str | None = Field(default=None, max_length=64)
    surface: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=128)
    geo: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=64)
    est_audience: int | None = Field(default=None, ge=0, le=10**12)


# ------------------------------------------------------------------ parsing


def parse_csv(data: bytes | str) -> list[dict[str, Any]]:
    """utf-8-sig text, delimiter sniffed among , ; tab | (RFC 4180 quoting). Raises IbxError when unreadable."""
    from app.errors import ErrorCode

    if isinstance(data, bytes):
        if len(data) > MAX_BYTES:
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"The CSV is larger than {MAX_BYTES} bytes.", http_status=413)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    else:
        text = data.lstrip("﻿")
    if not text.strip():
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "The CSV is empty.")
    sample = text[:20000]
    try:
        dialect: Any = csv.Sniffer().sniff(sample.split("\n", 1)[0] + "\n", delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        header = sample.split("\n", 1)[0]
        delimiter = max((",", ";", "\t", "|"), key=header.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fields = [f.strip().lower() for f in (reader.fieldnames or []) if f]
    if "url" not in fields or "name" not in fields:
        raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "The CSV header must have at least the columns name and url.",
                       details={"columns": fields[:50]})
    rows = []
    for raw in reader:
        if raw is None:
            continue
        clean = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k is not None}
        if not any(isinstance(v, str) and v for v in clean.values()):
            continue  # blank line
        rows.append(clean)
        if len(rows) > MAX_ROWS:
            raise IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, f"At most {MAX_ROWS} rows per import.", http_status=413)
    return rows


# ------------------------------------------------------------------ per-row state


@dataclass
class Row:
    row_no: int
    raw: dict[str, Any]
    status: str = "pending"
    codes: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    data: CatalogueRow | None = None
    url: str = ""
    fetch_url: str = ""
    normalized_url: str = ""
    platform_key: str | None = None
    connector_key: str | None = None
    surface: str | None = None
    surface_label: str | None = None
    language: str | None = None
    est_audience: int | None = None
    relevance_score: int | None = None
    relevance_reason: str | None = None
    http_status: int | None = None
    duplicate_of: int | None = None
    destination_id: str | None = None
    rules_status: str | None = None  # confirmed | proposed; None = the row states no rules
    rules_proposal_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def fail(self, status: str, code: str, message: str) -> None:
        self.status = status
        self.codes.append(code)
        self.messages.append(message)

    def warn(self, code: str, message: str) -> None:
        self.codes.append(code)
        self.messages.append(message)

    def report(self) -> dict[str, Any]:
        return {
            "row_no": self.row_no, "status": self.status, "code": ";".join(self.codes), "message": " | ".join(self.messages)[:1000],
            "destination_id": self.destination_id or "", "name": (self.data.name if self.data else str(self.raw.get("name") or ""))[:255],
            "url": str(self.raw.get("url") or "")[:2000], "normalized_url": self.normalized_url, "platform_key": self.platform_key or "",
            "connector_key": self.connector_key or "", "surface": self.surface or "", "language": self.language or "",
            "est_audience": "" if self.est_audience is None else self.est_audience,
            "relevance_score": "" if self.relevance_score is None else self.relevance_score,
            "relevance_reason": self.relevance_reason or "", "http_status": "" if self.http_status is None else self.http_status,
            "duplicate_of_row": "" if self.duplicate_of is None else self.duplicate_of,
            "rules_status": self.rules_status or "", "rules_proposal_id": self.rules_proposal_id or "",
        }


@dataclass
class SiteInfo:
    """Network result for one normalised URL (shared by its duplicate rows)."""

    page_status: int | None = None
    error: str | None = None
    message: str | None = None
    meta: PageMeta | None = None
    platform: str | None = None
    platform_evidence: str | None = None
    est_audience: int | None = None
    audience_source: str | None = None
    enrich_error: str | None = None
    language: str | None = None
    language_confidence: float | None = None
    detector: str | None = None
    robots_note: str | None = None
    elapsed_ms: float = 0.0


Progress = Callable[[int, str], Awaitable[None]]


def _errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in exc.errors())[:500]


def _language(value: str | None) -> tuple[str | None, list[str]]:
    langs = [code for code in normalize_langs(value) if re.fullmatch(r"[a-z]{2,3}", code)]
    if not langs:
        return None, []
    joined = ";".join(langs)
    while len(joined) > 16 and ";" in joined:
        joined = joined.rsplit(";", 1)[0]
    return joined[:16], langs


# ------------------------------------------------------------------ the importer


class Importer:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        fetcher: Fetcher | None = None,
        progress: Progress | None = None,
        now: Any = None,
        timeout_s: float = 5.0,
        import_unreachable: bool = False,
        relevance_llm: bool = False,
        import_id: str | None = None,
        llm: Any = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.session_factory = session_factory
        self._fetcher = fetcher
        self.progress = progress
        self.now = now
        self.timeout_s = timeout_s
        self.import_unreachable = import_unreachable
        self.relevance_llm = relevance_llm
        self.import_id = import_id
        self.llm = llm
        self.timings: dict[str, float] = {}
        self.detector: str | None = None
        self.llm_meta: list[dict[str, Any]] = []

    async def _progress(self, percent: int, note: str) -> None:
        if self.progress is not None:
            await self.progress(max(0, min(99, percent)), note)

    # ---------------------------------------------------------------- stage 1

    def _resolve_connector(self, row: Row, connector: str | None) -> bool:
        """Set connector + surface; False (row failed) when the connector or the surface is unknown."""
        if connector is None:
            return True
        manifest = self.registry.manifest(connector)
        if manifest is None:
            row.fail("invalid", "CONNECTOR_UNKNOWN", f"unknown connector_key '{connector}' (known: {', '.join(self.registry.keys())})")
            return False
        row.connector_key = connector
        param = manifest.instance_param
        given = (row.data.instance or {}).get(param) if row.data is not None else None
        if not param.endswith("url") and not (isinstance(given, str) and given.strip()):
            row.fail("invalid", "INSTANCE_MISSING",
                     f"connector '{connector}' needs instance.{param} (add a column instance_{param}); the row URL can not be "
                     f"used for it. To list the destination for a person to post instead, use connector_key 'assisted'.")
            return False
        known = [s.key for s in manifest.surfaces]
        label = row.surface_label
        if not label:
            row.surface = known[0]
            return True
        if label in known:
            row.surface = label
            return True
        for candidate in SURFACE_ALIASES.get(connector, {}).get(label, []):
            if candidate in known:
                row.surface = candidate
                row.extra["surface_label"] = label
                return True
        row.fail("invalid", "SURFACE_UNKNOWN",
                 f"connector '{connector}' has no surface '{label}' (known: {', '.join(known)}; "
                 f"aliases: {', '.join(sorted(SURFACE_ALIASES.get(connector, {}))) or 'none'})")
        return False

    def _validate(self, row: Row) -> None:
        try:
            data = CatalogueRow.model_validate(row.raw)
        except ValidationError as exc:
            row.fail("invalid", "ROW_INVALID", _errors(exc))
            return
        except (ValueError, TypeError) as exc:
            row.fail("invalid", "ROW_INVALID", str(exc)[:300])
            return
        row.data = data
        try:
            row.fetch_url, row.normalized_url = check_url_syntax(data.url)
        except ValueError as exc:
            row.fail("invalid", "URL_INVALID", str(exc))
            return
        row.url = data.url.strip()
        platform = (data.platform_key or "").strip().lower() or None
        row.platform_key = PLATFORM_ALIASES.get(platform, platform) if platform else None
        row.surface_label = (data.surface or "").strip().lower() or None
        row.language, langs = _language(data.language)
        if len(langs) > 1:
            row.extra["languages"] = langs
        row.est_audience = data.est_audience
        connector = data.connector_key or PLATFORM_CONNECTOR.get(row.platform_key or "")
        if connector and connector not in self.registry.keys() and not data.connector_key:
            row.warn("CONNECTOR_FALLBACK", f"no '{connector}' connector is installed; using 'assisted' (a person posts, L1)")
            connector = "assisted"
        if not self._resolve_connector(row, connector):
            return
        self._rules(row)

    def _rules(self, row: Row) -> None:
        from app.rules.models import RuleSet

        assert row.data is not None
        rules = row.data.rules_dict()
        if rules is None:
            row.rules_status = None
            return
        try:
            RuleSet.model_validate(rules)
        except (ValidationError, ValueError) as exc:
            text = {k: getattr(row.data, k) for k in RULE_COLUMNS if getattr(row.data, k) is not None}
            if row.data.rules is not None:
                text["rules"] = row.data.rules
            row.extra["rules_text"] = text
            first = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) and exc.errors() else str(exc)
            row.warn("RULES_UNPARSED", f"rule columns kept as text, rules stay unconfirmed until a person confirms them ({first[:120]})")
            row.data = row.data.model_copy(update={**{k: None for k in RULE_COLUMNS}, "rules": None})
            row.rules_status = None
            return
        from app.catalogue.service import rules_origin

        origin = rules_origin(row.data, DEFAULT_RULES_STATUS)
        row.rules_status = origin["status"]
        if origin["confirmation_refused"] and "RULES_NOT_CONFIRMED" not in row.codes:
            row.warn("RULES_NOT_CONFIRMED", f"rules_status=confirmed is only accepted with rules_source=manual (an "
                     f"operator's own rules); rules_source={origin['source']}, so they are stored as a proposal")

    # ---------------------------------------------------------------- stage 2

    async def _site(self, fetcher: Fetcher, row: Row) -> SiteInfo:
        info = SiteInfo()
        assert row.data is not None
        start = time.perf_counter()
        robots = await fetcher.robots(row.fetch_url)
        info.robots_note = robots.note
        # The page is needed for language (detect or cross-check), platform hints and the relevance description; a row
        # that already states all three only needs reachability (HEAD, then GET when HEAD is refused).
        need_page = row.language is None or row.connector_key is None or row.data.relevance_score is None
        page = await (fetcher.get(row.fetch_url) if need_page else fetcher.head_then_get(row.fetch_url))
        info.page_status, info.error, info.message = page.status, page.error, page.message
        if page.error is None and page.status is not None and page.status >= 400:
            info.error, info.message = f"HTTP_{page.status}", f"the URL answered HTTP {page.status}"
        if info.error is not None and info.error not in ("ROBOTS_DISALLOWED",):
            info.elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            return info
        if page.ok and page.body and "html" in page.headers.get("content-type", "html"):
            info.meta = page_meta(page.text())
        probe_data: dict[str, Any] = {}
        if row.connector_key is None and info.error is None:
            detection = await detect_platform(fetcher, row.fetch_url, info.meta)
            info.platform, info.platform_evidence, probe_data = detection.platform, detection.evidence, detection.probe_data
        platform = row.platform_key or info.platform
        if info.error is None and platform in ("discourse", "lemmy", "peertube"):
            enrichment = await enrich(fetcher, platform, row.fetch_url, probe_data)
            info.est_audience, info.audience_source, info.enrich_error = enrichment.est_audience, enrichment.source, enrichment.error
        if info.meta is not None:
            text = " ".join(x for x in (info.meta.title, info.meta.description, info.meta.text) if x)
            info.language, info.language_confidence, info.detector = await asyncio.to_thread(detect_language, text)
        info.elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        return info

    async def _network(self, rows: list[Row]) -> dict[str, SiteInfo]:
        targets: dict[str, Row] = {}
        for row in rows:
            if row.status == "pending" and row.normalized_url not in targets:
                targets[row.normalized_url] = row
        self.detector = await asyncio.to_thread(warm_up)
        fetcher = self._fetcher or Fetcher(self.settings, timeout_s=self.timeout_s)
        results: dict[str, SiteInfo] = {}
        done = 0
        total = max(1, len(targets))

        async def one(key: str, row: Row) -> None:
            nonlocal done
            try:
                results[key] = await self._site(fetcher, row)
            except Exception as exc:  # noqa: BLE001 - one bad site must never stop the import
                results[key] = SiteInfo(error="CHECK_FAILED", message=type(exc).__name__)
            done += 1
            if done % CHUNK == 0:
                await self._progress(5 + int(75 * done / total), f"checked {done}/{len(targets)} sites")

        try:
            await asyncio.gather(*(one(k, r) for k, r in targets.items()))
        finally:
            if self._fetcher is None:
                await fetcher.aclose()
        self.requests = fetcher.requests
        return results

    def _apply_site(self, row: Row, info: SiteInfo | None) -> None:
        if info is None:
            return
        row.http_status = info.page_status
        # reachability first: an unreachable site is reported as such, whatever its platform or surface would have been
        if info.error in UNREACHABLE or (info.error or "").startswith("HTTP_") or info.error in ("URL_NOT_ALLOWED", "CHECK_FAILED"):
            if info.error == "URL_NOT_ALLOWED":
                row.fail("invalid", "URL_NOT_ALLOWED", "not a public http(s) address (internal or private hosts are refused)")
            else:
                row.fail("unreachable", info.error or "UNREACHABLE", info.message or "the URL could not be reached")
            return
        if info.platform and not row.platform_key:
            row.platform_key = info.platform
            row.extra["platform_detected"] = {"platform": info.platform, "evidence": info.platform_evidence}
        if row.connector_key is None:
            connector = PLATFORM_CONNECTOR.get(row.platform_key or "")
            if connector and connector not in self.registry.keys():
                row.warn("CONNECTOR_FALLBACK", f"detected {row.platform_key}, but no '{connector}' connector is installed; using "
                         "'assisted' (a person posts, L1)")
                connector = "assisted"
            elif connector is None and info.error is None:
                row.warn("PLATFORM_UNDETECTED", "no known platform answered the probes; using 'assisted' (a person posts, L1)")
                connector = "assisted"
            if connector is None:
                connector = "assisted"
            if not self._resolve_connector(row, connector):
                return
            self._rules(row)
        if info.error == "ROBOTS_DISALLOWED":
            row.warn("ROBOTS_DISALLOWED", "robots.txt disallows this URL: not checked, imported without reachability")
        if info.enrich_error and info.enrich_error != "NO_COUNT":
            row.warn("ENRICH_FAILED", f"member count not read ({info.enrich_error})")
        if info.est_audience is not None:
            row.extra["enrichment"] = {"est_audience": info.est_audience, "source": info.audience_source}
            if row.est_audience is None:
                row.est_audience = info.est_audience
        if info.language:
            row.extra["language_detected"] = {"language": info.language, "confidence": info.language_confidence,
                                              "detector": info.detector}
            if row.language is None:
                row.language = info.language
            elif info.language not in (row.extra.get("languages") or [row.language]) and (info.language_confidence or 0) >= 0.8:
                row.warn("LANGUAGE_MISMATCH", f"the row says '{row.language}', the page reads as '{info.language}' "
                         f"({info.detector} {info.language_confidence}); the row's value is kept")
        if info.meta is not None:
            row.extra["page"] = {"title": info.meta.title, "description": (info.meta.description or "")[:300] or None}

    # ---------------------------------------------------------------- stage 3 + 4

    async def _db_existing(self, rows: list[Row]) -> dict[tuple[str, str], str]:
        if self.session_factory is None:
            return {}
        urls = sorted({r.normalized_url for r in rows if r.status == "pending"})
        found: dict[tuple[str, str], str] = {}
        async with self.session_factory() as session:
            for i in range(0, len(urls), 500):
                stmt = select(DestinationRow.id, DestinationRow.platform_key, DestinationRow.normalized_url).where(
                    DestinationRow.normalized_url.in_(urls[i:i + 500]))
                for dest_id, platform, normalized in (await session.execute(stmt)).all():
                    found[(PLATFORM_ALIASES.get(platform, platform), normalized)] = dest_id
        return found

    def _dedupe_key(self, row: Row) -> tuple[str, str]:
        return (row.platform_key or row.connector_key or "", row.normalized_url)

    async def _relevance(self, row: Row) -> None:
        assert row.data is not None
        if row.data.relevance_score is not None:
            row.relevance_score, row.relevance_reason = row.data.relevance_score, row.data.relevance_reason
            return
        page = row.extra.get("page") or {}
        item = rel.RelevanceInput(name=row.data.name, category=row.data.category, audience_tags=row.data.audience_tags,
                                  description=page.get("description"), title=page.get("title"), url=row.url,
                                  language=row.language)
        result = await rel.score(item, use_llm=self.relevance_llm, llm=self.llm)
        row.relevance_score, row.relevance_reason = result.score, result.reason
        row.extra["relevance"] = {"method": result.method, "lexicon_score": result.lexicon_score}
        if result.llm:
            row.extra["relevance"]["llm"] = result.llm
            if result.llm.get("used"):
                self.llm_meta.append(result.llm)

    # ---------------------------------------------------------------- stage 5

    def _destination_in(self, row: Row) -> DestinationIn:
        assert row.data is not None
        data = row.data.model_dump(exclude_none=True)
        extra = dict(row.data.model_extra or {})
        for key in list(extra):
            data.pop(key, None)
        data.update(connector_key=row.connector_key, platform_key=row.platform_key or row.connector_key, surface=row.surface,
                    language=row.language, est_audience=row.est_audience, relevance_score=row.relevance_score,
                    relevance_reason=row.relevance_reason)
        payload = {**{k: v for k, v in extra.items() if v is not None}, **data,
                   "import": {"import_id": self.import_id, "row_no": row.row_no, **row.extra}}
        return DestinationIn.model_validate(payload)

    async def _insert(self, rows: list[Row]) -> None:
        from app.catalogue import service

        assert self.session_factory is not None
        todo = [r for r in rows if r.status == "pending"]
        for start in range(0, len(todo), CHUNK):
            chunk = todo[start:start + CHUNK]
            async with self.session_factory() as session:
                for row in chunk:
                    try:
                        async with session.begin_nested():
                            dest, created, warnings = await service.create_destination(
                                session, self.registry, self._destination_in(row), now=self.now, confirmed_by="catalogue_import",
                                default_rules_status=DEFAULT_RULES_STATUS)
                    except IbxError as err:
                        row.fail("invalid", err.code.value, err.message[:300])
                        continue
                    except (ValidationError, ValueError) as exc:
                        row.fail("invalid", "ROW_INVALID", str(exc)[:300])
                        continue
                    except SQLAlchemyError as exc:
                        row.fail("invalid", "DB_REJECTED", f"the database refused the row ({type(exc).__name__})")
                        continue
                    row.destination_id = dest.id
                    if not created:
                        row.fail("duplicate", "DUPLICATE_IN_DB", "a destination with this connector, instance and surface exists")
                        continue
                    row.rules_proposal_id = (dest.extra or {}).get("rules_proposal_id")
                    for warning in warnings:
                        code, _, message = warning.partition(":")
                        if code in ("RULES_UNCONFIRMED", "RULES_PROPOSED"):
                            row.extra.setdefault("notes", []).append(code)
                            continue
                        row.warn(code.strip(), message.strip()[:200])
                await session.commit()
            done = min(len(todo), start + CHUNK)
            await self._progress(85 + int(14 * done / max(1, len(todo))), f"inserted {done}/{len(todo)} rows")

    # ---------------------------------------------------------------- run

    async def run(self, raw_rows: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
        t0 = time.perf_counter()
        rows = [Row(row_no=n, raw=raw) for n, raw in enumerate(raw_rows, start=1)]
        for row in rows:
            self._validate(row)
        self.timings["validate_s"] = round(time.perf_counter() - t0, 3)
        await self._progress(5, f"validated {len(rows)} rows")

        t1 = time.perf_counter()
        self.requests = 0
        sites: dict[str, SiteInfo] = {} if dry_run else await self._network(rows)
        self.timings["network_s"] = round(time.perf_counter() - t1, 3)

        t2 = time.perf_counter()
        for row in rows:
            if row.status == "pending":
                self._apply_site(row, sites.get(row.normalized_url))
            if row.status == "pending" and row.connector_key is None:  # dry run: no detection possible
                row.warn("PLATFORM_NOT_CHECKED", "no platform or connector given and no network in a dry run; would detect")
                row.connector_key = None
        seen: dict[tuple[str, str], int] = {}
        for row in rows:
            if row.status not in ("pending",) and not (row.status == "unreachable" and row.normalized_url):
                continue
            key = self._dedupe_key(row)
            if key in seen:
                row.status, row.codes, row.messages = "duplicate", ["DUPLICATE_IN_FILE"], [f"same platform and URL as row {seen[key]}"]
                row.duplicate_of = seen[key]
            else:
                seen[key] = row.row_no
        existing = {} if dry_run else await self._db_existing(rows)
        for row in rows:
            if row.status == "pending" and self._dedupe_key(row) in existing:
                row.status, row.codes, row.messages = "duplicate", ["DUPLICATE_IN_DB"], ["already in the catalogue"]
                row.destination_id = existing[self._dedupe_key(row)]
        for row in rows:
            if row.status == "unreachable" and self.import_unreachable:
                row.status = "pending"
                row.codes[-1] = f"UNREACHABLE_IMPORTED:{row.codes[-1]}"
            if row.status == "pending":
                await self._relevance(row)
        self.timings["dedupe_relevance_s"] = round(time.perf_counter() - t2, 3)

        t3 = time.perf_counter()
        if not dry_run and self.session_factory is not None:
            await self._insert(rows)
        for row in rows:
            if row.status == "pending":
                row.status = "warning" if row.codes else "ok"
        self.timings["insert_s"] = round(time.perf_counter() - t3, 3)
        self.timings["total_s"] = round(time.perf_counter() - t0, 3)
        return self._report(rows, dry_run)

    def _report(self, rows: list[Row], dry_run: bool) -> dict[str, Any]:
        counts = {s: 0 for s in ("ok", "warning", "duplicate", "invalid", "unreachable")}
        codes: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
            for code in row.codes:
                codes[code.split(":")[0]] = codes.get(code.split(":")[0], 0) + 1
        imported = counts["ok"] + counts["warning"]
        return {
            "dry_run": dry_run,
            "summary": {"rows": len(rows), "imported": 0 if dry_run else imported, "would_import": imported if dry_run else None,
                        **counts, "codes": dict(sorted(codes.items()))},
            "timings": self.timings,
            "network_requests": getattr(self, "requests", 0),
            "language_detector": self.detector,
            "llm_used": len(self.llm_meta),
            "llm_providers": sorted({f"{m.get('provider')}:{m.get('model')}" for m in self.llm_meta}),
            "llm_tokens": sum(int(m.get("total_tokens") or 0) for m in self.llm_meta),
            "rows": [r.report() for r in rows],
        }


def report_csv(report: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=REPORT_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in report.get("rows", []):
        writer.writerow(row)
    return out.getvalue()


# ------------------------------------------------------------------ CLI: dry run on a local file (no network, no DB)


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path

    from app.config import get_settings
    from app.contract.registry import get_registry

    parser = argparse.ArgumentParser(description="Dry-run a catalogue CSV: validate, map, normalise, de-duplicate and "
                                                 "score relevance. No network and no database.")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--report", type=Path, help="write the row-by-row report CSV here")
    args = parser.parse_args(argv)
    importer = Importer(get_settings(), get_registry())
    report = asyncio.run(importer.run(parse_csv(args.csv.read_bytes()), dry_run=True))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report_csv(report), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
