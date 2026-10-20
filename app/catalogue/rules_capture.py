"""Rules capture (ADV2): read a community's rules, PROPOSE posting rules with verbatim quotes, and keep the proposal
until a person confirms or rejects it. A proposal is never used for posting; only a confirmed rule set is.

Steps (job ``rules.capture``):
1. **Fetch** in this order: the platform API (Lemmy community sidebar, Discourse ``/about.json``), then the
   destination's ``rules_url`` page. Every fetch goes through the shared ``app.reliability.polite_fetch.PoliteFetcher``
   our declared ``USER_AGENT`` on every request, ``robots.txt`` checked for that agent on every hop (platform-API
   URLs and redirect targets included), the SSRF guard on every hop, and at most one request per second per public
   site. A person may also paste the rules text (``text``), e.g. for a site that blocks automated reading.
   Problems (robots-blocked, HTTP 403, a JavaScript-only page, ...) become structured warnings ``{code, message}``.
2. **Extract text** (HTML -> text with the standard library parser; scripts, styles and navigation are dropped).
3. **Propose** with the LLM chain (``app.providers.llm``; mock = deterministic extractor, the final fallback).
4. **Verify quotes.** A field is used only if its quote appears verbatim in the fetched text (whitespace-normalised).
   Otherwise the field is ``unknown``: it gets the conservative default (``RuleSet.conservative_default``) and a flag.
5. **Store** the proposal (``rule_sets`` row, status ``proposed``) with the page text's sha256 and the evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.tables import DestinationRow, RuleSetRow, new_id
from app.errors import ErrorCode, IbxError
from app.providers.llm import LLMMeta, complete_json
from app.providers.llm.mock import extract_rules, looks_like_injection
from app.providers.llm.rules_task import FIELDS, SYSTEM_PROMPT, RulesExtraction, user_prompt
from app.reliability.polite_fetch import HostPacer, Page, PoliteFetcher
from app.rules.lang import normalize_lang
from app.rules.models import RuleSet

USER_AGENT = "IncuBrixRulesReader/1.0 (+read-only; respects robots.txt)"
MAX_PAGE_BYTES = 2_000_000
MAX_TEXT_CHARS = 200_000
MIN_QUOTE_CHARS = 8
TEXT_MIME = ["text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/json", None]

# ------------------------------------------------------------------ HTML -> text

_SKIP = {"script", "style", "noscript", "template", "svg", "head", "nav", "header", "footer", "form", "button", "iframe"}
_BLOCK = {"p", "div", "li", "ol", "ul", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "section",
          "article", "main", "blockquote", "pre", "dd", "dt", "hr", "table", "aside"}


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\r", "")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in value.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()[:MAX_TEXT_CHARS]


def html_to_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return clean_text("".join(parser.parts))


def page_text(data: bytes, mime: str | None) -> str:
    raw = data.decode("utf-8", errors="replace")
    if mime in ("text/html", "application/xhtml+xml") or (mime is None and "<html" in raw[:2000].lower()):
        return html_to_text(raw)
    return clean_text(raw)


# ------------------------------------------------------------------ quote verification


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", value).strip()


def quote_in(quote: str | None, source: str) -> bool:
    """True when ``quote`` (at least MIN_QUOTE_CHARS) appears verbatim in ``source``. Only whitespace runs, Unicode
    compatibility forms and curly quotes are normalised; case and wording must match exactly."""
    if not quote or len(_norm(quote)) < MIN_QUOTE_CHARS:
        return False
    return _norm(quote) in _norm(source)


# ------------------------------------------------------------------ fetching (robots.txt on every hop + 1 request/second/host)

MIN_INTERVAL_S = 1.0
# One pacer for the whole process: capture jobs running side by side in one worker still keep 1 request/s per site.
PACER = HostPacer()
JS_ONLY_MAX_CHARS = 200


def capture_fetcher(settings: Settings, http: httpx.AsyncClient | None) -> PoliteFetcher:
    """The shared polite fetcher with our declared user agent (sent on every request, and the agent robots.txt is
    evaluated for), robots.txt on every hop, the SSRF guard on every hop and the 1 request/s/site pacing."""
    return PoliteFetcher(settings, user_agent=USER_AGENT, client=http, per_site=1,
                         timeout_s=settings.MEDIA_FETCH_TIMEOUT_S, public_min_interval_s=MIN_INTERVAL_S,
                         max_body=MAX_PAGE_BYTES, pacer=PACER)


@dataclass
class SourceDoc:
    kind: str  # pasted | lemmy_sidebar | discourse_about | rules_url
    url: str | None
    text: str
    sha256: str  # of the bytes we received (or the pasted text)
    size: int
    note: str | None = None

    def evidence(self) -> dict[str, Any]:
        return {"type": "source", "kind": self.kind, "url": self.url, "sha256": self.sha256, "bytes": self.size,
                "text_chars": len(self.text), "note": self.note}


def issue(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """One capture warning as the API returns it: ``{code, message, ...}`` (``field``, ``http_status``, ``url``)."""
    return {"code": code, "message": message, **{k: v for k, v in extra.items() if v is not None}}


@dataclass
class Gathered:
    docs: list[SourceDoc] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        """The issues as ``"CODE: message"`` lines (job warnings, logs)."""
        return [f"{i['code']}: {i['message']}" for i in self.issues]

    @property
    def text(self) -> str:
        return "\n\n".join(d.text for d in self.docs if d.text)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _refused() -> IbxError:
    return IbxError(ErrorCode.REQUEST_VALIDATION_FAILED, "The URL must be a public http(s) address.", retryable=False,
                    details={"errors": [{"loc": ["url"], "type": "url_not_allowed", "msg": "not a public http(s) URL"}]})


async def _fetch(fetcher: PoliteFetcher, url: str, what: str, failed_code: str, out: Gathered,
                 allowed_mime: list[str | None]) -> Page | None:
    """GET ``url`` through the polite fetcher. Problems become issues on ``out``; a refused (internal) URL raises."""
    page = await fetcher.get(url)
    if page.error == "URL_NOT_ALLOWED":
        raise _refused()
    if page.error in ("ROBOTS_DISALLOWED", "ROBOTS_UNREACHABLE"):
        out.issues.append(issue(page.error, f"{what}: {page.message}; not fetched. Paste the rules text instead.",
                                url=page.url))
        return None
    if page.error is not None:
        out.issues.append(issue(failed_code, f"{what}: {page.error} ({page.message})", url=page.url))
        return None
    if not page.ok:
        out.issues.append(issue(failed_code, f"{what}: the site answered HTTP {page.status}", url=page.url,
                                http_status=page.status))
        return None
    if page.mime not in allowed_mime:
        out.issues.append(issue(failed_code, f"{what}: unsupported content type ({page.mime})", url=page.url))
        return None
    if page.truncated:
        out.issues.append(issue("PAGE_TRUNCATED", f"{what}: only the first {fetcher.max_body} bytes were read", url=page.url))
    return page


def _looks_js_only(page: Page, text: str) -> bool:
    raw = page.text()[:200_000].lower()
    return len(text) < JS_ONLY_MAX_CHARS and ("<script" in raw or "enable javascript" in raw)


async def _platform_api(dest: DestinationRow, fetcher: PoliteFetcher, out: Gathered) -> None:
    """Rules published through the platform's own API (no HTML scraping). robots.txt applies here too."""
    instance = dest.instance or {}
    if dest.connector_key == "lemmy":
        from app.connectors.lemmy.connector import parse_community

        ref = parse_community(str(instance.get("community_url") or dest.url))
        url = f"{ref.api}/api/v3/community?name={ref.name}"
        page = await _fetch(fetcher, url, "platform API (Lemmy community)", "PLATFORM_API_FAILED", out, ["application/json"])
        if page is None:
            return
        community = (json.loads(page.body).get("community_view") or {}).get("community") or {}
        sidebar = community.get("description") or community.get("sidebar") or ""
        if sidebar:
            out.docs.append(SourceDoc("lemmy_sidebar", page.url, clean_text(sidebar), _sha(page.body), len(page.body)))
    elif dest.connector_key == "discourse":
        base = str(instance.get("base_url") or dest.url).rstrip("/")
        page = await _fetch(fetcher, f"{base}/about.json", "platform API (Discourse about.json)", "PLATFORM_API_FAILED",
                            out, ["application/json"])
        if page is None:
            return
        about = json.loads(page.body).get("about") or {}
        parts = [about.get("title") or "", about.get("description") or "", about.get("extended_site_description") or ""]
        body = clean_text("\n".join(html_to_text(p) for p in parts if p))
        if body:
            out.docs.append(SourceDoc("discourse_about", page.url, body, _sha(page.body), len(page.body)))


async def gather_sources(dest: DestinationRow, settings: Settings, http: httpx.AsyncClient | None, *,
                         rules_url: str | None = None, pasted: str | None = None,
                         use_platform_api: bool = True) -> Gathered:
    out = Gathered()
    if pasted and pasted.strip():
        data = pasted.encode()
        out.docs.append(SourceDoc("pasted", None, clean_text(pasted), _sha(data), len(data), "pasted by a person"))
    async with capture_fetcher(settings, http) as fetcher:
        if use_platform_api:
            try:
                await _platform_api(dest, fetcher, out)
            except (ValueError, KeyError, TypeError, AttributeError):
                out.issues.append(issue("PLATFORM_API_FAILED", "platform API: the platform answered with unexpected data"))
        url = rules_url or dest.rules_url
        if url:
            page = await _fetch(fetcher, url, "rules page", "RULES_URL_FAILED", out, TEXT_MIME)
            if page is not None:
                text = page_text(page.body, page.mime)
                if page.mime in (None, "text/html", "application/xhtml+xml") and _looks_js_only(page, text):
                    out.issues.append(issue("JS_ONLY_PAGE", f"rules page: almost no text without JavaScript ({len(text)} "
                                            "characters); paste the rules text instead", url=page.url))
                out.docs.append(SourceDoc("rules_url", page.url, text, _sha(page.body), len(page.body),
                                          "robots.txt checked on every hop"))
    return out


def field_warnings(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-field warnings: a quote that was not found (or is addressed to AI readers), or a field that fell back to the
    conservative default."""
    out = []
    for f in fields:
        name, status = f.get("field"), f.get("status")
        if status == "quote_not_found":
            out.append(issue("QUOTE_NOT_FOUND", f"{name}: proposed {f.get('proposed')!r}, but its quote is not in the "
                             f"text; conservative default {f.get('applied')!r} used", field=name))
        elif status == "suspicious_quote":
            out.append(issue("SUSPICIOUS_QUOTE", f"{name}: the quote is an instruction to AI readers; conservative "
                             f"default {f.get('applied')!r} used", field=name))
        elif status == "not_stated":
            out.append(issue("FIELD_DEFAULTED", f"{name}: not stated; conservative default {f.get('applied')!r} used",
                             field=name))
    return out


def _parse_warning(line: str) -> dict[str, Any]:
    code, sep, message = str(line).partition(":")
    return issue(code.strip(), message.strip()) if sep and code.strip().isupper() else issue("CAPTURE_WARNING", str(line))


# ------------------------------------------------------------------ proposing


async def propose(text: str, settings_llm: Any = None, http: httpx.AsyncClient | None = None,
                  max_chars: int | None = None) -> tuple[RulesExtraction, LLMMeta]:
    """Ask the chain (mock = deterministic extractor on the same text) for a ``RulesExtraction``."""
    from app.providers.llm.config import get_llm_settings

    llm_settings = settings_llm or get_llm_settings()
    limit = max_chars or llm_settings.LLM_MAX_INPUT_CHARS
    sent = text[:limit]
    return await complete_json(user_prompt(sent), RulesExtraction, system=SYSTEM_PROMPT,
                               mock=lambda _prompt: extract_rules(text), settings=llm_settings, http=http,
                               max_tokens=900)


def _rules_value(name: str, value: Any) -> dict[str, Any]:
    """One extraction field -> RuleSet keys."""
    if name == "frequency_cap":
        return {"frequency_cap": value}
    if name == "self_promo":
        return {"self_promo": value}
    if name == "link_policy":
        return {"link_policy": value}
    if name == "disclosure_required":
        return {"disclosure": {"required": bool(value)}}
    if name == "language":
        return {"languages": [value]}
    if name == "automation_allowed":
        return {"automation_allowed": bool(value)}
    raise KeyError(name)


def _default_value(name: str, default: RuleSet) -> Any:
    if name == "frequency_cap":
        cap = default.frequency_cap
        return f"{cap.max_posts}/{cap.window}" if cap else None
    if name == "self_promo":
        return default.self_promo
    if name == "link_policy":
        return default.link_policy
    if name == "disclosure_required":
        return default.disclosure.required
    if name == "language":
        return default.languages[0] if default.languages else None
    return default.automation_allowed


def build_proposal(extraction: RulesExtraction, docs: list[SourceDoc], *, destination_language: str | None,
                   rules_url: str | None, from_llm: bool) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Verified fields keep their value; every other field gets the conservative default and a flag.
    Returns (rules for RuleSet, field evidence, flags)."""
    lang = normalize_lang(destination_language)
    default = RuleSet.conservative_default(languages=[lang] if lang else [])
    rules: dict[str, Any] = {}
    fields: list[dict[str, Any]] = []
    flags: list[str] = []
    for name in FIELDS:
        guess = getattr(extraction, name)
        source = next((d for d in docs if guess.quote and quote_in(guess.quote, d.text)), None)
        if guess.value is not None and source is not None and looks_like_injection(guess.quote):
            status, source = "suspicious_quote", None  # the "rule" is text addressed to an AI reader
        elif guess.value is not None and source is not None:
            status = "verified"
        else:
            status = "quote_not_found" if guess.value is not None else "not_stated"
        if status == "verified":
            applied = guess.value
        else:
            applied = _default_value(name, default)
            why = {"quote_not_found": "quote not found in the page", "not_stated": "not stated",
                   "suspicious_quote": "the quote is an instruction to AI readers, not a rule"}[status]
            flags.append(f"{name}: {why}; conservative default used")
        if applied is not None:
            rules.update(_rules_value(name, applied))
        fields.append({
            "type": "field", "field": name, "status": status, "proposed": guess.value, "applied": applied,
            "default_used": status != "verified", "quote": guess.quote, "confidence": guess.confidence,
            "source_kind": source.kind if source else None, "source_url": source.url if source else None,
        })
    # A stated language means "posts must be in it"; an unknown one is the conservative default (the community's
    # catalogue language, or none known: then every post is held until a person states it).
    rules["language_policy"] = "must_match"
    rules["source"] = "llm" if from_llm else "page"
    if rules_url:
        rules["rules_url"] = rules_url
    RuleSet.model_validate(rules)  # never store something the engine can not read
    return rules, fields, flags


# ------------------------------------------------------------------ storage


async def store_proposal(session: AsyncSession, dest: DestinationRow, *, rules: dict[str, Any],
                         evidence: list[dict[str, Any]], page_sha256: str, source: str) -> RuleSetRow:
    """A new ``proposed`` rule_sets row (the next version number), under the destination's row lock."""
    await session.execute(select(DestinationRow.id).where(DestinationRow.id == dest.id).with_for_update())
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"rules:{dest.id}"})
    latest = (await session.execute(select(func.max(RuleSetRow.version))
                                    .where(RuleSetRow.destination_id == dest.id))).scalar() or 0
    row = RuleSetRow(id=new_id("rprop"), destination_id=dest.id, version=latest + 1, rules=rules, status="proposed",
                     source=source, evidence=evidence, rules_page_sha256=page_sha256)
    session.add(row)
    await session.flush()
    return row


def proposal_parts(row: RuleSetRow) -> dict[str, Any]:
    """Split a proposal's evidence list into its parts for the API and the confirm page."""
    evidence = list(row.evidence or [])
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        by_type.setdefault(str(item.get("type")), []).append(item)
    capture = (by_type.get("capture") or [{}])[0]
    return {
        "fields": by_type.get("field", []),
        "sources": by_type.get("source", []),
        "llm": (by_type.get("llm") or [None])[0],
        "capture": capture,
        "decision": (by_type.get("decision") or [None])[-1],
        "flags": capture.get("flags", []),
        "warnings": proposal_warnings(capture, by_type.get("field", [])),
    }


def proposal_warnings(capture: dict[str, Any], fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every warning a person should see before confirming: the capture's (robots, HTTP errors, JS-only page, a
    row's unverified rules ...) and the per-field ones (quote not found, default used)."""
    stored = capture.get("issues")
    if stored is None:  # older stored proposals kept "CODE: message" lines only
        stored = [_parse_warning(w) for w in capture.get("warnings") or []]
    return [*stored, *field_warnings(fields)]


def proposal_out(row: RuleSetRow, settings: Settings | None = None) -> dict[str, Any]:
    parts = proposal_parts(row)
    status = {"proposed": "proposed", "accepted": "confirmed", "rejected": "rejected"}.get(row.status, row.status)
    return {
        "proposal_id": row.id,
        "destination_id": row.destination_id,
        "status": status,
        "version": row.version,
        "source": row.source,
        "rules": row.rules,
        "fields": parts["fields"],
        "flags": parts["flags"],
        "warnings": parts["warnings"],
        "origin": parts["capture"].get("origin", "rules_capture"),
        "sources": parts["sources"],
        "llm": parts["llm"],
        "rules_page_sha256": row.rules_page_sha256,
        "captured_at": parts["capture"].get("captured_at"),
        "job_id": parts["capture"].get("job_id"),
        "decision": parts["decision"],
        "ui_url": f"/ui/rules/{row.id}",
    }


def proposal_from_row(row_rules: dict[str, Any], *, destination_language: str | None, rules_url: str | None,
                      rules_source: str, quotes: dict[str, Any] | None = None, refused_confirmation: bool = False,
                      ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """A catalogue row's rule columns as a proposal: the values the row states are kept (shown as "from the
    catalogue row", with the row's quote when it gives one); every field it does not state gets the same conservative
    default a capture would use. Returns (rules, field evidence, flags, issues)."""
    from app.rules.models import _KEY_ALIASES

    lang = normalize_lang(destination_language)
    default = RuleSet.conservative_default(languages=[lang] if lang else [])
    stated_rs = RuleSet.model_validate(row_rules)
    canonical = {_KEY_ALIASES.get(k, k) for k in row_rules}
    field_key = {"frequency_cap": "frequency_cap", "self_promo": "self_promo", "link_policy": "link_policy",
                 "disclosure_required": "disclosure", "language": "languages", "automation_allowed": "automation_allowed"}
    rules = dict(row_rules)
    fields: list[dict[str, Any]] = []
    flags: list[str] = []
    for name in FIELDS:
        stated = field_key[name] in canonical
        value = _default_value(name, stated_rs if stated else default)
        if not stated:
            flags.append(f"{name}: not stated in the catalogue row; conservative default used")
            if value is not None:
                rules.update(_rules_value(name, value))
        quote = (quotes or {}).get(name) if stated else None
        fields.append({
            "type": "field", "field": name, "status": "stated_in_row" if stated else "not_stated", "proposed": value if stated
            else None, "applied": value, "default_used": not stated, "quote": str(quote)[:500] if quote else None,
            "confidence": None, "source_kind": f"catalogue_row:{rules_source}" if stated else None,
            "source_url": rules_url if stated else None,
        })
    rules.setdefault("language_policy", "must_match")
    RuleSet.model_validate(rules)  # never store something the engine can not read
    issues = [issue("UNVERIFIED_ROW_RULES", f"These rules come from the catalogue row (rules_source={rules_source}); this "
                    "service did not read them from the community. Check them against the rules page before confirming.",
                    url=rules_url)]
    if refused_confirmation:
        issues.append(issue("CONFIRMATION_NOT_ACCEPTED", "The row says rules_status=confirmed, but only an operator's own "
                            "rules (rules_source=manual) are confirmed on import; a person confirms these here."))
    return rules, fields, flags, issues


async def get_proposal(session: AsyncSession, proposal_id: str, *, lock: bool = False) -> RuleSetRow:
    row = await session.get(RuleSetRow, proposal_id, with_for_update=lock)
    if row is None or not row.id.startswith("rprop_"):
        raise IbxError(ErrorCode.NOT_FOUND, "Unknown rule proposal.")
    return row


def now_iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")
