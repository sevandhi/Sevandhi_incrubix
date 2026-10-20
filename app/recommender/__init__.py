"""Destination recommender (OUT1): given a content item, rank destinations by fit (audience, rules, past results)
with a reason for each. Ranking is deterministic code (``ranker.py``); an LLM may only re-phrase the reason text
(``reasons.py``). The evaluation (``eval.py``) measures precision@10 on a labelled set (``catalogue/labels/``)."""

from .profiles import ContentProfile, DestProfile
from .ranker import DEFAULT_WEIGHTS, FACTORS, Ranker

__all__ = ["DEFAULT_WEIGHTS", "FACTORS", "ContentProfile", "DestProfile", "Ranker"]
