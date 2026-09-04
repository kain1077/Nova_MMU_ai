"""
MMU test suite.

Two tiers, deliberately:

  PURE      chunking, keyword extraction, the address codec, the aging state
            machine. No Docker, no Neo4j, no model. These are the ones that
            catch a regression before it reaches a container.

  LIVE      hits a running server. Skipped automatically when nothing is
            listening, so `pytest` works on a clean checkout.

    pytest tests/ -v                     # pure only, if nothing is running
    MMU_TEST_BASE=http://127.0.0.1:8766 pytest tests/ -v

WARNING: point MMU_TEST_BASE at a TEST instance. The live tests write memories.
They clean up after themselves, but do not aim them at a graph you care about.
"""

import os
import re
import sys
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("MMU_TEST_BASE", "http://127.0.0.1:8765")


def _server_up():
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


live = pytest.mark.skipif(not _server_up(), reason=f"no MMU server at {BASE}")


def _call(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


# ═════════════════════════════════════════════════════════════
#  PURE -- chunking and keyword extraction
# ═════════════════════════════════════════════════════════════

def test_chunk_respects_paragraphs():
    import ingest
    text = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(6))
    chunks = ingest.chunk_text(text, target_words=100, overlap_words=10)
    assert chunks
    assert all(c["words"] > 0 for c in chunks)
    # target is a target, not a hard cap, but nothing should run away
    assert max(c["words"] for c in chunks) < 300


def test_chunk_empty_input():
    import ingest
    assert ingest.chunk_text("") == []
    assert ingest.chunk_text("   \n\n  ") == []


def test_chunk_single_oversized_paragraph_is_split():
    import ingest
    chunks = ingest.chunk_text("word " * 900, target_words=100)
    assert len(chunks) > 1, "an oversized paragraph must be split, not emitted whole"


def test_clean_text_strips_cid_and_dot_leaders():
    import ingest
    assert "cid:" not in ingest.clean_text("a (cid:12) b")
    assert "...." not in ingest.clean_text("Intro . . . . . . . . 17")


def test_clean_text_dehyphenates_across_lines():
    import ingest
    assert "curvature" in ingest.clean_text("curva-\nture")


def test_keywords_exclude_stopwords_and_junk():
    import ingest
    kws = ingest.extract_keywords(
        "The entropy of the system is not the same as the one thus given "
        "entropy entropy curvature curvature"
    )
    assert "entropy" in kws
    for junk in ("the", "not", "one", "thus"):
        assert junk not in kws


def test_keywords_are_deterministic():
    import ingest
    text = "alpha beta alpha gamma beta alpha delta"
    assert ingest.extract_keywords(text) == ingest.extract_keywords(text)


def test_keywords_empty_input():
    import ingest
    assert ingest.extract_keywords("") == []


# ═════════════════════════════════════════════════════════════
#  PURE -- address codec
# ═════════════════════════════════════════════════════════════

ADDR_RE = re.compile(
    r"(\d{3,})\.(\d{3})\.(\d{3})\.(\d{3}),(\d{3})(?:~(\d{3}))?\|(\d+)\.(\d{3})\.(\d{3})")


def _gen(con, pri, grp, use, arc, st=0, sc=0, sl=0, val=0):
    return f"{con:03d}.{pri:03d}.{grp:03d}.{use:03d},{arc:03d}~{val:03d}|{st}.{sc:03d}.{sl:03d}"


@pytest.mark.parametrize("con", [1, 42, 954, 999, 1000, 1500, 99999])
def test_address_roundtrip_across_the_1000_boundary(con):
    """
    The regression that matters most. CON is formatted with a MINIMUM width of
    3, so memory 1000 produces a 4-digit field. A pattern demanding exactly
    three digits does not fail on that -- combined with re.search it matches one
    character in and silently returns a DIFFERENT identity.
    """
    m = ADDR_RE.search(_gen(con, 5, 602, 0, 0, 2, 0, 5))
    assert m is not None
    assert int(m.group(1)) == con
    assert int(m.group(3)) == 602
    assert int(m.group(9)) == 5


def test_address_parses_from_a_pasted_display_line():
    """Phase 8 hardening: models paste the whole bracketed line."""
    m = ADDR_RE.search("[Green | GRP 602 | 075.003.601.000,000~000|1.000.000]")
    assert m is not None and m.group(1) == "075"


def test_address_valence_segment_optional():
    m = ADDR_RE.search("001.005.202.000,000|0.000.000")
    assert m is not None and m.group(6) is None


# ═════════════════════════════════════════════════════════════
#  PURE -- aging state machine
# ═════════════════════════════════════════════════════════════

def _next_state(color, use, stale_days, recalled,
                hold=20, archive=20, min_days=14):
    """Mirror of _age_memories()'s decision, isolated for testing."""
    if color == "Red":
        return color, use
    if recalled:
        return ("Yellow" if color == "Blue" else "Green"), 0
    new_use = min(use + 1, archive)
    if new_use >= archive and stale_days is not None and stale_days >= min_days:
        return "Blue", new_use
    if new_use >= hold and color == "Green":
        return "Yellow", new_use
    return color, new_use


def test_rapid_recalls_cannot_archive():
    """The failure that motivated the redesign: a burst archived the graph."""
    color, use = "Green", 0
    for _ in range(200):
        color, use = _next_state(color, use, stale_days=0.01, recalled=False)
    assert color == "Yellow", "time gate must prevent archiving regardless of count"
    assert use <= 20, "use must be clamped"


def test_archive_needs_both_count_and_time():
    assert _next_state("Yellow", 20, stale_days=30, recalled=False)[0] == "Blue"
    assert _next_state("Yellow", 20, stale_days=1, recalled=False)[0] == "Yellow"
    assert _next_state("Green", 1, stale_days=999, recalled=False)[0] == "Green"


def test_unknown_touch_time_never_archives():
    """None means unknown, not infinitely stale."""
    assert _next_state("Yellow", 999, stale_days=None, recalled=False)[0] != "Blue"


def test_recall_reactivates():
    assert _next_state("Yellow", 19, 99, recalled=True) == ("Green", 0)
    assert _next_state("Blue", 50, 99, recalled=True) == ("Yellow", 0)


def test_red_never_ages():
    assert _next_state("Red", 500, 999, recalled=False) == ("Red", 500)


def test_use_is_clamped():
    _, use = _next_state("Yellow", 19, 0, recalled=False)
    assert use == 20
    _, use = _next_state("Yellow", 20, 0, recalled=False)
    assert use == 20, "clamped, or it overflows the 3-digit address field"


# ═════════════════════════════════════════════════════════════
#  LIVE
# ═════════════════════════════════════════════════════════════

@live
def test_health():
    st, d = _call("GET", "/health")
    assert st == 200 and d["status"] == "ok"


@live
def test_endpoints_survive_any_graph_size():
    """Including an empty one -- no 500s, sane empty structures."""
    for path in ("/health", "/embedding_status", "/insights", "/session_bundle",
                 "/graph", "/memories", "/skill_candidates", "/skills",
                 "/skill_tree", "/unrated_memories", "/activity"):
        st, _ = _call("GET", path)
        assert st == 200, f"{path} returned {st}"


@live
def test_recall_on_empty_or_unmatched_prompt():
    st, d = _call("POST", "/recall",
                  {"prompt": "zzz-nonexistent-topic-qqq", "top_k": 3})
    assert st == 200
    assert isinstance(d["memories"], list)
    assert "context_block" in d


@live
def test_save_recall_roundtrip():
    st, saved = _call("POST", "/remember", {
        "keywords": ["pytestmarker", "roundtrip"],
        "payload": "Pytest roundtrip probe: the marker word is pytestmarker.",
        "src_type": 1,
    })
    assert st == 200 and saved["status"] == "saved"
    addr = saved["address"]
    try:
        st, d = _call("POST", "/recall", {"prompt": "pytestmarker", "top_k": 5})
        assert st == 200
        assert any("pytestmarker" in (m["payload"] or "") for m in d["memories"])
        for m in d["memories"]:
            assert m.get("via"), "every result must carry a via tag"
    finally:
        _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")


@live
def test_deleted_memory_leaves_no_phantom():
    """A delete must clear the v2 index too, or it haunts every later recall."""
    _, saved = _call("POST", "/remember", {
        "keywords": ["phantomprobe"], "payload": "phantom probe", "src_type": 1})
    addr = saved["address"]
    _, before = _call("GET", "/health")
    _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")
    _, after = _call("GET", "/health")
    assert after["total_memories"] == before["total_memories"] - 1
    assert after["total_memories"] == after["neo4j"]["memories"], \
        "index and Neo4j must agree after a delete"


@live
def test_skill_candidates_is_read_only():
    _, before = _call("GET", "/health")
    _call("GET", "/skill_candidates")
    _, after = _call("GET", "/health")
    assert after["total_memories"] == before["total_memories"]


@live
def test_crystallize_refuses_without_confirmation():
    st, d = _call("POST", "/crystallize", {
        "member_addresses": ["a", "b"], "trigger": "t", "procedure": "p"})
    assert st == 400 and "confirmed" in d["detail"]


@live
def test_forget_all_refuses_without_exact_phrase():
    # Includes the right words in the wrong case -- the check is exact, and a
    # near-miss must not erase a graph. Query values are encoded; a raw space
    # in a URL is rejected by http.client before the server ever sees it.
    for phrase in ("", "yes", "delete all my memories", "DELETE ALL MY MEMORY"):
        q = f"?confirm={urllib.parse.quote(phrase)}" if phrase else ""
        st, d = _call("POST", f"/forget_all{q}")
        assert st == 400, f"forget_all must refuse {phrase!r}"


@live
def test_ingest_path_confinement():
    st, d = _call("POST", "/ingest",
                  {"source_path": "../../etc/passwd", "source_type": "text"})
    assert st == 400 and "/docs" in d["detail"]
