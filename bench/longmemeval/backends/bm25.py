"""
BM25 over the haystack turns. The floor.

Implemented here rather than pulled from rank_bm25 so the harness stays
dependency-light and so the parameters are visible: a baseline nobody can
inspect is a baseline nobody should trust.

BM25 is a genuinely strong lexical baseline and beats naive embeddings on
keyword-heavy questions more often than people expect. If a semantic system
can't clear it, that's worth knowing before publishing anything else.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from ..dataset import Instance
from .base import Retrieved

# Standard Robertson/Sparck-Jones defaults. k1 controls term-frequency
# saturation, b controls length normalization.
K1 = 1.5
B = 0.75

_TOKEN = re.compile(r"[a-z0-9']+")

# A short list on purpose. An aggressive stoplist quietly turns BM25 into a
# weaker baseline than it should be, which would flatter everything measured
# against it.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "if", "in",
    "into", "is", "it", "no", "not", "of", "on", "or", "such", "that", "the",
    "their", "then", "there", "these", "they", "this", "to", "was", "will",
    "with", "i", "you", "he", "she", "we", "my", "your",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


class BM25Backend:
    name = "bm25"

    def __init__(self, k1: float = K1, b: float = B):
        self.k1 = k1
        self.b = b
        self._turns: list = []
        self._docs: list[Counter] = []
        self._lengths: list[int] = []
        self._avg_len = 0.0
        self._idf: dict[str, float] = {}

    def setup(self, instance: Instance) -> None:
        self._turns = instance.turns
        self._docs = [Counter(tokenize(t.text)) for t in self._turns]
        self._lengths = [sum(d.values()) for d in self._docs]
        n = len(self._docs)
        self._avg_len = (sum(self._lengths) / n) if n else 0.0

        df = Counter()
        for doc in self._docs:
            df.update(doc.keys())

        # Robertson-Sparck-Jones idf with the +0.5 smoothing, floored at a small
        # positive value: the raw form goes negative for terms in more than half
        # the documents, which lets a common term subtract from a score.
        self._idf = {
            term: max(1e-6, math.log((n - freq + 0.5) / (freq + 0.5) + 1.0))
            for term, freq in df.items()
        }

    def query(self, question: str, top_k: int) -> list[Retrieved]:
        if not self._docs:
            return []

        q_terms = tokenize(question)
        scored: list[tuple[float, int]] = []

        for i, doc in enumerate(self._docs):
            length = self._lengths[i]
            norm = self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1.0))
            score = 0.0
            for term in q_terms:
                tf = doc.get(term)
                if not tf:
                    continue
                score += self._idf.get(term, 0.0) * (tf * (self.k1 + 1)) / (tf + norm)
            if score > 0:
                scored.append((score, i))

        scored.sort(key=lambda pair: -pair[0])

        return [
            Retrieved(
                session_id=self._turns[i].session_id,
                turn_id=self._turns[i].turn_id,
                text=self._turns[i].text,
                score=score,
                rank=rank,
            )
            for rank, (score, i) in enumerate(scored[:top_k])
        ]

    def teardown(self) -> None:
        self._turns = []
        self._docs = []
        self._lengths = []
        self._idf = {}
