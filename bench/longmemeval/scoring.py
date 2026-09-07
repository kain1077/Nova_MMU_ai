"""
Turning retrieved items into numbers.

TWO METRICS, AND WHY THEY ARE REPORTED SEPARATELY

**Retrieval recall@k** is the honest headline. It asks one question -- did the
system surface a turn from a session that actually contained the answer -- and
answers it with no model in the loop. Anyone can reproduce it exactly. It is
also the number that isolates what MMU contributes, since MMU is a retrieval
system and not a reader.

**QA accuracy** is what published comparisons quote, so it is here, but it is
confounded by two model choices (the reader and the judge) that have nothing to
do with the memory system. A better reader lifts every backend at once. Report
it with the model names attached, or it means nothing.

If the two disagree -- good retrieval, poor QA -- the retrieval is not the
problem, and saying so requires having measured both.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

import requests

from .backends.base import Retrieved
from .dataset import Instance


@dataclass
class InstanceResult:
    question_id: str
    question_type: str
    backend: str
    retrieved_sessions: list[str] = field(default_factory=list)
    evidence_sessions: list[str] = field(default_factory=list)
    hit_at_k: dict[int, bool] = field(default_factory=dict)
    scorable: bool = True          # False when no evidence is marked
    answer: str | None = None
    gold: str | None = None
    judged_correct: bool | None = None
    ingest_seconds: float = 0.0
    query_seconds: float = 0.0

    def to_json(self) -> dict:
        d = asdict(self)
        d["hit_at_k"] = {str(k): v for k, v in self.hit_at_k.items()}
        return d


def score_retrieval(instance: Instance, retrieved: list[Retrieved],
                    ks: tuple[int, ...] = (1, 3, 5, 10)) -> dict[int, bool]:
    """
    Hit@k for each k: did any of the top k come from an evidence session?

    Session granularity, not turn, because that is how LongMemEval marks
    evidence. Scoring at turn level would count a correct session retrieval as
    a miss whenever the answer spans turns, which is common.
    """
    evidence = instance.evidence_session_ids
    ordered = [r.session_id for r in sorted(retrieved, key=lambda r: r.rank)]
    return {k: any(s in evidence for s in ordered[:k]) for k in ks}


def aggregate(results: list[InstanceResult],
              ks: tuple[int, ...] = (1, 3, 5, 10)) -> dict:
    """
    Roll instance results into headline and per-category numbers.

    Instances with no marked evidence are excluded from recall@k rather than
    counted as misses -- counting them would understate every backend by the
    same amount, which is still wrong, and would make the number depend on how
    complete the evidence marking happened to be.
    """
    scorable = [r for r in results if r.scorable]
    judged = [r for r in results if r.judged_correct is not None]

    def recall_at(rows, k):
        if not rows:
            return None
        return sum(1 for r in rows if r.hit_at_k.get(k)) / len(rows)

    by_type: dict[str, dict] = {}
    for qtype in sorted({r.question_type for r in results}):
        rows = [r for r in scorable if r.question_type == qtype]
        judged_rows = [r for r in judged if r.question_type == qtype]
        by_type[qtype] = {
            "n": len([r for r in results if r.question_type == qtype]),
            "n_scorable": len(rows),
            "recall_at_k": {k: recall_at(rows, k) for k in ks},
            "qa_accuracy": (sum(1 for r in judged_rows if r.judged_correct)
                            / len(judged_rows)) if judged_rows else None,
        }

    return {
        "n_instances": len(results),
        "n_scorable": len(scorable),
        "recall_at_k": {k: recall_at(scorable, k) for k in ks},
        "qa_accuracy": (sum(1 for r in judged if r.judged_correct) / len(judged)
                        if judged else None),
        "n_judged": len(judged),
        "mean_ingest_seconds": (sum(r.ingest_seconds for r in results) / len(results)
                                if results else 0.0),
        "mean_query_seconds": (sum(r.query_seconds for r in results) / len(results)
                               if results else 0.0),
        "by_question_type": by_type,
    }


# ── optional: reader + judge ─────────────────────────────────────────────────

class ChatClient:
    """
    Minimal OpenAI-compatible chat client, for the reader and the judge.

    Both are optional. Nothing in the retrieval numbers depends on this class,
    which is the point -- the reproducible half of the benchmark has no model in
    it.
    """

    def __init__(self, base: str | None = None, model: str | None = None,
                 api_key: str | None = None, timeout: float = 180.0):
        self.base = (base or os.environ.get(
            "BENCH_CHAT_BASE", "http://127.0.0.1:1234/v1")).rstrip("/")
        self.model = model or os.environ.get("BENCH_CHAT_MODEL", "")
        self.api_key = api_key or os.environ.get("BENCH_CHAT_API_KEY", "")
        self.timeout = timeout
        self._session = requests.Session()

    def complete(self, system: str, user: str, temperature: float = 0.0) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        r = self._session.post(
            f"{self.base}/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": temperature,
            },
            headers=headers,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


READER_SYSTEM = (
    "Answer the question using only the retrieved conversation excerpts. "
    "They are fragments of past conversations with the user and may be "
    "incomplete or irrelevant. If they do not contain the answer, say exactly "
    "'I don't know' -- do not guess, and do not use outside knowledge."
)

JUDGE_SYSTEM = (
    "You grade a predicted answer against a gold answer. They need not match "
    "word for word; judge whether the prediction conveys the same fact. "
    "Respond with JSON only: {\"correct\": true} or {\"correct\": false}."
)


def read_answer(client: ChatClient, question: str,
                retrieved: list[Retrieved]) -> str:
    if not retrieved:
        return "I don't know"
    context = "\n\n".join(
        f"[{i + 1}] {r.text}" for i, r in enumerate(retrieved))
    return client.complete(
        READER_SYSTEM,
        f"Retrieved excerpts:\n{context}\n\nQuestion: {question}",
    )


def judge_answer(client: ChatClient, question: str, gold: str,
                 predicted: str) -> bool:
    raw = client.complete(
        JUDGE_SYSTEM,
        f"Question: {question}\nGold answer: {gold}\nPredicted: {predicted}",
    )
    try:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return bool(json.loads(raw[start:end + 1]).get("correct", False))
    except (ValueError, TypeError):
        pass
    # A judge that returned something unparseable has not said the answer was
    # right, so it isn't scored as right.
    return False
