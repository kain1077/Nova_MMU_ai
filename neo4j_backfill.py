"""
Neo4j Backfill Script
=====================
One-time migration of all existing SQLite memories into Neo4j.
Safe to re-run — uses MERGE so nothing gets duplicated.

Run from C:\\mmu\\ after docker compose up:
    python neo4j_backfill.py

What it does:
    1. Reads every memory from SQLite
    2. Writes Memory + Source nodes to Neo4j
    3. Writes Keyword nodes + HAS_KEYWORD edges
    4. Builds SIMILAR_TO edges between fuzzy-matched keywords
    5. Reports what was written

Does NOT build CO_RECALLED edges — those come from live sessions.
"""

import sqlite3
import os
import sys
import time
from difflib import SequenceMatcher

# ── Config ────────────────────────────────────────────
# Adjust these paths if yours differ

DB_PATH    = os.environ.get("MMU_DB_PATH",  r"C:\mmu\data\memory_system.db")
NEO4J_URI  = os.environ.get("NEO4J_URI",   "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER",  "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASS",  "mmupassword")

SIMILAR_THRESH = 0.72
SOURCE_LABELS  = {0: "Conversation", 1: "AI-Self", 2: "Document", 3: "Web"}

# ── Helpers ───────────────────────────────────────────

def stem(word):
    w = word.lower().strip()
    return w[:4] if len(w) >= 4 else w

def similar_score(a, b):
    a, b = a.lower(), b.lower()
    if a == b:            return 1.0
    if a in b or b in a: return 0.9
    if stem(a) == stem(b): return 0.85
    return SequenceMatcher(None, a, b).ratio()

def sep(label):
    print(f"\n{'─'*55}\n  {label}\n{'─'*55}")

# ── Main ──────────────────────────────────────────────

def run():
    # Connect to Neo4j
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        driver.verify_connectivity()
        print(f"✅ Neo4j connected at {NEO4J_URI}")
    except Exception as e:
        print(f"❌ Neo4j connection failed: {e}")
        print("   Make sure Docker is running:  docker compose up -d")
        sys.exit(1)

    # Connect to SQLite
    if not os.path.exists(DB_PATH):
        print(f"❌ SQLite DB not found at: {DB_PATH}")
        print("   Set MMU_DB_PATH env var if your path differs")
        sys.exit(1)

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT address, keywords, payload, color, src_type, "
        "src_chunk, src_line, created_at, note FROM memories"
    )
    rows = cursor.fetchall()
    print(f"✅ SQLite connected — {len(rows)} memories to backfill")

    if not rows:
        print("   Nothing to backfill.")
        return

    sep(f"Phase 1 — Writing {len(rows)} Memory nodes")

    written   = 0
    skipped   = 0
    all_keywords = []   # collect for SIMILAR_TO pass

    with driver.session() as s:

        # Bootstrap constraints first
        for stmt in [
            "CREATE CONSTRAINT memory_addr IF NOT EXISTS FOR (m:Memory) REQUIRE m.address IS UNIQUE",
            "CREATE CONSTRAINT keyword_term IF NOT EXISTS FOR (k:Keyword) REQUIRE k.term IS UNIQUE",
            "CREATE CONSTRAINT source_key   IF NOT EXISTS FOR (src:Source) REQUIRE src.source_key IS UNIQUE",
        ]:
            try:
                s.run(stmt)
            except Exception:
                pass

        for addr, keywords, payload, color, src_type, src_chunk, src_line, created_at, note in rows:
            try:
                now = created_at or "unknown"
                src_label = SOURCE_LABELS.get(src_type, "Unknown")

                # Memory node
                s.run("""
                    MERGE (m:Memory {address: $address})
                    ON CREATE SET
                        m.payload    = $payload,
                        m.color      = $color,
                        m.priority   = $priority,
                        m.src_type   = $src_type,
                        m.src_label  = $src_label,
                        m.src_chunk  = $src_chunk,
                        m.src_line   = $src_line,
                        m.note       = $note,
                        m.created_at = $created_at,
                        m.use        = 0,
                        m.arc        = 0
                    ON MATCH SET
                        m.color   = $color,
                        m.payload = $payload
                """, address=addr, payload=payload, color=color,
                     priority=5, src_type=src_type, src_label=src_label,
                     src_chunk=src_chunk, src_line=src_line,
                     note=note or "", created_at=now)

                # Source node + FROM_SOURCE edge
                source_key = f"{src_type}.{src_chunk}.{src_line}"
                s.run("""
                    MERGE (src:Source {source_key: $key})
                    ON CREATE SET
                        src.src_type  = $src_type,
                        src.src_label = $src_label,
                        src.chunk     = $src_chunk,
                        src.line      = $src_line
                    WITH src
                    MATCH (m:Memory {address: $addr})
                    MERGE (m)-[:FROM_SOURCE]->(src)
                """, key=source_key, src_type=src_type, src_label=src_label,
                     src_chunk=src_chunk, src_line=src_line, addr=addr)

                # Keyword nodes + HAS_KEYWORD edges
                kw_list = [k.strip().lower() for k in keywords.split(",") if k.strip()]
                for term in kw_list:
                    s.run("""
                        MERGE (k:Keyword {term: $term})
                        ON CREATE SET k.stem = $stem, k.freq = 1,
                                      k.created_at = $now
                        ON MATCH  SET k.freq = k.freq + 1
                    """, term=term, stem=stem(term), now=now)

                    s.run("""
                        MATCH (m:Memory  {address: $addr})
                        MATCH (k:Keyword {term: $term})
                        MERGE (m)-[:HAS_KEYWORD]->(k)
                    """, addr=addr, term=term)

                    all_keywords.append(term)

                written += 1
                icon = {"Red":"🔴","Green":"🟢","Yellow":"🟡","Blue":"🔵"}.get(color,"⚪")
                print(f"  {icon} [{color:7s}] {addr[:35]}  keywords: {keywords[:45]}")

            except Exception as e:
                skipped += 1
                print(f"  ⚠️  Skipped {addr[:30]}: {e}")

    sep(f"Phase 2 — Building SIMILAR_TO edges between {len(set(all_keywords))} unique keywords")

    unique_kws = list(set(all_keywords))
    pairs_written = 0

    with driver.session() as s:
        for i, a in enumerate(unique_kws):
            for b in unique_kws[i+1:]:
                score = similar_score(a, b)
                if score >= SIMILAR_THRESH:
                    s.run("""
                        MATCH (ka:Keyword {term: $a})
                        MATCH (kb:Keyword {term: $b})
                        MERGE (ka)-[r:SIMILAR_TO]-(kb)
                        ON CREATE SET r.score = $score,
                                      r.stem_match = ($stem_a = $stem_b)
                        ON MATCH  SET r.score = CASE
                            WHEN $score > r.score THEN $score
                            ELSE r.score END
                    """, a=a, b=b, score=score,
                         stem_a=stem(a), stem_b=stem(b))
                    pairs_written += 1
                    print(f"  🔗 '{a}' ↔ '{b}'  score={score:.2f}")

    sep("Phase 3 — Verification")

    with driver.session() as s:
        counts = s.run("""
            MATCH (m:Memory)  WITH count(m) AS mem
            MATCH (k:Keyword) WITH mem, count(k) AS kw
            OPTIONAL MATCH ()-[r:SIMILAR_TO]-()
            RETURN mem, kw, count(r)/2 AS sim_edges
        """).single()

        print(f"  Memory nodes  : {counts['mem']}")
        print(f"  Keyword nodes : {counts['kw']}")
        print(f"  SIMILAR_TO    : {counts['sim_edges']} edges")

        # Show which keyword pairs are connected by SIMILAR_TO
        sims = s.run("""
            MATCH (a:Keyword)-[r:SIMILAR_TO]-(b:Keyword)
            RETURN a.term AS a, b.term AS b, r.score AS score
            ORDER BY score DESC LIMIT 15
        """).data()

        if sims:
            print(f"\n  Top keyword similarities (potential cross-references):")
            for row in sims:
                print(f"    '{row['a']}' ↔ '{row['b']}'  {row['score']:.2f}")

    print(f"\n{'='*55}")
    print(f"  Backfill complete!")
    print(f"  Written : {written} memories")
    print(f"  Skipped : {skipped}")
    print(f"  SIMILAR_TO edges : {pairs_written}")
    print(f"\n  Now start a chat in LM Studio — CO_RECALLED edges")
    print(f"  will build automatically as memories are recalled together.")
    print(f"\n  View in Neo4j Browser: http://127.0.0.1:7474")
    print(f"  Query to see connections:")
    print(f"    MATCH (a:Memory)-[r:CO_RECALLED]-(b:Memory)")
    print(f"    RETURN a,r,b ORDER BY r.weight DESC")
    print(f"{'='*55}\n")

    driver.close()
    conn.close()

if __name__ == "__main__":
    run()
