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

The live tests write memories, and every /recall they make ages every memory it
does not return. That is not destructive -- aged memories go Green -> Yellow and
come back on the next recall -- but it is a real change to a real graph, and a
long enough test run can push memories to Blue.

So the default target is port 8766, the second-instance convention, and NOT 8765,
which is where a real graph usually lives. Aiming the live tests at 8765 needs an
explicit opt-in:

    MMU_TEST_ALLOW_PRODUCTION=1 MMU_TEST_BASE=http://127.0.0.1:8765 pytest tests/

This default used to be 8765, which meant a bare `pytest tests/` on a machine
running MMU normally silently exercised the user's own memories. It aged four of
them before anyone noticed, and turned up an index-drift bug on the way -- a good
outcome from a bad default, but the default was still backwards.

[Running a second instance] in the README covers standing up something disposable.
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

# Defaults to the second-instance port on purpose. See the module docstring.
# Neo4j credentials have to be in the environment BEFORE neo4j_layer is
# imported anywhere in this process. Its NEO4J_* names are module-level
# constants read at import time, so whichever test imported it first froze in
# whatever the environment held then -- and a helper loading .env afterwards
# could not undo it. Every Neo4j-backed test then skipped itself with
# "no Neo4j" while Neo4j was running the whole time, which is worse than
# failing, because nothing reports it.
#
# Loaded ONLY from .env or an already-set environment. No default URI, on
# purpose: bolt://127.0.0.1:7687 is where a developer's real graph lives, and
# these tests write. Defaulting to it would aim them at production the moment
# a checkout had no .env -- the same hazard MMU_TEST_ALLOW_PRODUCTION guards
# on the HTTP side, which the Neo4j side had no equivalent for. Without
# credentials the driver stays unconfigured and the tests skip, which is the
# safe direction to fail in.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = os.path.join(_ROOT, ".env")
if os.path.exists(_ENV):
    with open(_ENV, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())
if os.environ.get("NEO4J_PASS"):
    os.environ.setdefault("NEO4J_USER", "neo4j")
    os.environ.setdefault("NEO4J_ENABLED", "true")

BASE = os.environ.get("MMU_TEST_BASE", "http://127.0.0.1:8766")

# The port a normal MMU install listens on, and therefore the one the live tests
# must not touch without being told to.
PRODUCTION_PORT = "8765"
ALLOW_PRODUCTION = os.environ.get("MMU_TEST_ALLOW_PRODUCTION", "").strip() not in ("", "0", "false", "False")

_AIMED_AT_PRODUCTION = f":{PRODUCTION_PORT}" in BASE and not ALLOW_PRODUCTION

_PRODUCTION_REASON = (
    f"refusing to run live tests against {BASE}: port {PRODUCTION_PORT} is the "
    f"default MMU port. These tests write memories and age the ones they don't "
    f"return. Point MMU_TEST_BASE at a throwaway instance, or set "
    f"MMU_TEST_ALLOW_PRODUCTION=1 if this really is disposable."
)


def _server_up():
    # Checked only when the target is allowed, so a refused target costs no
    # request at all -- the same ordering the benchmark harness uses.
    if _AIMED_AT_PRODUCTION:
        return False
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


_LIVE_OK = _server_up()

live = pytest.mark.skipif(
    not _LIVE_OK,
    reason=(_PRODUCTION_REASON if _AIMED_AT_PRODUCTION else f"no MMU server at {BASE}"),
)

if _AIMED_AT_PRODUCTION:
    # A skip reason is easy to miss in a quiet run, and this one is the
    # difference between "the live tests didn't run" and "the live tests ran
    # against your real graph".
    print(f"\n!! {_PRODUCTION_REASON}\n", file=sys.stderr)


# mmu_server imports fastapi, a container dependency that is routinely absent
# on the host. pytest.importorskip() turned that into five SILENT skips: the
# two per-request-state regressions and all three batched-aging regressions did
# not run on any machine -- not in CI, not on the author's box -- and a summary
# line that prints skips next to passes reads as green. These tests cover the
# server itself, so a missing fastapi is a broken environment, not an optional
# extra, and it should say so.
try:
    import mmu_server as _mmu_server
    _MMU_SERVER_ERROR = None
except Exception as e:          # ImportError normally; a half-built env can raise others
    _mmu_server = None
    _MMU_SERVER_ERROR = f"{type(e).__name__}: {e}"

_SERVER_IMPORT_REASON = (
    f"cannot import mmu_server ({_MMU_SERVER_ERROR}). These tests exercise the "
    f"server module directly, so this is a missing host dependency rather than "
    f"an optional extra -- install it with: pip install -r requirements.txt"
)

needs_mmu_server = pytest.mark.skipif(_mmu_server is None, reason=_SERVER_IMPORT_REASON)

if _mmu_server is None:
    # Mirrors the production banner above, for anyone importing this module
    # outside pytest. Under pytest both banners are swallowed by collection-
    # time capture, which is why conftest.py repeats them in the report
    # header, where nothing can capture them.
    print("\n!! " + _SERVER_IMPORT_REASON + "\n", file=sys.stderr)


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


# ═════════════════════════════════════════════════════════════
#  PURE -- v2 index address rewrites
# ═════════════════════════════════════════════════════════════
#
# The address is not a stable identifier: _gen_addr() encodes USE into it, so
# aging rewrites it. The index is keyed by address and IS the read path, so
# every rewrite is a two-store operation that can go wrong in the middle.
# These are the tests for that seam.


def _index(tmp_path):
    from light_index_v2 import LightIndexV2
    return LightIndexV2(path=str(tmp_path / "idx.json"))


def _addr(con, use, pri=5, grp=101, st=1):
    return _gen(con, pri, grp, use, 0, st, 0, 0)


def test_two_memories_sharing_a_con_converge_on_one_address():
    """
    Why the collision below is inevitable rather than unlucky.

    USE clamps at ARCHIVE_THRESH, so every un-recalled memory eventually parks
    on the same USE value. Two memories that share a CON differ in nothing else
    the address records -- and duplicate CONs exist: they are the residue of the
    old count()+1 numbering, fixed at the source in get_next_con() but not
    repairable after the fact.
    """
    a_use, b_use = 3, 17
    for _ in range(50):
        a_use = min(a_use + 1, 20)
        b_use = min(b_use + 1, 20)
    assert _addr(33, a_use) == _addr(33, b_use), \
        "clamping makes duplicate-CON addresses converge, so rename must expect it"


def test_rename_never_evicts_the_memory_already_there(tmp_path):
    """
    The root cause of v2 index drift, reduced to three lines.

    rename() used to be `cache[new] = cache.pop(old)`. When `new` was occupied
    that silently destroyed the occupant's card: two memories in, one card out.
    Neo4j kept both nodes -- m.address is UNIQUE, so it rejected the same rename
    and the exception was swallowed -- so the evicted memory stayed in the
    graph, counted by /health and by every Cypher query, and invisible to
    recall, because the index IS the read path.

    That is the exact shape /index_repair reported on the live graph: one
    MISSING address, PHANTOM empty. A removal with no matching add is not
    ordinary drift; ordinary drift moves a card, it does not delete one.
    """
    ix = _index(tmp_path)
    climbing = _addr(33, 19)     # the memory whose USE counter is advancing
    parked   = _addr(33, 20)     # a duplicate-CON memory already sitting there
    ix.add(climbing, ["alpha"])
    ix.add(parked,   ["beta"])

    moved = ix.rename(climbing, parked)

    assert moved is False, "a rename onto an occupied address must be refused"
    assert len(ix.shortcut_cache) == 2, "no memory may be dropped from the index"
    assert climbing in ix.shortcut_cache, "the mover keeps its old address"
    assert ix.shortcut_cache[parked]["keywords"] == ["beta"], \
        "the occupant's card must be untouched, not overwritten"
    assert climbing in ix.keyword_index["alpha"], \
        "a refused rename must not move the gate's pointers either"


def test_rename_refuses_when_the_source_card_is_gone(tmp_path):
    """
    The other half of the same line. The keyword rewrite used to run whether or
    not a card was found, so renaming an unindexed address pointed live gate()
    terms at an address holding no card -- findable, unrankable and
    unhydratable, which reaches a caller as a recall quietly returning fewer
    results than it claimed.
    """
    ix = _index(tmp_path)
    present = _addr(41, 0)
    ix.add(present, ["gamma"])
    ghost = _addr(99, 0)

    assert ix.rename(ghost, _addr(99, 1)) is False
    assert list(ix.shortcut_cache) == [present]
    for term, addrs in ix.keyword_index.items():
        for a in addrs:
            assert a in ix.shortcut_cache, \
                f"{a} is reachable through the gate on '{term}' but has no card"


def test_an_uncontested_rename_still_moves_everything(tmp_path):
    """The guard must not cost the ordinary case: card, gate pointers and any
    neighbour references all follow, or the index rots the other way."""
    ix = _index(tmp_path)
    old, new = _addr(41, 0), _addr(41, 1)
    other = _addr(42, 0)
    ix.add(old, ["gamma"])
    ix.add(other, ["delta"])
    ix.shortcut_cache[other]["neighbors"] = [[old, 0.9, "similar"]]

    assert ix.rename(old, new) is True
    assert new in ix.shortcut_cache and old not in ix.shortcut_cache
    assert ix.shortcut_cache[new]["keywords"] == ["gamma"]
    assert new in ix.keyword_index["gamma"] and old not in ix.keyword_index["gamma"]
    assert ix.shortcut_cache[other]["neighbors"][0][0] == new, \
        "neighbour references must follow, or expansion points at nothing"


def test_rename_to_the_same_address_is_a_no_op(tmp_path):
    ix = _index(tmp_path)
    a = _addr(41, 0)
    ix.add(a, ["gamma"])
    assert ix.rename(a, a) is True
    assert ix.shortcut_cache[a]["keywords"] == ["gamma"]


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
    src = inspect.getsource(n4j.queue_skill_proposals)
    assert '_member_key(c["member_created"])' in src
    assert '_member_key(c["members"])' not in src,         "addresses are mutable and cannot key a proposal"
    assert "member_created" in inspect.getsource(n4j.find_skill_candidates)


def test_skill_floor_never_normalizes_against_the_max():
    """
    Phase 13.1 regression guard. Normalizing against max() made the bar track
    the single hottest edge in the graph, so every recall of the hottest pair
    raised the floor for every other cluster -- the system got less able to
    crystallize the more it was used. The reference must be a percentile.
    """
    import neo4j_layer as n4j
    assert 0.0 < n4j.SKILL_NORM_PERCENTILE < 1.0
    assert n4j.SKILL_MIN_ABS_WEIGHT > 0, "an absolute floor must exist under it"
    src = inspect.getsource(n4j.skill_weight_floor)
    assert "percentileCont" in src
    assert "max(r.weight)" not in src


def test_documents_are_eligible_for_crystallization():
    """
    Phase 13.1 regression guard. `src_type <> 2` was copied here from
    get_idle_context(), where excluding documents is correct. It made 86% of a
    real graph permanently unable to form a skill, so the densest region of
    memory stayed as hundreds of flat entries competing in every recall.
    Crystallization is compression; a reference corpus is its best case.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.find_skill_candidates)
    assert "src_type <> 2" not in src, \
        "documents must remain eligible for crystallization"
    # get_anticipated_context is the path where the exclusion IS correct -- a
    # paragraph of a paper is not a thing to be proactively reminded of. The
    # bug was copying that reasoning to the one place it does not hold.
    assert "src_type <> 2" in inspect.getsource(n4j.get_anticipated_context)


def test_previews_share_one_candidate_implementation():
    """
    The three paths had drifted into three different queries with three
    scoring formulas, and /insights advertised candidates /skill_candidates
    could never return. They must call the one function.
    """
    import neo4j_layer as n4j
    for fn in (n4j.get_insights, n4j.get_idle_context):
        assert "find_skill_candidates(" in inspect.getsource(fn), \
            f"{fn.__name__} must not compute its own candidates"


# ── Crystallized members stop accumulating ──
#
# The roadmap's stated reason for crystallizing is that a hot path CONVERTS
# into a skill "rather than accumulating recall-weight without bound forever".
# Nothing was wired to that. Members were paired like any other memory in both
# the graph and this index, and the aging pass promoted them back out of Blue
# on their first direct hit -- so a crystallized cluster went on thickening
# exactly as if it had never been compressed. On the graph this was found on,
# two of the three highest-degree hubs were crystallized members.


def test_a_crystallized_member_does_not_thicken_its_cluster(tmp_path):
    """
    The behaviour the whole fix exists for. A member still surfaces -- it is
    still a direct keyword hit -- it just stops making its own cluster denser
    every time it does.
    """
    idx = _index(tmp_path)
    for a in ("A", "B", "C"):
        idx.add(a, [f"kw{a}"])
    idx.set_skill_member(["B"], True)

    for _ in range(10):
        idx.bump_corecall(["A", "B", "C"])

    def neighbours(addr):
        return {n[0] for n in idx.shortcut_cache[addr]["neighbors"]}

    assert neighbours("A") == {"C"}, "A must not thicken toward the member"
    assert neighbours("C") == {"A"}, "C must not thicken toward the member"
    assert neighbours("B") == set(), "the member accumulates nothing"


def test_unflagged_memories_still_pair_normally(tmp_path):
    """
    The exclusion must be membership, not a general dampening. Everything that
    is not compressed keeps behaving exactly as it did.
    """
    idx = _index(tmp_path)
    for a in ("A", "B"):
        idx.add(a, [f"kw{a}"])
    idx.bump_corecall(["A", "B"])
    assert {n[0] for n in idx.shortcut_cache["A"]["neighbors"]} == {"B"}
    assert {n[0] for n in idx.shortcut_cache["B"]["neighbors"]} == {"A"}


def test_membership_is_carried_separately_from_colour(tmp_path):
    """
    Colour cannot encode this. A crystallized member and an archived memory are
    both Blue and must age differently -- the archived one is MEANT to warm
    back to Yellow when it resurfaces, the member is not. Reading membership
    off the colour would either freeze archived memories or leak members, and
    it leaked members.
    """
    idx = _index(tmp_path)
    idx.add("A", ["kwa"], color="Blue")          # archived, not crystallized
    idx.add("B", ["kwb"], color="Blue")          # crystallized member
    idx.set_skill_member(["B"], True)
    assert idx.shortcut_cache["A"]["skill_member"] is False
    assert idx.shortcut_cache["B"]["skill_member"] is True


def test_set_skill_member_reports_only_real_changes(tmp_path):
    """
    /index_repair reports how many flags it reconciled. A count of attempts
    rather than changes would report work on every run and make a no-op repair
    look like a repair.
    """
    idx = _index(tmp_path)
    idx.add("A", ["kwa"])
    assert idx.set_skill_member(["A"], True) == 1
    assert idx.set_skill_member(["A"], True) == 0, "already flagged is not a change"
    assert idx.set_skill_member(["missing"], True) == 0, "unknown address is not a change"
    assert idx.set_skill_member(["A"], False) == 1


def test_a_rebuild_does_not_uncompress_every_member():
    """
    rebuild_from_neo4j() discards both tiers and rebuilds from the graph. If it
    dropped membership, every crystallized member would come back ageable and
    pairing, and the only signal would be the graph slowly re-thickening.
    """
    import light_index_v2 as v2
    src = inspect.getsource(v2.LightIndexV2.rebuild_from_neo4j)
    assert "PROCEDURALIZED_FROM" in src, "the rebuild must read membership"
    assert "skill_member=" in src,       "and must pass it to add()"


def test_recalled_members_are_excluded_from_graph_pairing():
    """
    The other half, in Neo4j. write_recall_edges() paired Blue like any other
    colour ("Include if Green/Yellow/Blue"), and it runs from mmu.recall()
    BEFORE the endpoint knows which skills matched -- so the substitution was
    invisible to the graph no matter what recall delivered.

    Keyed on membership rather than colour precisely so the ordering stops
    mattering: it is a property of the memory, not of this recall.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.write_recall_edges)
    assert "PROCEDURALIZED_FROM" in src, \
        "pairing must exclude memories compressed into a skill"
    assert "Include if Green/Yellow/Blue" not in src, \
        "Blue members must no longer be paired unconditionally"
    # RECALLED_IN is episodic history and stays true for a member.
    assert "RECALLED_IN" in src


def test_membership_lookup_distinguishes_empty_from_unreachable():
    """
    /index_repair unflags anything the graph does not list as a member. An
    unreachable graph returning [] instead of None would unflag every member in
    the index on the strength of an answer that never arrived.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.get_skill_member_addresses)
    assert "return None" in src, "unreachable must be distinguishable from empty"


def test_colour_repair_restores_members_without_losing_their_history():
    """
    The flag stops a crystallized member ageing out of Blue; it cannot undo the
    ones that already had -- 11 of 33 on the graph this was found on, promoted
    to Yellow and Green in Neo4j as well as in the index. The repair has to
    target only members of live skills, and must not touch pre_skill_color:
    that is what uncrystallize restores, and overwriting it with Blue would
    turn an undo into a second demotion.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.repair_crystallized_colors)
    assert "PROCEDURALIZED_FROM" in src,    "must be keyed on skill membership"
    assert "'deprecated'" in src,           "a retired skill's members are not compressed"
    assert "SET m.color = 'Blue'" in src
    assert "pre_skill_color" not in src.split('"""', 2)[2], \
        "the pre-crystallization colour must be left for uncrystallize"
    assert "return None" in src, "unreachable must be distinguishable from nothing drifted"


def test_index_repair_restores_aged_out_members_from_what_landed():
    """
    Neo4j first, then the index -- and only for the rows the graph says it
    changed, the same rule the aging pass follows. Read as text for the reason
    the aging test above gives.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "mmu_server.py").read_text(
        encoding="utf-8")
    body = src.split("def index_repair", 1)[1].split("\n@app.", 1)[0]
    assert '"aged_out_of_skill"' in body, "the dry run must report the drift"
    assert "n4j.repair_crystallized_colors()" in body
    tail = body.split("n4j.repair_crystallized_colors()", 1)[1]
    assert "for row in restored" in tail, "the index must follow what landed"
    assert 'set_color(row["address"], "Blue")' in tail


def test_the_aging_pass_leaves_crystallized_members_alone():
    """
    The leak itself. _age_memories() promotes Blue back to Yellow on recall,
    which is right for an archived memory and wrong for a member -- and the
    tier-1 keyword gate has no colour filter, so members DO get recalled
    directly. The graph this was found on read 36 members against 34 Blue.

    Read as text rather than imported: mmu_server pulls in a web framework that
    CI does not install, which is why the endpoints have no round-trip
    coverage either.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "mmu_server.py").read_text(
        encoding="utf-8")
    body = src.split("def _age_memories", 1)[1].split("\n    def ", 1)[0]
    assert 'meta.get("skill_member")' in body, \
        "a memory compressed into a skill must not age"
    # Ordering guard: the check has to sit above the Blue -> Yellow promotion,
    # not below it, or the member is already warmed by the time it is skipped.
    assert body.index('meta.get("skill_member")') < body.index('"Yellow" if color == "Blue"')


# ── Phase 13.3: skill growth ──
#
# These are structural guards rather than round trips, for the same reason the
# crystallization guards above are: the logic lives in Cypher and needs a real
# Neo4j to execute. What they pin down is the set of properties that made
# growth worth building -- if a later edit reintroduces CREATE here, or drops
# the coalesce, the skill quietly goes back to being replaced rather than
# grown and every number that depends on invocation_count goes with it.


def test_growing_a_skill_never_creates_one():
    """
    The entire point of add_skill_members. crystallize_skill() CREATEs, which
    is why growing a skill used to mean uncrystallize-and-rebuild: a new
    skill_id, invocation_count back to zero, created_at gone, and the tree
    edges deleted on the way out. A CREATE in this function would restore that
    behaviour while looking like it had been fixed.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.add_skill_members)
    assert "CREATE (sk:Skill" not in src, "growth must not mint a new skill"
    assert "uuid.uuid4()" not in src,     "growth must not mint a new skill_id"
    for frozen in ("invocation_count", "created_at", "skill_id:"):
        assert f"SET sk.{frozen}" not in src, \
            f"{frozen} is the skill's history and growth must not rewrite it"


def test_growth_preserves_the_original_colour():
    """
    Members are restored to pre_skill_color when a skill is undone. Writing
    that property unconditionally would overwrite the real original with Blue
    for any memory demoted twice -- which is precisely what the coalesce in
    crystallize_skill() exists to prevent, and adding members is the second
    way a memory gets demoted.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.add_skill_members)
    assert "coalesce(m.pre_skill_color, m.color)" in src, \
        "an already-demoted member must keep its ORIGINAL colour"


def test_a_skill_may_not_steal_another_skills_member():
    """
    Colour is single-valued, so two active skills owning one memory disagree
    about what it should be the moment either is undone. crystallize_skill()
    learned this from a real double-confirm; growth is the other door into the
    same state and has to hold the same rule -- while still letting a caller
    re-send a member this skill already owns, which is a no-op and not a
    conflict with itself.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.add_skill_members)
    assert "sk.skill_id <> $sid" in src, \
        "ownership check must exclude the skill being grown"
    assert "sk.status <> 'deprecated'" in src


def test_removing_every_member_is_refused():
    """
    A Skill with no root system is still matchable and still delivered, with
    nothing left to trace it back to. Emptying one is uncrystallize's job --
    that deletes the Skill node too, so there is nothing left to match.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.remove_skill_members)
    assert "uncrystallize" in src, \
        "refusing to empty a skill must point at the operation that can"


def test_removal_respects_a_second_owner():
    """
    Same rule uncrystallize_skill() applies: a memory another active skill
    still owns stays Blue. Restoring it would contradict the skill that still
    claims it.
    """
    import neo4j_layer as n4j
    src = inspect.getsource(n4j.remove_skill_members)
    assert "still_owned" in src and "still_demoted" in src


def test_reindexing_a_rewritten_skill_drops_the_old_terms():
    """
    link_skill_keywords only ever MERGEd. That is correct for a new skill and
    wrong for one whose procedure was rewritten: the old wording's terms stay
    linked and the skill keeps matching prompts about text it no longer
    contains. The default must stay MERGE-only so existing callers are
    unaffected.
    """
    import neo4j_layer as n4j
    sig = inspect.signature(n4j.link_skill_keywords)
    assert sig.parameters["replace"].default is False, \
        "existing callers must keep MERGE-only behaviour"
    src = inspect.getsource(n4j.link_skill_keywords)
    assert "DELETE r" in src


def test_a_grown_skill_compresses_its_new_members_too():
    """
    The seam between growth and compression. A member added to an existing
    skill has to be marked compressed exactly as one added at crystallization
    is -- colour alone cannot carry it, since an archived memory is Blue too.
    Miss this and growing a skill quietly produces members that keep ageing and
    keep pairing: less compressed than the three it was created with, and
    nothing on the skill says which of its members are which.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "mmu_server.py").read_text(
        encoding="utf-8")
    add = src.split('@app.post("/skills/{skill_id}/members")', 1)[1].split("@app.post(", 1)[0]
    assert 'set_skill_member(info["added"], True)' in add, \
        "an added member must be marked compressed, not merely recoloured"
    rm = src.split('@app.post("/skills/{skill_id}/members/remove")', 1)[1].split("@app.post(", 1)[0]
    assert 'set_skill_member([r["address"] for r in info["removed"]], False)' in rm, \
        "only RESTORED members lose the flag -- still_demoted keep it"



# ═════════════════════════════════════════════════════════════
#  LIVE
# ═════════════════════════════════════════════════════════════

@live
def test_health():
    st, d = _call("GET", "/health")
    assert st == 200 and d["status"] == "ok"


@live
def test_health_graph_counts_are_not_silently_zero():
    """
    A connected graph holding memories must never report zero of them.

    get_neo4j_stats() chained three MATCH clauses through WITH. Such a chain
    yields no rows at all if any single link matches nothing, and the
    CO_RECALLED link matches nothing until the first recall creates an edge --
    so a fresh instance holding 60 memories and 110 keywords reported
    {"status": "connected", "memories": 0, "keywords": 0}. Wrong on exactly
    the graphs whose state is hardest to confirm another way, and it silently
    defeated any tool reading graph size from /health.

    Note this invariant only catches the regression while the graph has no
    CO_RECALLED edges, which is the state a fresh or freshly restored instance
    is in. On a warm graph the old query happened to be correct.
    """
    _, d = _call("GET", "/health")
    n4j = d.get("neo4j") or {}
    if n4j.get("status") != "connected":
        pytest.skip("Neo4j not connected on this instance")
    if not d.get("total_memories"):
        pytest.skip("empty instance -- zero is the honest answer here")

    assert n4j.get("memories"), (
        f"index reports {d['total_memories']} memories but a connected graph "
        f"reports {n4j.get('memories')}; co_recalled={n4j.get('co_recalled')}"
    )


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
def test_aging_a_use_increment_keeps_index_and_graph_in_step():
    """
    The regression for the drift itself, end to end.

    Aging encodes USE into the address, so a memory NOT returned by a recall
    gets a new address during that very recall. Both stores have to follow it
    together. When they did not, the memory stayed in Neo4j -- counted by
    /health and by every graph query -- and vanished from the read path.

    Probe A exists only to be recalled; probe B exists only to be aged by that
    recall without being returned. B is located afterwards through /memories
    rather than by recalling it, because recalling B resets its USE counter to
    zero and would erase the thing under test.

    Everything this test writes carries RUN_TAG in its payload, and cleanup
    finds it by that rather than by the addresses it was given -- aging has been
    rewriting those the whole time, which is the entire point. Deleting stale
    addresses is how a test leaves its own litter behind.
    """
    # RUN_TAG only ever appears in PAYLOADS, for cleanup. The keywords are
    # deliberately unrelated words: the gate matches on 4-char stems, so
    # keywords sharing a prefix would put every filler in the same gate hit as
    # the probes, and top_k would decide by luck which of them came back.
    RUN_TAG = "agingdriftprobe"
    marker_a = "zephyralpha"       # stem "zeph"
    marker_b = "quokkabravo"       # stem "quok"
    filler_kw = "ballastfiller"    # stem "ball"

    def _cleanup():
        st, listing = _call("GET", "/memories")
        if st != 200:
            return
        for m in listing["memories"]:
            if RUN_TAG in (m.get("payload") or ""):
                _call("DELETE",
                      f"/memories/{urllib.parse.quote(m['address'], safe='')}")

    try:
        # Aging is floored at MMU_AGING_MIN_MEMORIES so a nearly empty graph
        # does not archive itself. Clear the floor rather than skipping past it:
        # a test that only runs on a graph that happens to be big enough is a
        # test that does not run.
        st, health = _call("GET", "/health")
        assert st == 200
        filler = 0
        while health["total_memories"] + filler < 30:
            st, _f = _call("POST", "/remember", {
                "keywords": [f"{filler_kw}{filler}"],
                "payload": f"{RUN_TAG} filler {filler}, here only to clear the "
                           f"aging floor.",
                "src_type": 1})
            assert st == 200
            filler += 1

        st, _a = _call("POST", "/remember", {
            "keywords": [marker_a], "payload": f"{RUN_TAG} probe A: {marker_a}.",
            "src_type": 1})
        assert st == 200
        st, b = _call("POST", "/remember", {
            "keywords": [marker_b], "payload": f"{RUN_TAG} probe B: {marker_b}.",
            "src_type": 1})
        assert st == 200
        b_start = b["address"]

        # Recalling A ages B: B is not in the result set, so its USE climbs.
        for _ in range(3):
            st, _d = _call("POST", "/recall", {"prompt": marker_a, "top_k": 3})
            assert st == 200

        st, listing = _call("GET", "/memories")
        assert st == 200
        found = [m for m in listing["memories"]
                 if marker_b in (m.get("payload") or "")]
        assert len(found) == 1, f"probe B is not in the graph exactly once: {found}"
        b_now = found[0]["address"]

        m = ADDR_RE.search(b_now)
        assert m is not None, f"unparseable address after aging: {b_now}"
        if int(m.group(4)) == 0:
            pytest.skip("USE never advanced, so there was no address rewrite to "
                        "test. Check MMU_AGING_MIN_MEMORIES on this instance.")
        assert b_now != b_start, "a USE increment must rewrite the address"

        # The whole point: the rewrite moved the card, it did not drop it.
        st, d = _call("POST", "/index_repair")
        assert st == 200
        assert d["index_total"] == d["neo4j_total"], (
            f"index and graph disagree after aging: "
            f"missing={d['missing']} phantom={d['phantom']}")
        assert d["missing_count"] == 0, f"invisible to recall: {d['missing']}"
        assert d["phantom_count"] == 0, f"indexed but gone: {d['phantom']}"

        # And the aged memory is still reachable through the read path at its
        # new address. Agreeing totals alone would not prove that.
        st, d = _call("POST", "/recall", {"prompt": marker_b, "top_k": 5})
        assert st == 200
        assert any(marker_b in (mem.get("payload") or "")
                   for mem in d["memories"]), \
            "an aged memory must still be recallable at its rewritten address"
    finally:
        _cleanup()


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
def test_no_crystallized_member_has_aged_out_of_compression():
    """
    A member of an active skill that is anything but Blue is live in the recall
    pool while compressed into that skill. The dry run must say so, and on a
    healthy graph must find none.
    """
    st, d = _call("POST", "/index_repair")
    assert st == 200
    assert "aged_out_of_skill" in d and d["aged_out_count"] == len(d["aged_out_of_skill"])
    assert d["aged_out_of_skill"] == [], \
        "run POST /index_repair?apply=true to restore these to Blue"


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
def test_skill_proposal_sweep_creates_no_skills():
    """
    The sweep is what makes the daemon safe to run unattended: it may queue a
    proposal, never act on one. If it can create a Skill or demote a memory,
    the human gate on /crystallize has been routed around.
    """
    _, before_h = _call("GET", "/health")
    _, before_s = _call("GET", "/skills")

    st, d = _call("POST", "/skill_proposals/sweep")
    assert st == 200 and d["status"] == "swept"

    _, after_h = _call("GET", "/health")
    _, after_s = _call("GET", "/skills")
    assert after_h["total_memories"] == before_h["total_memories"]
    assert after_s["count"] == before_s["count"], \
        "a sweep must never crystallize anything"


@live
def test_skill_proposal_sweep_is_idempotent():
    """
    The daemon sweeps after every idle pass. A cluster that is still dense
    must refresh its proposal, not queue a second copy of the same finding.
    """
    _call("POST", "/skill_proposals/sweep")
    _, first = _call("GET", "/skill_proposals")
    st, d = _call("POST", "/skill_proposals/sweep")
    assert st == 200 and d["created"] == 0, "a repeat sweep must create nothing"
    _, second = _call("GET", "/skill_proposals")
    assert second["count"] == first["count"]


@live
def test_proposals_reach_the_model_somehow():
    """
    The queue was built, populated, and invisible: no MCP tool exposed it and
    the session bundle did not mention it, so the only thing that could see a
    proposal was curl. A review queue nothing can read is the same as no queue.
    """
    _call("POST", "/skill_proposals/sweep")
    _, q = _call("GET", "/skill_proposals")
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
    _, d = _call("GET", "/skill_candidates?limit=5")
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

    Retargeted from /skill_candidates to /skill_proposals. /skill_candidates is
    the raw triangle output, and since the merge it is an INPUT rather than
    something anyone reviews -- overlap there is not a defect, it is the signal
    merge_overlapping_candidates consumes to build a larger cluster. The queue
    is what a reviewer reads.

    The assertion is that overlap is DECLARED, not that it is absent. A sweep's
    own output is disjoint, but a live queue also holds proposals from earlier
    sweeps, and those can legitimately overlap a newer cluster without being
    contained by it -- they are real alternatives, and discarding them would
    throw away groupings a reviewer might prefer. They are retired
    automatically once one of them is crystallized.

    What must never happen again is overlap that nothing says out loud. That is
    what turned the queue into a minefield: the reviewer worked through it as a
    task list, hit an ownership refusal, read it as repairable, and looped.
    """
    _, d = _call("GET", "/skill_proposals?limit=50")
    props = [p for p in d.get("proposals", []) if p.get("status") == "pending"]
    if len(props) < 4:
        pytest.skip("too few proposals to crowd anything")

    by_id = {p["proposal_id"]: set(p["members"]) for p in props}
    for p in props:
        declared = set(p.get("excludes") or [])
        for other in props:
            if other["proposal_id"] == p["proposal_id"]:
                continue
            if by_id[p["proposal_id"]] & by_id[other["proposal_id"]]:
                assert other["proposal_id"] in declared, (
                    f"{p['proposal_id'][:8]} shares a member with "
                    f"{other['proposal_id'][:8]} and does not say so"
                )


@live
def test_skill_proposals_rejects_bad_status():
    st, _ = _call("GET", "/skill_proposals?status=bogus")
    assert st == 400


@live
def test_reject_unknown_proposal_is_404():
    st, _ = _call("POST", "/skill_proposals/no-such-proposal-id/reject")
    assert st == 404


@live
def test_candidates_report_the_floor_they_applied():
    """
    An empty candidate list is a legitimate answer, but only readable as one
    if the caller can see the bar that was applied and what set it. Reporting
    a bare count is how a structural exclusion stayed invisible for a phase.
    """
    st, d = _call("GET", "/skill_candidates")
    assert st == 200
    t = d["thresholds"]
    for k in ("weight_floor", "reference_weight", "reference", "floor_set_by"):
        assert k in t, f"thresholds must report {k}"
    assert t["floor_set_by"] in ("percentile", "absolute")


@live
def test_insights_and_skill_candidates_agree():
    """
    They disagreed systematically: /insights showed a top candidate above the
    documented propose-at-0.70 bar that /skill_candidates was structurally
    incapable of returning. A preview of a decision must preview the decision.
    """
    _, ins = _call("GET", "/insights")
    _, cands = _call("GET", "/skill_candidates?limit=10")
    a = [c["members"] for c in ins["crystallization_candidates"]]
    b = [c["members"] for c in cands["candidates"]]
    assert a == b, "/insights and /skill_candidates must report the same clusters"


@live
def test_a_crystallized_skill_is_actually_retrievable():
    """
    Phase 13.2. Crystallization was write-only: a Skill had no keywords and no
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
        sid = sk["skill_id"]

        st, d = _call("POST", "/recall",
                      {"prompt": "quibbleprobe widget torque", "top_k": 5})
        assert st == 200
        ids = [s["skill_id"] for s in d.get("skills", [])]
        assert sid in ids, "a crystallized skill must be retrievable by recall"

        # And it must SUBSTITUTE for its members, not arrive alongside them.
        # A skill delivered next to everything it compressed has added text
        # rather than saved it.
        hit = next(s for s in d["skills"] if s["skill_id"] == sid)
        assert hit["replaced_members"], "the skill must displace its own members"
        for addr in hit["replaced_members"]:
            assert addr not in d["context_block"],                 "a replaced member must not also appear in the context block"
        assert "SKILL" in d["context_block"]
    finally:
        if sid:
            _call("POST", f"/skills/{sid}/uncrystallize?confirm=UNCRYSTALLIZE")
        for addr in addrs:
            _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")


@live
def test_crystallize_can_create_a_branch():
    """
    Phase 13.2. link_skills() and POST /skills/{id}/link existed from Phase 13,
    but nothing a model could reach exposed them, so "crystallize these as
    branches off that skill" was not an instruction the system could carry out.
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
            made.append(sk["skill_id"])

        # The child names its parent, and the tree reflects it.
        _, skills = _call("GET", "/skills")
        child = next(s for s in skills["skills"] if s["skill_id"] == made[1])
        assert made[0] in (child.get("extends") or []),             "a skill created with extends= must actually be linked"

        _, tree = _call("GET", f"/skill_tree?root={made[0]}")
        kids = tree["tree"][0]["children"]
        assert [k["skill_id"] for k in kids] == [made[1]]
    finally:
        for sid in reversed(made):
            _call("POST", f"/skills/{sid}/uncrystallize?confirm=UNCRYSTALLIZE")
        for addr in addrs:
            _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")


@live
def test_an_existing_skill_can_be_branched_without_rebuilding():
    """
    Branching used to be possible only at creation, through crystallize_skill's
    `extends`. An already-created skill could therefore be branched only by
    uncrystallizing and rebuilding it -- which mints a new skill_id and
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
            made.append(sk["skill_id"])
        parent, child = made

        # Link by prefix, without recreating anything.
        st, d = _call("POST", f"/skills/{child[:8]}/link?parent_id={parent[:8]}")
        assert st == 200 and d["parent"] == parent

        # Idempotent: linking twice is not an error and makes one edge.
        st, _ = _call("POST", f"/skills/{child[:8]}/link?parent_id={parent[:8]}")
        assert st == 200
        _, tree = _call("GET", f"/skill_tree?root={parent}")
        assert len(tree["tree"][0]["children"]) == 1

        # The ids are unchanged -- that is the point of not rebuilding.
        _, skills = _call("GET", "/skills")
        ids = [s["skill_id"] for s in skills["skills"]]
        assert parent in ids and child in ids

        # Detach without destroying.
        st, d = _call("POST", f"/skills/{child}/unlink")
        assert st == 200 and d["edges_removed"] == 1
        _, skills = _call("GET", "/skills")
        assert child in [s["skill_id"] for s in skills["skills"]],             "unlink must not delete the skill"
    finally:
        for sid in reversed(made):
            _call("POST", f"/skills/{sid}/unlink")
        for sid in reversed(made):
            _call("POST", f"/skills/{sid}/uncrystallize?confirm=UNCRYSTALLIZE")
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
    st, d = _call("GET", "/skill_proposals?limit=50")
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
        st, _ = _call("POST", f"/skill_proposals/{blocked['proposal_id']}/crystallize",
                      {"member_addresses": [], "trigger": "t", "procedure": "p",
                       "confirmed": True})
        assert st == 409


@live
def test_skill_ids_accept_an_unambiguous_prefix():
    """
    Skill ids are UUIDs and are displayed truncated nearly everywhere -- tree
    views, summaries, logs. Requiring all 36 characters made the one form
    anybody actually has in front of them the one form that did not work, so a
    branch became a second root.
    """
    _, skills = _call("GET", "/skills")
    if len(skills["skills"]) < 1:
        pytest.skip("no skills to resolve")
    full = skills["skills"][0]["skill_id"]

    # A prefix that matches nothing is an error, not a guess.
    st, d = _call("POST", f"/skills/{full}/link?parent_id=zzzzzznope")
    assert st == 404 and "no skill with id" in d["detail"]

    # A self-link via prefix is still a self-link.
    st, d = _call("POST", f"/skills/{full[:8]}/link?parent_id={full[:8]}")
    assert st == 400, "prefix resolution must not defeat the self-link check"


@live
def test_proposals_report_the_queue_not_the_page():
    """
    The review tool said "5 proposals awaiting review" while 17 were queued,
    because it counted the page. Proposals are score-ordered, so one dense
    corpus owned that page -- and the honest reading of the output was that
    every proposal was about one topic, which was false.
    """
    st, d = _call("GET", "/skill_proposals?limit=1")
    assert st == 200
    assert "total" in d, "the queue size must be reported alongside the page"
    assert d["total"] >= d["count"]


@live
def test_skill_reindex_is_safe_to_rerun():
    """Anything crystallized before Phase 13.2 has no embedding and no
    keywords; the backfill must be idempotent, not just present."""
    st, first = _call("POST", "/skills/reindex")
    assert st == 200
    st, second = _call("POST", "/skills/reindex")
    assert st == 200 and second["count"] == 0,         "a second reindex must find nothing left to do"


@live
def test_crystallization_is_reversible():
    """
    Crystallization is the one operation that restructures memory, and it had
    no undo: deprecate_skill() left every member stranded in Blue. Members
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
                       f"/skills/{sk['skill_id']}/uncrystallize?confirm=UNCRYSTALLIZE")
        assert st == 200, un
        assert len(un["restored"]) == 2
        for m in un["restored"]:
            assert m["color"] != "Blue", "a restored memory must not stay demoted"

        _, skills = _call("GET", "/skills")
        assert sk["skill_id"] not in [s["skill_id"] for s in skills["skills"]]
    finally:
        for addr in addrs:
            _call("DELETE", f"/memories/{urllib.parse.quote(addr, safe='')}")


@live
def test_uncrystallize_requires_the_phrase():
    st, _ = _call("POST", "/skills/whatever/uncrystallize")
    assert st == 400


@live
def test_model_crystallization_is_gated():
    """
    Model-initiated crystallization is a configuration decision, and the server
    enforces it rather than trusting the MCP tool list -- a tool list is a
    client-side promise, and the write is not something to leave to a client
    keeping one.
    """
    _, q = _call("GET", "/skill_proposals")
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
    st, d = _call("POST", f"/skill_proposals/{pid}/crystallize",
                  {"member_addresses": [], "trigger": "t", "procedure": "p",
                   "confirmed": False},
                  headers={"X-MMU-Source": "model"})
    assert st in (400, 403), f"unexpected {st}: {d}"
    if st == 403:
        assert "MMU_ALLOW_MODEL_CRYSTALLIZE" in d["detail"],             "a refusal must name the flag that controls it"


@live
def test_a_memory_cannot_belong_to_two_active_skills():
    """
    Colour is single-valued, so two skills claiming one member disagree about
    what it should be the moment either is undone. Found the hard way: a stale
    proposal was confirmed while one of its members was already crystallized,
    and undoing it restored a memory the first skill still owned.
    """
    _, sk = _call("GET", "/skills")
    active = [s for s in sk["skills"] if s.get("status") == "active"]
    if not active:
        pytest.skip("no active skill to collide with")

    # Any proposal whose members overlap an active skill must be refused.
    _, props = _call("GET", "/skill_proposals")
    for p in props["proposals"]:
        st, d = _call("POST", f"/skill_proposals/{p['proposal_id']}/crystallize",
                      {"member_addresses": [], "trigger": "", "procedure": "",
                       "confirmed": True})
        # Empty trigger/procedure is rejected first; that is fine. What must
        # never happen is a 500 or a silent second claim.
        assert st in (400, 409), f"unexpected {st}: {d}"


@live
def test_crystallize_by_proposal_id_refuses_without_confirmation():
    """The proposal-id path is ergonomics, not a second door around the gate."""
    _call("POST", "/skill_proposals/sweep")
    _, q = _call("GET", "/skill_proposals")
    if not q["count"]:
        pytest.skip("nothing pending")
    pid = q["proposals"][0]["proposal_id"]
    st, d = _call("POST", f"/skill_proposals/{pid}/crystallize", {
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


# ─────────────────────────────────────────────────────────────
#  CONCURRENCY (pure)
#
#  Every MMU endpoint is a sync `def`, so Starlette runs it in a worker
#  thread and two requests genuinely overlap. There is also always a second
#  caller -- the idle daemon polls every 30 seconds against the same server a
#  conversation is using.
#
#  Both tests below fail on the unsynchronized versions of this code. The
#  index one lost 737 of 909 cards; the state one had four threads read a
#  fifth thread's session id.
# ─────────────────────────────────────────────────────────────

def test_index_survives_concurrent_writers_and_readers(tmp_path):
    """
    Readers and writers hitting LightIndexV2 at once must not corrupt it.

    Unsynchronized, this raised "dictionary changed size during iteration"
    from json.dump walking a live dict, and FileNotFoundError from two savers
    sharing one fixed ".tmp" filename -- one renamed it out from under the
    other. Writes were silently lost: the on-disk index ended up with a
    fraction of the cards that had been added.
    """
    import json as _json
    import threading as _threading
    from light_index_v2 import LightIndexV2

    idx = LightIndexV2(path=str(tmp_path / "idx.json"))
    for i in range(100):
        idx.add(f"001.{i % 900:03d}.500.001,001|1.000.000",
                [f"kw{i % 20}", f"w{i}"], color="Green")

    errors = []

    def writer(n):
        try:
            for i in range(60):
                idx.add(f"9{n:02d}.{i % 900:03d}.500.001,001|1.000.000",
                        [f"kw{i % 20}", f"t{n}"], color="Green")
                idx.save()
        except Exception as e:      # noqa: BLE001 -- the assertion reports it
            errors.append(f"writer: {e!r}")

    def reader():
        try:
            for i in range(120):
                idx.gate(f"kw{i % 20}")
                idx.stats()
        except Exception as e:      # noqa: BLE001
            errors.append(f"reader: {e!r}")

    threads = ([_threading.Thread(target=writer, args=(n,)) for n in range(4)] +
               [_threading.Thread(target=reader) for _ in range(4)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent access raised: {errors[:3]}"

    # Every write must have survived, and the file must still be readable.
    expected = 100 + 4 * 60
    blob = _json.loads((tmp_path / "idx.json").read_text())
    assert len(blob["shortcut_cache"]) == expected, (
        f"expected {expected} cards on disk, found "
        f"{len(blob['shortcut_cache'])} -- writes were lost")

    # A save that fails midway must not leave a temp file behind to be
    # mistaken for an index.
    assert not [f for f in os.listdir(tmp_path) if ".tmp" in f]


@needs_mmu_server
def test_per_request_state_does_not_leak_between_threads():
    """
    The session id, rename map, read path and timing belong to one in-flight
    recall, not to the shared MMUCore singleton.

    Held as plain attributes they leaked across concurrent requests: a
    response could report another request's read_path, and -- the damaging
    case -- /recall could rewrite its result addresses through an addr_map
    built by a different query. Those addresses go straight back to /rate and
    seed anticipation, and both miss silently when the address is wrong.
    """
    import threading as _threading

    mmu_server = _mmu_server

    core = mmu_server.MMUCore.__new__(mmu_server.MMUCore)
    core._req = _threading.local()

    leaks = []
    barrier = _threading.Barrier(6)

    def worker(n):
        core._current_session_id = f"session-{n}"
        core._last_read_path = f"path-{n}"
        core._last_addr_map = {f"a{n}": f"b{n}"}
        barrier.wait()          # maximize interleaving
        for _ in range(500):
            if core._current_session_id != f"session-{n}":
                leaks.append(("session_id", n, core._current_session_id))
                return
            if core._last_read_path != f"path-{n}":
                leaks.append(("read_path", n, core._last_read_path))
                return
            if core._last_addr_map != {f"a{n}": f"b{n}"}:
                leaks.append(("addr_map", n, core._last_addr_map))
                return

    threads = [_threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not leaks, f"per-request state leaked across threads: {leaks[:3]}"


@needs_mmu_server
def test_unset_per_request_state_matches_the_old_defaults():
    """
    Call sites used getattr(mmu, "_last_read_path", "v2") and friends. The
    properties must return those same defaults, or a thread that reads before
    writing changes behaviour rather than preserving it.
    """
    import threading as _threading

    mmu_server = _mmu_server

    core = mmu_server.MMUCore.__new__(mmu_server.MMUCore)
    core._req = _threading.local()

    assert core._current_session_id == "default"
    assert core._last_read_path == "v2"
    assert core._last_read_ms == 0
    assert core._last_addr_map == {}


# ─────────────────────────────────────────────────────────────
#  AGING WRITE BATCHING (pure)
#
#  The aging pass runs on EVERY recall and walks every memory in the graph.
#  Each address rewrite used to be its own Cypher query AND its own Bolt
#  session, so one recall on a 500-memory graph could issue 500 sequential
#  round trips before returning.
# ─────────────────────────────────────────────────────────────

class _FakeGraph:
    """
    Minimal stand-in for Neo4j that enforces the one rule this pass depends
    on: m.address is UNIQUE, so a rewrite into an occupied address must not
    move the node.
    """

    def __init__(self):
        self.addr_to_color = {}
        self.trips = 0
        self.rows = 0

    def write_color_updates_batch(self, updates):
        self.trips += 1
        self.rows += len(updates)
        landed = {}
        for old, new, color in updates:
            if old not in self.addr_to_color:
                continue                       # no such node: nothing written
            if new != old and new in self.addr_to_color:
                self.addr_to_color[old] = color    # target taken: colour only
                landed[old] = old
            else:
                self.addr_to_color.pop(old)
                self.addr_to_color[new] = color
                landed[old] = new
        return landed


def _aging_fixture(monkeypatch, tmp_path, count=60):
    mmu_server = _mmu_server
    monkeypatch.setattr(mmu_server, "AGING_MIN_MEMORIES", 5, raising=False)

    fake = _FakeGraph()
    monkeypatch.setattr(mmu_server.n4j, "write_color_updates_batch",
                        fake.write_color_updates_batch)

    from light_index_v2 import LightIndexV2
    core = mmu_server.MMUCore.__new__(mmu_server.MMUCore)
    core._req = __import__("threading").local()
    core._addr_collisions = set()
    core.v2_index = LightIndexV2(path=str(tmp_path / "idx.json"))

    addrs = []
    for i in range(count):
        # The first six share CON 001, which is what a duplicate CON looks
        # like -- their USE counters converge and the addresses collide.
        con = 1 if i < 6 else i
        a = f"{con:03d}.005.500.{i % 20:03d},001|0.000.000"
        if a in addrs:
            continue
        addrs.append(a)
        core.v2_index.add(a, [f"kw{i % 10}"], color="Green")
        fake.addr_to_color[a] = "Green"
    return core, fake, addrs


@needs_mmu_server
def test_aging_uses_one_round_trip_per_pass(monkeypatch, tmp_path):
    """One batched write per aging pass, however many memories change."""
    core, fake, addrs = _aging_fixture(monkeypatch, tmp_path)

    for expected_trips in (1, 2, 3):
        core._age_memories(recalled=addrs[:2])
        assert fake.trips == expected_trips, (
            f"expected {expected_trips} round trip(s), got {fake.trips}")

    # The point of the change: rows written greatly exceeds trips taken.
    assert fake.rows > 50, "fixture too small to be meaningful"
    assert fake.trips == 3


@needs_mmu_server
def test_batched_aging_keeps_index_and_graph_in_step(monkeypatch, tmp_path):
    """
    Batching must not reintroduce drift.

    Deferring the index writes until after the batch means the in-memory
    collision check can no longer see a claim made moments earlier by another
    memory in the same pass -- hence the `claimed` set. Without it two
    memories converging on one address both believe they got it, and the index
    ends up disagreeing with the graph about who owns what. Duplicate CONs in
    the fixture guarantee real collisions here.
    """
    core, fake, addrs = _aging_fixture(monkeypatch, tmp_path)

    for _ in range(4):
        core._age_memories(recalled=addrs[:2])
        indexed = set(core.v2_index.shortcut_cache)
        graphed = set(fake.addr_to_color)
        assert not (graphed - indexed), f"MISSING from index: {sorted(graphed - indexed)[:3]}"
        assert not (indexed - graphed), f"PHANTOM in index: {sorted(indexed - graphed)[:3]}"

    # Colours must agree too, not just the address sets.
    for addr, meta in core.v2_index.shortcut_cache.items():
        assert meta.get("color") == fake.addr_to_color[addr], f"colour drift at {addr}"


@needs_mmu_server
def test_aging_survives_an_unreachable_graph(monkeypatch, tmp_path):
    """
    A batch that writes nothing must leave the index completely untouched, so
    the two stores stay in step and the next recall retries from where it is.
    """
    core, fake, addrs = _aging_fixture(monkeypatch, tmp_path)
    before = dict(core.v2_index.shortcut_cache)

    monkeypatch.setattr(__import__("mmu_server").n4j,
                        "write_color_updates_batch", lambda updates: {})
    core._age_memories(recalled=addrs[:2])

    assert set(core.v2_index.shortcut_cache) == set(before), (
        "index moved despite the graph writing nothing")


# ═════════════════════════════════════════════════════════════
#  PHASE 7 (revised) -- the session bundle stays bounded
# ═════════════════════════════════════════════════════════════

def _artifact_driver():
    """
    A Neo4j driver for the one test that has to undo what it did.

    Kept local rather than promoted to a shared helper: this is the only test
    in the file that needs write access to the graph, and a module-level
    fixture would invite others to.
    """
    import io
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.path.join(root, ".env")
    if os.path.exists(env):
        for line in io.open(env, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    os.environ.setdefault("NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("NEO4J_USER", "neo4j")
    os.environ.setdefault("NEO4J_ENABLED", "true")
    try:
        import neo4j_layer as n4j
        return n4j.get_driver()
    except Exception:
        return None


@live
def test_session_bundle_does_not_dump_whole_artifacts():
    """
    The artifact block was 14.7k of a 17.4k bundle -- 84% of everything the
    model read before the first user word -- because it printed every unseen
    artifact in full and nothing ever marked one seen. A bundle that grows
    with the backlog is not context, it is a leak with a heading on it.

    The check is on the block, not on the response: /creative_outputs still
    carries the full text, and should.
    """
    _, d = _call("GET", "/session_bundle")
    block = d["context_block"]
    surfaced = d.get("surfaced_ids") or []
    if not surfaced:
        pytest.skip("no unseen artifacts on this graph")

    full = {c["output_id"]: c["content"] for c in d["creative_outputs"] if c.get("content")}
    for oid in surfaced:
        body = full.get(oid) or ""
        if len(body) > 400:
            assert body not in block,                 f"artifact {oid[:8]} was pasted into the bundle in full"


@live
def test_session_bundle_surfaces_at_most_the_cap():
    """
    The crystallization block below this one caps itself at three, reasoning
    that a wall trains the reader to scroll past the whole thing. The same
    reasoning always applied here; it was simply never applied.
    """
    _, d = _call("GET", "/session_bundle")
    surfaced = d.get("surfaced_ids") or []
    assert len(surfaced) <= 3, "the artifact block must stay capped"
    assert len(surfaced) <= d["unseen_count"], "cannot surface more than exist"


@live
def test_bundle_names_the_artifacts_it_surfaced():
    """
    surfaced_ids is the contract that lets a caller mark exactly what was
    shown. Marking "all unseen" instead would burn the artifacts still queued
    behind the cap -- they would go from waiting to read, to never shown and
    flagged as read.
    """
    _, d = _call("GET", "/session_bundle")
    surfaced = d.get("surfaced_ids") or []
    if not surfaced:
        pytest.skip("no unseen artifacts on this graph")
    for oid in surfaced:
        assert oid[:8] in d["context_block"],             f"{oid[:8]} is reported as surfaced but is not in the block"


@live
def test_artifact_opens_by_the_short_id_the_bundle_printed():
    """
    Cutting the block to openers is only a compression if the rest is still
    reachable; otherwise it is data loss with a nicer shape. The bundle prints
    eight characters of the id, so eight characters have to resolve.
    """
    _, d = _call("GET", "/session_bundle")
    surfaced = d.get("surfaced_ids") or []
    if not surfaced:
        pytest.skip("no unseen artifacts on this graph")

    oid = surfaced[0]
    st, full = _call("GET", f"/creative_outputs/{oid[:8]}")
    assert st == 200, "the id shown in the bundle must resolve"
    assert full["output_id"] == oid
    assert full.get("content"), "the recovery path must return the body"


@live
def test_unknown_artifact_id_is_a_404_not_a_guess():
    """
    Prefix lookup that picks an arbitrary winner hands back the wrong artifact
    silently, which is worse than refusing.
    """
    st, _ = _call("GET", "/creative_outputs/zzzzzzzz-no-such-artifact")
    assert st == 404


@live
def test_mark_seen_drains_only_what_was_named():
    """
    Regression guard on the bug this replaced: /creative_outputs/mark_seen
    existed with no caller anywhere in the system, so presented_to_user was
    false for every artifact ever written. Now that it is wired, the danger
    flips -- a caller that marks the whole unseen set consumes the backlog
    invisibly. It must mark what it was given and nothing else.
    """
    _, before = _call("GET", "/creative_outputs?unseen_only=true&limit=50")
    if before["count"] < 2:
        pytest.skip("need at least two unseen artifacts")

    target = before["outputs"][0]["output_id"]
    try:
        st, res = _call("POST", "/creative_outputs/mark_seen", {"output_ids": [target]})
        assert st == 200 and res["count"] == 1

        _, after = _call("GET", "/creative_outputs?unseen_only=true&limit=50")
        assert after["count"] == before["count"] - 1,             "marking one artifact must not consume the queue behind it"
        assert target not in [o["output_id"] for o in after["outputs"]]
    finally:
        # Put it back. Marking an artifact seen is the one assertion here that
        # cannot be made without doing the thing for real, and the thing is
        # irreversible through the API -- so it is reversed directly. Without
        # this the suite eats one real artifact from the queue on every run.
        drv = _artifact_driver()
        if drv is not None:
            with drv.session() as s:
                s.run("""
                    MATCH (co:CreativeOutput {output_id: $oid})
                    SET co.presented_to_user = false
                    REMOVE co.presented_at
                """, oid=target)
#  Backend refusals say what the backend said
# ═════════════════════════════════════════════════════════════

def test_backend_error_reads_the_nested_llama_cpp_body():
    """
    Session summaries were placeholders for days because raise_for_status()
    reports "400 Client Error: Bad Request for url: ..." and drops the body --
    and the body is the only place the token counts appear.

    The awkward part is the shape: llama.cpp-backed servers return a JSON
    document nested inside a string, behind a prose prefix, so the parser
    cannot test whether the value starts with a brace.
    """
    import json as _json
    import mmu_idle_daemon as d
    inner = _json.dumps({"error": {
        "code": 400,
        "message": "request (8810 tokens) exceeds the available context size "
                   "(8192 tokens), try increasing it",
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 8810,
        "n_ctx": 8192}})
    body = _json.dumps({"error": "Engine protocol predict request returned 400: " + inner})

    e = d._backend_error(400, body)
    assert e.overflow is True
    assert e.n_prompt == 8810 and e.n_ctx == 8192
    assert "8810 tokens" in e.message
    assert "Engine protocol" not in e.message, "the inner message is the specific one"


def test_backend_error_distinguishes_overflow_from_other_faults():
    """
    Overflow is the one a caller can act on by sending less. A missing model or
    a dead server does not get better when retried smaller, so mislabelling one
    as the other buys a pointless retry loop.
    """
    import json as _json
    import mmu_idle_daemon as d
    body = _json.dumps({"error": {"message": 'Invalid model identifier "".',
                                  "code": "model_not_found"}})
    e = d._backend_error(400, body)
    assert e.overflow is False
    assert "Invalid model identifier" in e.message


@pytest.mark.parametrize("body", ["", "Internal Server Error", "{not json",
                                  '{"detail":[{"msg":"bad"}]}'])
def test_backend_error_never_raises_on_a_body_it_cannot_read(body):
    """An unparseable body is still a body worth reporting."""
    import mmu_idle_daemon as d
    e = d._backend_error(500, body)
    assert e.status == 500
    assert isinstance(e.message, str)


def test_context_length_ignores_the_embedding_model():
    """
    The embedding model is loaded for most of the server's life -- every recall
    uses it -- and reports a 2048 window. Counting it as a chat candidate
    collapsed the prompt budget to the floor at every depth: a deep pass
    assembled 2043 characters and called it context.

    "Assume the smallest loaded window" is the right rule. It just has to be
    the smallest window something could actually reason in.
    """
    import mmu_idle_daemon as d

    class FakeResp:
        status_code = 200
        @staticmethod
        def json():
            return {"data": [
                {"id": "chat-big",  "state": "loaded",     "type": "vlm",
                 "loaded_context_length": 176640},
                {"id": "embedder",  "state": "loaded",     "type": "embeddings",
                 "loaded_context_length": 2048},
                {"id": "chat-idle", "state": "not-loaded", "type": "llm",
                 "loaded_context_length": 131072},
            ]}

    llm = d.LMStudioClient(base="http://127.0.0.1:1/v1", model="local-model")
    real = d.requests.get
    d.requests.get = lambda *a, **k: FakeResp()
    try:
        assert llm.context_length() == 176640, "the embedder must not set the budget"
    finally:
        d.requests.get = real


def test_context_length_keeps_every_loaded_model_when_types_are_absent():
    """
    Only backends that say what a model IS get filtered. One that reports no
    type keeps the old behaviour rather than having a guess applied to it.
    """
    import mmu_idle_daemon as d

    class FakeResp:
        status_code = 200
        @staticmethod
        def json():
            return {"data": [
                {"id": "a", "state": "loaded", "loaded_context_length": 8192},
                {"id": "b", "state": "loaded", "loaded_context_length": 32768},
            ]}

    llm = d.LMStudioClient(base="http://127.0.0.1:1/v1", model="local-model")
    real = d.requests.get
    d.requests.get = lambda *a, **k: FakeResp()
    try:
        assert llm.context_length() == 8192, "smallest loaded, as before"
    finally:
        d.requests.get = real


def test_halving_a_transcript_leaves_the_system_message_alone():
    """
    The system message carries the JSON shape the reply has to match. Trimming
    it would trade a prompt that does not fit for a reply that cannot parse.
    """
    import mmu_idle_daemon as d
    system = {"role": "system", "content": "Reply with ONLY JSON: {...}"}
    user   = {"role": "user", "content": "turn text. " * 500}
    out = d._halve_transcript_message([system, user])
    assert out[0] == system
    assert len(out[1]["content"]) < len(user["content"])
    assert "trimmed to fit" in out[1]["content"]


def test_halving_reports_nothing_left_to_cut():
    """
    The shrink loop has to terminate. A message already small enough returns
    None so the caller re-raises instead of retrying an identical request.
    """
    import mmu_idle_daemon as d
    assert d._halve_transcript_message([{"role": "user", "content": "hi"}]) is None
    assert d._halve_transcript_message([{"role": "system", "content": "x" * 5000}]) is None


def test_prompt_budget_reserves_room_for_tools_and_the_reply():
    """
    The window covers the prompt, the tool schemas sent beside it, and the
    tokens reserved for the reply. Budgeting only the first is what let a
    prompt that "fit" get rejected on arrival.
    """
    import mmu_idle_daemon as d

    class Fake:
        def __init__(self, n): self.n = n
        def context_length(self): return self.n

    small = d._prompt_char_budget(Fake(8192))
    big   = d._prompt_char_budget(Fake(176640))
    assert small < big
    assert small <= (8192 - d.MAX_TOKENS - d.TOOL_SCHEMA_RESERVE) * d.CHARS_PER_TOKEN
    assert big == d.IDLE_MAX_CHARS, "a large window is capped by the char ceiling"
    assert d._prompt_char_budget(Fake(None)) is None, "unknown context keeps the default"
    assert d._prompt_char_budget(Fake(512)) == 2000, "an unusable window still returns a floor"


# ═════════════════════════════════════════════════════════════
#  Overlapping proposals are merged, not offered as a queue
# ═════════════════════════════════════════════════════════════

def _n4j():
    """A live Neo4j driver, or None, with credentials loaded from .env."""
    import io
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.path.join(root, ".env")
    if os.path.exists(env):
        for line in io.open(env, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    os.environ.setdefault("NEO4J_URI", "bolt://127.0.0.1:7687")
    os.environ.setdefault("NEO4J_USER", "neo4j")
    os.environ.setdefault("NEO4J_ENABLED", "true")
    try:
        import neo4j_layer as n4j
        return n4j, n4j.get_driver()
    except Exception:
        return None, None


def test_merge_returns_disjoint_clusters():
    """
    The contract, and the whole reason this exists.

    Two clusters sharing a memory are alternatives: colour is single-valued, so
    crystallizing either makes the other permanently unconfirmable. A queue of
    26 proposals, 24 of which shared members with another, read as a list of
    tasks and behaved as a minefield -- the reviewer hit an ownership refusal,
    read it as repairable, uncrystallized to free the members, and looped.
    """
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")
    cands = n4j.find_skill_candidates(limit=40)
    if len(cands) < 2:
        pytest.skip("not enough candidates on this graph")
    _, ref_w, _ = n4j.skill_weight_floor()
    with drv.session() as s:
        merged = n4j.merge_overlapping_candidates(s, cands, ref_w)
    seen = set()
    for c in merged:
        stamps = set(str(t) for t in c["member_created"])
        assert not (stamps & seen), "merged clusters must not share a member"
        seen |= stamps


def test_merge_respects_every_bound():
    """
    An absolute coherence floor alone does not hold. Each admission moves a
    mean over every pair, so a growing cluster ratchets through the floor
    without any single step crossing it -- 35 triples became one 18-member
    cluster across six domains that way, and nothing ever dropped below 0.45.
    """
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")
    cands = n4j.find_skill_candidates(limit=40)
    if len(cands) < 2:
        pytest.skip("not enough candidates on this graph")
    _, ref_w, _ = n4j.skill_weight_floor()
    with drv.session() as s:
        merged = n4j.merge_overlapping_candidates(s, cands, ref_w)
    for c in merged:
        assert len(c["members"]) <= n4j.MERGE_MAX_MEMBERS, "size bound"
        sem = c.get("semantic_coherence")
        if sem is not None and c.get("merged_from", 1) > 1:
            assert sem >= n4j.MERGE_COHERENCE_FLOOR, "coherence floor"


def test_merge_can_exceed_three_members():
    """
    find_skill_candidates is MATCH (a)-(b)-(c): every candidate it can produce
    has exactly three members. Clusters larger than that were not rejected by
    the design, they were unreachable by it. Merging is the route past three
    without rewriting the traversal.
    """
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")
    cands = n4j.find_skill_candidates(limit=40)
    if len(cands) < 4:
        pytest.skip("not enough candidates on this graph")
    assert {len(c["members"]) for c in cands} == {3}, "the raw query is a triangle"
    _, ref_w, _ = n4j.skill_weight_floor()
    with drv.session() as s:
        merged = n4j.merge_overlapping_candidates(s, cands, ref_w)
    if not any(c.get("merged_from", 1) > 1 for c in merged):
        pytest.skip("no overlapping candidates on this graph to merge")
    assert max(len(c["members"]) for c in merged) > 3


def test_merge_is_a_noop_without_overlap():
    """A candidate that touches nothing must come back unchanged."""
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")
    cands = n4j.find_skill_candidates(limit=40)
    if not cands:
        pytest.skip("no candidates on this graph")
    _, ref_w, _ = n4j.skill_weight_floor()
    with drv.session() as s:
        one = n4j.merge_overlapping_candidates(s, [cands[0]], ref_w)
    assert len(one) == 1
    assert set(one[0]["member_created"]) == set(cands[0]["member_created"])


@live
def test_proposals_declare_what_they_exclude():
    """
    Overlap between PENDING proposals was never reported -- only overlap with
    an existing skill. So after uncrystallizing, both conflicting proposals
    showed as unblocked, both were attempted, and the loop closed.
    """
    _, d = _call("GET", "/skill_proposals?limit=50")
    props = d.get("proposals", [])
    if len(props) < 2:
        pytest.skip("need at least two proposals")
    for p in props:
        assert "excludes" in p, "a proposal must say which others it rules out"
    by_id = {p["proposal_id"]: set(p.get("members", [])) for p in props}
    for p in props:
        for other in p["excludes"]:
            if other in by_id:
                assert by_id[other] & by_id[p["proposal_id"]], \
                    "excludes must name only proposals that really share a member"


@live
def test_blocked_proposal_offers_the_tree_instead_of_a_dead_end():
    """
    A blocked proposal used to read as repairable by uncrystallizing, which is
    the advice that produced the loop. Its unclaimed members can still be
    crystallized UNDER the owning skill, so the queue has to say so.
    """
    _, d = _call("GET", "/skill_proposals?limit=50")
    blocked = [p for p in d.get("proposals", []) if p.get("blocked")]
    if not blocked:
        pytest.skip("nothing blocked on this graph")
    for p in blocked:
        assert "free_members" in p
        for b in p["blocked_by"]:
            assert b.get("skill_id"), "the owning skill must be named, not implied"
        if p["free_members"]:
            assert p.get("owner_skill_id"), \
                "any free member at all makes this growable -- the queue " \
                "must name the routine to grow"


def test_rejecting_a_merged_cluster_releases_its_fragments():
    """
    Merging retires the smaller clusters a large one contains. That is right
    while the large one is live and wrong the moment it is rejected: saying no
    to an 8-member cluster would otherwise silently say no to the 3-member
    clusters inside it, which nobody reviewed.

    And the sweep cannot repair it -- superseded is not pending, so the
    upsert's ON MATCH leaves those rows alone forever. Nothing would ever offer
    them again.

    Runs on synthetic proposals it creates and deletes, so it never touches a
    real review queue.
    """
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")

    big, frag, other = "test-big-key", "test-frag-key", "test-other-key"
    ids = {}
    try:
        with drv.session() as s:
            for key, status, sup in ((big, "pending", None),
                                     (frag, "superseded", big),
                                     (other, "superseded", "someone-elses-key")):
                pid = "test-" + key
                ids[key] = pid
                # The label has to match what the real code writes, or this
                # test passes against a graph shape that cannot occur. A
                # pending row is a :SkillProposal; a superseded one was
                # relabelled :ProposalRecord when it was absorbed, and that is
                # the label the release query looks for.
                label = "SkillProposal" if status == "pending" else "ProposalRecord"
                s.run(f"""
                    CREATE (p:{label} {{proposal_id: $pid, member_key: $key,
                                        status: $status, superseded_by: $sup,
                                        member_created: [], skill_score: 0.9,
                                        created_at: $now, updated_at: $now}})
                """, pid=pid, key=key, status=status, sup=sup,
                     now=n4j.datetime.now().isoformat())

        ok, msg = n4j.reject_skill_proposal(ids[big], note="test")
        assert ok, msg

        with drv.session() as s:
            rows = {r["k"]: (r["st"], r["lbl"]) for r in s.run("""
                MATCH (p) WHERE (p:SkillProposal OR p:ProposalRecord)
                  AND p.member_key IN $keys
                RETURN p.member_key AS k, p.status AS st, labels(p) AS lbl
            """, keys=[big, frag, other])}

        assert rows[big][0] == "rejected"
        assert rows[frag][0] == "pending", \
            "a fragment of the rejected cluster must return"
        assert rows[other][0] == "superseded", \
            "a fragment of a DIFFERENT cluster must not"

        # Status and label move together, in both directions. A released
        # fragment that stayed a :ProposalRecord would be pending and invisible
        # to every read that matters.
        assert "SkillProposal" in rows[frag][1], "released, so a proposal again"
        assert "ProposalRecord" not in rows[frag][1]
        assert "ProposalRecord" in rows[big][1], "rejected, so not a proposal"
        assert "SkillProposal" not in rows[big][1]
        assert "ProposalRecord" in rows[other][1], "still superseded, still a record"
    finally:
        with drv.session() as s:
            s.run("""MATCH (p) WHERE (p:SkillProposal OR p:ProposalRecord)
                       AND p.member_key IN $keys DETACH DELETE p""",
                  keys=[big, frag, other])


# ═════════════════════════════════════════════════════════════
#  A skill inherits its members' associations
# ═════════════════════════════════════════════════════════════

def test_association_score_ranks_beside_meaning_not_above_it():
    """
    Association weight has no ceiling, so a linear score lets one hot
    neighbourhood outrank everything reached by meaning. A first cut used a
    reference of 8 and effectively everything saturated -- a query scored 0.947
    by association against semantic matches at 0.86.

    Pure, because the curve is the decision and it should not need a graph in a
    particular state to check. The reference is calibrated against a real
    distribution: per-edge weights run median 3, p90 15, max 59, and a score
    sums every edge from the memories one query matched.
    """
    import neo4j_layer as n4j
    curve = lambda w: w / (w + n4j.ASSOC_REF)

    assert curve(1) < curve(10) < curve(100), "monotonic in weight"
    assert curve(10 ** 9) < 1.0, "saturating, never reaching 1"
    assert curve(n4j.ASSOC_REF) == pytest.approx(0.5), "0.5 at the reference"
    # A typical strong semantic match sits around 0.85. Association has to
    # clear a real bar to get there, rather than arriving by accumulation.
    assert curve(100) < 0.85, "a merely well-connected skill must not outrank meaning"


def test_association_floor_keeps_weak_matches_out():
    """
    A matched skill WITHHOLDS its members from the delivered context, so a weak
    match subtracts evidence rather than adding noise. That is the same reason
    min_semantic is high, and it applies here too.
    """
    import neo4j_layer as n4j
    curve = lambda w: w / (w + n4j.ASSOC_REF)
    assert 0.0 < n4j.ASSOC_FLOOR < 0.75, "a floor that is neither absent nor a wall"
    assert curve(5) < n4j.ASSOC_FLOOR, "an incidental association must not deliver a skill"


@live
def test_closed_proposals_are_relabelled_and_pending_ones_are_not():
    """
    A rejected or crystallized proposal is not a proposal any more.

    It used to keep the :SkillProposal label, so `MATCH (p:SkillProposal)` in
    the browser counted settled decisions as pending work. The label now moves
    on every closing transition, and the migration in bootstrap_schema() moves
    the rows that were already closed when it arrived -- without that the
    split describes only decisions made from here on, and it is the EXISTING
    rows that cause the confusion.

    Read from source: bootstrap_schema() runs once at startup, against a graph
    this suite must not migrate to prove a point.
    """
    with open(os.path.join(_ROOT, "neo4j_layer.py"), encoding="utf-8") as fh:
        body = fh.read()

    boot = body[body.index("def bootstrap_schema("):]
    boot = boot[:boot.index(chr(10) + "def ", 1)]

    assert "SET p:ProposalRecord REMOVE p:SkillProposal" in boot, (
        "bootstrap_schema must relabel closed proposals that predate the label")
    assert "SET r:SkillProposal REMOVE r:ProposalRecord" in boot, (
        "and must restore any pending row wrongly left as a record")
    assert boot.index("CONSTRAINTS") < boot.index("ProposalRecord"), (
        "the migration must run after the constraints are ensured")

    # Pending is never swept up -- that would hide live proposals from review.
    mig = boot[boot.index("MATCH (p:SkillProposal)"):]
    mig = mig[:mig.index("RETURN count(p)")]
    assert "'pending'" not in mig, "a pending proposal must not be relabelled"
    for st in ("rejected", "crystallized", "superseded"):
        assert st in mig, st + " must be migrated"

    # And every closing transition relabels, not only the migration.
    for fn in ("def reject_skill_proposal(", "def close_proposal_for_members(",
               "def retire_unconfirmable_proposals("):
        blk = body[body.index(fn):]
        blk = blk[:blk.index(chr(10) + "def ", 1)]
        assert "ProposalRecord" in blk, fn + " closes a proposal without relabelling"


def test_a_rejected_cluster_is_never_re_offered_by_a_sweep():
    """
    The guarantee the relabel silently removed.

    "Rejection is permanent" was never enforced anywhere. It worked because
    the sweep's MERGE found the closed row and its ON MATCH -- which only
    writes while status = 'pending' -- declined to touch it. Once closed rows
    carry a different label the MERGE stops seeing them, and the next sweep
    would CREATE a fresh pending proposal for a cluster the user refused.

    The ceiling's own `known` lookup is not this check: it runs only when the
    queue is full, so on a graph with room it never executes at all.
    """
    with open(os.path.join(_ROOT, "neo4j_layer.py"), encoding="utf-8") as fh:
        body = fh.read()
    fn = body[body.index("def queue_skill_proposals("):]
    fn = fn[:fn.index(chr(10) + "def ", 1)]

    guard = fn.index("MATCH (r:ProposalRecord {member_key: $key})")
    merge = fn.index("MERGE (p:SkillProposal {member_key: $key})")
    assert guard < merge, "the closed-cluster check must run BEFORE the MERGE"

    # Both labels, wherever a cluster is looked up by key.
    known = fn[fn.index("known = s.run("):]
    known = known[:known.index("single()")]
    assert "ProposalRecord" in known, "the ceiling lookup must see closed clusters"


def test_rejected_records_are_never_pruned():
    """
    Retention forgets bookkeeping; it must not reverse a decision.

    A rejected record is the only thing standing between the sweep and
    re-offering that cluster -- deleting it does not forget the rejection, it
    undoes it. Superseded and crystallized records are different: one cannot
    come back (its members are Blue and out of candidacy) and the other should
    if its members are ever free again.
    """
    with open(os.path.join(_ROOT, "neo4j_layer.py"), encoding="utf-8") as fh:
        body = fh.read()
    fn = body[body.index("def prune_proposal_records("):]
    fn = fn[:fn.index(chr(10) + "def ", 1)]

    cut = fn.index("DETACH DELETE r")
    where = fn[fn.rindex("WHERE", 0, cut):cut]
    assert "'superseded', 'crystallized'" in where, (
        "the prune must name exactly the statuses it removes")
    assert "rejected" not in where, (
        "a rejected record must never be selected for deletion")

    # days=0 disables it, rather than meaning "everything is older than now".
    assert "if window <= 0:" in fn, "a non-positive window must disable pruning"


def test_projecting_retracts_an_association_to_an_absorbed_member():
    """
    `WHERE NOT o IN members` stops the projection WRITING a self-edge, which
    is not the same as one never existing: an outsider earns its edge
    honestly, add_skill_members later absorbs it, and the edge it already
    holds now points at its own skill.

    An edge from a member is a skill recommending itself -- /skills/growth
    reads association as evidence the skill should grow, so a self-edge makes
    delivering a member read as growth.

    Synthetic skill and memory, torn down whether or not this passes.
    """
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")

    sid, stamp = "test-retract-skill", "test-retract-2000-01-01T00:00:00"
    try:
        with drv.session() as s:
            s.run("""
                CREATE (sk:Skill {skill_id: $sid, trigger: 'test', procedure: 'test',
                                  created_at: $now, status: 'active'})
                CREATE (m:Memory {created_at: $stamp, address: $stamp})
                CREATE (m)-[:PROCEDURALIZED_FROM]->(sk)
                CREATE (m)-[:ASSOCIATED_WITH {weight: 5, seeded: 5,
                                              created_at: $now, last_at: $now}]->(sk)
            """, sid=sid, stamp=stamp, now=n4j.datetime.now().isoformat())
            before = s.run("""
                MATCH (:Memory {created_at: $stamp})-[a:ASSOCIATED_WITH]->
                      (:Skill {skill_id: $sid})
                RETURN count(a) AS n
            """, stamp=stamp, sid=sid).single()["n"]
        assert before == 1, "fixture did not take"

        n4j.project_skill_associations(sid)

        with drv.session() as s:
            after = s.run("""
                MATCH (:Memory {created_at: $stamp})-[a:ASSOCIATED_WITH]->
                      (:Skill {skill_id: $sid})
                RETURN count(a) AS n
            """, stamp=stamp, sid=sid).single()["n"]
            member = s.run("""
                MATCH (:Memory {created_at: $stamp})-[r:PROCEDURALIZED_FROM]->
                      (:Skill {skill_id: $sid})
                RETURN count(r) AS n
            """, stamp=stamp, sid=sid).single()["n"]
        assert after == 0, "a member's association with its own skill is retracted"
        assert member == 1, "retraction must not touch membership"
    finally:
        with drv.session() as s:
            s.run("MATCH (m:Memory {created_at: $stamp}) DETACH DELETE m", stamp=stamp)
            s.run("MATCH (sk:Skill {skill_id: $sid}) DETACH DELETE sk", sid=sid)


def test_review_routines_asks_before_clearing_the_queue():
    """
    Being asked to CHECK the queue is not being asked to empty it.

    With crystallization enabled there was nothing between the two requests,
    and nineteen proposals went through in one turn without anyone seeing
    them. The fix is a question, not a lock -- the capability is unchanged.
    """
    with open(os.path.join(_ROOT, "mmu_mcp_server.py"), encoding="utf-8") as fh:
        src = fh.read()
    block = src[src.index("These are proposals only."):]
    block = block[:block.index("return ")]
    # Rejoin adjacent string literals, so these assertions survive a rewrap.
    # The instruction is one sentence to the model; where the source happens
    # to break the line is not part of it.
    joined = re.sub(r'"\s*"', "", block)

    assert "ALLOW_CRYSTALLIZE" in block, (
        "the instruction must depend on whether the model can actually act")
    assert "ASK whether" in joined, "it must ask, not assume"
    assert "do not ask again for the rest of that run" in joined, (
        "one answer must cover the run -- asking per proposal is its own problem")

    # Every tool it names has to exist. Text naming an unregistered tool is
    # what sent the model round the loop twice before.
    for tool in ("crystallize_routine", "grow_routine"):
        assert tool in joined, "the instruction should name " + tool
        assert '"name": "' + tool + '"' in src, tool + " is named but not registered"


def test_a_skill_is_never_associated_with_its_own_members():
    """
    A skill already contains its members. An edge back would make every
    delivery register as growth, which is the one signal this edge exists to
    provide honestly.
    """
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")
    with drv.session() as s:
        rec = s.run("""
            MATCH (o:Memory)-[:ASSOCIATED_WITH]->(sk:Skill)
            WHERE (o)-[:PROCEDURALIZED_FROM]->(sk)
            RETURN count(*) AS n
        """).single()
    assert rec["n"] == 0


@live
def test_backfilling_associations_twice_does_not_double_them():
    """
    The projection SETS the seed rather than adding to it, and carries forward
    whatever has accumulated since. Re-running a repair must not inflate the
    thing it repairs -- and this one also runs on every membership change, so
    it gets re-run a lot.
    """
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")

    def total():
        with drv.session() as s:
            r = s.run("""MATCH (:Memory)-[a:ASSOCIATED_WITH]->(:Skill)
                         RETURN sum(a.weight) AS w, count(a) AS n""").single()
        return (r["w"] or 0, r["n"] or 0)

    # Both readings must bracket backfills with nothing else in between.
    # Recall writes CO_RECALLED edges, so a reading taken before some other
    # test ran a query is not a baseline -- the projection is DERIVED from
    # those edges and is supposed to move when they do.
    _call("POST", "/skills/project_associations?apply=true")
    once = total()
    if once[1] == 0:
        pytest.skip("nothing projected on this graph")
    _call("POST", "/skills/project_associations?apply=true")
    assert total() == once, "a second backfill over an unchanged graph must be a no-op"


@live
def test_growth_separates_the_seed_from_what_accumulated():
    """
    The reason the live edge is maintained: a memory repeatedly delivered
    beside a skill is evidence the skill should grow to include it -- and since
    add_skill_members() that is something the system can act on.

    Only `grown` is new evidence. A high weight that is entirely seed is just
    the cluster it already was.
    """
    st, d = _call("GET", "/skills/growth?limit=20&min_weight=1")
    assert st == 200
    if not d["count"]:
        pytest.skip("no growth candidates on this graph")
    for c in d["candidates"]:
        assert c["weight"] >= 1
        assert c["grown"] == c["weight"] - c["seeded"], \
            "grown must be what accumulated after the projection, not the total"
        assert c["seeded"] >= 0


@live
def test_uncrystallizing_drops_the_projection():
    """
    The edges describe a position in the graph that stops existing when the
    skill does. Left behind they would point at a deleted node, and a
    DETACH DELETE would remove them without anyone being told how many.
    """
    n4j, drv = _n4j()
    if drv is None:
        pytest.skip("no Neo4j")
    with drv.session() as s:
        rec = s.run("""
            MATCH (:Memory)-[a:ASSOCIATED_WITH]->(sk:Skill)
            WHERE sk.status = 'deprecated' OR sk.skill_id IS NULL
            RETURN count(a) AS n
        """).single()
    assert rec["n"] == 0, "no association may survive the skill it pointed at"


# ═════════════════════════════════════════════════════════════
#  A blocked proposal has a route that exists
# ═════════════════════════════════════════════════════════════

def _server_src():
    """mmu_server.py as text. Matches the read used elsewhere in this file."""
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1] / "mmu_server.py").read_text(
        encoding="utf-8")


def test_the_blocked_refusal_never_recommends_an_unreachable_route():
    """
    The regression guard for the loop itself.

    The refusal used to say "crystallize a different proposal with `extends`
    set to the owning skill id, or link the owning skill under a parent".
    Both are unreachable. The ownership check runs inside the write
    transaction and nothing relaxes it; _link_parent() runs after
    crystallize_skill has already committed and only draws an EXTENDS_SKILL
    edge between two Skill nodes. And crystallize takes a proposal_id with no
    member-subset parameter, so "crystallize just the free ones" could not be
    expressed even if it worked.

    A model told to do the impossible does it, fails, and tries the next
    impossible thing. That is the loop.
    """
    import inspect, neo4j_layer as n4j
    src = inspect.getsource(n4j.crystallize_skill)
    branch = src[src.index("if owners:"):]

    assert "grow_routine" in branch, "the refusal must name the tool that works"
    assert "/members" in branch, "and the HTTP route behind it"
    assert "does NOT help" in branch, "and must explicitly deny extends"
    for bad in ("with `extends` set", "extends set to the owning",
                "link the owning skill under a parent"):
        assert bad not in branch, f"refusal still recommends {bad!r}"


def test_the_refusal_separates_free_members_from_owned_ones():
    """
    Naming the owned addresses alone leaves the reader to work out which of
    its own members are still available -- and the complement is the part that
    matters. Both lists, explicitly, and a distinct case when there are none.
    """
    import inspect, neo4j_layer as n4j
    branch = inspect.getsource(n4j.crystallize_skill)
    branch = branch[branch.index("if owners:"):]
    assert "free = [a for a in member_addresses if a not in set(taken)]" in branch
    assert "if free:" in branch and "else:" in branch


def test_growth_is_gated_exactly_like_crystallization():
    """
    Growing demotes memories to Blue on a model's say-so, the same consequence
    crystallizing has. Same three gates -- and the server must not rely on the
    client keeping the MCP-side promise.
    """
    import inspect, mmu_mcp_server as mcp
    body = _server_src()
    body = body[body.index("def grow_from_proposal"):]
    assert "_guard_model_write" in body, "the endpoint must be server-gated"
    assert "confirmed" in body, "the endpoint must require confirmation"

    gated = inspect.getsource(mcp)
    gated = gated[gated.index("if ALLOW_CRYSTALLIZE:"):]
    assert '"name": "grow_routine"' in gated, \
        "grow_routine must only be registered behind ALLOW_CRYSTALLIZE"


def test_growth_never_reaches_the_idle_daemon():
    """
    The idle sweep's safety property is that it writes SkillProposal nodes and
    nothing else. Growth demotes real memories. Crystallize was deliberately
    kept out of IDLE_TOOLS for this reason and growth belongs on the same side
    of that line -- this is the kind of thing added later "for symmetry".
    """
    import inspect, mmu_idle_daemon as d
    src = inspect.getsource(d)
    tools = src[src.index("IDLE_TOOLS"):]
    tools = tools[:tools.index(chr(10) + "def ")] if (chr(10) + "def ") in tools else tools
    assert "grow" not in tools.lower(), "growth must stay out of IDLE_TOOLS"


def test_growth_only_targets_a_skill_that_blocks_the_proposal():
    """
    The safety rail. Without it the tool is "put any memories into any skill",
    which is far more capability than resolving a block requires. Growth may
    only merge a proposal into a skill the system itself reported as blocking
    that proposal.
    """
    body = _server_src()
    body = body[body.index("def grow_from_proposal"):]
    assert "does not own any member of this proposal" in body
    assert 'resolved not in {o["skill_id"] for o in owners}' in body, \
        "the target must be validated against the proposal's actual owners"


def test_growth_closes_the_proposal_with_its_whole_member_set():
    """
    close_proposal_for_members() keys on the member_key of everything it is
    given, so passing only the free subset computes a different key and
    matches nothing -- leaving the proposal pending and fully absorbed, which
    is exactly the state that sends a reader round again.
    """
    body = _server_src()
    body = body[body.index("def grow_from_proposal"):]
    assert "close_proposal_for_members(all_members" in body, \
        "the whole member set closes the proposal, not just the free ones"


def test_a_single_free_member_is_still_actionable():
    """
    The retirement floor was two, justified in its own comment by
    crystallizing the free members under the owner via `extends` -- a route
    that never existed. It therefore discarded the one-free-member case,
    which is precisely the one growth handles best.
    """
    import inspect, neo4j_layer as n4j
    src = inspect.getsource(n4j.retire_unconfirmable_proposals)
    assert "- claimed < 1" in src, "one free member must not be retired"
    assert "- claimed < 2" not in src


@live
def test_a_blocked_proposal_reports_the_routine_to_grow():
    """
    `suggested_parent` was named for a route that does not exist, and the name
    is what pushed both the queue text and the model toward `extends`. The
    value is the skill that already owns the most of this proposal.
    """
    _, d = _call("GET", "/skill_proposals?limit=50")
    blocked = [p for p in d.get("proposals", []) if p.get("blocked")]
    if not blocked:
        pytest.skip("nothing blocked on this graph")
    for p in blocked:
        assert "owner_skill_id" in p, "the routine to grow must be named"
        assert "suggested_parent" not in p, "the misleading name must be gone"
        if p.get("free_members"):
            assert p["owner_skill_id"], \
                "any free member at all makes this growable -- no >= 2 floor"


@live
def test_growing_refuses_a_routine_that_owns_nothing_in_the_proposal():
    """The safety rail, end to end."""
    _, d = _call("GET", "/skill_proposals?limit=50")
    blocked = [p for p in d.get("proposals", [])
               if p.get("blocked") and p.get("free_members")]
    _, sk = _call("GET", "/skills?status=active")
    if not blocked or len(sk.get("skills", [])) < 2:
        pytest.skip("need a blocked proposal and a second routine")

    target = blocked[0]
    stranger = next((s["skill_id"] for s in sk["skills"]
                     if s["skill_id"] != target["owner_skill_id"]), None)
    if not stranger:
        pytest.skip("no non-owning routine to try")

    st, body = _call("POST", f"/skill_proposals/{target['proposal_id']}/grow",
                     {"skill_id": stranger, "confirmed": True})
    assert st == 409, "growing into an unrelated routine must be refused"
    assert "does not own any member" in body.get("detail", "")


@live
def test_growing_requires_confirmation():
    """Same gate as crystallize: it buries memories in Blue."""
    _, d = _call("GET", "/skill_proposals?limit=50")
    blocked = [p for p in d.get("proposals", [])
               if p.get("blocked") and p.get("free_members")]
    if not blocked:
        pytest.skip("nothing blocked on this graph")
    st, _ = _call("POST", f"/skill_proposals/{blocked[0]['proposal_id']}/grow",
                  {"confirmed": False})
    assert st == 400


# ═════════════════════════════════════════════════════════════
#  The session context describes the session it is in
# ═════════════════════════════════════════════════════════════

def _proposal_block():
    """
    The emitted lines of the session bundle's crystallization block, with
    comments stripped.

    Comments in there quote the old wording in order to explain why it was
    wrong, so a scan that includes them finds exactly the strings it is
    checking are gone. What matters is what reaches the model.
    """
    src = _server_src()
    blk = src[src.index("MEMORY CLUSTERS READY FOR REVIEW"):]
    blk = blk[:blk.index("# Phase 8: blend in")]
    blk = chr(10).join(l for l in blk.split(chr(10))
                       if not l.strip().startswith("#"))
    # Join adjacent string literals. The emitted sentences are wrapped across
    # several of them, so a phrase the reader sees as one run is not
    # contiguous in the source and a substring check would miss it.
    return re.sub(r'"\s*\n\s*"', '', blk)


def test_the_proposal_block_matches_what_the_model_can_do():
    """
    The crystallization block asserted flatly that this "cannot be done from
    any tool you have". That holds only while MMU_ALLOW_MODEL_CRYSTALLIZE is
    off. With it on the model has crystallize_routine and grow_routine, and
    this text was contradicting its own tool list at the top of every single
    conversation.

    Read as source rather than rendered, because the wrong branch is the
    failure and a test that only ever sees one of them would not notice.
    """
    blk = _proposal_block()

    assert "if MODEL_MAY_CRYSTALLIZE:" in blk, \
        "what the model is told must depend on what it can do"
    permissive, gated = blk.split("else:", 1)
    assert "You CAN do this yourself" in permissive
    assert "cannot be done from any tool you have" in gated
    assert "cannot be done from any tool you have" not in permissive, \
        "the permissive branch must not repeat the human-gated claim"


def test_the_proposal_block_names_a_tool_that_exists():
    """
    It said "Use review_skills". That is not a tool -- the Skill to Routine
    rename covered the MCP surface and missed this string, so the one
    actionable instruction in the block named something the model could not
    call.
    """
    import inspect, mmu_mcp_server as mcp
    blk = _proposal_block()

    registered = set(re.findall(r'"name": "(\w+)"', inspect.getsource(mcp)))
    for named in re.findall(r"\b(review_\w+|crystallize_\w+|grow_\w+|uncrystallize_\w+)\b", blk):
        assert named in registered, \
            f"the session context tells the model to use {named!r}, which is not a tool"


def test_the_readme_does_not_promise_a_gate_the_flag_removes():
    """
    The README said "never automatically", "Nothing crystallizes on its own"
    and "You write the trigger and the procedure. Nothing else does." All
    three stop being true with MMU_ALLOW_MODEL_CRYSTALLIZE=true, and a reader
    meets them three hundred lines before the section that explains the flag.

    They are allowed to stay -- they describe the default -- but each has to
    carry the qualification with it.
    """
    import pathlib
    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8")

    assert "never automatically." not in readme, \
        "an unqualified 'never automatically' outlives the flag that breaks it"
    assert "Nothing else does." not in readme, \
        "'Nothing else does' is false whenever the model is the one doing it"

    blk = readme[readme.index("**Nothing crystallizes on its own.**"):]
    blk = blk[:blk.index("```")]
    assert "MMU_ALLOW_MODEL_CRYSTALLIZE" in blk, \
        "the claim and its exception must sit together, not 300 lines apart"


def test_every_status_the_sweep_writes_can_be_asked_for():
    """
    /skill_proposals validates `status` against a hardcoded list, and
    `superseded` was added to the sweep without being added here -- so the one
    status the merge and retirement work produce in bulk answered 400, for
    rows that are plainly there through status= (all).

    Read from the source of both sides rather than asserted as a literal, so
    a fifth status added to the writer and not the filter fails here rather
    than in someone's terminal.
    """
    import inspect, neo4j_layer as n4j

    src = _server_src()
    blk = src[src.index("def skill_proposals("):]
    blk = blk[:blk.index("@app.", 10)]
    allowed = set(re.findall(r'"(pending|rejected|crystallized|superseded)"', blk))

    written = set()
    for fn in (n4j.queue_skill_proposals, n4j.retire_unconfirmable_proposals,
               n4j.reject_skill_proposal, n4j.close_proposal_for_members):
        src_fn = inspect.getsource(fn)
        written |= set(re.findall(r"p\.status\s*=\s*'(\w+)'", src_fn))
        written |= set(re.findall(r"f\.status\s*=\s*'(\w+)'", src_fn))

    missing = written - allowed
    assert not missing, (
        f"the sweep writes {sorted(missing)} but /skill_proposals refuses it as "
        "a filter, so those rows cannot be listed")
