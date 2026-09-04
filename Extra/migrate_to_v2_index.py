"""
migrate_to_v2_index.py — Backfill + A/B harness for the two-tier index
======================================================================

Run this INSIDE the container, against the live Neo4j graph. It does
three things, in order, and stops if any step looks wrong:

  1. BUILD    — rebuild both tiers from the existing Memory/Keyword graph
  2. VERIFY   — sanity-check coverage against the graph's own counts
  3. BENCH    — A/B the gate path against the current 5-stage pipeline
                on a set of real prompts, reporting latency and overlap

Nothing is destructive. The old memory_index.json is left untouched, and
the new index writes to memory_index_v2.json. Neo4j is read-only here.

Usage
-----
    docker cp light_index_v2.py mmu-memory-server:/app/
    docker cp migrate_to_v2_index.py mmu-memory-server:/app/
    docker exec -it mmu-memory-server python /app/migrate_to_v2_index.py

    # bench only, once the index exists
    docker exec -it mmu-memory-server python /app/migrate_to_v2_index.py --bench
"""

import os
import sys
import time
import json

sys.path.insert(0, "/app")

from light_index_v2 import LightIndexV2, tokenize   # noqa: E402

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASS", "mmupassword")

# Prompts to A/B. Mix of things that SHOULD hit and things that should
# miss cleanly — the miss cases are where the gate earns its keep.
TEST_PROMPTS = [
    "what am I working on",
    "tell me about the MMU project",
    "how does the color matrix work",
    "what do you know about Neo4j",
    "who are you",
    "what's the weather in Denver",          # expected miss
    "explain quantum chromodynamics",         # expected miss
    "recipe for sourdough starter",           # expected miss
]

BAR = "─" * 66


def get_driver():
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    d.verify_connectivity()
    return d


# ─────────────────────────────────────────────
#  STEP 1 — BUILD
# ─────────────────────────────────────────────

def build(driver):
    print(BAR)
    print("STEP 1 — building two-tier index from Neo4j")
    print(BAR)

    idx = LightIndexV2()
    t0 = time.perf_counter()
    result = idx.rebuild_from_neo4j(driver)
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"  keywords indexed : {result['keywords']}")
    print(f"  cards built      : {result['cards']}")
    print(f"  shortcuts baked  : {result['shortcuts']}")
    print(f"  build time       : {elapsed:.0f} ms")
    print(f"  written to       : {idx.path}")

    size = os.path.getsize(idx.path) / 1024
    print(f"  index size       : {size:.1f} KB")
    return idx


# ─────────────────────────────────────────────
#  STEP 2 — VERIFY
# ─────────────────────────────────────────────

def verify(idx, driver):
    print()
    print(BAR)
    print("STEP 2 — verifying against graph counts")
    print(BAR)

    with driver.session() as s:
        graph = s.run("""
            MATCH (m:Memory) WITH count(m) AS mem
            MATCH (k:Keyword) WITH mem, count(k) AS kw
            OPTIONAL MATCH ()-[r:CO_RECALLED]-() WITH mem, kw, count(r) AS co
            OPTIONAL MATCH ()-[s2:SIMILAR_TO]-() RETURN mem, kw, co,
                   count(s2) AS sim
        """).single()

    stats = idx.stats()
    checks = [
        ("Memory nodes vs cards",
         graph["mem"], stats["cards"], graph["mem"] == stats["cards"]),
        ("Keyword nodes vs index keys",
         graph["kw"], stats["keywords"], stats["keywords"] >= graph["kw"]),
        ("Cards with shortcuts",
         stats["cards"], stats["cards_fresh"], stats["cards_fresh"] > 0),
    ]

    ok = True
    for label, expected, actual, passed in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label:<32} graph={expected:<6} index={actual}")
        ok = ok and passed

    print(f"\n  avg shortcuts per card : {stats['shortcuts_avg']}")
    print(f"  cold cards (need live) : {stats['cards_cold']}")

    if not ok:
        print("\n  ! Verification failed — do not flip the read path yet.")
    return ok


# ─────────────────────────────────────────────
#  STEP 3 — BENCH
# ─────────────────────────────────────────────

def live_recall(driver, prompt, top_k=10):
    """
    Approximation of the CURRENT 5-stage pipeline, for timing comparison.
    Deliberately mirrors the existing stage structure including the
    unconditional SIMILAR_TO and CO_RECALLED hops.
    """
    words, stems = tokenize(prompt)
    t0 = time.perf_counter()
    found = {}

    with driver.session() as s:
        # Stage 0 — pinned
        for r in s.run("MATCH (m:Memory {color:'Red'}) RETURN m.address AS a"):
            found[r["a"]] = "pinned"

        # Stage 0.5 — stem expansion
        expanded = [r["t"] for r in s.run(
            "MATCH (k:Keyword) WHERE k.stem IN $stems RETURN k.term AS t",
            stems=stems)]
        terms = list(set(words + expanded))

        # Stage 1 — direct
        for r in s.run("""
            MATCH (m:Memory)-[:HAS_KEYWORD]->(k:Keyword)
            WHERE k.term IN $terms OR k.stem IN $stems
            RETURN DISTINCT m.address AS a
        """, terms=terms, stems=stems):
            found[r["a"]] = "direct"

        seeds = [a for a, v in found.items() if v == "direct"]

        # Stage 2 — SIMILAR_TO (fires regardless of stage 1 outcome)
        for r in s.run("""
            MATCH (k1:Keyword)-[sim:SIMILAR_TO]-(k2:Keyword)<-[:HAS_KEYWORD]-(m:Memory)
            WHERE k1.term IN $terms AND sim.score >= 0.72 AND m.color <> 'Blue'
            RETURN DISTINCT m.address AS a LIMIT 20
        """, terms=terms):
            found.setdefault(r["a"], "similar")

        # Stage 3 — CO_RECALLED
        if seeds:
            for r in s.run("""
                MATCH (m:Memory)-[:CO_RECALLED]-(n:Memory)
                WHERE m.address IN $seeds AND n.color <> 'Blue'
                RETURN DISTINCT n.address AS a LIMIT 20
            """, seeds=seeds):
                found.setdefault(r["a"], "corecall")

    ms = (time.perf_counter() - t0) * 1000
    return list(found.keys())[:top_k], ms


def gate_recall(idx, driver, prompt, top_k=10):
    """The proposed path: gate first, expand from cache, Cypher only if cold."""
    t0 = time.perf_counter()

    direct, pinned = idx.gate(prompt)
    if not direct:
        # Clean miss — pinned only, zero Cypher
        results = idx.rank(set(), pinned, {}, top_k=top_k)
        return [r["address"] for r in results], (time.perf_counter() - t0) * 1000, 0

    expanded, cold = idx.expand(direct)
    cypher_calls = 0

    # Only touch the graph for seeds whose cache was stale or thin
    if cold:
        cypher_calls = 1
        with driver.session() as s:
            for r in s.run("""
                MATCH (m:Memory)-[:CO_RECALLED]-(n:Memory)
                WHERE m.address IN $cold AND n.color <> 'Blue'
                RETURN DISTINCT n.address AS a, 0.6 AS score LIMIT 20
            """, cold=cold):
                expanded.setdefault(r["a"], (r["score"], "corecall-live"))

    results = idx.rank(direct, pinned, expanded, top_k=top_k)
    ms = (time.perf_counter() - t0) * 1000
    return [r["address"] for r in results], ms, cypher_calls


def bench(idx, driver):
    print()
    print(BAR)
    print("STEP 3 — A/B benchmark")
    print(BAR)
    print(f"  {'prompt':<34}{'current':>9}{'gate':>9}{'cyph':>6}{'overlap':>9}")
    print(f"  {'-'*32:<34}{'-'*8:>9}{'-'*8:>9}{'-'*5:>6}{'-'*8:>9}")

    tot_cur = tot_gate = 0.0
    tot_cypher = 0

    for prompt in TEST_PROMPTS:
        cur, cur_ms = live_recall(driver, prompt)
        gat, gat_ms, calls = gate_recall(idx, driver, prompt)

        overlap = len(set(cur) & set(gat))
        union = len(set(cur) | set(gat)) or 1
        pct = f"{100 * overlap // union}%"

        label = (prompt[:31] + "...") if len(prompt) > 31 else prompt
        print(f"  {label:<34}{cur_ms:>8.0f}m{gat_ms:>8.0f}m{calls:>6}{pct:>9}")

        tot_cur += cur_ms
        tot_gate += gat_ms
        tot_cypher += calls

    n = len(TEST_PROMPTS)
    print(f"  {'-'*32:<34}{'-'*8:>9}{'-'*8:>9}{'-'*5:>6}{'-'*8:>9}")
    print(f"  {'average':<34}{tot_cur/n:>8.0f}m{tot_gate/n:>8.0f}m"
          f"{tot_cypher:>6}")

    if tot_gate < tot_cur:
        speedup = tot_cur / tot_gate if tot_gate else 0
        print(f"\n  Gate path is {speedup:.1f}x faster overall.")
    else:
        print("\n  ! Gate path is NOT faster — check cache freshness "
              "(cards_cold above).")

    print("\n  Note: low overlap on MISS prompts is expected and correct —")
    print("  the gate is supposed to return nothing where the current")
    print("  pipeline returns loose SIMILAR_TO noise. Check overlap on the")
    print("  HIT prompts only when judging recall quality.")


# ─────────────────────────────────────────────

def main():
    bench_only = "--bench" in sys.argv

    try:
        driver = get_driver()
    except Exception as e:
        print(f"Neo4j unreachable at {NEO4J_URI}: {e}")
        sys.exit(1)

    if bench_only:
        idx = LightIndexV2()
        if not idx.shortcut_cache:
            print("No v2 index found — run without --bench first.")
            sys.exit(1)
        bench(idx, driver)
    else:
        idx = build(driver)
        if verify(idx, driver):
            bench(idx, driver)
            print()
            print(BAR)
            print("Index built and benched. Nothing was changed in Neo4j,")
            print("and the old memory_index.json is untouched. To adopt:")
            print("  1. wire LightIndexV2 into mmu_server.recall()")
            print("  2. call idx.add() from add_memory()")
            print("  3. call idx.bump_corecall() where write_recall_edges fires")
            print("  4. call idx.rename() from _age_memories")
            print(BAR)

    driver.close()


if __name__ == "__main__":
    main()
