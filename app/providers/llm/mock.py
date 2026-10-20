"""The ``mock`` provider: deterministic, offline, free. It is the final fallback of every chain and the only
provider tests use.

* ``MockProvider`` answers a call with the caller's own deterministic function (``mock=`` of ``complete_json``).
  It never invents an answer: without such a function it reports itself unavailable.
* ``extract_rules(text)`` is the deterministic rules extractor used for rules capture. It finds the six posting
  rules with keyword/regex patterns (English, Spanish, German, French, Portuguese) and returns each with the
  sentence that matched, copied verbatim from the page, so its output passes the same quote check as an LLM's.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.providers.llm.base import Completion, LLMError, Provider

MOCK_MODEL = "mock-heuristic-v1"


class MockProvider(Provider):
    name = "mock"
    model = MOCK_MODEL
    free_tier_terms = "Deterministic local heuristics: nothing is sent anywhere."

    def __init__(self, fn: Callable[[str], Any] | None = None) -> None:
        self.fn = fn

    def available(self) -> tuple[bool, str]:
        return (True, "ok") if self.fn is not None else (False, "no deterministic extractor for this task")

    async def complete(self, messages: list[dict[str, str]], *, json_schema: dict[str, Any] | None,
                       max_tokens: int) -> Completion:
        if self.fn is None:
            raise LLMError("unavailable", "no deterministic extractor for this task")
        prompt = messages[-1]["content"] if messages else ""
        return Completion(text="", model=MOCK_MODEL, data=self.fn(prompt))


# ------------------------------------------------------------------ deterministic rules extractor

_NUM = {
    "a": 1, "an": 1, "one": 1, "single": 1, "once": 1, "two": 2, "twice": 2, "three": 3, "thrice": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "ten": 10,
    "un": 1, "una": 1, "uno": 1, "une": 1, "um": 1, "uma": 1, "ein": 1, "eine": 1, "einen": 1, "einem": 1,
    "dos": 2, "deux": 2, "zwei": 2, "duas": 2, "tres": 3, "trois": 3, "drei": 3, "três": 3,
}
_UNITS = [
    (r"24\s*h(?:ours?)?|day|días?|dia|tag(?:e|es)?|jour|journée", "P1D"),
    (r"fortnight|two weeks|2 weeks|14 days|dos semanas|zwei wochen|deux semaines", "P14D"),
    (r"7 days|week|semana|woche|semaine", "P7D"),
    (r"30 days|month|mes|monat|mois|mês", "P30D"),
]
_UNIT_RE = "|".join(u for u, _ in _UNITS)
_NUM_RE = r"\d+|" + "|".join(sorted(map(re.escape, _NUM), key=len, reverse=True))
_POST_WORDS = (r"(?:self[- ]promotional |promotional |new )?(?:posts?|threads?|topics?|submissions?|links?|articles?|"
               r"publicaci[oó]n(?:es)?|mensajes?|temas?|beitr[äa]g(?:e|en)?|themen?|posts?|publications?|messages?|"
               r"sujets?|postage(?:m|ns)|publica[çc][õo]es)")
_PER = r"(?:per|a|an|each|every|in any|in a|within (?:a|one|any)|por|cada|al|a la|pro|je|par|chaque|in der|in einer)"


def _unit(text: str) -> str | None:
    for pattern, iso in _UNITS:
        if re.fullmatch(pattern, text.strip().lower()):
            return iso
    return None


def _num(text: str) -> int | None:
    text = text.strip().lower()
    return int(text) if text.isdigit() else _NUM.get(text)


_CAP_PATTERNS = [
    # "one post per week", "max 2 threads a day", "una publicación por semana", "ein Beitrag pro Woche"
    re.compile(rf"\b(?P<n>{_NUM_RE})\s+(?:\w+\s+){{0,2}}?{_POST_WORDS}\s+(?:\w+\s+)?{_PER}\s+(?P<u>{_UNIT_RE})\b", re.I),
    # "post at most once a week", "only once per month", "twice a day"
    re.compile(rf"\b(?P<n>once|twice|thrice)\s+(?:{_PER})\s+(?P<u>{_UNIT_RE})\b", re.I),
    # "no more than 3 times per week", "(\d) times a week"
    re.compile(rf"\b(?P<n>{_NUM_RE})\s+(?:times|veces|mal|fois|vezes)\s+(?:{_PER})\s+(?P<u>{_UNIT_RE})\b", re.I),
    # "wait 7 days between posts" / "one week between posts"
    re.compile(r"\bwait (?:at least )?(?P<n2>\d+|one|a|two) (?P<u>days?|weeks?) between (?:your )?(?:posts|submissions|threads)", re.I),
]

_PROMO_RATIO = [
    re.compile(r"\b(?P<a>\d+)\s*:\s*1\b"),  # 9:1 rule -> ratio(10)
    re.compile(r"\b(?:1|one) in (?P<n>\d+|ten|five)\b", re.I),
    re.compile(r"\b(?:no more than |at most |up to |under )?(?P<p>\d+)\s*%\s*(?:of (?:your|all) (?:posts|activity|content|submissions))", re.I),
]
_PROMO_NO = re.compile(
    r"\bno (?:spam(?:ming)?(?:,| or| and)? ?)?(?:self[- ]?promo(?:tion)?|advertis(?:ing|ements?)|promotional posts)\b"
    r"|\bself[- ]?promo(?:tion|tional posts?)?\b[^.]{0,40}\b(?:not (?:allowed|permitted|tolerated)|prohibited|banned|forbidden|removed)"
    r"|\b(?:do not|don't|never) (?:promote|advertise|plug|market)\b"
    r"|\badvertis(?:ing|ements?)\b[^.]{0,30}\b(?:not (?:allowed|permitted)|prohibited|banned|forbidden)"
    r"|\bpromotional (?:posts|content)\b[^.]{0,30}\b(?:will be removed|not allowed|prohibited|banned)"
    r"|\bno (?:se permite|está permitida) (?:la )?(?:autopromoci[oó]n|publicidad)|\b(?:prohibida|sin) (?:la )?autopromoci[oó]n"
    r"|\bautopromoci[oó]n\b[^.]{0,30}\b(?:no est[aá] permitida|prohibida|no se permite)"
    r"|\bkeine (?:eigenwerbung|werbung|selbstdarstellung)\b|\b(?:eigenwerbung|werbung)\b[^.]{0,30}\b(?:nicht erlaubt|verboten|untersagt)"
    r"|\bpas (?:d['’]|de )(?:autopromotion|publicit[ée])|\b(?:autopromotion|publicit[ée])\b[^.]{0,30}\b(?:interdite?|pas autoris[ée]e?|non autoris[ée]e?)"
    r"|\b(?:autopromo[çc][ãa]o|propaganda)\b[^.]{0,30}\b(?:n[ãa]o [ée] permitid[ao]|proibid[ao])",
    re.I,
)
_PROMO_YES = re.compile(
    r"\bself[- ]?promo(?:tion)?\b[^.]{0,30}\b(?:is |are )?(?:allowed|welcome|fine|ok|okay|permitted)\b(?![^.]{0,20}\bonly\b)"
    r"|\b(?:feel free|you (?:may|can|are welcome to)) (?:to )?(?:share|promote|post|plug) (?:your own|your)\b"
    r"|\bpromoting your own (?:work|projects?|content)\b[^.]{0,20}\b(?:is )?(?:welcome|allowed|fine)"
    r"|\bse permite (?:la )?autopromoci[oó]n|\beigenwerbung ist erlaubt|\bautopromotion (?:est )?autoris[ée]e",
    re.I,
)
_LINK_NONE = re.compile(
    r"\bno (?:external )?links\b|\b(?:external )?links (?:are|is) (?:not (?:allowed|permitted)|prohibited|banned|forbidden)"
    r"|\b(?:do not|don't|never) (?:post|include|share|add) (?:any )?(?:external )?links\b"
    r"|\bposts? (?:containing|with) (?:external )?links (?:will be|are) (?:removed|deleted)"
    r"|\bno se permiten enlaces|\benlaces (?:no est[aá]n permitidos|prohibidos)"
    r"|\bkeine (?:externen )?links\b|\blinks sind (?:nicht erlaubt|verboten)"
    r"|\bpas de liens\b|\bles liens sont interdits|\bliens (?:externes )?(?:interdits|ne sont pas autoris[ée]s)"
    r"|\bn[ãa]o (?:s[ãa]o )?permitidos links|\blinks (?:s[ãa]o )?proibidos",
    re.I,
)
_LINK_SHORT = re.compile(
    r"\b(?:url|link) ?shorteners?\b|\bshortened (?:links|urls)\b|\bbit\.ly\b|\bshort links\b"
    r"|\bacortador(?:es)? de (?:enlaces|url)|\benlaces acortados|\blinkverk[üu]rzer|\burl-verk[üu]rzer|\bgek[üu]rzte links"
    r"|\braccourcisseurs? d['’]url|\bliens raccourcis|\bencurtador(?:es)? de (?:links|url)|\blinks encurtados",
    re.I,
)
_LINK_ANY = re.compile(
    r"\blinks (?:are|is) (?:allowed|welcome|fine|ok|okay|permitted)\b|\b(?:feel free|you (?:may|can)) (?:to )?(?:include|share|post|add) (?:relevant )?links\b"
    r"|\bse permiten enlaces|\blinks sind erlaubt|\bles liens sont (?:autoris[ée]s|accept[ée]s)",
    re.I,
)
_AI = r"(?:ai|a\.i\.|artificial intelligence|machine|llm|chatgpt|generative|ia|ki)"
_DISCLOSE_YES = re.compile(
    rf"\b(?:must|have to|has to|need to|needs to|required to|should|please)\s+(?:be\s+)?(?:clearly\s+)?(?:disclose[ds]?|label(?:l?ed)?|mark(?:ed)?|tag(?:ged)?|flag(?:ged)?|state[ds]?|note[ds]?)\b[^.]{{0,60}}\b{_AI}\b"
    rf"|\b{_AI}[- ](?:generated|assisted|written|created)\b[^.]{{0,60}}\b(?:must|has to|have to|needs? to|should) be (?:clearly )?(?:disclosed|labell?ed|marked|tagged|flagged|identified)"
    rf"|\bdisclose (?:the |any |your )?use of {_AI}\b|\b{_AI} disclosure (?:is )?(?:required|mandatory)"
    r"|\bgenerad[oa]s? (?:con|por) (?:ia|inteligencia artificial)\b[^.]{0,40}\b(?:debe[n]? (?:indicarse|etiquetarse|declararse|se[ñn]alarse))"
    r"|\b(?:indica|indique|declara|etiqueta)\b[^.]{0,40}\b(?:ia|inteligencia artificial)\b"
    r"|\bki-generierte?\b[^.]{0,40}\b(?:m[üu]ssen|muss)\b[^.]{0,30}\bgekennzeichnet|\b(?:kennzeichne|kennzeichnen)\b[^.]{0,40}\bki\b"
    r"|\bg[ée]n[ée]r[ée]e?s? par (?:l['’])?(?:ia|intelligence artificielle)\b[^.]{0,40}\b(?:doi(?:t|vent) [êe]tre (?:signal|indiqu|mentionn))"
    r"|\b(?:signalez|indiquez|mentionnez)\b[^.]{0,40}\b(?:ia|intelligence artificielle)\b",
    re.I,
)
_DISCLOSE_NO = re.compile(
    rf"\bno need to (?:disclose|label|mark|tag)\b[^.]{{0,40}}\b{_AI}\b|\b{_AI}[- ]generated (?:content|posts) (?:do(?:es)? not|don't) need (?:a |to be )?(?:label|disclos)",
    re.I,
)
_LANG_NAMES = {
    "english": "en", "inglés": "en", "ingles": "en", "englisch": "en", "anglais": "en",
    "spanish": "es", "español": "es", "espanol": "es", "castellano": "es", "spanisch": "es", "espagnol": "es",
    "german": "de", "deutsch": "de", "alemán": "de", "allemand": "de",
    "french": "fr", "français": "fr", "francais": "fr", "francés": "fr", "französisch": "fr",
    "portuguese": "pt", "português": "pt", "portugues": "pt", "portugués": "pt",
    "italian": "it", "italiano": "it", "hindi": "hi", "japanese": "ja", "tamil": "ta", "dutch": "nl",
}
_LANG_ALT = "|".join(sorted(map(re.escape, _LANG_NAMES), key=len, reverse=True))
_LANGUAGE = [
    re.compile(rf"\b(?:must|should|has to|have to|need to|needs to) be (?:written |posted )?in (?P<l>{_LANG_ALT})\b", re.I),
    re.compile(rf"\b(?P<l>{_LANG_ALT})[- ]only\b|\bonly (?:in )?(?P<l2>{_LANG_ALT})\b", re.I),
    re.compile(rf"\b(?:write|post|publish|use) (?:only |exclusively )?in (?P<l>{_LANG_ALT})\b", re.I),
    re.compile(rf"\b(?:solo|s[óo]lo|únicamente|unicamente|exclusivamente) en (?P<l>{_LANG_ALT})\b", re.I),
    re.compile(rf"\b(?:nur|ausschlie(?:ß|ss)lich) (?:auf )?(?P<l>{_LANG_ALT})\b", re.I),
    re.compile(rf"\b(?:uniquement|seulement|exclusivement) en (?P<l>{_LANG_ALT})\b|\ben (?P<l2>{_LANG_ALT}) (?:uniquement|seulement)\b", re.I),
    re.compile(rf"\b(?:the )?(?:official |working )?language (?:of this (?:community|forum|group) )?is (?P<l>{_LANG_ALT})\b", re.I),
    re.compile(rf"\bin (?P<l>{_LANG_ALT}) (?:only|please)\b", re.I),
    # es / pt / fr / de: "deben estar en español", "devem ser escritas em português", "doivent être rédigées en
    # français", "müssen auf Deutsch verfasst sein"
    re.compile(rf"\b(?:deben|debe|devem|deve|doivent|doit)\b[^.]{{0,30}}\b(?:en|em) (?P<l>{_LANG_ALT})\b", re.I),
    re.compile(rf"\b(?:m[üu]ssen|muss)\b[^.]{{0,20}}\bauf (?P<l>{_LANG_ALT})\b", re.I),
]
_AUTO_NO = re.compile(
    r"\bno (?:bots|automated (?:posts|posting|accounts|submissions)|auto-?posting|scheduled posts|automation)\b"
    r"|\b(?:bots|bot accounts|automated (?:posts|posting|accounts|submissions|tools)|auto-?posting|scheduling tools|scheduled posts)\b[^.]{0,40}\b(?:are |is )?(?:not (?:allowed|permitted)|prohibited|banned|forbidden|will be (?:banned|removed))"
    r"|\b(?:do not|don't|never) (?:use )?(?:bots|automate|automation|scripts)\b"
    r"|\bposts? must be (?:made|written|submitted) by (?:a )?(?:real )?(?:human|person|people)"
    r"|\bno se permiten (?:bots|publicaciones autom[aá]ticas)|\b(?:bots|publicaciones autom[aá]ticas)\b[^.]{0,30}\b(?:prohibid[oa]s|no est[aá]n permitid[oa]s)"
    r"|\bkeine bots\b|\b(?:bots|automatisierte beitr[äa]ge)\b[^.]{0,30}\b(?:nicht erlaubt|verboten|untersagt)"
    r"|\bpas de bots\b|\b(?:bots|publications automatis[ée]es)\b[^.]{0,30}\b(?:interdit|pas autoris|non autoris)",
    re.I,
)
_AUTO_YES = re.compile(
    r"\b(?:bots|automated (?:posts|posting)|scheduled posts|scheduling tools|cross-?posting tools)\b[^.]{0,30}\b(?:are |is )?(?:allowed|welcome|fine|ok|okay|permitted)\b"
    r"|\byou (?:may|can) (?:use )?(?:bots|scheduling tools|automation)\b",
    re.I,
)

# Text that addresses an AI reader ("ignore your previous instructions ...") is never a community rule.
INJECTION = re.compile(
    r"\b(?:ignore|disregard|forget|override)\b[^.]{0,40}\b(?:instructions?|prompts?|system message)\b"
    r"|\b(?:note|message|instructions?) (?:to|for) (?:any )?(?:ai|a\.i\.|llms?|language models?|chatgpt|assistants?|bots?)\b"
    r"|\b(?:ai|llm|language model) (?:systems?|assistants?|agents?|readers?) (?:reading|parsing|summari[sz]ing)\b"
    r"|\byou are (?:an? )?(?:ai|assistant|language model)\b",
    re.I,
)


def looks_like_injection(text: str | None) -> bool:
    return bool(text) and INJECTION.search(text) is not None


_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+|\n+")


def sentences(text: str) -> list[str]:
    """Pieces of ``text`` (lines and sentences), each an exact substring, stripped."""
    out = []
    for piece in _SENTENCE_END.split(text):
        piece = piece.strip()
        if len(piece) >= 6:
            out.append(piece[:400])
    return out


def _field(value: Any, quote: str | None, confidence: float) -> dict[str, Any]:
    return {"value": value, "quote": quote, "confidence": confidence}


def _none() -> dict[str, Any]:
    return _field(None, None, 0.0)


def _cap(sentence: str) -> str | None:
    for pattern in _CAP_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        groups = match.groupdict()
        if groups.get("n2") is not None:  # "wait 7 days between posts"
            n = _num(groups["n2"]) or 1
            unit = groups["u"].lower().rstrip("s")
            days = n * (7 if unit == "week" else 1)
            return f"1/P{days}D"
        n = _num(groups["n"])
        unit = _unit(groups["u"])
        if n and unit:
            return f"{n}/{unit}"
    return None


def _promo(sentence: str) -> str | None:
    for i, pattern in enumerate(_PROMO_RATIO):
        match = pattern.search(sentence)
        if not match or not re.search(r"promo|advert|own (?:content|work|links|channel)|selbst|eigen|autopromo", sentence, re.I):
            continue
        if i == 0:
            return f"ratio({int(match.group('a')) + 1})"
        if i == 1:
            n = _num(match.group("n")) or 10
            return f"ratio({n})"
        pct = int(match.group("p"))
        if 0 < pct <= 50:
            return f"ratio({round(100 / pct)})"
    if _PROMO_NO.search(sentence):
        return "disallowed"
    if _PROMO_YES.search(sentence):
        return "allowed"
    return None


def _links(sentence: str) -> str | None:
    if _LINK_SHORT.search(sentence) and re.search(
            r"not|no |ban|prohib|forbid|remov|delet|avoid|don't|never|verboten|nicht|gel[öo]scht|entfernt|interdit|supprim|"
            r"prohibid|elimina|borra|n[ãa]o", sentence, re.I):
        return "no_shorteners"
    if _LINK_NONE.search(sentence):
        return "none"
    if _LINK_ANY.search(sentence):
        return "any"
    return None


def _disclosure(sentence: str) -> bool | None:
    if _DISCLOSE_NO.search(sentence):
        return False
    if _DISCLOSE_YES.search(sentence):
        return True
    return None


def _language(sentence: str) -> str | None:
    for pattern in _LANGUAGE:
        match = pattern.search(sentence)
        if match:
            name = match.groupdict().get("l") or match.groupdict().get("l2")
            if name:
                return _LANG_NAMES.get(name.lower())
    return None


def _automation(sentence: str) -> bool | None:
    if _AUTO_NO.search(sentence):
        return False
    if _AUTO_YES.search(sentence):
        return True
    return None


_EXTRACTORS: dict[str, tuple[Callable[[str], Any], float]] = {
    "frequency_cap": (_cap, 0.8),
    "self_promo": (_promo, 0.7),
    "link_policy": (_links, 0.7),
    "disclosure_required": (_disclosure, 0.7),
    "language": (_language, 0.7),
    "automation_allowed": (_automation, 0.7),
}


def extract_rules(text: str) -> dict[str, Any]:
    """Deterministic rules extraction. Returns the ``RulesExtraction`` shape; each found value carries the sentence
    it came from (a verbatim substring of ``text``). The first matching sentence wins for each field."""
    found = {name: _none() for name in _EXTRACTORS}
    for sentence in sentences(text):
        if looks_like_injection(sentence):
            continue
        for name, (fn, confidence) in _EXTRACTORS.items():
            if found[name]["value"] is not None:
                continue
            value = fn(sentence)
            if value is not None:
                found[name] = _field(value, sentence, confidence)
    return found
