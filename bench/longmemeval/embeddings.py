"""
Embedding client for the baselines.

Deliberately points at the *same* endpoint and model MMU is configured with.
A flat-vector baseline running on different embeddings would be comparing two
things at once, and the whole reason that baseline exists is to isolate one
thing: whether MMU's graph layer adds anything on top of plain vector search
over the same vectors.
"""

from __future__ import annotations

import os

import requests

# Imported, not required. This module gets imported to read a model name even
# on runs that use no vector backend at all -- a bm25-only run should not need
# numpy installed, so the failure belongs at construction, not at import.
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

_NUMPY_HINT = ("The flat_vector baseline needs numpy.\n"
               "    pip install -r bench/requirements.txt")


def require_numpy() -> None:
    if np is None:
        raise SystemExit(_NUMPY_HINT)


class EmbeddingClient:
    def __init__(self, base: str | None = None, model: str | None = None,
                 api_key: str | None = None, batch_size: int = 64,
                 timeout: float = 120.0, *, need_numpy: bool = True):
        if need_numpy:
            require_numpy()
        # Defaults mirror MMU's own env var names so one .env configures both.
        self.base = (base or os.environ.get(
            "MMU_EMBEDDING_BASE", "http://127.0.0.1:1234/v1")).rstrip("/")
        self.model = model or os.environ.get(
            "MMU_EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")
        self.api_key = api_key or os.environ.get("MMU_EMBEDDING_API_KEY", "")
        self.batch_size = batch_size
        self.timeout = timeout
        self._session = requests.Session()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def embed(self, texts: list[str]) -> "np.ndarray":
        """
        Embed a list of texts, returned L2-normalized so cosine similarity is
        a plain dot product. Shape (len(texts), dim).
        """
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            r = self._session.post(
                f"{self.base}/embeddings",
                json={"input": batch, "model": self.model},
                headers=self._headers(),
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()["data"]
            # Some servers return embeddings out of order; `index` is
            # authoritative where present.
            ordered = sorted(data, key=lambda d: d.get("index", 0))
            vectors.extend(d["embedding"] for d in ordered)

        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    def embed_one(self, text: str) -> "np.ndarray":
        return self.embed([text])[0]

    def probe(self) -> int:
        """Return the model's dimension, and fail loudly if unreachable."""
        try:
            return int(self.embed_one("dimension probe").shape[0])
        except requests.RequestException as e:
            raise SystemExit(
                f"Embedding endpoint {self.base} is not reachable: {e}\n"
                f"The flat-vector baseline needs it. Start LM Studio (or "
                f"whatever serves your embeddings) first."
            ) from e
