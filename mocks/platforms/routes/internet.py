"""Mock "internet" for the catalogue import (ADV1), mounted at /internet on mock-platforms.

Every path ``/internet/{site}/...`` is a synthetic web site. The site name says what it is:
``<kind>-<lang>-<topic>-<anything>``, for example ``discourse-de-ai-r1n0042``.

* ``kind``: ``discourse`` | ``lemmy`` | ``peertube`` | ``forem`` | ``ghost`` | ``plain`` (no known platform) |
  ``gone`` (404 everywhere) | ``broken`` (500 everywhere) | ``norobots`` (robots.txt disallows everything).
* ``lang``: en, es, de, fr, pt, it, nl, ja, zh, ru. Pages are written in that language.
* ``topic``: ai, video, marketing, saas, creators, founders, gardening, chess.

Behaviour that does not depend on the name is derived from a hash of the site name, so a run is repeatable:

* every request waits 200-2000 ms (``LATENCY_SCALE`` scales it; tests set it to 0);
* 5 % of sites never answer in time (they sleep ``HANG_S``, longer than the importer's 5 s timeout);
* 15 % of sites answer ``HEAD`` with 405, so the importer has to fall back to ``GET``;
* member counts are fixed per site, so enrichment can be checked.

Platform-shaped probe endpoints (only the fields the importer reads):
    discourse  GET /about.json                       about.stats.users_count
    lemmy      GET /api/v3/site                      site_view.counts.users, version
               GET /api/v3/community?name=           community_view.counts.subscribers
    peertube   GET /api/v1/config                    instance.name, serverVersion
               GET /api/v1/server/stats              totalUsers
    forem      GET /api/articles?per_page=1          [{type_of: "article", ...}]
    ghost      the home page has <meta name="generator" content="Ghost 5.96">
``generate_fixture`` writes the 1,000-row CSV used by ``scripts/prove_adv1.py`` (duplicates, bad URLs and mixed
languages) from the same rules, so the expected outcome of every row is known.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import os
import random
from dataclasses import dataclass
from html import escape
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

router = APIRouter()

LATENCY_SCALE = float(os.environ.get("IBX_INTERNET_LATENCY_SCALE", "1.0"))
HANG_S = float(os.environ.get("IBX_INTERNET_HANG_S", "12"))
TIMEOUT_PERCENT = 5
HEAD_405_PERCENT = 15

KINDS = ("discourse", "lemmy", "peertube", "forem", "ghost", "plain", "gone", "broken", "norobots")
LANGS = ("en", "es", "de", "fr", "pt", "it", "nl", "ja", "zh", "ru")
TOPICS = ("ai", "video", "marketing", "saas", "creators", "founders", "gardening", "chess")

TOPIC_WORDS: dict[str, dict[str, str]] = {
    "ai": {"en": "artificial intelligence", "es": "inteligencia artificial", "de": "künstliche Intelligenz",
           "fr": "intelligence artificielle", "pt": "inteligência artificial", "it": "intelligenza artificiale",
           "nl": "kunstmatige intelligentie", "ja": "人工知能", "zh": "人工智能", "ru": "искусственный интеллект"},
    "video": {"en": "video editing", "es": "edición de vídeo", "de": "Videobearbeitung", "fr": "montage vidéo",
              "pt": "edição de vídeo", "it": "montaggio video", "nl": "videobewerking", "ja": "動画編集", "zh": "视频剪辑",
              "ru": "видеомонтаж"},
    "marketing": {"en": "digital marketing", "es": "marketing digital", "de": "Online-Marketing", "fr": "marketing digital",
                  "pt": "marketing digital", "it": "marketing digitale", "nl": "online marketing", "ja": "デジタルマーケティング",
                  "zh": "数字营销", "ru": "цифровой маркетинг"},
    "saas": {"en": "SaaS products", "es": "productos SaaS", "de": "SaaS-Produkte", "fr": "logiciels SaaS", "pt": "produtos SaaS",
             "it": "prodotti SaaS", "nl": "SaaS-producten", "ja": "SaaSプロダクト", "zh": "SaaS产品", "ru": "SaaS-продукты"},
    "creators": {"en": "content creators", "es": "creadores de contenido", "de": "Content-Creator", "fr": "créateurs de contenu",
                 "pt": "criadores de conteúdo", "it": "creatori di contenuti", "nl": "contentmakers", "ja": "クリエイター",
                 "zh": "内容创作者", "ru": "авторы контента"},
    "founders": {"en": "startup founders", "es": "emprendedores", "de": "Gründer", "fr": "fondateurs de startups",
                 "pt": "fundadores de startups", "it": "fondatori di startup", "nl": "startup-oprichters", "ja": "起業家",
                 "zh": "创业者", "ru": "основатели стартапов"},
    "gardening": {"en": "gardening", "es": "jardinería", "de": "Gartenarbeit", "fr": "jardinage", "pt": "jardinagem",
                  "it": "giardinaggio", "nl": "tuinieren", "ja": "ガーデニング", "zh": "园艺", "ru": "садоводство"},
    "chess": {"en": "chess", "es": "ajedrez", "de": "Schach", "fr": "échecs", "pt": "xadrez", "it": "scacchi", "nl": "schaken",
              "ja": "チェス", "zh": "国际象棋", "ru": "шахматы"},
}

SENTENCES: dict[str, str] = {
    "en": "Welcome to our community about {t}. Members share questions, tips and news about {t} every day, "
          "and newcomers are always welcome here.",
    "es": "Bienvenidos a nuestra comunidad sobre {t}. Los miembros comparten preguntas, consejos y noticias sobre {t} "
          "todos los días, y los recién llegados siempre son bienvenidos.",
    "de": "Willkommen in unserer Community über {t}. Die Mitglieder teilen jeden Tag Fragen, Tipps und Neuigkeiten über {t}, "
          "und Neulinge sind hier immer willkommen.",
    "fr": "Bienvenue dans notre communauté sur {t}. Les membres partagent chaque jour des questions, des conseils et des "
          "nouvelles sur {t}, et les nouveaux venus sont toujours les bienvenus.",
    "pt": "Bem-vindos à nossa comunidade sobre {t}. Os membros compartilham perguntas, dicas e notícias sobre {t} todos os "
          "dias, e os novatos são sempre bem-vindos aqui.",
    "it": "Benvenuti nella nostra comunità su {t}. I membri condividono ogni giorno domande, consigli e notizie su {t}, "
          "e i nuovi arrivati sono sempre i benvenuti.",
    "nl": "Welkom in onze gemeenschap over {t}. Leden delen elke dag vragen, tips en nieuws over {t}, en nieuwkomers zijn "
          "hier altijd welkom.",
    "ja": "{t}についてのコミュニティへようこそ。メンバーは毎日{t}に関する質問やヒント、ニュースを共有しています。初めての方も歓迎します。",
    "zh": "欢迎来到我们的{t}社区。成员们每天分享关于{t}的问题、技巧和新闻，新成员随时欢迎。",
    "ru": "Добро пожаловать в наше сообщество, посвящённое теме «{t}». Участники каждый день делятся вопросами, советами и "
          "новостями, и новичкам здесь всегда рады.",
}
COMMUNITY_WORD = {"en": "Community", "es": "Comunidad", "de": "Community", "fr": "Communauté", "pt": "Comunidade",
                  "it": "Comunità", "nl": "Gemeenschap", "ja": "コミュニティ", "zh": "社区", "ru": "Сообщество"}


@dataclass(frozen=True)
class Site:
    name: str
    kind: str
    lang: str
    topic: str
    hangs: bool
    head_405: bool
    members: int
    subscribers: int

    @property
    def title(self) -> str:
        word = TOPIC_WORDS[self.topic][self.lang]
        return f"{word[:1].upper()}{word[1:]} {COMMUNITY_WORD[self.lang]}"

    @property
    def text(self) -> str:
        return SENTENCES[self.lang].format(t=TOPIC_WORDS[self.topic][self.lang])


def _hash(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16)


def site_profile(name: str) -> Site:
    """Everything the mock does for one site follows from its name (see the module docstring)."""
    parts = name.split("-")
    kind = parts[0] if parts and parts[0] in KINDS else "plain"
    lang = parts[1] if len(parts) > 1 and parts[1] in LANGS else "en"
    topic = parts[2] if len(parts) > 2 and parts[2] in TOPICS else "chess"
    h = _hash(name)
    hangs = kind not in ("gone", "broken") and h % 100 < TIMEOUT_PERCENT
    return Site(name=name, kind=kind, lang=lang, topic=topic, hangs=hangs, head_405=(h >> 8) % 100 < HEAD_405_PERCENT,
                members=20 + (h >> 16) % 20000, subscribers=5 + (h >> 40) % 5000)


def latency_s(site: str, method: str, path: str) -> float:
    return (0.2 + (_hash(site, method, path) % 1801) / 1000) * LATENCY_SCALE


def _page(site: Site, path: str) -> str:
    generator = {"discourse": '<meta name="generator" content="Discourse 3.3.2 - https://github.com/discourse/discourse">',
                 "ghost": '<meta name="generator" content="Ghost 5.96">',
                 "peertube": '<meta property="og:platform" content="PeerTube">'}.get(site.kind, "")
    title = escape(site.title)
    return (f'<!doctype html><html lang="{site.lang}"><head><meta charset="utf-8"><title>{title}</title>'
            f'<meta name="description" content="{escape(site.text)}">{generator}</head>'
            f"<body><h1>{title}</h1><p>{escape(site.text)}</p><p>{escape(path or '/')}</p></body></html>")


def _probe(site: Site, path: str, request: Request) -> Response | None:
    """The platform API endpoints. None = not an API path (serve an HTML page)."""
    kind = site.kind
    if kind == "discourse" and path == "about.json":
        return JSONResponse({"about": {"title": site.title, "description": site.text, "version": "3.3.2",
                                       "stats": {"users_count": site.members, "active_users_30_days": site.members // 10}}})
    if kind == "lemmy" and path == "api/v3/site":
        return JSONResponse({"site_view": {"site": {"name": site.title, "description": site.text},
                                           "counts": {"users": site.members}}, "version": "0.19.20"})
    if kind == "lemmy" and path == "api/v3/community":
        name = request.query_params.get("name") or ""
        if not name:
            return JSONResponse({"error": "couldnt_find_community"}, status_code=400)
        return JSONResponse({"community_view": {"community": {"name": name, "title": site.title, "description": site.text},
                                                "counts": {"subscribers": site.subscribers}}})
    if kind == "peertube" and path == "api/v1/config":
        return JSONResponse({"instance": {"name": site.title, "shortDescription": site.text}, "serverVersion": "6.3.0"})
    if kind == "peertube" and path == "api/v1/config/about":
        return JSONResponse({"instance": {"name": site.title, "shortDescription": site.text, "terms": "Be nice."}})
    if kind == "peertube" and path == "api/v1/server/stats":
        return JSONResponse({"totalUsers": site.members, "totalLocalVideos": site.members * 3})
    if kind == "forem" and path == "api/articles":
        return JSONResponse([{"type_of": "article", "id": 1, "title": site.title, "description": site.text}])
    if path.startswith("api/") or path.endswith(".json"):
        return JSONResponse({"error": "not_found"}, status_code=404)
    return None


async def _serve(request: Request, site_name: str, path: str) -> Response:
    site = site_profile(site_name)
    method = request.method
    if site.hangs:
        await asyncio.sleep(HANG_S)
    else:
        await asyncio.sleep(latency_s(site_name, method, path))
    if site.kind == "gone":
        return PlainTextResponse("not found", status_code=404)
    if site.kind == "broken":
        return PlainTextResponse("internal error", status_code=500)
    if path == "robots.txt":
        rules = "Disallow: /" if site.kind == "norobots" else "Disallow: /private"
        return PlainTextResponse(f"User-agent: *\n{rules}\n")
    if method == "HEAD" and site.head_405:
        return Response(status_code=405, headers={"Allow": "GET"})
    response = _probe(site, path, request)
    if response is None:
        response = HTMLResponse(_page(site, path))
    if method == "HEAD":
        return Response(status_code=response.status_code, media_type=response.media_type)
    return response


@router.api_route("/{site}", methods=["GET", "HEAD"], include_in_schema=False)
async def site_root(site: str, request: Request) -> Response:
    return await _serve(request, site, "")


@router.api_route("/{site}/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def site_path(site: str, path: str, request: Request) -> Response:
    return await _serve(request, site, path.strip("/"))


# ------------------------------------------------------------------ the 1,000-row fixture

PLATFORMS = {  # kind -> (platform_key, surface spellings incl. the seed's aliases and "" = not stated, path template)
    "discourse": ("discourse", ["forum", "topic", ""], ""),
    "lemmy": ("lemmy", ["community", "post", ""], "/c/{topic}{n}"),
    "forem": ("forem", ["tag", "article", ""], "/t/{topic}"),
    "ghost": ("ghost", ["publication", "article", ""], ""),
    "peertube": ("peertube", ["instance", ""], ""),
    "plain": ("", ["directory", "launch", ""], ""),
}
CATEGORY = {"ai": "ai", "video": "video", "marketing": "marketing", "saas": "saas", "creators": "creators",
            "founders": "founders", "gardening": "hobby", "chess": "games"}
COLUMNS = ["name", "url", "platform_key", "connector_key", "surface", "category", "audience_tags", "language", "geo",
           "est_audience", "frequency_cap", "self_promo_allowed", "link_policy", "disclosure_required", "automation_allowed"]
BAD_SYNTAX = ["not a url", "ftp://files.example.org/catalogue", "http://", "https://exa mple.com/forum", "javascript:alert(1)",
              "mailto:team@example.org", "https://user:secret@forum.example.org/", "//", "https://-bad-.example/"]
REFUSED = ["http://169.254.169.254/latest/meta-data", "http://10.0.0.5/admin", "http://postgres:5432/", "http://192.168.1.1/"]


def generate_fixture(n: int = 1000, *, base_url: str, run_tag: str = "r1", seed: int = 42) -> tuple[str, list[dict[str, Any]]]:
    """A catalogue CSV of ``n`` rows against this mock, and the expected outcome of each row.

    Mix: about 10 % duplicates of earlier rows (www., trailing slash, utm_*, fragment, upper-case host, http/https
    variants), about 6 % bad URLs (bad syntax, private/internal addresses, unresolvable hosts, 404/500 sites), mixed
    languages (10 languages, about half the rows leave ``language`` blank), about 30 % rows with no platform (the
    importer must detect it), a few unknown surfaces and unparseable rule texts. 5 % of the sites hang (the mock's rule).
    ``expected`` per row: ``duplicate`` | ``invalid`` | ``unreachable`` | ``imported`` (ok or warning).
    """
    rng = random.Random(seed)
    base = base_url.rstrip("/") + "/internet"
    rows: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []
    originals: list[int] = []
    kinds = ["discourse"] * 26 + ["lemmy"] * 26 + ["forem"] * 12 + ["ghost"] * 10 + ["peertube"] * 10 + ["plain"] * 16
    for i in range(n):
        roll = rng.random()
        row = {c: "" for c in COLUMNS}
        if originals and roll < 0.10:  # duplicate of an earlier good row, with a URL variant
            src = rows[rng.choice(originals)]
            row.update(src)
            row["url"] = _variant(src["url"], rng)
            rows.append(row)
            expected.append({"row_no": i + 1, "expected": "duplicate", "of_url": src["url"]})
            continue
        if roll < 0.13:
            row.update(name=f"Broken row {i}", url=rng.choice(BAD_SYNTAX + REFUSED), platform_key="discourse")
            rows.append(row)
            expected.append({"row_no": i + 1, "expected": "invalid"})
            continue
        if roll < 0.145:
            row.update(name=f"Missing host {i}", url=f"https://no-such-host-{run_tag}-{i}.invalid/forum", platform_key="discourse")
            rows.append(row)
            expected.append({"row_no": i + 1, "expected": "unreachable"})
            continue
        lang = rng.choice(LANGS)
        topic = rng.choice(TOPICS)
        if roll < 0.16:
            kind = rng.choice(["gone", "broken"])
        else:
            kind = rng.choice(kinds)
        site_name = f"{kind}-{lang}-{topic}-{run_tag}n{i:04d}"
        site = site_profile(site_name)
        platform, surfaces, path = PLATFORMS.get(kind, PLATFORMS["discourse"])
        if kind in ("gone", "broken"):
            platform, surfaces, path = PLATFORMS["discourse"]
        url = f"{base}/{site_name}{path.format(topic=topic, n=i)}"
        row.update(name=site.title, url=url, category=CATEGORY[topic], audience_tags=topic, geo="global")
        row["platform_key"] = platform if rng.random() < 0.7 else ""
        row["surface"] = rng.choice(surfaces)
        if rng.random() < 0.5:
            row["language"] = lang if rng.random() > 0.06 else ("en" if lang != "en" else "de")  # a few wrong languages
        if kind == "plain" and rng.random() < 0.5:
            row["connector_key"] = "assisted"
        if rng.random() < 0.2:
            row.update(frequency_cap="1/P7D", self_promo_allowed=rng.choice(["yes", "no"]), link_policy="any",
                       disclosure_required=rng.choice(["yes", "no"]), automation_allowed="yes")
        elif rng.random() < 0.05:
            row.update(frequency_cap="max 2 posts a week, ask mods first", self_promo_allowed="restricted")
        status = "imported"
        unreachable = kind in ("gone", "broken") or site.hangs
        if rng.random() < 0.01:
            row["surface"] = "carrier-pigeon"
            # with a stated platform/connector the surface is refused before any request; without one it can only be
            # judged after detection, so an unreachable site is reported as unreachable
            stated = bool(row["platform_key"] or row["connector_key"])
            status = "unreachable" if unreachable and not stated else "invalid"
        elif unreachable:
            status = "unreachable"
        rows.append(row)
        expected.append({"row_no": i + 1, "expected": status, "site": site_name, "kind": kind, "lang": lang, "topic": topic,
                         "members": site.members, "subscribers": site.subscribers})
        if status == "imported":
            originals.append(i)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue(), expected


def _variant(url: str, rng: random.Random) -> str:
    scheme, rest = url.split("://", 1)
    host, _, path = rest.partition("/")
    choice = rng.randrange(6)
    if choice == 0:
        return f"{scheme}://{host}/{path}/"
    if choice == 1:
        return f"{scheme}://{host}/{path}?utm_source=newsletter&utm_medium=email"
    if choice == 2:
        return f"{scheme}://{host}/{path}#about"
    if choice == 3:
        return f"{scheme}://{host.upper()}/{path}"
    if choice == 4:
        return f"{'https' if scheme == 'http' else 'http'}://{host}/{path}"
    return f"{scheme}://www.{host}/{path}"
