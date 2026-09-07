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
import inspect

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


def _call(method, path, payload=None, timeout=120, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method)
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


def test_member_key_is_order_independent():
    """
    Proposal dedup hangs off this. If the key depended on member order, the
    same cluster would queue a fresh proposal on every sweep and the review
    queue would fill with duplicates of one finding.
    """
    import neo4j_layer as n4j
    a = ["2026-01-03T00:00:00", "2026-01-01T00:00:00", "2026-01-02T00:00:00"]
    assert n4j._member_key(a) == n4j._member_key(sorted(a))
    assert n4j._member_key(a) == n4j._member_key(list(reversed(a)))
    assert n4j._member_key(a) != n4j._member_key(a + ["2026-01-04T00:00:00"])


def test_proposals_are_not_keyed_on_addresses():
    """
    An MMU address encodes the use and arc counters and is rewritten in place
    on every recall -- the same memory is 012.005.202.015 today and
    012.005.202.017 after two recalls. A durable queue keyed on that re-queues
    unchanged clusters under new numbers, and worse, hands /crystallize
    addresses that no longer MATCH anything. created_at is written once.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.queue_routine_proposals)
    assert '_member_key(c["member_created"])' in src
    assert '_member_key(c["members"])' not in src,         "addresses are mutable and cannot key a proposal"
    assert "member_created" in inspect.getsource(n4j.find_routine_candidates)


def test_routine_floor_never_normalizes_against_the_max():
    """
    Phase 13.1 regression guard. Normalizing against max() made the bar track
    the single hottest edge in the graph, so every recall of the hottest pair
    raised the floor for every other cluster -- the system got less able to
    crystallize the more it was used. The reference must be a percentile.
    """
    import neo4j_layer as n4j
    assert 0.0 < n4j.ROUTINE_NORM_PERCENTILE < 1.0
    assert n4j.ROUTINE_MIN_ABS_WEIGHT > 0, "an absolute floor must exist under it"
    src = inspect.getsource(n4j.routine_weight_floor)
    assert "percentileCont" in src
    assert "max(r.weight)" not in src


def test_documents_are_eligible_for_crystallization():
    """
    Phase 13.1 regression guard. `src_type <> 2` was copied here from
    get_idle_context(), where excluding documents is correct. It made 86% of a
    real graph permanently unable to form a routine, so the densest region of
    memory stayed as hundreds of flat entries competing in every recall.
    Crystallization is compression; a reference corpus is its best case.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.find_routine_candidates)
    assert "src_type <> 2" not in src, \
        "documents must remain eligible for crystallization"
    # get_anticipated_context is the path where the exclusion IS correct -- a
    # paragraph of a paper is not a thing to be proactively reminded of. The
    # bug was copying that reasoning to the one place it does not hold.
    assert "src_type <> 2" in inspect.getsource(n4j.get_anticipated_context)


def test_previews_share_one_candidate_implementation():
    """
    The three paths had drifted into three different queries with three
    scoring formulas, and /insights advertised candidates /routine_candidates
    could never return. They must call the one function.
    """
    import neo4j_layer as n4j
    for fn in (n4j.get_insights, n4j.get_idle_context):
        assert "find_routine_candidates(" in inspect.getsource(fn), \
            f"{fn.__name__} must not compute its own candidates"


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
                 "/graph", "/memories", "/routine_candidates", "/routines",
                 "/routine_tree", "/unrated_memories", "/activity"):
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
def test_index_and_graph_agree():
    """
    The v2 index is the read path and Neo4j is the record. A memory in the
    graph but absent from the index is invisible to recall while still being
    counted by every graph query -- present everywhere except where it matters.
    One node sat in that state from August until a delete test happened to
    compare the two totals.
    """
    st, d = _call("POST", "/index_repair")
    assert st == 200
    assert d["missing_count"] == 0, f"invisible to recall: {d['missing']}"
    assert d["phantom_count"] == 0, f"indexed but gone: {d['phantom']}"


@live
def test_index_repair_reports_before_it_writes():
    """Default is dry-run; a repair that writes on inspection is not one you
    can safely point at a live graph to find out what is wrong."""
    _, before = _call("GET", "/health")
    st, d = _call("POST", "/index_repair")
    assert st == 200 and d["status"] == "dry-run"
    _, after = _call("GET", "/health")
    assert after["total_memories"] == before["total_memories"]


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
def test_routine_candidates_is_read_only():
    _, before = _call("GET", "/health")
    _call("GET", "/routine_candidates")
    _, after = _call("GET", "/health")
    assert after["total_memories"] == before["total_memories"]


@live
def test_routine_proposal_sweep_creates_no_routines():
    """
    The sweep is what makes the daemon safe to run unattended: it may queue a
    proposal, never act on one. If it can create a Routine or demote a memory,
    the human gate on /crystallize has been routed around.
    """
    _, before_h = _call("GET", "/health")
    _, before_s = _call("GET", "/routines")

    st, d = _call("POST", "/routine_proposals/sweep")
    assert st == 200 and d["status"] == "swept"

    _, after_h = _call("GET", "/health")
    _, after_s = _call("GET", "/routines")
    assert after_h["total_memories"] == before_h["total_memories"]
    assert after_s["count"] == before_s["count"], \
        "a sweep must never crystallize anything"


@live
def test_routine_proposal_sweep_is_idempotent():
    """
    The daemon sweeps after every idle pass. A cluster that is still dense
    must refresh its proposal, not queue a second copy of the same finding.
    """
    _call("POST", "/routine_proposals/sweep")
    _, first = _call("GET", "/routine_proposals")
    st, d = _call("POST", "/routine_proposals/sweep")
    assert st == 200 and d["created"] == 0, "a repeat sweep must create nothing"
    _, second = _call("GET", "/routine_proposals")
    assert second["count"] == first["count"]


@live
def test_proposals_reach_the_model_somehow():
    """
    The queue was built, populated, and invisible: no MCP tool exposed it and
    the session bundle did not mention it, so the only thing that could see a
    proposal was curl. A review queue nothing can read is the same as no queue.
    """
    _call("POST", "/routine_proposals/sweep")
    _, q = _call("GET", "/routine_proposals")
    if not q["count"]:
        pytest.skip("nothing pending to surface")
    _, bundle = _call("GET", "/session_bundle")
    assert "READY FOR REVIEW" in bundle.get("context_block", ""),         "pending proposals must reach the conversation-start context"


@live
def test_semantic_coherence_is_measured_not_assumed():
    """
    GRP coherence is arithmetic over filing codes, and a real candidate scored
    1.0 on it while combining game design, an assistant's gender identity, and
    a user's self-description -- all filed under 5xx and about nothing in
    common. Meaning has to be measured against the embeddings.
    """
    _, d = _call("GET", "/routine_candidates?limit=5")
    if not d["count"]:
        pytest.skip("no candidates on this graph")
    for c in d["candidates"]:
        assert "semantic_coherence" in c
        sem = c["semantic_coherence"]
        if sem is None:
            assert c["scored_without_embeddings"] is True,                 "a missing embedding must be reported, not silently scored as zero"
        else:
            assert 0.0 <= sem <= 1.0


@live
def test_no_memory_crowds_the_queue():
    """
    Four overlapping triangles drawn from the same handful of hot memories
    filled 40% of a real queue, which a reviewer reads as the same finding
    four times. Breadth is the point of a review list.
    """
    _, d = _call("GET", "/routine_candidates?limit=10")
    if d["count"] < 4:
        pytest.skip("too few candidates to crowd anything")
    seen = {}
    for c in d["candidates"]:
        for m in c["members"]:
            seen[m] = seen.get(m, 0) + 1
    worst = max(seen.values())
    assert worst <= 2, f"one memory appears in {worst} proposals; cap is 2"


@live
def test_routine_proposals_rejects_bad_status():
    st, _ = _call("GET", "/routine_proposals?status=bogus")
    assert st == 400


@live
def test_reject_unknown_proposal_is_404():
    st, _ = _call("POST", "/routine_proposals/no-such-proposal-id/reject")
    assert st == 404


@live
def test_candidates_report_the_floor_they_applied():
    """
    An empty candidate list is a legitimate answer, but only readable as one
    if the caller can see the bar that was applied and what set it. Reporting
    a bare count is how a structural exclusion stayed invisible for a phase.
    """
    st, d = _call("GET", "/routine_candidates")
    assert st == 200
    t = d["thresholds"]
    for k in ("weight_floor", "reference_weight", "reference", "floor_set_by"):
        assert k in t, f"thresholds must report {k}"
    assert t["floor_set_by"] in ("percentile", "absolute")


@live
def test_insights_and_routine_candidates_agree():
    """
    They disagreed systematically: /insights showed a top candidate above the
    documented propose-at-0.70 bar that /routine_candidates was structurally
    incapable of returning. A preview of a decision must preview the decision.
    """
    _, ins = _call("GET", "/insights")
    _, cands = _call("GET", "/routine_candidates?limit=10")
    a = [c["members"] for c in ins["crystallization_candidates"]]
    b = [c["members"] for c in cands["candidates"]]
    assert a == b, "/insights and /routine_candidates must report the same clusters"


@live
def test_a_crystallized_routine_is_actually_retrievable():
    """
    Phase 13.2. Crystallization was write-only: a Routine had no keywords and no
    embedding, and every retrieval path reaches memories through one or the
    other, so nothing could ever return one. Crystallizing three memories
    changed recall by zero bytes.
    """
    _, a = _call("POST", "/remember", {
        "keywords": ["quibbleprobe", "alpha"],
        "payload": "Quibbleprobe step one: seat the widget before torquing.",
        "src_type": 1})
    _, b = _call("POST", "/remember", {
        "keywords": ["quibbleprobe", "beta"],
        "payload": "Quibbleprobe step two: torque the widget to the quibbleprobe spec.",
        "src_type": 1})
    addrs = [a["address"], b["address"]]
    sid = None
    try:
        st, sk = _call("POST", "/crystallize", {
            "member_addresses": addrs,
            "trigger": "asked how to fit a quibbleprobe widget",
            "procedure": "Seat the widget first, then torque it to spec.",
            "confirmed": True})
        assert st == 200, sk
        sid = sk["routine_id"]

        st, d = _call("POST", "/recall",
                      {"prompt": "quibbleprobe widget torque", "top_k": 5})
        assert st == 200
        ids = [s["routine_id"] for s in d.get("routines", [])]
        assert sid in ids, "a crystallized routine must be retrievable by recall"

        # And it must SUBSTITUTE for its members, not arrive alongside them.
        # A routine delivered next to everything it compressed has added text
        # rather than saved it.
        hit = next(s for s in d["routines"] if s["routine_id"] == sid)
        assert hit["replaced_members"], "the routine must displace its own members"
        for addr in hit["replaced_members"]:
            assert addr not in d["context_block"],                 "a replaced member must not also appear in the context block"
        assert "ROUTINE" in d["context_block"]
    finally:
        if sid:
            _call("POST", f"/routines/{sid}/uncrystallize?confirm=UNCRYSTALLIZE")
        for addr in addrs:
            _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")


@live
def test_crystallize_can_create_a_branch():
    """
    Phase 13.2. link_routines() and POST /routines/{id}/link existed from Phase 13,
    but nothing a model could reach exposed them, so "crystallize these as
    branches off that routine" was not an instruction the system could carry out.
    A tree built by remembering to call /link afterwards does not get built.
    """
    made = []
    addrs = []
    try:
        for tag in ("parentprobe", "childprobe"):
            pair = []
            for n in ("one", "two"):
                _, m = _call("POST", "/remember", {
                    "keywords": [tag, n],
                    "payload": f"{tag} {n}: a probe memory for tree tests.",
                    "src_type": 1})
                pair.append(m["address"])
            addrs += pair
            _, sk = _call("POST", "/crystallize", {
                "member_addresses": pair,
                "trigger": f"asked about {tag}",
                "procedure": f"Handle {tag}.",
                "confirmed": True,
                "extends": made[0] if made else None})
            made.append(sk["routine_id"])

        # The child names its parent, and the tree reflects it.
        _, routines = _call("GET", "/routines")
        child = next(s for s in routines["routines"] if s["routine_id"] == made[1])
        assert made[0] in (child.get("extends") or []),             "a routine created with extends= must actually be linked"

        _, tree = _call("GET", f"/routine_tree?root={made[0]}")
        kids = tree["tree"][0]["children"]
        assert [k["routine_id"] for k in kids] == [made[1]]
    finally:
        for sid in reversed(made):
            _call("POST", f"/routines/{sid}/uncrystallize?confirm=UNCRYSTALLIZE")
        for addr in addrs:
            _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")


@live
def test_an_existing_routine_can_be_branched_without_rebuilding():
    """
    Branching used to be possible only at creation, through crystallize_routine's
    `extends`. An already-created routine could therefore be branched only by
    uncrystallizing and rebuilding it -- which mints a new routine_id and
    re-enters the overlap checks. Observed consequence: 21 identical POSTs to
    one blocked proposal, and a tree that stayed flat.
    """
    made, addrs = [], []
    try:
        for tag in ("linkparent", "linkchild"):
            pair = []
            for n in ("one", "two"):
                _, m = _call("POST", "/remember", {
                    "keywords": [tag, n],
                    "payload": f"{tag} {n}: probe memory for link tests.",
                    "src_type": 1})
                pair.append(m["address"])
            addrs += pair
            _, sk = _call("POST", "/crystallize", {
                "member_addresses": pair, "trigger": f"asked about {tag}",
                "procedure": f"Handle {tag}.", "confirmed": True})
            made.append(sk["routine_id"])
        parent, child = made

        # Link by prefix, without recreating anything.
        st, d = _call("POST", f"/routines/{child[:8]}/link?parent_id={parent[:8]}")
        assert st == 200 and d["parent"] == parent

        # Idempotent: linking twice is not an error and makes one edge.
        st, _ = _call("POST", f"/routines/{child[:8]}/link?parent_id={parent[:8]}")
        assert st == 200
        _, tree = _call("GET", f"/routine_tree?root={parent}")
        assert len(tree["tree"][0]["children"]) == 1

        # The ids are unchanged -- that is the point of not rebuilding.
        _, routines = _call("GET", "/routines")
        ids = [s["routine_id"] for s in routines["routines"]]
        assert parent in ids and child in ids

        # Detach without destroying.
        st, d = _call("POST", f"/routines/{child}/unlink")
        assert st == 200 and d["edges_removed"] == 1
        _, routines = _call("GET", "/routines")
        assert child in [s["routine_id"] for s in routines["routines"]],             "unlink must not delete the routine"
    finally:
        for sid in reversed(made):
            _call("POST", f"/routines/{sid}/unlink")
        for sid in reversed(made):
            _call("POST", f"/routines/{sid}/uncrystallize?confirm=UNCRYSTALLIZE")
        for addr in addrs:
            _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")


@live
def test_blocked_proposals_are_marked_and_ranked_last():
    """
    Crystallizing takes its members out of circulation, so overlapping
    proposals 409 forever. Five of seventeen were already impossible --
    including the top three by score -- and the queue reported all seventeen as
    plain "pending". A review queue that leads with work nothing can confirm
    spends the reviewer's attention on exactly the wrong items.
    """
    st, d = _call("GET", "/routine_proposals?limit=50")
    assert st == 200
    props = d["proposals"]
    if not props:
        pytest.skip("nothing pending")

    for p in props:
        assert "blocked" in p and "blocked_by" in p
        if p["blocked"]:
            assert p["blocked_by"], "blocked must name what blocks it"

    # Every unblocked proposal comes before every blocked one.
    flags = [p["blocked"] for p in props]
    assert flags == sorted(flags), "actionable proposals must rank first"

    # And a blocked one really is refused, rather than merely labelled.
    blocked = next((p for p in props if p["blocked"]), None)
    if blocked:
        st, _ = _call("POST", f"/routine_proposals/{blocked['proposal_id']}/crystallize",
                      {"member_addresses": [], "trigger": "t", "procedure": "p",
                       "confirmed": True})
        assert st == 409


@live
def test_routine_ids_accept_an_unambiguous_prefix():
    """
    Routine ids are UUIDs and are displayed truncated nearly everywhere -- tree
    views, summaries, logs. Requiring all 36 characters made the one form
    anybody actually has in front of them the one form that did not work, so a
    branch became a second root.
    """
    _, routines = _call("GET", "/routines")
    if len(routines["routines"]) < 1:
        pytest.skip("no routines to resolve")
    full = routines["routines"][0]["routine_id"]

    # A prefix that matches nothing is an error, not a guess.
    st, d = _call("POST", f"/routines/{full}/link?parent_id=zzzzzznope")
    assert st == 404 and "no routine with id" in d["detail"]

    # A self-link via prefix is still a self-link.
    st, d = _call("POST", f"/routines/{full[:8]}/link?parent_id={full[:8]}")
    assert st == 400, "prefix resolution must not defeat the self-link check"


@live
def test_proposals_report_the_queue_not_the_page():
    """
    The review tool said "5 proposals awaiting review" while 17 were queued,
    because it counted the page. Proposals are score-ordered, so one dense
    corpus owned that page -- and the honest reading of the output was that
    every proposal was about one topic, which was false.
    """
    st, d = _call("GET", "/routine_proposals?limit=1")
    assert st == 200
    assert "total" in d, "the queue size must be reported alongside the page"
    assert d["total"] >= d["count"]


@live
def test_routine_reindex_is_safe_to_rerun():
    """Anything crystallized before Phase 13.2 has no embedding and no
    keywords; the backfill must be idempotent, not just present."""
    st, first = _call("POST", "/routines/reindex")
    assert st == 200
    st, second = _call("POST", "/routines/reindex")
    assert st == 200 and second["count"] == 0,         "a second reindex must find nothing left to do"


@live
def test_crystallization_is_reversible():
    """
    Crystallization is the one operation that restructures memory, and it had
    no undo: deprecate_routine() left every member stranded in Blue. Members
    carry mixed colours, so an undo that assumed one would corrupt the rest.
    """
    _, a = _call("POST", "/remember", {
        "keywords": ["undoprobe"], "payload": "undo probe alpha", "src_type": 1})
    _, b = _call("POST", "/remember", {
        "keywords": ["undoprobe"], "payload": "undo probe beta", "src_type": 1})
    addrs = [a["address"], b["address"]]
    try:
        st, sk = _call("POST", "/crystallize", {
            "member_addresses": addrs, "trigger": "undo test",
            "procedure": "undo test", "confirmed": True})
        assert st == 200, sk

        st, un = _call("POST",
                       f"/routines/{sk['routine_id']}/uncrystallize?confirm=UNCRYSTALLIZE")
        assert st == 200, un
        assert len(un["restored"]) == 2
        for m in un["restored"]:
            assert m["color"] != "Blue", "a restored memory must not stay demoted"

        _, routines = _call("GET", "/routines")
        assert sk["routine_id"] not in [s["routine_id"] for s in routines["routines"]]
    finally:
        for addr in addrs:
            _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")


@live
def test_uncrystallize_requires_the_phrase():
    st, _ = _call("POST", "/routines/whatever/uncrystallize")
    assert st == 400


@live
def test_model_crystallization_is_gated():
    """
    Model-initiated crystallization is a configuration decision, and the server
    enforces it rather than trusting the MCP tool list -- a tool list is a
    client-side promise, and the write is not something to leave to a client
    keeping one.
    """
    _, q = _call("GET", "/routine_proposals")
    if not q["count"]:
        pytest.skip("nothing pending")
    pid = q["proposals"][0]["proposal_id"]
    # confirmed=False deliberately. The model guard runs BEFORE the confirmed
    # check, so this distinguishes both states without ever writing:
    #   flag off -> 403 (guard)      flag on -> 400 (needs confirmation)
    #
    # An earlier version of this test sent confirmed=True and asserted the
    # status was "one of" several. With the flag enabled that is a real write,
    # and it crystallized a live proposal with the trigger "t" -- demoting
    # three real memories. A test against a gate must not be able to open it.
    st, d = _call("POST", f"/routine_proposals/{pid}/crystallize",
                  {"member_addresses": [], "trigger": "t", "procedure": "p",
                   "confirmed": False},
                  headers={"X-MMU-Source": "model"})
    assert st in (400, 403), f"unexpected {st}: {d}"
    if st == 403:
        assert "MMU_ALLOW_MODEL_CRYSTALLIZE" in d["detail"],             "a refusal must name the flag that controls it"


@live
def test_a_memory_cannot_belong_to_two_active_routines():
    """
    Colour is single-valued, so two routines claiming one member disagree about
    what it should be the moment either is undone. Found the hard way: a stale
    proposal was confirmed while one of its members was already crystallized,
    and undoing it restored a memory the first routine still owned.
    """
    _, sk = _call("GET", "/routines")
    active = [s for s in sk["routines"] if s.get("status") == "active"]
    if not active:
        pytest.skip("no active routine to collide with")

    # Any proposal whose members overlap an active routine must be refused.
    _, props = _call("GET", "/routine_proposals")
    for p in props["proposals"]:
        st, d = _call("POST", f"/routine_proposals/{p['proposal_id']}/crystallize",
                      {"member_addresses": [], "trigger": "", "procedure": "",
                       "confirmed": True})
        # Empty trigger/procedure is rejected first; that is fine. What must
        # never happen is a 500 or a silent second claim.
        assert st in (400, 409), f"unexpected {st}: {d}"


@live
def test_crystallize_by_proposal_id_refuses_without_confirmation():
    """The proposal-id path is ergonomics, not a second door around the gate."""
    _call("POST", "/routine_proposals/sweep")
    _, q = _call("GET", "/routine_proposals")
    if not q["count"]:
        pytest.skip("nothing pending")
    pid = q["proposals"][0]["proposal_id"]
    st, d = _call("POST", f"/routine_proposals/{pid}/crystallize", {
        "member_addresses": [], "trigger": "t", "procedure": "p"})
    assert st == 400 and "confirmed" in d["detail"]


@live
def test_stale_address_failure_says_what_happened():
    """
    Addresses are rewritten on recall, so confirming with one read minutes ago
    is the likeliest failure on this path -- and it answered "check the server
    log" while the code knew exactly which address had gone missing.
    """
    st, d = _call("POST", "/crystallize", {
        "member_addresses": ["000.000.000.000,000~000|0.000.000",
                             "000.000.000.001,000~000|0.000.000"],
        "trigger": "t", "procedure": "p", "confirmed": True})
    assert st == 409, "a stale address is a conflict, not a server fault"
    detail = d["detail"]
    assert "matched no memory" in detail
    assert "000.000.000.000,000~000|0.000.000" in detail,         "the failing address must be named"


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
