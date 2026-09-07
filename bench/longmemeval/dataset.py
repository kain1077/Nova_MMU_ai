"""
Loading LongMemEval into a shape the backends can share.

A note on the schema: this loader is written against the format described in
the LongMemEval paper and release, but that format has moved before and the
field names below are the part most likely to be stale. Rather than fail with a
KeyError three layers down, `load()` validates up front and `inspect()` dumps
the real keys of a real record so a mismatch takes one command to diagnose:

    python -m bench.longmemeval.run inspect --dataset path/to/longmemeval_s.json

If the keys don't match what FIELD_CANDIDATES expects, add the actual name to
the relevant tuple. That is the whole fix -- nothing else in the harness reads
the raw records.

WHAT THE PIECES ARE

Each LongMemEval instance is one question plus a haystack of chat sessions,
most of which are distractors. A small number of sessions are marked as
evidence: they contain what the question is actually about. That marking is
what makes retrieval measurable without a reader model -- see scoring.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# Field names seen across LongMemEval releases and mirrors. First match wins.
# Add to these rather than editing call sites if you hit a variant.
FIELD_CANDIDATES = {
    "question_id":   ("question_id", "qid", "id"),
    "question":      ("question", "query", "q"),
    "answer":        ("answer", "gold_answer", "a"),
    "question_type": ("question_type", "type", "category", "task_type"),
    "haystack":      ("haystack_sessions", "sessions", "haystack"),
    "evidence":      ("answer_session_ids", "evidence_session_ids",
                      "answer_sessions", "evidence_sessions"),
    "session_dates": ("haystack_dates", "session_dates", "dates"),
    "session_ids":   ("haystack_session_ids", "session_ids"),
}


def _pick(record: dict, key: str, default: Any = None) -> Any:
    for candidate in FIELD_CANDIDATES[key]:
        if candidate in record:
            return record[candidate]
    return default


@dataclass
class Turn:
    """One utterance. `index` is its position within its own session."""
    session_id: str
    index: int
    role: str
    text: str

    @property
    def turn_id(self) -> str:
        return f"{self.session_id}#{self.index}"


@dataclass
class Session:
    session_id: str
    turns: list[Turn]
    date: str | None = None
    is_evidence: bool = False

    def as_text(self) -> str:
        return "\n".join(f"{t.role}: {t.text}" for t in self.turns)


@dataclass
class Instance:
    question_id: str
    question: str
    answer: str
    question_type: str
    sessions: list[Session] = field(default_factory=list)

    @property
    def evidence_session_ids(self) -> set[str]:
        return {s.session_id for s in self.sessions if s.is_evidence}

    @property
    def turns(self) -> list[Turn]:
        return [t for s in self.sessions for t in s.turns]

    def __repr__(self) -> str:
        return (f"Instance({self.question_id}, type={self.question_type}, "
                f"sessions={len(self.sessions)}, turns={len(self.turns)}, "
                f"evidence={len(self.evidence_session_ids)})")


def _normalize_turn(raw: Any, session_id: str, index: int) -> Turn | None:
    """
    Turns appear as dicts ({role, content}) in most releases and as bare
    strings in some mirrors. Both are accepted; anything else is skipped rather
    than crashing a 500-instance run over one malformed record.
    """
    if isinstance(raw, str):
        return Turn(session_id, index, "user", raw)
    if isinstance(raw, dict):
        text = raw.get("content") or raw.get("text") or raw.get("value") or ""
        if not text:
            return None
        return Turn(session_id, index, raw.get("role", "user"), text)
    return None


def _normalize_instance(record: dict, position: int) -> Instance:
    question_id = str(_pick(record, "question_id", f"q{position}"))
    haystack = _pick(record, "haystack", []) or []
    dates = _pick(record, "session_dates", []) or []
    explicit_ids = _pick(record, "session_ids", []) or []
    evidence = set(str(e) for e in (_pick(record, "evidence", []) or []))

    sessions: list[Session] = []
    for i, raw_session in enumerate(haystack):
        # A session id may be carried alongside the haystack, embedded in the
        # session object, or absent entirely. Synthesizing one from the
        # position is fine -- ids only have to be stable within an instance,
        # because that's the only scope anything compares them in.
        if i < len(explicit_ids):
            session_id = str(explicit_ids[i])
        elif isinstance(raw_session, dict) and "session_id" in raw_session:
            session_id = str(raw_session["session_id"])
        else:
            session_id = f"{question_id}_s{i}"

        raw_turns = raw_session
        if isinstance(raw_session, dict):
            raw_turns = (raw_session.get("turns")
                         or raw_session.get("messages")
                         or raw_session.get("content") or [])

        turns = []
        for j, raw_turn in enumerate(raw_turns if isinstance(raw_turns, list) else []):
            turn = _normalize_turn(raw_turn, session_id, j)
            if turn:
                turns.append(turn)

        if not turns:
            continue

        sessions.append(Session(
            session_id=session_id,
            turns=turns,
            date=str(dates[i]) if i < len(dates) else None,
            # Evidence is matched by id and, as a fallback, by position --
            # some mirrors mark evidence as indices into the haystack.
            is_evidence=(session_id in evidence or str(i) in evidence),
        ))

    return Instance(
        question_id=question_id,
        question=str(_pick(record, "question", "")),
        answer=str(_pick(record, "answer", "")),
        question_type=str(_pick(record, "question_type", "unknown")),
        sessions=sessions,
    )


def load(path: str | Path) -> list[Instance]:
    """Load and normalize a LongMemEval JSON file."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else raw.get("data", raw.get("instances", []))
    if not records:
        raise ValueError(
            f"{path} parsed but contained no records. Run `inspect` against it "
            f"to see what the top level actually looks like."
        )

    instances = [_normalize_instance(r, i) for i, r in enumerate(records)]

    empty = [i for i in instances if not i.sessions]
    no_evidence = [i for i in instances if i.sessions and not i.evidence_session_ids]

    if len(empty) == len(instances):
        raise ValueError(
            f"Loaded {len(instances)} records from {path} but none had usable "
            f"sessions -- the haystack field name has probably changed.\n"
            f"Run: python -m bench.longmemeval.run inspect --dataset {path}"
        )
    if empty:
        print(f"  warning: {len(empty)} instances had no usable sessions, skipped")
    if no_evidence:
        # Not fatal: retrieval recall can't be scored for these, but end-to-end
        # QA still can. scoring.py excludes them from recall@k rather than
        # counting them as misses, which would understate every backend equally
        # but still be wrong.
        print(f"  warning: {len(no_evidence)} instances have no evidence sessions "
              f"marked; excluded from recall@k, still used for QA accuracy")

    return [i for i in instances if i.sessions]


def stratified_subset(instances: list[Instance], n: int,
                      seed: int = 0) -> list[Instance]:
    """
    A subset that keeps the question-type mix of the full set.

    Debugging a harness against 500 instances is slow and against the first 50
    is misleading, because the file is usually grouped by type -- you end up
    tuning against one category. This spreads the sample across all of them.
    """
    import random
    rng = random.Random(seed)

    by_type: dict[str, list[Instance]] = {}
    for inst in instances:
        by_type.setdefault(inst.question_type, []).append(inst)

    per_type = max(1, n // max(1, len(by_type)))
    picked: list[Instance] = []
    for group in by_type.values():
        picked.extend(rng.sample(group, min(per_type, len(group))))

    # Top up to n from whatever's left, so a lopsided type distribution still
    # returns the requested count.
    if len(picked) < n:
        remaining = [i for i in instances if i not in picked]
        rng.shuffle(remaining)
        picked.extend(remaining[: n - len(picked)])

    return picked[:n]


def inspect(path: str | Path, limit: int = 2) -> None:
    """Print the real structure of a dataset file, for when loading fails."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else raw.get("data", raw.get("instances", []))

    print(f"top level      : {type(raw).__name__}")
    print(f"record count   : {len(records)}")
    if not records:
        print("No records found. Top-level keys:", list(raw)[:20] if isinstance(raw, dict) else "n/a")
        return

    first = records[0]
    print(f"record keys    : {list(first)}\n")

    for key, candidates in FIELD_CANDIDATES.items():
        found = next((c for c in candidates if c in first), None)
        mark = "ok  " if found else "MISS"
        print(f"  [{mark}] {key:<14} -> {found or 'none of ' + str(candidates)}")

    print()
    for record in records[:limit]:
        haystack = _pick(record, "haystack", [])
        print(f"question    : {str(_pick(record, 'question', ''))[:100]}")
        print(f"type        : {_pick(record, 'question_type')}")
        print(f"evidence    : {_pick(record, 'evidence')}")
        print(f"sessions    : {len(haystack)}")
        if haystack:
            s = haystack[0]
            print(f"session[0]  : {type(s).__name__}"
                  + (f", keys={list(s)}" if isinstance(s, dict) else f", len={len(s)}"))
            turns = s if isinstance(s, list) else (
                s.get("turns") or s.get("messages") or [])
            if turns:
                print(f"turn[0]     : {json.dumps(turns[0])[:200]}")
        print("-" * 60)


def iter_batched(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
