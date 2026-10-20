"""What the recommender knows about one destination and one content item, from either source:

- ``DestProfile.from_seed_row``: a row of the real seed catalogue (``catalogue/seeds/seed_v1.csv``), used by the
  offline evaluation. The seed's rule columns are free text (``yes`` / ``no`` / ``restricted`` / ``approval`` /
  ``prohibited``), and are mapped as described on each field.
- ``DestProfile.from_db``: a ``destinations`` row plus its confirmed ``rule_sets`` row, used by the API. Rules are
  merged with the rules gate's ``resolve_ruleset`` (inline > record fields > confirmed catalogue rules).

Only rules that are actually stated are filled in; ``None`` means "not stated", which the ranker treats as unknown,
never as allowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.catalogue.normalize import normalize_url
from app.rules.lang import detect_language, normalize_lang

from .text import category_strengths, category_tokens, tokenize

LEVELS = ("L0", "L1", "L2", "L3", "L4")
VIDEO_SURFACES = {"instance", "video-platform", "channel", "video"}
LISTING_SURFACES = {"launch", "directory", "curated-list", "marketplace", "newsletter"}
FEED_SURFACES = {"feed-ingest", "ping-api"}

# Research boilerplate in the seed's notes column: true but says nothing about the audience.
_BOILERPLATE = re.compile(
    r"quote verified against|active users|posting_restricted_to_mods|/guidelines not fetched|posting via api needs|"
    r"upload needs an account|/about/instance is a js page|rules text comes from|joinpeertube index|third-party "
    r"workspace|tos page text not parsed|/tos redirects|url redirects|local videos|redirects here|about\.json|"
    r"robots\.txt|treat as assisted|ap(i|p) needs admin",
    re.I,
)


def clean_notes(text: str | None) -> str:
    if not text:
        return ""
    sentences = re.split(r"(?<=[.;])\s+", text)
    return " ".join(s for s in sentences if s.strip() and not _BOILERPLATE.search(s)).strip()


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in re.split(r"[;,]", str(value)) if p.strip()]


def _languages(value: Any) -> list[str]:
    out: list[str] = []
    for part in _split_list(value):
        code = "multi" if part.lower() in ("multi", "mul", "*", "any") else normalize_lang(part)
        if code and code not in out:
            out.append(code)
    return out


@dataclass
class DestProfile:
    ref: str  # stable key: the normalised URL (the same key labels.csv uses)
    name: str
    url: str
    connector_key: str
    surface: str  # the catalogue's own surface label (e.g. "directory"), not the connector's alias
    destination_id: str | None = None  # the database id (API); None for seed rows
    platform_key: str | None = None
    category: str | None = None
    audience_tags: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    est_audience: int | None = None
    level: str = "L0"
    status: str = "active"
    excluded_reason: str | None = None
    relevance_score: int | None = None
    relevance_reason: str | None = None
    # rules: None = not stated
    self_promo: str | None = None  # allowed | disallowed | restricted | ratio(n)
    automation: str | None = None  # yes | no | approval
    ai_content: str | None = None  # disclose | prohibited
    frequency_cap: Any = None  # app.rules.models.FrequencyCap
    frequency_cap_text: str | None = None
    language_policy: str | None = None  # any | must_match (stated)
    rules_confirmed: bool = False
    rules_text: str = ""
    notes: str = ""

    # ---- derived

    @property
    def effective_language_policy(self) -> str:
        if self.language_policy:
            return self.language_policy
        if self.languages and "en" not in self.languages and "multi" not in self.languages:
            return "must_match"
        return "any"

    @property
    def rules_known(self) -> bool:
        return any(v is not None for v in (self.self_promo, self.automation, self.ai_content, self.frequency_cap)) or \
            bool(self.frequency_cap_text) or bool(self.rules_text)

    @property
    def kind(self) -> str:
        """audience | video | listing | feed: what sort of item the destination takes (rubric §2)."""
        surface = (self.surface or "").lower()
        if surface in FEED_SURFACES or self.connector_key == "mrss":
            return "feed"
        if surface in VIDEO_SURFACES or self.connector_key == "peertube":
            return "video"
        if surface in LISTING_SURFACES:
            return "listing"
        return "audience"

    def relevance(self) -> int:
        if self.relevance_score is not None:
            return int(self.relevance_score)
        from app.catalogue.relevance import RelevanceInput, lexicon_score

        return lexicon_score(RelevanceInput(name=self.name, category=self.category,
                                            audience_tags=list(self.audience_tags))).score

    def doc_tokens(self) -> list[str]:
        tags = " ".join(t.replace("-", " ") for t in self.audience_tags)
        parts = [self.name, self.name, self.category or "", tags, tags, self.rules_text, self.notes]
        return tokenize(" ".join(parts))

    def category_strengths(self) -> dict[str, float]:
        return category_strengths(title=self.name, tags=self.audience_tags, category=self.category or "",
                                  body=f"{self.rules_text} {self.notes}")

    def tfidf_tokens(self) -> list[str]:
        return self.doc_tokens() + category_tokens(self.category_strengths())

    # ---- builders

    @classmethod
    def from_seed_row(cls, row: dict[str, str]) -> DestProfile:
        from app.rules.models import FrequencyCap

        promo = {"yes": "allowed", "no": "disallowed", "restricted": "restricted"}.get(
            (row.get("self_promo_allowed") or "").strip().lower())
        automation = {"yes": "yes", "no": "no", "approval": "approval", "restricted": "approval"}.get(
            (row.get("automation_allowed") or "").strip().lower())
        ai_content = {"yes": "disclose", "prohibited": "prohibited"}.get(
            (row.get("disclosure_required") or "").strip().lower())
        cap_text = (row.get("frequency_cap") or "").strip()
        cap = None
        if cap_text:
            try:
                cap = FrequencyCap.model_validate(cap_text)
                cap_text = ""
            except Exception:  # noqa: BLE001 - free text such as "relaunch only with significant new iterations"
                cap = None
        link = (row.get("link_policy") or "").strip()
        rules_text = " ".join(p for p in (cap_text, link, (row.get("rules_quote") or "").strip()) if p)
        audience = (row.get("est_audience") or "").strip()
        return cls(
            ref=normalize_url(row["url"]), name=row["name"].strip(), url=row["url"].strip(),
            connector_key=(row.get("connector_key") or "assisted").strip(), surface=(row.get("surface") or "").strip(),
            platform_key=(row.get("platform_key") or "").strip() or None, category=(row.get("category") or "").strip() or None,
            audience_tags=_split_list(row.get("audience_tags")), languages=_languages(row.get("language")),
            est_audience=int(audience) if audience.isdigit() else None,
            self_promo=promo, automation=automation, ai_content=ai_content, frequency_cap=cap,
            frequency_cap_text=cap_text or None, rules_text=rules_text, notes=clean_notes(row.get("notes")),
        )

    @classmethod
    def from_db(cls, row: Any, confirmed_rules: dict[str, Any] | None = None) -> DestProfile:
        """``row`` is a ``DestinationRow`` (or anything with the same attributes)."""
        from app.errors import IbxError
        from app.rules.gate import resolve_ruleset

        extra = dict(getattr(row, "extra", None) or {})
        imported = dict(extra.get("import") or {})
        surface = imported.get("surface_label") or extra.get("surface_label") or row.surface
        languages = _languages(imported.get("languages") or extra.get("languages") or row.language)
        profile = cls(
            ref=row.normalized_url or normalize_url(row.url), destination_id=row.id, name=row.name, url=row.url,
            connector_key=row.connector_key, surface=surface, platform_key=row.platform_key, category=row.category,
            audience_tags=_split_list(row.audience_tags), languages=languages, est_audience=row.est_audience,
            level=row.level or "L0", status=row.status or "active", excluded_reason=row.excluded_reason,
            relevance_score=row.relevance_score, relevance_reason=row.relevance_reason,
            rules_confirmed=confirmed_rules is not None,
        )
        page = imported.get("page") or extra.get("page") or {}
        text_bits = [str(imported.get("rules_text") or extra.get("rules_text") or ""),
                     str(extra.get("rules_quote") or imported.get("rules_quote") or "")]
        profile.rules_text = " ".join(b for b in text_bits if b).strip()
        profile.notes = " ".join(b for b in (clean_notes(extra.get("notes") or imported.get("notes")),
                                             str(page.get("description") or "")) if b).strip()
        try:
            ruleset, _source = resolve_ruleset(row, confirmed_rules)
        except IbxError:
            ruleset = None  # invalid stored rules: treat as unknown (publications would refuse them anyway)
        if ruleset is not None:
            stated = ruleset.model_fields_set
            if "self_promo" in stated:
                profile.self_promo = ruleset.self_promo
            if "automation_allowed" in stated:
                profile.automation = "yes" if ruleset.automation_allowed else "no"
            if "disclosure" in stated and ruleset.disclosure.required:
                profile.ai_content = "disclose"
            if ruleset.frequency_cap is not None:
                profile.frequency_cap = ruleset.frequency_cap
            if ruleset.language_policy:
                profile.language_policy = ruleset.language_policy
            if ruleset.languages and not profile.languages:
                profile.languages = list(ruleset.languages)
        if isinstance(extra.get("ai_content"), str) and extra["ai_content"] in ("disclose", "prohibited"):
            profile.ai_content = extra["ai_content"]
        return profile


@dataclass
class ContentProfile:
    content_id: str | None
    title: str
    body: str
    tags: list[str]
    language: str | None
    is_promotional: bool
    promo_signals: list[str]
    ai_generated: bool | None
    content_type: str = "text"
    has_video: bool = False
    has_link: bool = False
    declared_promotional: bool | None = None

    @property
    def is_product_announcement(self) -> bool:
        """Declared promotional, or links the owner's own site: what launch sites and directories list. A CTA word
        alone (e.g. "discount" in a pricing story) makes an item promotional for the rules, not a product listing."""
        return self.declared_promotional is True or any(s.startswith("own_link:") for s in self.promo_signals)

    def tokens(self) -> list[str]:
        tags = " ".join(t.replace("-", " ") for t in self.tags)
        return tokenize(" ".join((self.title, self.title, tags, tags, self.body)))

    def category_strengths(self) -> dict[str, float]:
        return category_strengths(title=self.title, tags=self.tags, body=self.body)

    def tfidf_tokens(self) -> list[str]:
        return self.tokens() + category_tokens(self.category_strengths())

    @classmethod
    def from_content(cls, content: Any, content_id: str | None = None) -> ContentProfile:
        """``content``: a dict in ContentIn spelling, a ``ContentIn`` or a ``ContentItemRow``."""
        from app.contract.models import Rendition
        from app.rules.promo_detector import detect_promo

        if not isinstance(content, dict):
            if hasattr(content, "model_dump"):
                content = content.model_dump()
            else:  # ContentItemRow
                extra = dict(getattr(content, "extra", None) or {})
                content_id = content_id or content.id
                content = {"title": content.title, "body": content.body, "tags": content.tags, "language": content.language,
                           "is_promotional": content.is_promotional, "ai_generated": content.ai_generated,
                           "owner_domains": content.owner_domains, "canonical_url": content.canonical_url,
                           "media": content.media, "content_type": extra.get("content_type") or "text",
                           "link_url": extra.get("link_url")}
        title = str(content.get("title") or "")
        body = str(content.get("body") or "")
        tags = [str(t) for t in content.get("tags") or []]
        language = normalize_lang(content.get("language")) or detect_language(f"{title}\n{body}")
        content_type = str(content.get("content_type") or "text")
        media = list(content.get("media") or [])
        has_video = content_type in ("video", "video_link") or any(
            str((m.get("type") if isinstance(m, dict) else getattr(m, "type", "")) or "").startswith("video") for m in media)
        rendition = Rendition.model_validate({
            "title": title, "body": body, "tags": tags, "language": language, "canonical_url": content.get("canonical_url"),
            "link_url": content.get("link_url"), "is_promotional": content.get("is_promotional"),
            "ai_generated": content.get("ai_generated"),
        })
        promo = detect_promo(rendition, owner_domains=tuple(content.get("owner_domains") or ()))
        return cls(
            content_id=content_id or content.get("id"), title=title, body=body, tags=tags, language=language,
            is_promotional=bool(content.get("is_promotional")) or promo.is_promotional,
            promo_signals=[f"{s.type}:{s.value}" for s in promo.signals], ai_generated=content.get("ai_generated"),
            content_type=content_type, has_video=has_video, declared_promotional=content.get("is_promotional"),
            has_link=bool(content.get("link_url") or content.get("canonical_url") or "http" in body),
        )
