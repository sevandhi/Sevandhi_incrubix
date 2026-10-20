"""Text features for the recommender: one tokenizer, TF-IDF cosine and BM25, in pure Python (no new dependency).

- ``tokenize`` lower-cases (NFKC + casefold), strips URLs and fediverse handles, splits on letters/digits, drops a
  small multilingual stop list, applies a light English suffix strip (``videos``→``video``, ``editing``→``edit``)
  and turns runs of CJK characters into character bigrams.
- ``category_tokens`` adds one pseudo-token ``cat_<audience>`` per IncuBrix audience that the catalogue's multilingual lexicon
  finds in the text, repeated by its field strength. This is what lets a German or Spanish item meet an English
  community about the same audience: the lexicon knows "KI", "vídeo", "creadores" and so on.
- ``TfidfIndex`` is fitted on the destination documents (the candidate corpus): sublinear tf, smoothed idf,
  L2-normalised vectors, cosine similarity. A query term that no destination uses has no idf and is ignored.
- ``Bm25Index`` is the classic Okapi BM25 (k1 = 1.2, b = 0.75) over the same tokens, without category tokens. It is
  only used as a baseline and for pooling.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence

_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_HANDLE = re.compile(r"!?[\w.-]+@[\w.-]+\.[a-z]{2,}", re.I)
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")

STOPWORDS = frozenset(
    """
    a an the and or but if then than so of to in on at by for from with without into onto over under about as is are
    was were be been being it its this that these those there here we our us you your i me my they them their he she
    his her what which who whom how why when where can could will would should may might must do does did done have
    has had not no nor only own same too very just also more most other some such any each all both few many much
    one two three via per vs etc get got let lets use used using up out off again once
    der die das und oder ist sind ein eine einen einem einer ich du wir ihr sie mit für auf aus bei von zu zum zur
    den dem des im am an als auch wie aber wenn nicht noch nur so schon habt haben hat
    el la los las un una unos unas y o que es son en de del al por para con sin como más muy pero este esta estos
    le les des du et ou est sont une pour dans avec sans sur ce cette ces qui nous vous ils elles mais pas plus
    o os as um uma em do da dos das com não mais muito
    """.split()
)


def _stem(token: str) -> str:
    """A deliberately light English suffix strip, applied to both sides alike."""
    if len(token) <= 3 or not token.isascii():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ing") and len(token) > 6:
        return token[:-3]
    if token.endswith(("ches", "shes", "xes", "sses")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is", "aas")):
        return token[:-1]
    return token


def tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _HANDLE.sub(" ", _URL.sub(" ", text))
    out: list[str] = []
    for raw in _TOKEN.findall(text):
        if _CJK.search(raw):
            chars = [c for c in raw if _CJK.match(c)]
            out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
            if len(chars) == 1:
                out.append(chars[0])
            continue
        if raw.isdigit() or raw in STOPWORDS or len(raw) < 2:
            continue
        out.append(_stem(raw))
    return out


def category_strengths(title: str = "", tags: Sequence[str] = (), body: str = "", category: str = "") -> dict[str, float]:
    """Audience categories found by the catalogue's multilingual lexicon, with their field strength (name/title 2, category and
    tags 1.5, body 1). Empty when nothing matches."""
    from app.catalogue.relevance import RelevanceInput, lexicon_score

    result = lexicon_score(RelevanceInput(name=title or "", category=category or None, audience_tags=list(tags),
                                          description=body or None))
    weights = {"name": 2.0, "title": 2.0, "category": 1.5, "tags": 1.5, "description": 1.0}
    return {cat: sum(weights[f] for f in fields) for cat, fields in result.signals.items()}


def category_tokens(strengths: dict[str, float]) -> list[str]:
    tokens: list[str] = []
    for cat, strength in strengths.items():
        tokens.extend([f"cat_{cat}"] * max(1, round(strength)))
    return tokens


class TfidfIndex:
    """TF-IDF over a fixed corpus of documents (the destinations)."""

    def __init__(self, docs: Sequence[Sequence[str]]) -> None:
        self.n = len(docs)
        df: Counter[str] = Counter()
        for doc in docs:
            df.update(set(doc))
        self.idf = {t: math.log((self.n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        self.doc_vectors = [self.vector(doc) for doc in docs]

    def vector(self, tokens: Iterable[str]) -> dict[str, float]:
        counts = Counter(t for t in tokens if t in self.idf)
        vec = {t: (1.0 + math.log(c)) * self.idf[t] for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return {t: v / norm for t, v in vec.items()} if norm else {}

    @staticmethod
    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(t, 0.0) for t, v in a.items())

    @staticmethod
    def shared_terms(a: dict[str, float], b: dict[str, float], n: int = 4) -> list[str]:
        """The terms that contribute most to cos(a, b), best first (category pseudo-tokens excluded)."""
        common = [(a[t] * b[t], t) for t in a.keys() & b.keys() if not t.startswith("cat_")]
        return [t for _, t in sorted(common, key=lambda x: (-x[0], x[1]))[:n]]


class Bm25Index:
    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avgdl = (sum(self.len) / self.n) if self.n else 0.0
        df: Counter[str] = Counter()
        for doc in docs:
            df.update(set(doc))
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: Iterable[str]) -> list[float]:
        terms = [t for t in set(query) if t in self.idf]
        out = []
        for tf, dl in zip(self.tf, self.len, strict=True):
            s = 0.0
            for t in terms:
                f = tf.get(t, 0)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            out.append(s)
        return out
