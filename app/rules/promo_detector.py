"""Is this post self-promotional? Conservative on purpose: any one signal is enough.

Signals (each one is reported as evidence, so a block always names its reason):
  flag              the caller set ``is_promotional: true``
  own_link          a link to one of the creator's own domains/channels (``owner_domains``)
  utm               a tracking parameter (``utm_source=...``) in a link
  affiliate         an affiliate/referral parameter or affiliate short link
  monetization_link a tip/shop/link-in-bio page (patreon.com, ko-fi.com, gumroad.com, linktr.ee ...)
  channel_link      a link to a channel/profile page (youtube.com/@name, twitch.tv/name ...)
  cta               a call to action ("subscribe", "use code", "link in bio" ... in en/es/fr/de/pt/hi + ja/ta)
  self_reference    "my channel", "my course", "our app" ...
  shortener         a shortened link (bit.ly ...): it hides where it goes, possibly the creator's own site

``is_promotional: false`` from the caller does NOT switch the detector off: a missed violation is worse
than an extra block, and every block says which signal fired so a person can fix the post.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.rules.links import extract_links, is_shortener, matches_domain, split_url

# Phrases are matched case-insensitively on whole words (Latin) or as substrings (other scripts).
CTA_LEXICON: dict[str, list[str]] = {
    "en": [
        "subscribe",
        "subscribe now",
        "subscribe for more",
        "hit the bell",
        "smash that like",
        "like and subscribe",
        "sign up",
        "signup",
        "sign-up",
        "register now",
        "join now",
        "join my",
        "use code",
        "use my code",
        "promo code",
        "coupon",
        "discount",
        "discount code",
        "buy now",
        "order now",
        "shop now",
        "get yours",
        "limited time",
        "free trial",
        "start your free",
        "link in bio",
        "link in my bio",
        "check out my",
        "follow me",
        "follow us",
        "dm me",
        "book a call",
        "pre-order",
        "preorder",
        "support me",
        "donate",
        "sponsored",
        "affiliate link",
        "download now",
        "get it now",
        "grab your",
        "don't miss",
        "early bird",
    ],
    "es": [
        "suscríbete",
        "suscribete",
        "suscríbanse",
        "regístrate",
        "registrate",
        "usa el código",
        "código de descuento",
        "cupón",
        "descuento",
        "compra ahora",
        "enlace en la bio",
        "link en la bio",
        "sígueme",
        "sigueme",
        "prueba gratis",
    ],
    "fr": [
        "abonnez-vous",
        "abonne-toi",
        "inscrivez-vous",
        "inscris-toi",
        "code promo",
        "utilisez le code",
        "réduction",
        "achetez maintenant",
        "lien en bio",
        "lien dans la bio",
        "suivez-moi",
        "essai gratuit",
    ],
    "de": [
        "abonniere",
        "abonnieren",
        "abonniert",
        "abonnier",
        "jetzt anmelden",
        "melde dich an",
        "meldet euch an",
        "rabattcode",
        "gutscheincode",
        "gutschein",
        "rabatt",
        "jetzt kaufen",
        "jetzt bestellen",
        "link in der bio",
        "folgt mir",
        "folge mir",
        "kostenlos testen",
    ],
    "pt": [
        "inscreva-se",
        "se inscreva",
        "assine",
        "cadastre-se",
        "cupom",
        "código de desconto",
        "desconto",
        "compre agora",
        "link na bio",
        "me siga",
        "siga-me",
        "teste grátis",
    ],
    "hi": [
        "सब्सक्राइब",
        "सब्सक्राइब करें",
        "चैनल को सब्सक्राइब",
        "छूट",
        "कूपन",
        "अभी खरीदें",
        "लिंक बायो में",
        "फॉलो करें",
        "डिस्काउंट",
    ],
    "ja": ["チャンネル登録", "登録してね", "割引", "クーポン", "今すぐ購入", "フォローして"],
    "ta": ["சப்ஸ்கிரைப்", "சப்ஸ்க்ரைப்", "தள்ளுபடி", "கூப்பன்", "இப்போதே வாங்குங்கள்"],
    "it": ["iscriviti", "iscrivetevi", "codice sconto", "acquista ora", "seguimi", "link in bio"],
    "ru": ["подписывайтесь", "подпишись", "подпишитесь", "промокод", "скидка", "купить сейчас", "ссылка в профиле"],
    "zh": ["订阅", "關注我", "关注我", "优惠码", "優惠碼", "折扣", "立即购买"],
}
CTA_PATTERNS = [
    re.compile(r"\b\d{1,3}\s?%\s?(?:off|de descuento|de réduction|rabatt|de desconto)\b", re.I),
    re.compile(r"\b(?:code|código|gutschein)\s*[:=]?\s*[A-Z0-9]{4,}\b"),
]
SELF_REFERENCE = re.compile(
    r"\b(?:my|our)\s+(?:new\s+|latest\s+)?(?:channel|course|newsletter|podcast|app|product|startup|book|ebook|"
    r"store|shop|merch|patreon|website|saas|service|agency|discord|masterclass|workshop)\b",
    re.I,
)

UTM_PARAMS = re.compile(r"^utm_", re.I)
AFFILIATE_PARAMS = {
    "ref",
    "ref_id",
    "refid",
    "referral",
    "referral_code",
    "ref_code",
    "aff",
    "affid",
    "aff_id",
    "affiliate",
    "affiliate_id",
    "partner",
    "partner_id",
    "coupon",
    "promo",
    "promocode",
    "promo_code",
    "discount",
    "discount_code",
    "via",
    "irclickid",
    "clickid",
    "click_id",
    "sub_id",
    "subid",
}
AFFILIATE_HOSTS = {"amzn.to", "amzn.eu", "go.skimresources.com", "shareasale.com", "awin1.com", "click.linksynergy.com"}
MONETIZATION_HOSTS = {
    "patreon.com",
    "ko-fi.com",
    "buymeacoffee.com",
    "gumroad.com",
    "linktr.ee",
    "beacons.ai",
    "stan.store",
    "onlyfans.com",
    "substack.com",
    "kofi.com",
    "paypal.me",
    "teespring.com",
    "redbubble.com",
    "etsy.com",
    "lemonsqueezy.com",
    "payhip.com",
}


@dataclass(frozen=True)
class Signal:
    type: str
    value: str
    field: str


@dataclass
class PromoResult:
    is_promotional: bool
    signals: list[Signal] = field(default_factory=list)

    def summary(self) -> str:
        return "; ".join(f"{s.type} '{s.value}' in {s.field}" for s in self.signals)

    def evidence(self) -> list[dict[str, str]]:
        return [{"type": s.type, "value": s.value, "field": s.field} for s in self.signals]


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    escaped = re.escape(_normalise(phrase))
    if all(not ch.isalpha() or "LATIN" in unicodedata.name(ch, "") for ch in phrase):
        return re.compile(rf"(?<!\w){escaped}(?!\w)")  # Latin script: whole words only
    return re.compile(escaped)  # Devanagari/Tamil/Japanese: substring (word boundaries are unreliable there)


_CTA_REGEXES = [(lang, phrase, _phrase_regex(phrase)) for lang, phrases in CTA_LEXICON.items() for phrase in phrases]


def find_cta(text: str | None) -> list[str]:
    """CTA phrases found in ``text`` (longest first, no overlaps reported twice)."""
    if not text:
        return []
    norm = _normalise(text)
    hits: list[str] = []
    for _lang, phrase, regex in sorted(_CTA_REGEXES, key=lambda item: -len(item[1])):
        if regex.search(norm) and not any(_normalise(phrase) in _normalise(h) for h in hits):
            hits.append(phrase)
    for pattern in CTA_PATTERNS:
        match = pattern.search(text)
        if match:
            hits.append(match.group().strip())
    return hits


def _is_channel(host: str, path: str) -> bool:
    if re.fullmatch(r"(?:m\.)?youtube\.com", host):
        return bool(re.match(r"^/(?:@[^/]+|channel/|c/|user/)", path))
    if host in ("twitch.tv", "kick.com"):
        return bool(re.match(r"^/[^/]+/?$", path))
    if host in ("tiktok.com", "instagram.com"):
        return bool(re.match(r"^/@?[^/]+/?$", path)) and not path.startswith(("/p/", "/reel/"))
    return False


def detect_promo(rendition, owner_domains=()) -> PromoResult:
    """Run every signal over the rendition. ``owner_domains`` lists the creator's own sites/channels,
    e.g. ``["creator.dev", "youtube.com/@creator"]``."""
    signals: list[Signal] = []
    if rendition.is_promotional is True:
        signals.append(Signal("flag", "is_promotional=true", "is_promotional"))

    for link in extract_links(rendition):
        host, path, query = split_url(link.url)
        for entry in owner_domains:
            if matches_domain(link.url, entry):
                signals.append(Signal("own_link", link.url, link.field))
                break
        for key, _value in query:
            if UTM_PARAMS.match(key):
                signals.append(Signal("utm", f"{key} in {link.url}", link.field))
                break
        affiliate_key = next((k for k, _ in query if k.lower() in AFFILIATE_PARAMS), None)
        is_amazon = host.startswith("amazon.") or ".amazon." in f".{host}"
        if affiliate_key is None and is_amazon:  # Amazon Associates links use ?tag=
            affiliate_key = next((k for k, _ in query if k.lower() == "tag"), None)
        if affiliate_key or host in AFFILIATE_HOSTS:
            signals.append(Signal("affiliate", affiliate_key or host, link.field))
        if host in MONETIZATION_HOSTS or any(host.endswith("." + h) for h in MONETIZATION_HOSTS):
            signals.append(Signal("monetization_link", link.url, link.field))
        if is_shortener(link.url):
            signals.append(Signal("shortener", link.url, link.field))
        if _is_channel(host, path):
            signals.append(Signal("channel_link", link.url, link.field))

    for field_name in ("title", "body"):
        text = getattr(rendition, field_name) or ""
        signals += [Signal("cta", phrase, field_name) for phrase in find_cta(text)]
        signals += [Signal("self_reference", m.group(), field_name) for m in SELF_REFERENCE.finditer(text)]
    tag_text = " ".join(rendition.tags)
    signals += [Signal("cta", phrase, "tags") for phrase in find_cta(tag_text)]

    return PromoResult(is_promotional=bool(signals), signals=signals)
