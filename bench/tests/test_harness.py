"""
Tests for the harness itself, on a synthetic dataset.

These exist because a benchmark that is silently broken is worse than no
benchmark: it produces numbers, and numbers get quoted. Everything here runs
with nothing running -- no MMU, no embedding endpoint, no dataset download.

The synthetic fixture is built so the correct answer is knowable: one session
contains a distinctive string that appears nowhere else, so a working retriever
must rank it first and a broken one cannot do so by accident.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.longmemeval import dataset as ds          # noqa: E402
from bench.longmemeval import safety                 # noqa: E402
from bench.longmemeval.backends.bm25 import BM25Backend, tokenize   # noqa: E402
from bench.longmemeval.scoring import (InstanceResult, aggregate,   # noqa: E402
                                       score_retrieval)


# ── fixture ──────────────────────────────────────────────────────────────────

NEEDLE = "peregrine falcon named Bartholomew"


def _raw_instance(qid: str = "q0") -> dict:
    """One instance: three distractor sessions and one that holds the answer."""
    return {
        "question_id": qid,
        "question": "What is the name of my bird?",
        "answer": "Bartholomew",
        "question_type": "single-session-user",
        "answer_session_ids": [f"{qid}_s2"],
        "haystack_session_ids": [f"{qid}_s{i}" for i in range(4)],
        "haystack_sessions": [
            [{"role": "user", "content": "I need to rotate the tires on the car."},
             {"role": "assistant", "content": "Most makers suggest every 5,000 miles."}],
            [{"role": "user", "content": "The sourdough starter is not rising."},
             {"role": "assistant", "content": "Try feeding it twice a day for a week."}],
            [{"role": "user", "content": f"I adopted a {NEEDLE} last spring."},
             {"role": "assistant", "content": "What a striking bird to care for."}],
            [{"role": "user", "content": "Remind me to file the quarterly taxes."},
             {"role": "assistant", "content": "The deadline is the fifteenth."}],
        ],
    }


@pytest.fixture
def instance():
    return ds._normalize_instance(_raw_instance(), 0)


@pytest.fixture
def dataset_file(tmp_path):
    path = tmp_path / "synthetic.json"
    records = [_raw_instance(f"q{i}") for i in range(6)]
    for i, record in enumerate(records):
        record["question_type"] = ["temporal", "multi-session", "knowledge-update"][i % 3]
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


# ── dataset ──────────────────────────────────────────────────────────────────

def test_normalize_builds_sessions_and_turns(instance):
    assert len(instance.sessions) == 4
    assert len(instance.turns) == 8
    assert instance.question_id == "q0"
    assert instance.answer == "Bartholomew"


def test_evidence_session_is_marked(instance):
    assert instance.evidence_session_ids == {"q0_s2"}
    evidence = [s for s in instance.sessions if s.is_evidence]
    assert len(evidence) == 1
    assert NEEDLE in evidence[0].as_text()


def test_turn_ids_are_unique_within_an_instance(instance):
    ids = [t.turn_id for t in instance.turns]
    assert len(ids) == len(set(ids))


def test_load_roundtrips_a_file(dataset_file):
    instances = ds.load(dataset_file)
    assert len(instances) == 6
    assert all(i.sessions for i in instances)


def test_bare_string_turns_are_accepted():
    raw = _raw_instance()
    raw["haystack_sessions"] = [["just a string turn", "and another"]]
    raw["haystack_session_ids"] = ["q0_s0"]
    inst = ds._normalize_instance(raw, 0)
    assert len(inst.sessions) == 1
    assert inst.sessions[0].turns[0].text == "just a string turn"


def test_missing_haystack_field_raises_with_a_useful_message(tmp_path):
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps([{"question": "q?", "totally_different_key": []}]),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="inspect"):
        ds.load(path)


def test_stratified_subset_spans_question_types(dataset_file):
    instances = ds.load(dataset_file)
    subset = ds.stratified_subset(instances, 3, seed=1)
    assert len(subset) == 3
    assert len({i.question_type for i in subset}) == 3


# ── BM25 ─────────────────────────────────────────────────────────────────────

def test_bm25_ranks_the_evidence_session_first(instance):
    backend = BM25Backend()
    backend.setup(instance)
    results = backend.query("What is the name of my peregrine falcon?", top_k=5)
    assert results, "BM25 returned nothing for a query with obvious lexical overlap"
    assert results[0].session_id == "q0_s2"


def test_bm25_teardown_leaves_nothing(instance):
    backend = BM25Backend()
    backend.setup(instance)
    backend.teardown()
    assert backend.query("anything at all", top_k=5) == []


def test_tokenize_drops_stopwords_but_keeps_content():
    tokens = tokenize("The name of my bird is Bartholomew")
    assert "bartholomew" in tokens
    assert "the" not in tokens
    assert "of" not in tokens


def test_bm25_idf_never_goes_negative(instance):
    """A term in most documents must not subtract from a score."""
    backend = BM25Backend()
    backend.setup(instance)
    assert all(v > 0 for v in backend._idf.values())


# ── scoring ──────────────────────────────────────────────────────────────────

class _Fake:
    """Stands in for Retrieved without importing it, to keep this test honest
    about only needing session_id and rank."""
    def __init__(self, session_id, rank):
        self.session_id = session_id
        self.rank = rank


def test_score_retrieval_respects_rank(instance):
    hits = score_retrieval(instance, [_Fake("q0_s0", 0), _Fake("q0_s2", 1)], ks=(1, 3))
    assert hits[1] is False      # evidence was not first
    assert hits[3] is True       # but was within three


def test_score_retrieval_all_miss(instance):
    hits = score_retrieval(instance, [_Fake("q0_s0", 0), _Fake("q0_s1", 1)], ks=(1, 5))
    assert hits == {1: False, 5: False}


def test_aggregate_excludes_unscorable_instances():
    rows = [
        InstanceResult("q0", "temporal", "mmu", hit_at_k={1: True}, scorable=True),
        InstanceResult("q1", "temporal", "mmu", hit_at_k={1: False}, scorable=True),
        # No evidence marked: must not be counted as a miss.
        InstanceResult("q2", "temporal", "mmu", hit_at_k={1: False}, scorable=False),
    ]
    summary = aggregate(rows, ks=(1,))
    assert summary["n_instances"] == 3
    assert summary["n_scorable"] == 2
    assert summary["recall_at_k"][1] == 0.5


def test_aggregate_reports_per_question_type():
    rows = [
        InstanceResult("q0", "temporal", "mmu", hit_at_k={1: True}, scorable=True),
        InstanceResult("q1", "abstention", "mmu", hit_at_k={1: False}, scorable=True),
    ]
    summary = aggregate(rows, ks=(1,))
    assert summary["by_question_type"]["temporal"]["recall_at_k"][1] == 1.0
    assert summary["by_question_type"]["abstention"]["recall_at_k"][1] == 0.0


def test_aggregate_leaves_qa_none_when_nothing_was_judged():
    rows = [InstanceResult("q0", "temporal", "mmu", hit_at_k={1: True})]
    assert aggregate(rows, ks=(1,))["qa_accuracy"] is None


# ── safety ───────────────────────────────────────────────────────────────────

def test_production_port_is_refused_before_any_network_call():
    with pytest.raises(safety.UnsafeTarget, match="default MMU"):
        safety.check_target("http://127.0.0.1:8765")


def test_production_port_refusal_can_be_overridden(monkeypatch):
    """--allow-default-port gets past check 1, but check 2 still applies."""
    monkeypatch.setattr(safety, "_health", lambda *a, **k: {"total_memories": 5})
    monkeypatch.setattr(safety, "has_sentinel", lambda *a, **k: False)
    with pytest.raises(safety.UnsafeTarget, match="no harness sentinel"):
        safety.check_target("http://127.0.0.1:8765", allow_default_port=True)


def test_graph_without_sentinel_is_refused(monkeypatch):
    monkeypatch.setattr(safety, "_health", lambda *a, **k: {"total_memories": 1009})
    monkeypatch.setattr(safety, "has_sentinel", lambda *a, **k: False)
    with pytest.raises(safety.UnsafeTarget, match="1009"):
        safety.check_target("http://127.0.0.1:8766")


def test_graph_with_sentinel_is_accepted(monkeypatch):
    monkeypatch.setattr(safety, "_health", lambda *a, **k: {"total_memories": 12})
    monkeypatch.setattr(safety, "has_sentinel", lambda *a, **k: True)
    target = safety.check_target("http://127.0.0.1:8766")
    assert target.base_url == "http://127.0.0.1:8766"
    assert target.url("/recall") == "http://127.0.0.1:8766/recall"


def test_erase_refuses_when_the_sentinel_vanished(monkeypatch):
    monkeypatch.setattr(safety, "has_sentinel", lambda *a, **k: False)
    with pytest.raises(safety.UnsafeTarget, match="Sentinel missing"):
        safety.erase(safety.Target("http://127.0.0.1:8766"))


def test_unreachable_server_is_not_silently_treated_as_empty():
    with pytest.raises(safety.UnsafeTarget, match="Could not reach"):
        safety.check_target("http://127.0.0.1:9", require_sentinel=False)
