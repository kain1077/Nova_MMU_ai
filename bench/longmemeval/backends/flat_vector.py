"""
Flat vector search over the same embeddings MMU uses. No graph, no keyword
gate, no aging -- just cosine similarity against every turn.

This is the baseline that matters. MMU's claim to be more than a vector store
is its graph layer: CO_RECALLED weights, keyword gating, skills. If MMU cannot
beat this, that claim is unsupported and the honest thing is to publish the tie.

Everything here is in-process. That's deliberate: it removes HTTP, Neo4j and
container overhead from the comparison, so if MMU wins it wins on retrieval
quality rather than on the baseline being handicapped.
"""

from __future__ import annotations

import numpy as np

from ..dataset import Instance
from ..embeddings import EmbeddingClient
from .base import Retrieved


class FlatVectorBackend:
    name = "flat_vector"

    def __init__(self, client: EmbeddingClient | None = None):
        self.client = client or EmbeddingClient()
        self._matrix: np.ndarray | None = None
        self._turns: list = []

    def setup(self, instance: Instance) -> None:
        self._turns = instance.turns
        texts = [t.text for t in self._turns]
        self._matrix = self.client.embed(texts) if texts else None

    def query(self, question: str, top_k: int) -> list[Retrieved]:
        if self._matrix is None or len(self._turns) == 0:
            return []

        q = self.client.embed_one(question)
        # Both sides are L2-normalized, so the dot product IS cosine similarity.
        scores = self._matrix @ q

        k = min(top_k, len(scores))
        # argpartition for the top-k, then sort just those -- the full sort is
        # wasted work when k is 10 and there are thousands of turns.
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]

        return [
            Retrieved(
                session_id=self._turns[i].session_id,
                turn_id=self._turns[i].turn_id,
                text=self._turns[i].text,
                score=float(scores[i]),
                rank=rank,
            )
            for rank, i in enumerate(top)
        ]

    def teardown(self) -> None:
        self._matrix = None
        self._turns = []
