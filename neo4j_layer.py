"""
Neo4j Graph Layer — Phase 4 (Self-Correction)
=============================================
SQLite removed. Neo4j is the sole data store.

Node types:    Memory · Keyword · Session · Source
Edge types:    HAS_KEYWORD · SIMILAR_TO · CO_RECALLED
               FROM_SOURCE · RECALLED_IN · EVOLVES_FROM

Phase 1: parallel Neo4j writes alongside SQLite
Phase 2: graph reads + two-tier LightIndexV2 gate (10.7x speedup)
Phase 3: SQLite removed, session_bundle endpoint, GRP taxonomy
Phase 4: self-correction via false_recall_count + reflection audit
  - flag_memory()          increment false_recall_count on a Memory node
  - get_flagged_memories() surface memories with count >= threshold
  - reset_flag_count()     clear count after successful audit
  - apply_memory_correction() apply Nova's correction to a flagged memory
"""

import os
import json
import time
import uuid
import hashlib
import logging
from datetime import datetime
from difflib import SequenceMatcher

log = logging.getLogger("neo4j_layer")

NEO4J_URI     = os.environ.get("NEO4J_URI",     "bolt://127.0.0.1:7687")
NEO4J_USER    = os.environ.get("NEO4J_USER",    "neo4j")
NEO4J_PASS    = os.environ.get("NEO4J_PASS",    "mmupassword")
NEO4J_ENABLED = os.environ.get("NEO4J_ENABLED", "false").lower() == "true"

# Keyword similarity threshold for SIMILAR_TO edges
SIMILAR_THRESH = 0.72

# -- Phase 9: semantic embedding layer --
# Dimension is fixed at vector-index creation time and MUST match whatever
# MMU_EMBEDDING_MODEL actually returns. Verified against the live LM Studio
# instance at deploy time, not assumed from the model name.
EMBEDDING_DIM   = int(os.environ.get("MMU_EMBEDDING_DIM", "768"))
EMBEDDING_INDEX = "memory_embedding"
ROUTINE_EMBEDDING_INDEX = "routine_embedding"

SOURCE_LABELS = {
    0: "Conversation",
    1: "AI-Self",
    2: "Document",
    3: "Web",
    4: "Background Cognition",   # Phase 7 -- created during an idle cognition pass
}

# ─────────────────────────────────────────────
#  DRIVER INIT
# ─────────────────────────────────────────────

_driver = None

def get_driver():
    global _driver
    if _driver is not None:
        return _driver
    if not NEO4J_ENABLED:
        return None
    try:
        from neo4j import GraphDatabase
        _driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASS)
        )
        _driver.verify_connectivity()
        log.info(f"Neo4j connected at {NEO4J_URI}")
        return _driver
    except Exception as e:
        log.warning(f"Neo4j unavailable — parallel writes disabled: {e}")
        return None

def neo4j_session():
    """Context manager — returns None if Neo4j is down."""
    driver = get_driver()
    if driver is None:
        return None
    return driver.session()

# ─────────────────────────────────────────────
#  SCHEMA BOOTSTRAP
# ─────────────────────────────────────────────

CONSTRAINTS = [
    "CREATE CONSTRAINT memory_addr IF NOT EXISTS FOR (m:Memory) REQUIRE m.address IS UNIQUE",
    "CREATE CONSTRAINT keyword_term IF NOT EXISTS FOR (k:Keyword) REQUIRE k.term IS UNIQUE",
    "CREATE CONSTRAINT session_id   IF NOT EXISTS FOR (s:Session) REQUIRE s.session_id IS UNIQUE",
    "CREATE CONSTRAINT source_key   IF NOT EXISTS FOR (s:Source)  REQUIRE s.source_key IS UNIQUE",
    # Phase 7 -- CreativeOutput nodes produced during background cognition
    "CREATE CONSTRAINT creative_out  IF NOT EXISTS FOR (c:CreativeOutput) REQUIRE c.output_id IS UNIQUE",
    # Phase 12 -- Routine nodes (procedural memory crystallization)
    "CREATE CONSTRAINT routine_id      IF NOT EXISTS FOR (sk:Routine) REQUIRE sk.routine_id IS UNIQUE",
    # Phase 13.1 -- queued crystallization proposals awaiting human review.
    # member_key is unique so a repeated sweep MERGEs onto the same proposal
    # instead of stacking duplicates of a cluster that is simply still dense.
    "CREATE CONSTRAINT proposal_id   IF NOT EXISTS FOR (p:RoutineProposal) REQUIRE p.proposal_id IS UNIQUE",
    "CREATE CONSTRAINT proposal_key  IF NOT EXISTS FOR (p:RoutineProposal) REQUIRE p.member_key IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX memory_color    IF NOT EXISTS FOR (m:Memory)  ON (m.color)",
    "CREATE INDEX memory_priority IF NOT EXISTS FOR (m:Memory)  ON (m.priority)",
    "CREATE INDEX keyword_stem    IF NOT EXISTS FOR (k:Keyword) ON (k.stem)",
    # Phase 6.5 valence
    "CREATE INDEX memory_valence  IF NOT EXISTS FOR (m:Memory)  ON (m.valence_type)",
]

def bootstrap_schema():
    """
    Create constraints and indexes on first run. Safe to re-run.
    Also runs Phase 4 migration: backfills false_recall_count and audit
    fields onto any Memory node that was created before Phase 4.
    """
    s = neo4j_session()
    if s is None:
        return
    try:
        with s:
            for stmt in CONSTRAINTS + INDEXES:
                try:
                    s.run(stmt)
                except Exception as e:
                    log.debug(f"Schema stmt skipped (may already exist): {e}")

            # -- Phase 9: vector index on Memory.embedding --
            # Neo4j does not allow query parameters inside an index OPTIONS
            # map, so the dimension is interpolated. The int() cast at import
            # time already guarantees it is not injectable.
            try:
                s.run(f"""
                    CREATE VECTOR INDEX {EMBEDDING_INDEX} IF NOT EXISTS
                    FOR (m:Memory) ON (m.embedding)
                    OPTIONS {{ indexConfig: {{
                        `vector.dimensions`: {int(EMBEDDING_DIM)},
                        `vector.similarity_function`: 'cosine'
                    }} }}
                """)
                log.info("Vector index %s ensured (dim=%d, cosine)",
                         EMBEDDING_INDEX, EMBEDDING_DIM)

                # Phase 13.2: the same treatment for Routine. A separate index
                # rather than a shared one -- routines and memories are ranked
                # against each other only after both are retrieved, and mixing
                # labels in one ANN index would make "top k memories" and "top
                # k routines" compete for the same k.
                s.run(f"""
                    CREATE VECTOR INDEX {ROUTINE_EMBEDDING_INDEX} IF NOT EXISTS
                    FOR (sk:Routine) ON (sk.embedding)
                    OPTIONS {{ indexConfig: {{
                        `vector.dimensions`: {int(EMBEDDING_DIM)},
                        `vector.similarity_function`: 'cosine'
                    }} }}
                """)
                log.info("Vector index %s ensured", ROUTINE_EMBEDDING_INDEX)
            except Exception as e:
                # Community edition below 5.11 has no vector index support.
                # Semantic recall degrades to the keyword path; nothing breaks.
                log.warning("Vector index unavailable - semantic recall disabled: %s", e)

            # Phase 4 migration — idempotent backfill
            s.run("""
                MATCH (m:Memory)
                WHERE m.false_recall_count IS NULL
                SET m.false_recall_count = 0,
                    m.last_audited       = null,
                    m.audit_notes        = ''
            """)

            # Phase 10 migration -- last_touched_at, idempotent.
            #
            # Backfilled to NOW rather than created_at on purpose. We do not
            # know when these were last recalled, and created_at would make a
            # memory written weeks ago but recalled yesterday look stale enough
            # to archive on its next count trip. Seeding "now" is the
            # conservative direction: it delays archiving rather than
            # accelerating it, and real touch data replaces it within a
            # session or two of normal use.
            s.run("""
                MATCH (m:Memory)
                WHERE m.last_touched_at IS NULL
                SET m.last_touched_at = $now
            """, now=datetime.now().isoformat())
        log.info("Neo4j schema bootstrapped (Phase 4 fields backfilled)")
    except Exception as e:
        log.warning(f"Neo4j schema bootstrap failed: {e}")

# ─────────────────────────────────────────────
#  KEYWORD HELPERS
# ─────────────────────────────────────────────

def _stem(word):
    """4-char stem for grouping similar words."""
    w = word.lower().strip()
    return w[:4] if len(w) >= 4 else w

def _similar_score(a, b):
    """Return similarity score 0-1 between two keyword strings."""
    a, b = a.lower(), b.lower()
    if a == b:          return 1.0
    if a in b or b in a: return 0.9
    if _stem(a) == _stem(b): return 0.85
    return SequenceMatcher(None, a, b).ratio()

def _get_existing_keywords(s):
    """Fetch all keyword terms currently in Neo4j."""
    result = s.run("MATCH (k:Keyword) RETURN k.term AS term")
    return [r["term"] for r in result]

def _upsert_keyword_and_link(s, term, memory_addr):
    """
    Merge Keyword node, link to Memory with HAS_KEYWORD,
    then build SIMILAR_TO edges to existing keywords.
    """
    stem = _stem(term)

    # Upsert the Keyword node
    s.run("""
        MERGE (k:Keyword {term: $term})
        ON CREATE SET k.stem = $stem, k.freq = 1, k.created_at = $now
        ON MATCH  SET k.freq = k.freq + 1
    """, term=term, stem=stem, now=datetime.now().isoformat())

    # HAS_KEYWORD edge: Memory -> Keyword
    s.run("""
        MATCH (m:Memory  {address: $addr})
        MATCH (k:Keyword {term: $term})
        MERGE (m)-[:HAS_KEYWORD]->(k)
    """, addr=memory_addr, term=term)

    # SIMILAR_TO edges to existing keywords
    existing = _get_existing_keywords(s)
    for other in existing:
        if other == term:
            continue
        score = _similar_score(term, other)
        if score >= SIMILAR_THRESH:
            s.run("""
                MATCH (a:Keyword {term: $a})
                MATCH (b:Keyword {term: $b})
                MERGE (a)-[r:SIMILAR_TO]-(b)
                ON CREATE SET r.score = $score, r.stem_match = ($stem_a = $stem_b)
                ON MATCH  SET r.score = CASE WHEN $score > r.score THEN $score ELSE r.score END
            """, a=term, b=other, score=score,
                stem_a=stem, stem_b=_stem(other))

# ─────────────────────────────────────────────
#  PUBLIC WRITE API
# ─────────────────────────────────────────────

def write_memory(address, keywords_str, payload, color,
                 priority, src_type, src_chunk, src_line,
                 note, created_at,
                 from_idle_pass=False, cognition_depth=None):
    """
    Write a Memory node to Neo4j.
    Called by add_memory().

    Phase 7 additions (both optional, default to the non-idle case so every
    existing caller keeps working unchanged):
      from_idle_pass   True when this memory was created during a background
                       cognition pass rather than a live conversation.
      cognition_depth  "light" | "medium" | "deep" -- which idle tier produced it.
    """
    s = neo4j_session()
    if s is None:
        return
    try:
        with s:
            # Upsert Memory node
            s.run("""
                MERGE (m:Memory {address: $address})
                ON CREATE SET
                    m.payload             = $payload,
                    m.color               = $color,
                    m.priority            = $priority,
                    m.src_type            = $src_type,
                    m.src_label           = $src_label,
                    m.src_chunk           = $src_chunk,
                    m.src_line            = $src_line,
                    m.note                = $note,
                    m.created_at          = $created_at,
                    m.use                 = 0,
                    m.arc                 = 0,
                    m.false_recall_count  = 0,
                    m.last_audited        = null,
                    m.audit_notes         = '',
                    m.from_idle_pass      = $from_idle_pass,
                    m.cognition_depth     = $cognition_depth,
                    m.valence_type        = 0,
                    m.valence_intensity   = 0,
                    m.valence_rated_at    = null,
                    m.last_touched_at     = $created_at
                ON MATCH SET
                    m.color      = $color,
                    m.payload    = $payload
            """,
                address         = address,
                payload         = payload,
                color           = color,
                priority        = priority,
                src_type        = src_type,
                src_label       = SOURCE_LABELS.get(src_type, "Unknown"),
                src_chunk       = src_chunk,
                src_line        = src_line,
                note            = note or "",
                created_at      = created_at,
                from_idle_pass  = bool(from_idle_pass),
                cognition_depth = cognition_depth
            )

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
            """,
                key       = source_key,
                src_type  = src_type,
                src_label = SOURCE_LABELS.get(src_type, "Unknown"),
                src_chunk = src_chunk,
                src_line  = src_line,
                addr      = address
            )

            # Keyword nodes + HAS_KEYWORD + SIMILAR_TO edges
            kw_list = [k.strip().lower() for k in keywords_str.split(",") if k.strip()]
            for term in kw_list:
                _upsert_keyword_and_link(s, term, address)

        log.debug(f"Neo4j write OK: {address}")

    except Exception as e:
        log.warning(f"Neo4j write failed (SQLite unaffected): {e}")


def write_recall_edges(recalled_addresses, session_id):
    """
    After a recall hit:
      - RECALLED_IN edges from each Memory to the Session node
      - CO_RECALLED edges between every pair of non-pinned co-recalled memories
        (bidirectional MERGE, weight increments each time)
    Called by recall() after SQLite aging pass.
    """
    if not recalled_addresses:
        return

    driver = get_driver()
    if driver is None:
        return

    now = datetime.now().isoformat()

    try:
        # Single session, single transaction block — no double-open
        with driver.session() as s:

            # Step 1: filter Red nodes using actual Neo4j color
            # Use OPTIONAL MATCH so missing nodes don't silently drop
            non_pinned = []
            for addr in recalled_addresses:
                rec = s.run(
                    "OPTIONAL MATCH (m:Memory {address: $addr}) "
                    "RETURN m.color AS color",
                    addr=addr
                ).single()
                color = rec["color"] if rec else None
                if color != "Red":
                    # Include if Green/Yellow/Blue OR not yet in Neo4j
                    non_pinned.append(addr)

            # Step 2: upsert Session node
            s.run("""
                MERGE (sess:Session {session_id: $sid})
                ON CREATE SET sess.started_at = $now
            """, sid=session_id, now=now)

            # Step 2b: Phase 10 -- stamp last_touched_at on every recalled memory.
            # This is the single source of truth for "when was this last
            # actually used", read by BOTH the aging state machine and temporal
            # pattern detection. Written once, here, so the two cannot drift.
            s.run("""
                MATCH (m:Memory)
                WHERE m.address IN $addrs
                SET m.last_touched_at = $now
            """, addrs=list(recalled_addresses), now=now)

            # Step 3: RECALLED_IN edges for all recalled nodes
            for addr in recalled_addresses:
                s.run("""
                    OPTIONAL MATCH (m:Memory {address: $addr})
                    WITH m WHERE m IS NOT NULL
                    MATCH (sess:Session {session_id: $sid})
                    MERGE (m)-[r:RECALLED_IN]->(sess)
                    ON CREATE SET r.first_at = $now
                    ON MATCH  SET r.last_at  = $now
                """, addr=addr, sid=session_id, now=now)

            # Step 4: CO_RECALLED edges between every non-pinned pair
            pairs_written = 0
            for i, addr_a in enumerate(non_pinned):
                for addr_b in non_pinned[i+1:]:
                    result = s.run("""
                        OPTIONAL MATCH (a:Memory {address: $addr_a})
                        OPTIONAL MATCH (b:Memory {address: $addr_b})
                        WITH a, b WHERE a IS NOT NULL AND b IS NOT NULL
                        MERGE (a)-[r:CO_RECALLED]-(b)
                        ON CREATE SET r.weight = 1,    r.last_turn = $now,
                                      r.occurrences = [$now]
                        ON MATCH  SET r.weight = r.weight + 1,
                                      r.last_turn = $now,
                                      // Phase 10: capped append-only history.
                                      // A bare counter can only ever say "the
                                      // last time"; interval detection needs
                                      // the gaps BETWEEN times. Capped at 20
                                      // so hot pairs cannot grow unbounded.
                                      r.occurrences =
                                          (coalesce(r.occurrences, []) + [$now])[-20..]
                        RETURN r.weight AS w
                    """, addr_a=addr_a, addr_b=addr_b, now=now).single()
                    if result:
                        pairs_written += 1

        log.info(f"Neo4j recall edges | session={session_id[:8]} | "
                 f"{len(recalled_addresses)} recalled | "
                 f"{len(non_pinned)} non-pinned | "
                 f"{pairs_written} CO_RECALLED edges written")

    except Exception as e:
        log.warning(f"Neo4j recall edges failed (SQLite unaffected): {e}")


# ─────────────────────────────────────────────
#  PHASE 8 -- EPISODIC MEMORY / SESSION CONTINUITY
# ─────────────────────────────────────────────
#
# The Session node and RECALLED_IN edge above already existed (Phase 3), but
# session_id was minted fresh on every single /recall call -- it was per-query
# CO_RECALLED bookkeeping, not a real conversation identity. Phase 8 adds a
# SEPARATE stable session_id (minted once per MCP bridge process by
# mmu_mcp_server.py, since LM Studio spawns that process fresh per
# conversation) threaded through as the X-MMU-Session header, plus a new
# HAPPENED_IN edge distinct from RECALLED_IN:
#   RECALLED_IN -- "this memory was surfaced by a search during this session"
#   HAPPENED_IN -- "this memory was created or actively rated during this session"
# Both are meaningful and both can exist on the same memory/session pair.

def write_happened_in(address, session_id):
    """
    Phase 8: mark that a memory was created or rated during a specific
    conversation session. Called by /remember and /rate when the request
    carries an X-MMU-Session header.
    """
    driver = get_driver()
    if driver is None or not session_id:
        return
    now = datetime.now().isoformat()
    try:
        with driver.session() as s:
            s.run("""
                MERGE (sess:Session {session_id: $sid})
                ON CREATE SET sess.started_at = $now
                WITH sess
                MATCH (m:Memory {address: $addr})
                MERGE (m)-[r:HAPPENED_IN]->(sess)
                ON CREATE SET r.first_at = $now
                ON MATCH  SET r.last_at  = $now
            """, addr=address, sid=session_id, now=now)
    except Exception as e:
        log.warning(f"write_happened_in failed (address={address}, session={session_id[:8] if session_id else '?'}): {e}")


def write_session_close(session_id, summary, emotional_tone, decisions_made, source="unknown", turns_count=0):
    """
    Phase 8: close out a conversation session with a summary. Called by the
    idle daemon once /activity has been quiet past MMU_SESSION_CLOSE_SEC,
    i.e. once the conversation that owned this session_id has probably ended.

    source: "transcript" when built from the real LM Studio conversation
    file, "activity-only" as a fallback when no transcript could be found
    (e.g. a different client, or the conversations folder isn't reachable).
    """
    driver = get_driver()
    if driver is None:
        return False
    now = datetime.now().isoformat()
    try:
        with driver.session() as s:
            s.run("""
                MERGE (sess:Session {session_id: $sid})
                ON CREATE SET sess.started_at = $now
                SET sess.summary         = $summary,
                    sess.emotional_tone  = $tone,
                    sess.decisions_made  = $decisions,
                    sess.closed_at       = $now,
                    sess.source          = $source,
                    sess.turns_count     = $turns_count
            """, sid=session_id, summary=summary, tone=emotional_tone,
                 decisions=decisions_made, now=now, source=source, turns_count=turns_count)
        return True
    except Exception as e:
        log.warning(f"write_session_close failed (session={session_id[:8] if session_id else '?'}): {e}")
        return False


def get_session_resume(session_id):
    """
    Phase 8: everything needed to resume a specific prior session -- its
    summary plus every memory that HAPPENED_IN it (created/rated during it).
    """
    driver = get_driver()
    if driver is None or not session_id:
        return None
    try:
        with driver.session() as s:
            sess_rec = s.run("""
                MATCH (sess:Session {session_id: $sid})
                WHERE sess.closed_at IS NOT NULL
                RETURN sess.session_id      AS session_id,
                       sess.started_at      AS started_at,
                       sess.closed_at       AS closed_at,
                       sess.summary         AS summary,
                       sess.emotional_tone  AS emotional_tone,
                       sess.decisions_made  AS decisions_made,
                       sess.source          AS source
            """, sid=session_id).single()
            if sess_rec is None:
                return None

            memories = []
            for r in s.run("""
                MATCH (m:Memory)-[rel:HAPPENED_IN]->(sess:Session {session_id: $sid})
                RETURN m.address AS address, m.payload AS payload, m.color AS color,
                       split(m.address, '.')[2] AS grp
                ORDER BY rel.first_at
            """, sid=session_id):
                memories.append(dict(r))

            result = dict(sess_rec)
            result["memories_touched"] = memories
            return result
    except Exception as e:
        log.warning(f"get_session_resume failed (session={session_id[:8] if session_id else '?'}): {e}")
        return None


def get_latest_closed_session():
    """
    Phase 8: convenience lookup for session_bundle -- the most recently
    closed session, so a fresh conversation can open with "last time we
    talked about X" without the client needing to already know a session_id.
    """
    driver = get_driver()
    if driver is None:
        return None
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (sess:Session)
                WHERE sess.closed_at IS NOT NULL
                RETURN sess.session_id AS session_id
                ORDER BY sess.closed_at DESC
                LIMIT 1
            """).single()
            if rec is None:
                return None
    except Exception as e:
        log.warning(f"get_latest_closed_session failed: {e}")
        return None
    return get_session_resume(rec["session_id"])


def write_color_update(old_address, new_address, new_color):
    """
    When aging changes an address (USE counter increments),
    update the Memory node's address and color in Neo4j.
    For Blue transitions: also marks the node as archived.
    """
    s = neo4j_session()
    if s is None:
        return
    try:
        with s:
            if old_address == new_address:
                # Just color change
                s.run("""
                    MATCH (m:Memory {address: $addr})
                    SET m.color = $color
                """, addr=old_address, color=new_color)
            else:
                # Address changed — update and preserve edges via EVOLVES_FROM
                s.run("""
                    MATCH (old:Memory {address: $old_addr})
                    SET old.address = $new_addr,
                        old.color   = $new_color
                """, old_addr=old_address, new_addr=new_address, new_color=new_color)
    except Exception as e:
        log.warning(f"Neo4j color update failed: {e}")


def write_addr_rename(old_address, new_address):
    """
    Phase 6.5: Rename a Memory node's address without changing color or any
    other property. Used when a valence update changes only the ~VAL segment.
    """
    s = neo4j_session()
    if s is None:
        return
    try:
        with s:
            s.run("""
                MATCH (m:Memory {address: $old})
                SET m.address = $new
            """, old=old_address, new=new_address)
    except Exception as e:
        log.warning(f"Neo4j addr rename failed: {e}")


def write_valence(address, val_type, val_intensity, emotion_label=None):
    """
    Phase 6.5: Set valence properties on a Memory node.
    val_type:       0=unrated  1=like  2=dislike
    val_intensity:  0-9  (0 when unrated/cleared)
    emotion_label:  Phase 6.6 -- optional named emotion string (e.g. "Proud", "Frustrated")
    """
    s = neo4j_session()
    if s is None:
        return
    try:
        with s:
            s.run("""
                MATCH (m:Memory {address: $addr})
                SET m.valence_type          = $vt,
                    m.valence_intensity     = $vi,
                    m.valence_rated_at      = $now,
                    m.valence_emotion_label = $el
            """, addr=address, vt=val_type, vi=val_intensity,
                 now=datetime.now().isoformat(),
                 el=emotion_label)
    except Exception as e:
        log.warning(f"Neo4j write_valence failed: {e}")


def get_unrated_memories(limit=10):
    """
    Phase 6.5: Return memories with no valence rating yet (valence_type=0 or
    NULL), ordered by total CO_RECALLED weight descending so the most-connected
    memories surface first -- they are the richest candidates for a feeling.
    """
    s = neo4j_session()
    if s is None:
        return []
    try:
        with s:
            results = s.run("""
                MATCH (m:Memory)
                WHERE (m.valence_type IS NULL OR m.valence_type = 0)
                  AND m.color <> 'Blue'
                OPTIONAL MATCH (m)-[cr:CO_RECALLED]-()
                WITH m, coalesce(sum(cr.weight), 0) AS total_weight
                ORDER BY total_weight DESC
                LIMIT $lim
                RETURN m.address    AS address,
                       m.color      AS color,
                       substring(m.payload, 0, 100) AS preview,
                       m.src_type   AS src_type,
                       toInteger(split(m.address, '.')[2]) AS grp_code,
                       total_weight AS co_recall_weight
            """, lim=limit)
            return [dict(r) for r in results]
    except Exception as e:
        log.warning(f"get_unrated_memories failed: {e}")
        return []


def write_delete(address):
    """Remove a Memory node and all its relationships."""
    s = neo4j_session()
    if s is None:
        return
    try:
        with s:
            s.run("""
                MATCH (m:Memory {address: $addr})
                DETACH DELETE m
            """, addr=address)
    except Exception as e:
        log.warning(f"Neo4j delete failed: {e}")


def get_neo4j_stats():
    """Return graph stats for the /health endpoint."""
    s = neo4j_session()
    if s is None:
        return {"status": "disabled"}
    try:
        with s:
            counts = s.run("""
                MATCH (m:Memory)  WITH count(m) AS mem
                MATCH (k:Keyword) WITH mem, count(k) AS kw
                MATCH ()-[r:CO_RECALLED]-() WITH mem, kw, count(r)/2 AS co
                RETURN mem, kw, co
            """).single()
            hubs = s.run("""
                MATCH (m:Memory)-[r:CO_RECALLED]-()
                RETURN m.address AS addr, count(r) AS connections
                ORDER BY connections DESC LIMIT 3
            """).data()
            return {
                "status":       "connected",
                "memories":     counts["mem"]  if counts else 0,
                "keywords":     counts["kw"]   if counts else 0,
                "co_recalled":  counts["co"]   if counts else 0,
                "top_hubs":     hubs
            }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ═════════════════════════════════════════════
#  PHASE 2 — GRAPH READS
# ═════════════════════════════════════════════
#
# Replaces the light-index + SQLite two-stage recall with
# Cypher traversal. The win over Phase 1 is expansion:
#   - SIMILAR_TO hop finds memories whose keywords are fuzzy
#     siblings of the query terms (no Python fuzzy pass needed)
#   - CO_RECALLED hop finds memories historically recalled
#     alongside the direct hits, even with zero keyword overlap
#
# Scoring weights — tune these if recall feels too broad/narrow
W_DIRECT   = 1.00    # exact term or stem match
W_SIMILAR  = 0.85    # multiplied by the SIMILAR_TO edge score
W_CORECALL = 0.60    # multiplied by normalized CO_RECALLED weight
W_PINNED   = 1.00    # Red nodes, always included

# Blue (archived) memories only surface on a DIRECT hit —
# they stay dormant through SIMILAR_TO and CO_RECALLED expansion



def _row_to_dict(rec, score, via):
    """Normalize a Cypher record into the standard memory dict."""
    return {
        "address":   rec["address"],
        "keywords":  rec.get("keywords") or "",
        "payload":   rec["payload"],
        "color":     rec["color"],
        "src_label": rec.get("src_label") or "Unknown",
        "note":      rec.get("note") or "",
        "score":     round(score, 3),
        "via":       via,      # how this memory was found — useful for debugging
    }


def graph_recall(terms, stems, top_k=10, expand_corecall=True):
    """
    Phase 2 recall via Cypher traversal.

    terms  — set of prompt words + bigrams (lowercased)
    stems  — set of 4-char stems of those words
    top_k  — max memories to return

    Returns (results, timing_ms) or (None, 0) if Neo4j is unavailable,
    so the caller can fall back to the SQLite path.
    """
    driver = get_driver()
    if driver is None:
        return None, 0

    t0 = time.perf_counter()
    terms_l = [t.lower() for t in terms]
    stems_l = [s.lower() for s in stems]

    # addr -> (score, via) — highest score wins per address
    scored = {}
    rows   = {}

    def offer(rec, score, via):
        addr = rec["address"]
        rows[addr] = rec
        prev = scored.get(addr)
        if prev is None or score > prev[0]:
            scored[addr] = (score, via)

    try:
        with driver.session() as s:

            # ── Stage 0: Red pinned — always in context ──
            for rec in s.run("""
                MATCH (m:Memory {color: 'Red'})
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                RETURN m.address   AS address,
                       m.payload   AS payload,
                       m.color     AS color,
                       m.priority  AS priority,
                       m.src_label AS src_label,
                       m.note      AS note,
                       collect(k.term) AS kwList
            """):
                d = dict(rec)
                d["keywords"] = ",".join(d.pop("kwList") or [])
                offer(d, W_PINNED, "pinned")

            # ── Stage 0.5: stem expansion ──────────────────────
            # Find all keyword nodes sharing a stem with any prompt word.
            # Expands "working" -> ["work style","academic work"] etc.
            # so Stage 1 can traverse from those into memories.
            stem_expanded = set(terms_l)
            for rec in s.run("""
                MATCH (k:Keyword)
                WHERE k.stem IN $stems
                RETURN k.term AS term
            """, stems=stems_l):
                stem_expanded.add(rec["term"])
            terms_expanded_l = list(stem_expanded)

            # ── Stage 1: direct keyword match (Green/Yellow/Blue) ──
            # Blue CAN surface here on a direct hit — only excluded from hops
            for rec in s.run("""
                MATCH (k:Keyword)
                WHERE k.term IN $terms OR k.stem IN $stems
                MATCH (m:Memory)-[:HAS_KEYWORD]->(k)
                WHERE m.color <> 'Red'
                WITH m, count(DISTINCT k) AS kwHits
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(ak:Keyword)
                RETURN m.address   AS address,
                       m.payload   AS payload,
                       m.color     AS color,
                       m.priority  AS priority,
                       m.src_label AS src_label,
                       m.note      AS note,
                       kwHits      AS kwHits,
                       collect(ak.term) AS kwList
                ORDER BY kwHits DESC
            """, terms=terms_expanded_l, stems=stems_l):
                d = dict(rec)
                d["keywords"] = ",".join(d.pop("kwList") or [])
                hits = d.pop("kwHits", 1)
                # More matching keywords = slightly stronger, capped at 1.0
                offer(d, min(W_DIRECT, 0.80 + 0.05 * hits), "direct")

            # Seeds for CO_RECALLED expansion = the direct hits so far
            seeds = [a for a, (sc, via) in scored.items() if via == "direct"]

            # ── Stage 2: SIMILAR_TO one-hop (Blue excluded) ──
            # Uses WHERE r.score >= threshold to prune weak edges early
            # and LIMIT per memory to avoid combinatorial explosion
            for rec in s.run("""
                MATCH (k:Keyword)
                WHERE k.term IN $terms OR k.stem IN $stems
                WITH collect(k) AS seedKws
                UNWIND seedKws AS k
                MATCH (k)-[sim:SIMILAR_TO]-(k2:Keyword)
                WHERE sim.score >= $sim_thresh
                WITH k2, max(sim.score) AS simScore
                MATCH (m:Memory)-[:HAS_KEYWORD]->(k2)
                WHERE m.color IN ['Green','Yellow']
                WITH m, max(simScore) AS simScore
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(ak:Keyword)
                RETURN m.address   AS address,
                       m.payload   AS payload,
                       m.color     AS color,
                       m.priority  AS priority,
                       m.src_label AS src_label,
                       m.note      AS note,
                       simScore    AS simScore,
                       collect(ak.term) AS kwList
                LIMIT 20
            """, terms=terms_expanded_l, stems=stems_l, sim_thresh=SIMILAR_THRESH):
                d = dict(rec)
                d["keywords"] = ",".join(d.pop("kwList") or [])
                sim = d.pop("simScore", 0.75) or 0.75
                offer(d, W_SIMILAR * sim, "similar")

            # ── Stage 3: CO_RECALLED expansion (Blue excluded) ──
            if expand_corecall and seeds:
                for rec in s.run("""
                    MATCH (seed:Memory) WHERE seed.address IN $seeds
                    MATCH (seed)-[r:CO_RECALLED]-(m:Memory)
                    WHERE NOT m.address IN $seeds
                      AND m.color <> 'Red' AND m.color <> 'Blue'
                    WITH m, max(r.weight) AS w
                    OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(ak:Keyword)
                    RETURN m.address   AS address,
                           m.payload   AS payload,
                           m.color     AS color,
                           m.priority  AS priority,
                           m.src_label AS src_label,
                           m.note      AS note,
                           w           AS weight,
                           collect(ak.term) AS kwList
                    ORDER BY w DESC
                    LIMIT 25
                """, seeds=seeds):
                    d = dict(rec)
                    d["keywords"] = ",".join(d.pop("kwList") or [])
                    w = d.pop("weight", 1) or 1
                    # Normalize: weight 1 -> 0.3, weight 5+ -> full W_CORECALL
                    norm = min(1.0, 0.3 + 0.14 * (w - 1))
                    offer(d, W_CORECALL * norm, "co_recalled")

    except Exception as e:
        log.warning(f"Neo4j graph_recall failed, caller should fall back: {e}")
        return None, 0

    # ── Stage 5: merge, rank, cut ──
    merged = []
    for addr, (score, via) in scored.items():
        merged.append(_row_to_dict(rows[addr], score, via))

    merged.sort(key=lambda d: (-d["score"], rows[d["address"]].get("priority", 9)))
    merged = merged[:top_k]

    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info(f"graph_recall | {len(scored)} candidates -> {len(merged)} returned "
             f"| {elapsed_ms:.1f}ms")
    return merged, elapsed_ms


def graph_neighbors(address, limit=5):
    """
    Return the memories most strongly co-recalled with a given address.
    Powers a new /neighbors endpoint — 'what else comes to mind with this?'
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (m:Memory {address: $addr})-[r:CO_RECALLED]-(n:Memory)
                RETURN n.address AS address,
                       n.payload AS payload,
                       n.color   AS color,
                       r.weight  AS weight
                ORDER BY r.weight DESC
                LIMIT $limit
            """, addr=address, limit=limit)]
    except Exception as e:
        log.warning(f"graph_neighbors failed: {e}")
        return []


def get_next_con():
    """
    The next free CON (address identity) number.

    max(existing) + 1, not count() + 1. The count is wrong the moment anything
    is deleted: delete 5 of 100 and count()+1 hands back 96, which is already
    taken, violating the memory_addr UNIQUE constraint or silently colliding.

    Returns 1 on an empty graph or if Neo4j is unavailable -- the caller is
    creating a memory either way, and a low CON is recoverable while a crash is
    not.
    """
    driver = get_driver()
    if driver is None:
        return 1
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (m:Memory)
                RETURN max(toInteger(split(m.address, '.')[0])) AS max_con
            """).single()
            return int((rec["max_con"] or 0)) + 1 if rec else 1
    except Exception as e:
        log.warning(f"get_next_con failed, falling back to count: {e}")
        return get_memory_count() + 1


def delete_all_memories():
    """
    Remove every Memory node and everything attached to it.

    Keeps Routine nodes: they are a separate abstraction and erasing memories
    should not silently destroy crystallized routines. Callers wanting a truly
    blank slate can drop the Docker volume.

    Returns the number of Memory nodes removed.
    """
    driver = get_driver()
    if driver is None:
        return 0
    try:
        with driver.session() as s:
            n = s.run("MATCH (m:Memory) RETURN count(m) AS n").single()["n"]
            # Batched so a large graph does not build one enormous transaction.
            while True:
                got = s.run("""
                    MATCH (m:Memory) WITH m LIMIT 1000
                    DETACH DELETE m
                    RETURN count(*) AS removed
                """).single()["removed"]
                if not got:
                    break
            # Keyword and Session nodes left orphaned by that are noise.
            s.run("MATCH (k:Keyword) WHERE NOT (k)<-[:HAS_KEYWORD]-() DETACH DELETE k")
            s.run("MATCH (sess:Session) WHERE NOT (sess)<-[]-() DETACH DELETE sess")
            return n
    except Exception as e:
        log.warning(f"delete_all_memories failed: {e}")
        return 0


def vector_index_info():
    """
    Describe the vector index, for the startup self-check.
    Returns a short string, or None when the index is absent.
    """
    driver = get_driver()
    if driver is None:
        return None
    try:
        with driver.session() as s:
            rec = s.run("""
                SHOW INDEXES YIELD name, type, state, options
                WHERE name = $n
                RETURN state, options
            """, n=EMBEDDING_INDEX).single()
            if not rec:
                return None
            opts = rec["options"] or {}
            cfg = (opts.get("indexConfig") or {}) if isinstance(opts, dict) else {}
            dim = cfg.get("vector.dimensions")
            sim = cfg.get("vector.similarity_function")
            return f"{EMBEDDING_INDEX} {rec['state']}, dim={dim}, {sim}"
    except Exception as e:
        log.debug(f"vector_index_info failed: {e}")
        return None


def get_index_source_rows():
    """
    Every memory as the v2 index needs it: address, colour, priority, src_type
    and keyword terms.

    Same projection rebuild_from_neo4j() uses, exposed separately so drift can
    be repaired incrementally. A full rebuild also discards the shortcut cache
    and resets the generation counter, which is a heavy price for reinstating
    one card.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (m:Memory)
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                RETURN m.address  AS address,
                       m.color    AS color,
                       m.priority AS priority,
                       m.src_type AS src_type,
                       collect(DISTINCT k.term) AS keywords
            """)]
    except Exception as e:
        log.warning(f"get_index_source_rows failed: {e}")
        return []


def get_memory_count():
    """Return total Memory node count for CON address segment generation."""
    driver = get_driver()
    if driver is None:
        return 0
    try:
        with driver.session() as s:
            result = s.run("MATCH (m:Memory) RETURN count(m) AS c").single()
            return result["c"] if result else 0
    except Exception as e:
        log.warning(f"get_memory_count failed: {e}")
        return 0


def fetch_all_memories():
    """
    Return all Memory nodes ordered by color then address.
    Replaces the SQLite SELECT * FROM memories for the /memories endpoint.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            result = s.run("""
                MATCH (m:Memory)
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                RETURN m.address    AS address,
                       m.payload    AS payload,
                       m.color      AS color,
                       m.src_type   AS src_type,
                       m.src_label  AS src_label,
                       m.created_at AS created_at,
                       m.note       AS note,
                       collect(k.term) AS kwList
                ORDER BY m.color, m.address
            """)
            rows = []
            for rec in result:
                d = dict(rec)
                d["keywords"] = ",".join(d.pop("kwList") or [])
                rows.append(d)
            return rows
    except Exception as e:
        log.warning(f"fetch_all_memories failed: {e}")
        return []


def get_session_bundle(top_per_domain=1):
    """
    Return the top warm card per GRP domain (1xx–8xx).
    Yellow > Green > Red by recency; priority breaks ties.
    Used by the /session_bundle endpoint to pre-inject context before the LLM reasons.
    """
    driver = get_driver()
    if driver is None:
        return {}

    domains = {}
    try:
        with driver.session() as s:
            for domain_prefix in range(1, 9):
                grp_start = domain_prefix * 100
                grp_end   = grp_start + 99
                result = s.run("""
                    MATCH (m:Memory)
                    WHERE m.color IN ['Red', 'Green', 'Yellow']
                    WITH m, toInteger(split(m.address, '.')[2]) AS grp
                    WHERE grp >= $start AND grp <= $end
                    OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                    WITH m, grp, collect(k.term) AS kws
                    ORDER BY
                        CASE m.color
                            WHEN 'Yellow' THEN 0
                            WHEN 'Green'  THEN 1
                            ELSE               2
                        END ASC,
                        m.priority ASC
                    LIMIT $limit
                    RETURN m.address   AS address,
                           m.payload   AS payload,
                           m.color     AS color,
                           m.priority  AS priority,
                           grp         AS grp,
                           kws         AS keywords
                """, start=grp_start, end=grp_end, limit=top_per_domain)

                mems = []
                for rec in result:
                    d = dict(rec)
                    d["keywords"] = ",".join(d.pop("keywords") or [])
                    mems.append(d)

                if mems:
                    domains[f"{domain_prefix}xx"] = mems
    except Exception as e:
        log.warning(f"get_session_bundle failed: {e}")

    return domains


def fetch_payloads(addresses):
    """
    Fetch full Memory node data for a list of addresses directly from Neo4j.
    Used by _v2_recall() in mmu_server.py for payload hydration — replaces
    the SQLite lookup so GRP-renamed addresses always resolve correctly.
    SQLite addresses became stale after the GRP taxonomy backfill; Neo4j
    is the single source of truth for current addresses.

    Returns list of dicts: address, payload, color, src_type, src_label,
    note, keywords (comma-joined string).
    Returns [] if Neo4j is unavailable or addresses is empty.
    """
    driver = get_driver()
    if driver is None or not addresses:
        return []
    try:
        with driver.session() as s:
            result = s.run("""
                MATCH (m:Memory)
                WHERE m.address IN $addresses
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                RETURN m.address   AS address,
                       m.payload   AS payload,
                       m.color     AS color,
                       m.src_type  AS src_type,
                       m.src_label AS src_label,
                       m.note      AS note,
                       collect(k.term) AS kwList
            """, addresses=list(addresses))
            rows = []
            for rec in result:
                d = dict(rec)
                d["keywords"] = ",".join(d.pop("kwList") or [])
                rows.append(d)
            return rows
    except Exception as e:
        log.warning(f"fetch_payloads failed: {e}")
        return []


# ═════════════════════════════════════════════
#  PHASE 12 — PROCEDURAL MEMORY CRYSTALLIZATION
# ═════════════════════════════════════════════
#
# Memories that cluster densely and cohere by domain compress into a Routine,
# mirroring declarative memory becoming procedural. Source memories are never
# deleted -- they are demoted to Blue and become the routine's root system.
#
# THREE-STEP BY DESIGN, and the split is structural rather than conventional:
#
#   find_routine_candidates()  reads. Writes nothing. Safe to poll.
#   queue_routine_proposals()  writes RoutineProposal nodes only. Never touches a
#                            Memory node. Safe for the idle daemon.
#   crystallize_routine()      writes, atomically, and only on explicit human
#                            confirmation.
#
# This is the one phase that restructures Nova's own memory rather than adding
# capability beside it. Getting it wrong is not a bug, it is an identity-model
# change nobody approved. crystallize_routine() is deliberately NOT reachable
# from mmu_idle_daemon.py's IDLE_TOOLS.
#
# ── Phase 13.1: two bugs that made crystallization unreachable ──
#
# Symptom: 999 memories, 724 co-recall edges, zero Routine nodes ever formed.
#
# 1. DOCUMENTS WERE EXCLUDED. This function carried `src_type <> 2`, copied
#    from get_anticipated_context() where it is correct -- a paragraph of a
#    physics paper is not a thing to be proactively *reminded* of, and that
#    filter stays exactly where it is. The reasoning does not survive
#    the trip. Crystallization is the opposite operation, and compressing a
#    large reference corpus into one procedural node is exactly what a graph
#    of 861 documents needs. The filter made 86% of memories permanently
#    ineligible, so the densest region of the graph could never form a routine
#    and stayed as hundreds of flat memories competing in every recall. That
#    is a retrieval bias with a structural cause, not a tuning problem.
#
# 2. WEIGHTS WERE NORMALIZED AGAINST THE GRAPH MAXIMUM. That maximum is one
#    hot edge, and it lives in whichever source class is recalled most often
#    (conversation, observed at weight 34, against a document-to-document
#    maximum of 9). Every population was being measured with a yardstick
#    borrowed from another one. Worse, it was anti-scaling: each recall of the
#    hottest pair raised the bar for every other cluster, so the system grew
#    *less* able to crystallize the more it was used.
#
# The fix for (2) is a high percentile instead of the max -- it describes the
# top of the real distribution, barely moves when one pair gets hammered, and
# stays stable as the graph grows -- with an absolute floor underneath it so a
# young graph still cannot manufacture a candidate out of noise.

# Reference point for weight normalization. Deliberately not max().
ROUTINE_NORM_PERCENTILE = 0.90

# Absolute floor, applied under the normalized one. On a young or sparse graph
# p90 can itself be 1-2, and 0.6 * that would admit noise. A pair that has not
# been co-recalled at least this often is not evidence of anything, whatever
# the rest of the distribution happens to look like.
ROUTINE_MIN_ABS_WEIGHT = 3.0


def routine_weight_floor(min_pairwise_norm=0.6):
    """
    The co-recall weight a pair must clear to count toward a routine cluster.

    Returns (floor, reference_weight, basis) where reference_weight is the
    percentile the floor came from and basis names which of the two rules
    actually bound. Callers report these rather than a bare number, so a
    "no candidates" answer can be read as evidence instead of a shrug.
    """
    driver = get_driver()
    if driver is None:
        return ROUTINE_MIN_ABS_WEIGHT, 0.0, "absolute"
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH ()-[r:CO_RECALLED]-()
                RETURN percentileCont(r.weight, $p) AS ref
            """, p=float(ROUTINE_NORM_PERCENTILE)).single()
            ref = float((rec and rec["ref"]) or 0.0)
    except Exception as e:
        log.warning(f"routine_weight_floor failed, using absolute floor: {e}")
        return ROUTINE_MIN_ABS_WEIGHT, 0.0, "absolute"

    scaled = float(min_pairwise_norm) * ref
    if scaled >= ROUTINE_MIN_ABS_WEIGHT:
        return scaled, ref, "percentile"
    return ROUTINE_MIN_ABS_WEIGHT, ref, "absolute"


def find_routine_candidates(min_cluster=3, min_pairwise_norm=0.6, limit=10,
                          max_per_domain=2, max_member_reuse=2):
    """
    Clusters where EVERY pair is densely co-recalled -- not merely anchored on
    one popular node.

    The previews in get_insights() and get_idle_context() used to answer a
    different question ("which single memory has strong neighbours"), which is
    the right shape for a preview and the wrong shape for deciding to
    crystallize: a hub with many weak-to-each-other neighbours would qualify
    while not being a coherent routine at all. Both previews now call this
    function, so the three code paths cannot drift apart again -- they had,
    and /insights was advertising candidates this function could never return.

    min_pairwise_norm is on a 0-1 scale against the p90 co-recall weight, not
    the maximum. See the block comment above for why that distinction is the
    difference between a system that can crystallize and one that cannot.

    Documents are eligible. A cluster of reference chunks that are always
    recalled together is the clearest case for compression there is; every
    candidate reports src_mix so a reviewer can see what it is made of.

    RANKING IS DONE IN THE QUERY, on the same score the caller sees. It used to
    ORDER BY raw avg_weight and LIMIT before Python scored anything, which is
    the max-normalization bug one layer down: raw weight is dominated by
    whichever source class is recalled most, so the hottest corner of the graph
    filled every slot and no document cluster ever reached the scoring step.

    THREE SIGNALS, because the first two can both be satisfied by a cluster
    that means nothing:

      avg_weight_norm     they are recalled together (structural evidence)
      grp_coherence       they share a GRP domain (filing evidence)
      semantic_coherence  they are actually about the same thing

    The third was added after a real review: a candidate scored grp_coherence
    1.0 on nothing but arithmetic -- game design, an assistant's gender
    identity, and a user's self-description all happen to be filed under 5xx.
    GRP agreement is a statement about where things were filed, not about what
    they say. Cosine over the embeddings that already exist on every node
    answers the question the GRP code was being asked to stand in for.

    Neo4j normalizes cosine to [0,1] with 0.5 = orthogonal, so it is rescaled
    to a real 0-1 here. When any member lacks an embedding, semantic_coherence
    is None and the score falls back to the two structural signals, with
    scored_without_embeddings set so that is visible rather than silent.

    Two caps shape the returned set, because a review queue is an attention
    budget:

      max_per_domain    one GRP domain may not own the queue. A graph that is
                        87% one subject would otherwise propose only that
                        subject forever -- the retrieval bias reproducing
                        itself in the review list.
      max_member_reuse  one memory may not appear in more than N proposals.
                        Overlapping triangles drawn from the same four hot
                        memories filled 4 of 10 slots with what a reviewer
                        reads as the same finding four times.

    Neither cap costs coverage: if capping would return fewer than `limit`,
    the leftovers are filled back in by score order. Set either to 0 to
    disable. Nothing is ever excluded outright -- this phase exists because a
    silent structural exclusion went unnoticed for two phases.

    Triangles are the unit: a mutually-dense triple is the smallest cluster
    that can be evidence of anything. min_cluster above 3 therefore returns
    nothing rather than a padded triangle -- growing a cluster past a triple
    is not implemented, and quietly returning three members for a request of
    five would be a lie about what was found.

    Returns [] when nothing qualifies. On a young graph that is the expected
    answer, not a failure -- do not lower the threshold to manufacture one.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        floor, ref_w, _basis = routine_weight_floor(min_pairwise_norm)
        if ref_w <= 0:
            return []

        # Sample generously, then apply the caps in Python. The query already
        # sorts by score, so this is headroom for the caps rather than a
        # second ranking pass.
        sample = max(int(limit) * 20, 200)

        with driver.session() as s:
            rows = s.run("""
                MATCH (a:Memory)-[r1:CO_RECALLED]-(b:Memory)-[r2:CO_RECALLED]-(c:Memory)
                MATCH (a)-[r3:CO_RECALLED]-(c)
                WHERE a.address < b.address AND b.address < c.address
                  AND r1.weight >= $floor AND r2.weight >= $floor AND r3.weight >= $floor
                  AND a.color <> 'Blue' AND b.color <> 'Blue' AND c.color <> 'Blue'
                WITH a, b, c,
                     (r1.weight + r2.weight + r3.weight) / 3.0        AS avg_weight,
                     toInteger(split(a.address, '.')[2])              AS ga,
                     toInteger(split(b.address, '.')[2])              AS gb,
                     toInteger(split(c.address, '.')[2])              AS gc
                // Mean pairwise cosine. Null if any member lacks an embedding,
                // which the caller reports rather than papering over.
                WITH a, b, c, avg_weight, ga, gb, gc,
                     (vector.similarity.cosine(a.embedding, b.embedding) +
                      vector.similarity.cosine(b.embedding, c.embedding) +
                      vector.similarity.cosine(a.embedding, c.embedding)) / 3.0 AS sim_raw
                // Whole-cluster agreement on a GRP domain. For a triple this is
                // exactly max(domain count) / 3.
                WITH a, b, c, avg_weight, ga, gb, gc, sim_raw,
                     CASE WHEN ga / 100 = gb / 100 AND gb / 100 = gc / 100 THEN 1.0
                          WHEN ga / 100 = gb / 100
                            OR gb / 100 = gc / 100
                            OR ga / 100 = gc / 100 THEN 0.6667
                          ELSE 0.3333 END                             AS coherence,
                     // Clamped: a pair far above p90 is not "better than
                     // perfect", and letting it exceed 1.0 is what made the
                     // documented ">= 0.70 proposes" threshold meaningless.
                     CASE WHEN avg_weight / $ref > 1.0 THEN 1.0
                          ELSE avg_weight / $ref END                  AS avg_norm
                // Rescale off Neo4j's 0.5-is-orthogonal convention.
                WITH a, b, c, avg_weight, ga, gb, gc, coherence, avg_norm,
                     CASE WHEN sim_raw IS NULL THEN null
                          WHEN (sim_raw - 0.5) * 2 < 0 THEN 0.0
                          WHEN (sim_raw - 0.5) * 2 > 1 THEN 1.0
                          ELSE (sim_raw - 0.5) * 2 END                AS semantic
                RETURN [a.address, b.address, c.address]              AS members,
                       // Stable identity. Addresses encode the use and arc
                       // counters and are rewritten in place as a memory is
                       // recalled, so they cannot key anything that outlives
                       // the moment. created_at is written once.
                       [a.created_at, b.created_at, c.created_at]     AS member_created,
                       avg_weight,
                       avg_norm,
                       coherence,
                       semantic,
                       CASE WHEN semantic IS NULL
                            THEN (avg_norm * 0.5) + (coherence * 0.5)
                            ELSE (avg_norm * 0.4) + (coherence * 0.3)
                               + (semantic * 0.3) END                 AS routine_score,
                       [a.payload, b.payload, c.payload]              AS payloads,
                       [a.src_type, b.src_type, c.src_type]           AS src_types,
                       [ga, gb, gc]                                   AS grps
                ORDER BY routine_score DESC, avg_weight DESC
                LIMIT $lim
            """, floor=floor, ref=float(ref_w), lim=sample)

            scored, seen = [], set()
            for r in rows:
                members = list(r["members"])
                if len(members) < int(min_cluster):
                    continue
                key = tuple(sorted(members))
                if key in seen:
                    continue
                seen.add(key)

                grps = [g for g in (r["grps"] or []) if g is not None]
                domains = [g // 100 for g in grps]
                # The cluster's own domain, for the cap: whichever domain most
                # of its members belong to.
                dom = max(set(domains), key=domains.count) if domains else None

                src_mix = {}
                for t in (r["src_types"] or []):
                    if t is None:
                        continue
                    label = SOURCE_LABELS.get(t, f"Type-{t}")
                    src_mix[label] = src_mix.get(label, 0) + 1

                sem = r["semantic"]
                scored.append((dom, members, {
                    "members":            members,
                    "member_created":     [t for t in (r["member_created"] or []) if t],
                    "avg_weight":         round(float(r["avg_weight"] or 0.0), 3),
                    "avg_weight_norm":    round(float(r["avg_norm"] or 0.0), 4),
                    "grp_coherence":      round(float(r["coherence"] or 0.0), 3),
                    "semantic_coherence": (round(float(sem), 3) if sem is not None else None),
                    "scored_without_embeddings": sem is None,
                    "routine_score":        round(float(r["routine_score"] or 0.0), 4),
                    "grps":               grps,
                    "domain":             dom,
                    "src_mix":            src_mix,
                    "previews":           [(p or "")[:90] for p in (r["payloads"] or [])],
                }))

        limit = int(limit)
        out, used_dom, used_mem, overflow = [], {}, {}, []
        for dom, members, cand in scored:          # already in score order
            dom_ok = (not max_per_domain) or used_dom.get(dom, 0) < int(max_per_domain)
            mem_ok = (not max_member_reuse) or all(
                used_mem.get(m, 0) < int(max_member_reuse) for m in members
            )
            if dom_ok and mem_ok:
                out.append(cand)
                used_dom[dom] = used_dom.get(dom, 0) + 1
                for m in members:
                    used_mem[m] = used_mem.get(m, 0) + 1
            else:
                overflow.append(cand)
            if len(out) >= limit:
                break

        # The caps trim breadth, never depth.
        if len(out) < limit:
            out.extend(overflow[:limit - len(out)])
        return out
    except Exception as e:
        log.warning(f"find_routine_candidates failed: {e}")
        return []


def crystallize_routine(member_addresses, trigger, procedure, confidence=0.0):
    """
    The confirm step. ONE transaction, all-or-nothing.

    Creates the Routine, wires PROCEDURALIZED_FROM from every member, and demotes
    each member to Blue. Members are never deleted -- per the roadmap they are
    the routine's root system, and a memory that has been compressed is still the
    evidence the compression was drawn from.

    Returns (routine_dict, None) on success, or (None, reason) on failure with
    nothing half-applied.

    The reason is returned rather than logged and swallowed. This is the one
    path in the system that only a human ever walks, and its most likely
    failure is the least guessable: MMU addresses encode the use and arc
    counters and are rewritten on recall, so a member address read from the
    queue five minutes ago may already match nothing. "Check the server log"
    is not an acceptable answer to that when the code knows exactly which
    addresses went missing.
    """
    driver = get_driver()
    if driver is None:
        return None, "no database connection"
    if not member_addresses:
        return None, "no member addresses given"

    routine_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    try:
        with driver.session() as s:
            # execute_write gives a real transaction: a failure part-way rolls
            # back rather than leaving memories demoted with no Routine to show
            # for it.
            def _tx(tx):
                live = [r["a"] for r in tx.run("""
                    MATCH (m:Memory) WHERE m.address IN $addrs
                    RETURN m.address AS a
                """, addrs=list(member_addresses))]
                if len(live) != len(member_addresses):
                    missing = [a for a in member_addresses if a not in live]
                    raise ValueError(
                        f"{len(missing)} of {len(member_addresses)} member "
                        f"addresses matched no memory: {', '.join(missing)}. "
                        "MMU addresses are rewritten in place when a memory is "
                        "recalled, so a stale address is the usual cause -- "
                        "re-read GET /routine_proposals and use the addresses it "
                        "returns now, or confirm by proposal_id instead."
                    )

                # A memory already compressed into an active Routine cannot be
                # compressed into a second one. Colour is single-valued, so two
                # routines claiming the same member disagree about what it should
                # be the moment either is undone -- which is exactly how this
                # was found: a stale proposal was confirmed twice, and undoing
                # the second restored a member the first still owned.
                taken = [r["a"] for r in tx.run("""
                    MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk:Routine)
                    WHERE m.address IN $addrs AND sk.status <> 'deprecated'
                    RETURN DISTINCT m.address AS a
                """, addrs=list(member_addresses))]
                if taken:
                    raise ValueError(
                        f"{len(taken)} member(s) already belong to an active "
                        f"routine: {', '.join(taken)}. This will fail identically "
                        "every time until that changes -- retrying is not a "
                        "path forward. Either uncrystallize the routine that owns "
                        "them, or pick a different proposal; the review queue "
                        "marks which proposals are blocked and lists the "
                        "unblocked ones first."
                    )

                tx.run("""
                    CREATE (sk:Routine {
                        routine_id: $sid, trigger: $trigger, procedure: $procedure,
                        confidence: $conf, invocation_count: 0, last_invoked: null,
                        created_at: $now, status: 'active'
                    })
                """, sid=routine_id, trigger=trigger, procedure=procedure,
                     conf=float(confidence), now=now)

                tx.run("""
                    MATCH (sk:Routine {routine_id: $sid})
                    MATCH (m:Memory) WHERE m.address IN $addrs
                    MERGE (m)-[:PROCEDURALIZED_FROM]->(sk)
                    // Remember what this was before demoting it. Members here
                    // are a mix of colours -- Green and Yellow in the first
                    // real crystallization -- so an undo that assumed one
                    // would corrupt the aging state of the rest. coalesce
                    // keeps the ORIGINAL colour if this memory was somehow
                    // demoted once before.
                    SET m.pre_routine_color = coalesce(m.pre_routine_color, m.color),
                        m.color           = 'Blue'
                """, sid=routine_id, addrs=list(member_addresses))

                # Clear any proposal marker for these members.
                tx.run("""
                    MATCH (m:Memory) WHERE m.address IN $addrs
                    REMOVE m.routine_candidate_id
                """, addrs=list(member_addresses))
                return True

            s.execute_write(_tx)

        log.info("Crystallized routine %s from %d memories", routine_id, len(member_addresses))
        return {
            "routine_id": routine_id, "trigger": trigger, "procedure": procedure,
            "confidence": float(confidence), "status": "active",
            "created_at": now, "members": list(member_addresses),
        }, None
    except Exception as e:
        log.warning(f"crystallize_routine failed (nothing applied): {e}")
        return None, str(e)


def uncrystallize_routine(routine_id):
    """
    Reverse a crystallization: delete the Routine and restore its members.

    Returns (info, None) on success or (None, reason) on failure, with nothing
    half-applied.

    This exists because crystallization is the one operation in the system that
    restructures memory rather than adding to it, and it was the one operation
    with no way back. deprecate_routine() marks a Routine dead but leaves every
    member sitting in Blue, which is the state the recall gate treats as
    inactive -- so a routine judged wrong afterwards left its evidence buried.

    Refuses while another routine extends this one, for the same reason
    deprecate_routine() does: a live child pointing at a deleted parent is a
    broken tree, and silently orphaning it would be worse than refusing.

    Members are restored to pre_routine_color. A member crystallized before that
    property existed has no recorded colour and is restored to Green, reported
    in colors_guessed so the caller can say so rather than imply precision it
    does not have.
    """
    driver = get_driver()
    if driver is None:
        return None, "no database connection"
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (sk:Routine {routine_id: $sid}) RETURN sk.trigger AS trigger
            """, sid=routine_id).single()
            if not rec:
                return None, "no such routine"

            blockers = [r["cid"] for r in s.run("""
                MATCH (child:Routine)-[:EXTENDS_ROUTINE]->(sk:Routine {routine_id: $sid})
                WHERE child.status <> 'deprecated'
                RETURN child.routine_id AS cid
            """, sid=routine_id)]
            if blockers:
                return None, (f"{len(blockers)} active child routine(s) still extend "
                              f"this one: {', '.join(blockers)}")

            def _tx(tx):
                # Only members that this routine alone owns get restored. One
                # still compressed into another active routine stays Blue and
                # keeps its recorded colour -- restoring it would contradict
                # the routine that still claims it.
                rows = [dict(r) for r in tx.run("""
                    MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk:Routine {routine_id: $sid})
                    OPTIONAL MATCH (m)-[:PROCEDURALIZED_FROM]->(other:Routine)
                    WHERE other.routine_id <> $sid AND other.status <> 'deprecated'
                    WITH m, count(other) AS others
                    RETURN m.address AS address,
                           m.pre_routine_color AS pre,
                           m.pre_routine_color IS NULL AS guessed,
                           others > 0 AS still_owned
                """, sid=routine_id)]

                keep = [r["address"] for r in rows if r["still_owned"]]
                restore = [r["address"] for r in rows if not r["still_owned"]]

                if restore:
                    tx.run("""
                        MATCH (m:Memory) WHERE m.address IN $addrs
                        SET m.color = coalesce(m.pre_routine_color, 'Green')
                        REMOVE m.pre_routine_color
                    """, addrs=restore)

                tx.run("""
                    MATCH (m:Memory)-[r:PROCEDURALIZED_FROM]->(sk:Routine {routine_id: $sid})
                    DELETE r
                """, sid=routine_id)

                rows = [dict(r, kept=(r["address"] in keep)) for r in rows]

                # Reopen the proposal so the cluster returns to review rather
                # than vanishing: undoing a crystallization is a statement that
                # the routine was wrong, not that the pattern was imaginary.
                tx.run("""
                    MATCH (p:RoutineProposal {routine_id: $sid})
                    SET p.status      = 'pending',
                        p.routine_id    = null,
                        p.reviewed_at = null
                """, sid=routine_id)

                tx.run("MATCH (sk:Routine {routine_id: $sid}) DETACH DELETE sk", sid=routine_id)
                return rows

            restored = s.execute_write(_tx)

        log.info("Uncrystallized routine %s; restored %d memories", routine_id, len(restored))
        return {
            "routine_id": routine_id,
            "trigger":  rec["trigger"],
            "restored": [{"address": r["address"], "color": r["pre"] or "Green"}
                         for r in restored if not r["kept"]],
            # Left demoted on purpose: another active routine still owns these.
            "still_demoted": [r["address"] for r in restored if r["kept"]],
            "colors_guessed": [r["address"] for r in restored
                               if r["guessed"] and not r["kept"]],
        }, None
    except Exception as e:
        log.warning(f"uncrystallize_routine failed (nothing applied): {e}")
        return None, str(e)


# ═════════════════════════════════════════════
#  PHASE 13.2 — ROUTINE DELIVERY
# ═════════════════════════════════════════════
#
# Crystallization was write-only. A Routine node carried trigger and procedure
# and nothing else: no keywords, no embedding, no edge into the Keyword graph.
# Every retrieval path in this system reaches a memory through keywords or
# through the vector index, so a Routine was unreachable by all of them -- and
# invocation_count, written as 0 at creation, was never incremented because
# nothing ever invoked anything.
#
# Measured before this existed: crystallizing three memories changed recall by
# zero bytes. The 636-character routine was 11% of its 5,377 characters of source
# and was never delivered in place of them, so the compression was real on
# paper and absent in practice.
#
# The model noticed before the code did. Asked about routines, it saved an
# ordinary Memory titled "ROUTINE NODE: ..." with a trigger phrase and a
# provenance footer -- reimplementing the mechanism at the only layer that was
# actually retrievable.


def write_routine_embedding(routine_id, vector):
    """Attach an embedding to a Routine, in Neo4j's native vector encoding."""
    driver = get_driver()
    if driver is None or not vector:
        return False
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (sk:Routine {routine_id: $sid})
                CALL db.create.setNodeVectorProperty(sk, 'embedding', $vector)
                RETURN count(sk) AS n
            """, sid=routine_id, vector=[float(x) for x in vector]).single()
            return bool(rec and rec["n"])
    except Exception as e:
        log.warning(f"write_routine_embedding failed: {e}")
        return False


def link_routine_keywords(routine_id, terms):
    """
    Link a Routine into the same Keyword graph memories use.

    Deliberately the existing HAS_KEYWORD edge and the existing Keyword nodes
    rather than a parallel vocabulary: a routine about dimensional relativity and
    a memory about dimensional relativity should match the same term, or the
    keyword gate would need to learn about routines as a special case.
    """
    driver = get_driver()
    if driver is None or not terms:
        return 0
    try:
        n = 0
        with driver.session() as s:
            for term in terms:
                term = (term or "").strip().lower()
                if not term:
                    continue
                s.run("""
                    MERGE (k:Keyword {term: $term})
                    ON CREATE SET k.freq = 0, k.stem = $term
                    WITH k
                    MATCH (sk:Routine {routine_id: $sid})
                    MERGE (sk)-[:HAS_KEYWORD]->(k)
                """, term=term, sid=routine_id)
                n += 1
        return n
    except Exception as e:
        log.warning(f"link_routine_keywords failed: {e}")
        return 0


def match_routines(query_vector=None, terms=None, limit=3, min_semantic=0.75):
    """
    Find active routines relevant to a query, by embedding and by keyword.

    Returns [{routine_id, trigger, procedure, confidence, members, score, via}]
    where via is "semantic" or "keyword", so a caller can report WHY a routine
    surfaced -- the same transparency rule that puts `via` on every memory row.

    Deprecated routines are never matched. A routine with no embedding falls back
    to the keyword path rather than being invisible, which is what keeps a
    routine crystallized before this phase from silently disappearing.

    min_semantic is deliberately high. A routine substitutes for its source
    memories in the delivered context, so a loose match does not merely add
    noise, it withholds the memories the caller would otherwise have seen.
    """
    driver = get_driver()
    if driver is None:
        return []

    terms = [t.strip().lower() for t in (terms or []) if (t or "").strip()]
    found = {}

    try:
        with driver.session() as s:
            if query_vector:
                try:
                    for r in s.run("""
                        CALL db.index.vector.queryNodes($index, $k, $vector)
                        YIELD node, score
                        WHERE node.status = 'active' AND score >= $floor
                        RETURN node.routine_id AS sid, score AS score
                    """, index=ROUTINE_EMBEDDING_INDEX, k=max(int(limit) * 3, 10),
                         vector=[float(x) for x in query_vector],
                         floor=float(min_semantic)):
                        found[r["sid"]] = (float(r["score"]), "semantic")
                except Exception as e:
                    # No vector index (older Neo4j), or no embedded routines yet.
                    log.debug("routine vector match unavailable: %s", e)

            if terms:
                for r in s.run("""
                    MATCH (sk:Routine)-[:HAS_KEYWORD]->(k:Keyword)
                    WHERE sk.status = 'active' AND k.term IN $terms
                    WITH sk, count(DISTINCT k) AS hits
                    RETURN sk.routine_id AS sid, hits
                    ORDER BY hits DESC LIMIT $lim
                """, terms=terms, lim=max(int(limit) * 3, 10)):
                    sid, hits = r["sid"], r["hits"]
                    # Fraction of the query's terms this routine carries. Kept on
                    # the same 0-1 scale as the cosine score so one threshold
                    # and one ordering apply to both paths.
                    score = hits / float(len(terms))
                    if sid not in found or score > found[sid][0]:
                        found[sid] = (score, "keyword")

            if not found:
                return []

            rows = s.run("""
                MATCH (sk:Routine) WHERE sk.routine_id IN $sids
                OPTIONAL MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk)
                RETURN sk.routine_id   AS routine_id,
                       sk.trigger    AS trigger,
                       sk.procedure  AS procedure,
                       sk.confidence AS confidence,
                       collect(m.address) AS members
            """, sids=list(found.keys()))

            out = []
            for r in rows:
                score, via = found[r["routine_id"]]
                d = dict(r)
                d["score"] = round(score, 4)
                d["via"]   = via
                out.append(d)
            out.sort(key=lambda d: -d["score"])
            return out[:int(limit)]
    except Exception as e:
        log.warning(f"match_routines failed: {e}")
        return []


def record_routine_invocation(routine_ids):
    """
    Count a delivery. invocation_count existed from Phase 12 and was never
    incremented, which meant there was no way to tell a routine that earns its
    place from one that has never once been used.
    """
    driver = get_driver()
    if driver is None or not routine_ids:
        return False
    try:
        with driver.session() as s:
            s.run("""
                MATCH (sk:Routine) WHERE sk.routine_id IN $sids
                SET sk.invocation_count = coalesce(sk.invocation_count, 0) + 1,
                    sk.last_invoked     = $now
            """, sids=list(routine_ids), now=datetime.now().isoformat())
        return True
    except Exception as e:
        log.warning(f"record_routine_invocation failed: {e}")
        return False


def get_routines_needing_index():
    """
    Active routines with no embedding or no keywords -- everything crystallized
    before delivery existed, plus anything whose embedding write failed.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (sk:Routine)
                WHERE sk.status = 'active'
                  AND (sk.embedding IS NULL
                       OR NOT (sk)-[:HAS_KEYWORD]->(:Keyword))
                RETURN sk.routine_id  AS routine_id,
                       sk.trigger   AS trigger,
                       sk.procedure AS procedure
            """)]
    except Exception as e:
        log.warning(f"get_routines_needing_index failed: {e}")
        return []


def resolve_routine_id(value):
    """
    Accept a full routine_id or an unambiguous prefix. Returns (routine_id, error).

    Routine ids are UUIDs and get displayed truncated almost everywhere -- tree
    views, summaries, logs. Requiring the full 36 characters means the id
    someone actually has in front of them is the one form that does not work,
    which is how a branch ends up as a second root.

    An ambiguous prefix is an error, never a guess: silently picking one of two
    matching routines would attach a branch to the wrong parent.
    """
    value = (value or "").strip()
    if not value:
        return None, "no routine id given"
    driver = get_driver()
    if driver is None:
        return None, "no database connection"
    try:
        with driver.session() as s:
            hits = [r["sid"] for r in s.run("""
                MATCH (sk:Routine)
                WHERE sk.routine_id = $v OR sk.routine_id STARTS WITH $v
                RETURN sk.routine_id AS sid
                ORDER BY CASE WHEN sk.routine_id = $v THEN 0 ELSE 1 END
                LIMIT 5
            """, v=value)]
        if not hits:
            return None, f"no routine with id {value}"
        if hits[0] == value or len(hits) == 1:
            return hits[0], None
        return None, (f"{len(hits)} routines start with {value}: "
                      f"{', '.join(h[:12] for h in hits)}. Use more characters.")
    except Exception as e:
        log.warning(f"resolve_routine_id failed: {e}")
        return None, str(e)


def link_routines(child_id, parent_id):
    """
    Phase 13: child EXTENDS_ROUTINE parent.

    Refuses to create a cycle. A routine tree with a cycle is not a tree, and the
    deprecation guard below walks children -- a cycle would make that walk
    non-terminating.
    """
    driver = get_driver()
    if driver is None:
        return False
    if child_id == parent_id:
        raise ValueError("a routine cannot extend itself")
    try:
        with driver.session() as s:
            # Would the new edge close a loop? True if parent already reaches
            # child by following EXTENDS_ROUTINE upward.
            cyc = s.run("""
                MATCH (p:Routine {routine_id: $pid}), (c:Routine {routine_id: $cid})
                RETURN EXISTS((p)-[:EXTENDS_ROUTINE*1..]->(c)) AS cycles
            """, pid=parent_id, cid=child_id).single()
            if cyc and cyc["cycles"]:
                raise ValueError("that link would create a cycle in the routine tree")

            rec = s.run("""
                MATCH (c:Routine {routine_id: $cid}), (p:Routine {routine_id: $pid})
                MERGE (c)-[:EXTENDS_ROUTINE]->(p)
                RETURN count(*) AS n
            """, cid=child_id, pid=parent_id).single()
            return bool(rec and rec["n"])
    except ValueError:
        raise
    except Exception as e:
        log.warning(f"link_routines failed: {e}")
        return False


def unlink_routine(child_id, parent_id=None):
    """
    Detach a routine from its parent, making it a root again.

    Reparenting previously meant uncrystallizing and rebuilding, which changes
    the routine_id, re-enters the overlap checks, and destroys work to change one
    edge. Detaching is the cheap operation and should be reachable as one.

    parent_id=None removes every EXTENDS_ROUTINE edge from this routine.
    Returns (removed_count, error).
    """
    driver = get_driver()
    if driver is None:
        return 0, "no database connection"
    try:
        with driver.session() as s:
            if not s.run("MATCH (sk:Routine {routine_id:$cid}) RETURN count(sk) AS n",
                         cid=child_id).single()["n"]:
                return 0, "no such routine"
            if parent_id:
                rec = s.run("""
                    MATCH (c:Routine {routine_id:$cid})-[r:EXTENDS_ROUTINE]->(p:Routine {routine_id:$pid})
                    DELETE r RETURN count(r) AS n
                """, cid=child_id, pid=parent_id).single()
            else:
                rec = s.run("""
                    MATCH (c:Routine {routine_id:$cid})-[r:EXTENDS_ROUTINE]->(:Routine)
                    DELETE r RETURN count(r) AS n
                """, cid=child_id).single()
        return int(rec["n"]) if rec else 0, None
    except Exception as e:
        log.warning(f"unlink_routine failed: {e}")
        return 0, str(e)


def get_routine_tree(root_routine_id=None):
    """
    Walk EXTENDS_ROUTINE. Returns the tree under root_routine_id, or the whole
    forest (every routine with no parent) when no root is given.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (sk:Routine)
                OPTIONAL MATCH (sk)-[:EXTENDS_ROUTINE]->(p:Routine)
                RETURN sk.routine_id AS routine_id, sk.trigger AS trigger,
                       sk.status AS status, sk.confidence AS confidence,
                       collect(DISTINCT p.routine_id) AS parents
            """)
            nodes = {}
            for r in rows:
                d = dict(r)
                d["parents"]  = [p for p in (d["parents"] or []) if p]
                d["children"] = []
                nodes[d["routine_id"]] = d

            for sid, d in nodes.items():
                for p in d["parents"]:
                    if p in nodes:
                        nodes[p]["children"].append(sid)

            def build(sid, seen):
                if sid in seen:          # defensive; link_routines blocks cycles
                    return {"routine_id": sid, "cycle": True}
                seen = seen | {sid}
                n = nodes[sid]
                return {
                    "routine_id":   sid,
                    "trigger":    n["trigger"],
                    "status":     n["status"],
                    "confidence": n["confidence"],
                    "children":   [build(c, seen) for c in n["children"]],
                }

            if root_routine_id:
                if root_routine_id not in nodes:
                    return []
                return [build(root_routine_id, set())]
            roots = [sid for sid, d in nodes.items() if not d["parents"]]
            return [build(r, set()) for r in roots]
    except Exception as e:
        log.warning(f"get_routine_tree failed: {e}")
        return []


def deprecate_routine(routine_id):
    """
    Phase 13: set a Routine to deprecated, but ONLY if no active child extends it.

    The roadmap states a parent cannot be deprecated while a child is active.
    Enforced here as a real check rather than a comment: a silent success would
    leave a live routine extending a dead parent, and the tree would be wrong in a
    way nothing later would notice.

    Returns (True, None) or (False, reason).
    """
    driver = get_driver()
    if driver is None:
        return False, "Neo4j unavailable"
    try:
        with driver.session() as s:
            exists = s.run("MATCH (sk:Routine {routine_id:$sid}) RETURN sk.status AS st",
                           sid=routine_id).single()
            if not exists:
                return False, "no such routine"

            blockers = [r["cid"] for r in s.run("""
                MATCH (child:Routine)-[:EXTENDS_ROUTINE]->(sk:Routine {routine_id: $sid})
                WHERE child.status = 'active'
                RETURN child.routine_id AS cid
            """, sid=routine_id)]
            if blockers:
                return False, (
                    f"{len(blockers)} active child routine(s) still extend this one: "
                    f"{', '.join(blockers)}. Deprecate or reparent them first."
                )

            s.run("MATCH (sk:Routine {routine_id:$sid}) SET sk.status = 'deprecated'",
                  sid=routine_id)
            return True, None
    except Exception as e:
        log.warning(f"deprecate_routine failed: {e}")
        return False, str(e)


def propose_meta_routine(min_shared=2):
    """
    Phase 13: routine pairs sharing >= min_shared source memories are candidates
    for a common parent.

    Proposes only. Creating the meta-routine is a human decision, same discipline
    as crystallization itself -- this is the step where the tree starts encoding
    claims about how Nova's abilities relate, which is not a call to make
    automatically.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(a:Routine)
                MATCH (m)-[:PROCEDURALIZED_FROM]->(b:Routine)
                WHERE a.routine_id < b.routine_id
                WITH a, b, count(DISTINCT m) AS shared
                WHERE shared >= $ms
                RETURN a.routine_id AS routine_a, a.trigger AS trigger_a,
                       b.routine_id AS routine_b, b.trigger AS trigger_b,
                       shared
                ORDER BY shared DESC
            """, ms=int(min_shared))
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"propose_meta_routine failed: {e}")
        return []


def get_routines(status=None):
    """List Routine nodes with their source-memory counts."""
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (sk:Routine)
                WHERE $status IS NULL OR sk.status = $status
                OPTIONAL MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk)
                OPTIONAL MATCH (sk)-[:EXTENDS_ROUTINE]->(parent:Routine)
                RETURN sk.routine_id AS routine_id, sk.trigger AS trigger,
                       sk.procedure AS procedure, sk.confidence AS confidence,
                       sk.status AS status, sk.created_at AS created_at,
                       sk.invocation_count AS invocation_count,
                       count(DISTINCT m) AS source_memories,
                       collect(DISTINCT parent.routine_id) AS extends
                ORDER BY sk.created_at DESC
            """, status=status)
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"get_routines failed: {e}")
        return []


# ═════════════════════════════════════════════
#  PHASE 13.1 — ROUTINE PROPOSAL QUEUE
# ═════════════════════════════════════════════
#
# The human gate on crystallize_routine() is correct and stays. What it lacked
# was a doorbell: nothing ever surfaced a candidate for review, so in practice
# the gate was never approached and no routine was ever formed. find_routine_
# candidates() is read-only and safe to poll, but a poll nobody runs proposes
# nothing.
#
# A RoutineProposal is a durable, deduplicated note that a cluster looked ready.
# The daemon may create and refresh them. It may not act on them. Turning one
# into a Routine still requires POST /crystallize with confirmed=true, which is
# still a human decision -- the queue changes who does the noticing, not who
# does the deciding.


def _member_key(member_created):
    """
    Stable identity for a cluster, order-independent.

    Keyed on created_at, NOT on address. An MMU address encodes the use and
    arc counters, and mmu_server rewrites it in place every time a memory is
    recalled or ages -- the same memory is 012.005.202.015 today and
    012.005.202.017 after two recalls. Keying a durable queue on that gives a
    fresh key for an unchanged cluster, so every sweep would re-queue what it
    already had and the review list would fill with the same finding wearing
    new numbers. created_at is written once at save and never updated.
    """
    joined = "|".join(sorted(str(t) for t in member_created))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _created_for_addresses(session, member_addresses):
    """Resolve current addresses to their immutable created_at stamps."""
    rows = session.run("""
        MATCH (m:Memory) WHERE m.address IN $addrs RETURN m.created_at AS t
    """, addrs=list(member_addresses))
    return [r["t"] for r in rows if r["t"]]


# A review queue is an attention budget, not a log. The sweep runs after every
# idle pass, and the graph shifts between passes, so without a ceiling the
# pending list grows for as long as nobody reviews it -- and a queue of two
# hundred proposals is functionally the same as the empty one this phase set
# out to fix. Existing proposals still refresh at the ceiling; only new ones
# wait for room.
MAX_PENDING_PROPOSALS = 25


def queue_routine_proposals(candidates=None, min_score=0.70, limit=10,
                          max_pending=MAX_PENDING_PROPOSALS):
    """
    Write pending RoutineProposal nodes for candidates that clear min_score.

    Touches no Memory node and creates no Routine. The default min_score matches
    the threshold the roadmap always documented for proposing; it finally
    means something now that routine_score is clamped to 0-1.

    A proposal already marked rejected is NOT resurrected. If a reviewer has
    said no to a cluster, the sweep re-offering it every 20 minutes would be
    nagging, not noticing. Scores on still-pending proposals are refreshed so
    the queue reflects the current graph rather than the day it first fired.

    Stops creating new proposals once max_pending are already waiting. The
    ceiling is on unreviewed work, not on the graph: refreshes continue, and
    reviewing or rejecting anything makes room immediately.

    Returns {"created", "refreshed", "skipped_rejected", "deferred", "pending"}.
    """
    driver = get_driver()
    if driver is None:
        return {"created": 0, "refreshed": 0, "skipped_rejected": 0,
                "deferred": 0, "pending": 0}

    if candidates is None:
        candidates = find_routine_candidates(limit=limit)

    ranked = [c for c in candidates if float(c.get("routine_score", 0)) >= float(min_score)]
    created = refreshed = skipped = deferred = 0
    now = datetime.now().isoformat()

    try:
        with driver.session() as s:
            pending_now = s.run("""
                MATCH (p:RoutineProposal {status: 'pending'}) RETURN count(p) AS n
            """).single()["n"]

            for c in ranked:
                # Room check is per-candidate: rejecting something mid-sweep
                # should let the next one through rather than wait a pass.
                if pending_now >= int(max_pending):
                    known = s.run("""
                        MATCH (p:RoutineProposal {member_key: $key}) RETURN count(p) AS n
                    """, key=_member_key(c["member_created"])).single()["n"]
                    if not known:
                        deferred += 1
                        continue
                key = _member_key(c["member_created"])
                rec = s.run("""
                    MERGE (p:RoutineProposal {member_key: $key})
                    ON CREATE SET p.proposal_id   = $pid,
                                  p.members       = $members,
                                  p.member_created = $created,
                                  p.status        = 'pending',
                                  p.created_at    = $now,
                                  p.updated_at    = $now,
                                  p.reviewed_at   = null,
                                  p.review_note   = '',
                                  p.routine_id      = null,
                                  p.routine_score   = $score,
                                  p.avg_weight    = $avgw,
                                  p.grp_coherence = $coh,
                                  p.semantic_coherence = $sem,
                                  p.grps          = $grps,
                                  p.previews      = $previews,
                                  p.src_mix       = $srcmix
                    // Refresh scores on a still-pending proposal so the queue
                    // reflects the graph now. Never touch status: a rejected
                    // proposal that is still dense must stay rejected.
                    // Addresses drift as members are recalled; refresh the
                    // display copy so a reviewer never sees a stale one.
                    ON MATCH SET  p.members       = CASE WHEN p.status = 'pending'
                                                        THEN $members ELSE p.members END,
                                  p.updated_at    = CASE WHEN p.status = 'pending'
                                                        THEN $now ELSE p.updated_at END,
                                  p.routine_score   = CASE WHEN p.status = 'pending'
                                                        THEN $score ELSE p.routine_score END,
                                  p.avg_weight    = CASE WHEN p.status = 'pending'
                                                        THEN $avgw ELSE p.avg_weight END,
                                  p.grp_coherence = CASE WHEN p.status = 'pending'
                                                        THEN $coh ELSE p.grp_coherence END,
                                  p.semantic_coherence = CASE WHEN p.status = 'pending'
                                                        THEN $sem ELSE p.semantic_coherence END,
                                  p.previews      = CASE WHEN p.status = 'pending'
                                                        THEN $previews ELSE p.previews END,
                                  p.src_mix       = CASE WHEN p.status = 'pending'
                                                        THEN $srcmix ELSE p.src_mix END
                    RETURN p.status AS status, p.created_at = $now AS is_new
                """,
                    key=key, pid=str(uuid.uuid4()), members=list(c["members"]),
                    created=list(c["member_created"]),
                    now=now, score=float(c.get("routine_score", 0.0)),
                    avgw=float(c.get("avg_weight", 0.0)),
                    coh=float(c.get("grp_coherence", 0.0)),
                    sem=(float(c["semantic_coherence"])
                         if c.get("semantic_coherence") is not None else None),
                    grps=list(c.get("grps") or []),
                    previews=list(c.get("previews") or []),
                    # Neo4j stores no maps on properties; JSON keeps the mix
                    # readable without inventing a node per source class.
                    srcmix=json.dumps(c.get("src_mix") or {}),
                ).single()

                if rec and rec["is_new"]:
                    created += 1
                    pending_now += 1
                elif rec and rec["status"] == "pending":
                    refreshed += 1
                else:
                    skipped += 1

            pending = s.run("""
                MATCH (p:RoutineProposal {status: 'pending'}) RETURN count(p) AS n
            """).single()["n"]

        if created:
            log.info("Queued %d new routine proposal(s); %d refreshed, %d already rejected",
                     created, refreshed, skipped)
        if deferred:
            log.info("Deferred %d proposal(s): %d already pending review (ceiling %d)",
                     deferred, pending, max_pending)
        return {"created": created, "refreshed": refreshed,
                "skipped_rejected": skipped, "deferred": deferred,
                "pending": pending}
    except Exception as e:
        log.warning(f"queue_routine_proposals failed: {e}")
        return {"created": 0, "refreshed": 0, "skipped_rejected": 0,
                "deferred": 0, "pending": 0}


def get_routine_proposals(status="pending", limit=50):
    """
    List queued proposals, highest score first. status=None returns all.

    Members are resolved live from created_at and re-ordered to match it.
    Two reasons, both learned the hard way:

      - Addresses drift. A proposal queued days ago stored addresses that have
        since been rewritten by recall, and handing those to /crystallize would
        MATCH nothing.
      - collect() does not preserve the order of the list it was matched
        against. Returning it as-is paired each address with another member's
        preview, so the review screen confidently mislabelled which memory was
        which -- the one failure mode a review step cannot have.

    Payloads and GRPs are read live for the same reason: a preview captured at
    queue time describes what the memory said then, and the reviewer is being
    asked about now.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            where = "WHERE p.status = $status" if status else ""
            rows = s.run(f"""
                MATCH (p:RoutineProposal)
                {where}
                OPTIONAL MATCH (m:Memory) WHERE m.created_at IN p.member_created
                WITH p, collect({{created_at: m.created_at,
                                 address:    m.address,
                                 payload:    m.payload,
                                 color:      m.color,
                                 grp: toInteger(split(m.address, '.')[2])}}) AS live
                // A member already compressed into an active Routine cannot be
                // compressed into a second one, so this proposal can never be
                // confirmed while that routine exists. Saying so here is the
                // difference between a queue and a list of things to try.
                OPTIONAL MATCH (bm:Memory)-[:PROCEDURALIZED_FROM]->(bsk:Routine)
                WHERE bm.created_at IN p.member_created
                  AND bsk.status <> 'deprecated'
                // Filter the nulls OUT here, not in Python. An OPTIONAL
                // MATCH that finds nothing still collects one all-null map, so
                // size(blockers) was 1 for every proposal and the ordering
                // below silently did nothing.
                WITH p, live,
                     [b IN collect(DISTINCT {{routine_id: bsk.routine_id,
                                             trigger:  bsk.trigger}})
                      WHERE b.routine_id IS NOT NULL] AS blockers
                RETURN blockers         AS blockers,
                       p.proposal_id    AS proposal_id,
                       p.member_key     AS member_key,
                       p.member_created AS member_created,
                       live             AS live_members,
                       p.status         AS status,
                       p.routine_score    AS routine_score,
                       p.avg_weight     AS avg_weight,
                       p.grp_coherence  AS grp_coherence,
                       p.semantic_coherence AS semantic_coherence,
                       p.src_mix        AS src_mix,
                       p.created_at     AS created_at,
                       p.updated_at     AS updated_at,
                       p.reviewed_at    AS reviewed_at,
                       p.review_note    AS review_note,
                       p.routine_id       AS routine_id
                // Actionable proposals first. Score still orders within each
                // group, but a review queue that leads with items nothing can
                // confirm wastes the reviewer's attention on the ones ranked
                // highest -- which is exactly what happened: the top three by
                // score were all blocked.
                ORDER BY size(blockers) ASC, p.routine_score DESC, p.created_at ASC
                LIMIT $lim
            """, status=status, lim=int(limit))

            out = []
            for r in rows:
                d = dict(r)
                stamps = list(d.pop("member_created") or [])
                live = [m for m in (d.pop("live_members") or []) if m.get("created_at")]

                # Restore the order the cluster was queued in.
                order = {t: i for i, t in enumerate(stamps)}
                live.sort(key=lambda m: order.get(m["created_at"], len(order)))

                d["members"]         = [m["address"] for m in live]
                d["previews"]        = [(m.get("payload") or "")[:90] for m in live]
                d["grps"]            = [m["grp"] for m in live if m.get("grp") is not None]
                d["colors"]          = [m.get("color") for m in live]
                d["members_missing"] = len(stamps) - len(live)

                blockers = [b for b in (d.pop("blockers", None) or [])
                            if b.get("routine_id")]
                d["blocked_by"] = blockers
                d["blocked"]    = bool(blockers)

                try:
                    d["src_mix"] = json.loads(d.get("src_mix") or "{}")
                except (TypeError, ValueError):
                    d["src_mix"] = {}
                out.append(d)
            return out
    except Exception as e:
        log.warning(f"get_routine_proposals failed: {e}")
        return []


def count_routine_proposals(status="pending"):
    """How many proposals exist, independent of any page limit."""
    driver = get_driver()
    if driver is None:
        return 0
    try:
        with driver.session() as s:
            where = "WHERE p.status = $status" if status else ""
            rec = s.run(f"MATCH (p:RoutineProposal) {where} RETURN count(p) AS n",
                        status=status).single()
            return int(rec["n"]) if rec else 0
    except Exception as e:
        log.warning(f"count_routine_proposals failed: {e}")
        return 0


def reject_routine_proposal(proposal_id, note=""):
    """
    Mark a proposal rejected so later sweeps stop re-offering it.

    Rejection is permanent by design: it is a judgement about the cluster, and
    a sweep that could undo it would make the judgement pointless.
    """
    driver = get_driver()
    if driver is None:
        return False, "no database connection"
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (p:RoutineProposal {proposal_id: $pid})
                RETURN p.status AS status
            """, pid=proposal_id).single()
            if not rec:
                return False, "no such proposal"
            if rec["status"] == "crystallized":
                return False, "that proposal already became a routine"

            s.run("""
                MATCH (p:RoutineProposal {proposal_id: $pid})
                SET p.status      = 'rejected',
                    p.reviewed_at = $now,
                    p.review_note = $note,
                    p.updated_at  = $now
            """, pid=proposal_id, now=datetime.now().isoformat(),
                 note=(note or ""))
        return True, "rejected"
    except Exception as e:
        log.warning(f"reject_routine_proposal failed: {e}")
        return False, str(e)


def get_proposal_members(proposal_id):
    """
    Current addresses for a queued proposal, resolved from the immutable
    created_at stamps. Returns (addresses, error).

    This is what makes confirm-by-proposal-id safe: the addresses are read at
    the moment of the write instead of being carried by the reviewer from a
    listing that may be minutes stale.
    """
    driver = get_driver()
    if driver is None:
        return [], "no database connection"
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (p:RoutineProposal {proposal_id: $pid})
                RETURN p.member_created AS stamps, p.status AS status
            """, pid=proposal_id).single()
            if not rec:
                return [], "no such proposal"
            if rec["status"] == "crystallized":
                return [], "that proposal has already been crystallized"

            stamps = list(rec["stamps"] or [])
            addrs = [r["a"] for r in s.run("""
                MATCH (m:Memory) WHERE m.created_at IN $stamps
                RETURN m.address AS a
            """, stamps=stamps)]
            if len(addrs) != len(stamps):
                return [], (f"{len(stamps) - len(addrs)} of {len(stamps)} source "
                            "memories no longer exist; this proposal is stale")
            return addrs, None
    except Exception as e:
        log.warning(f"get_proposal_members failed: {e}")
        return [], str(e)


def close_proposal_for_members(member_addresses, routine_id):
    """
    Close the loop after a human crystallizes: if the confirmed member set
    matches a queued proposal, mark it crystallized so the sweep stops
    proposing a cluster that already became a routine.

    Best-effort. Crystallization has already committed by the time this runs,
    and a bookkeeping failure must not be reported as a failed crystallization.
    """
    driver = get_driver()
    if driver is None:
        return False
    try:
        with driver.session() as s:
            # /crystallize is given addresses; the queue is keyed on created_at.
            created = _created_for_addresses(s, member_addresses)
            if not created:
                return False
            res = s.run("""
                MATCH (p:RoutineProposal {member_key: $key})
                SET p.status      = 'crystallized',
                    p.routine_id    = $sid,
                    p.reviewed_at = $now,
                    p.updated_at  = $now
                RETURN p.proposal_id AS pid
            """, key=_member_key(created), sid=routine_id,
                 now=datetime.now().isoformat()).single()
        return bool(res)
    except Exception as e:
        log.warning(f"close_proposal_for_members failed (routine was created): {e}")
        return False



# ═════════════════════════════════════════════
#  PHASE 10 — PROACTIVE MEMORY (PATTERN DETECTION)
# ═════════════════════════════════════════════
#
# Two independent anticipation signals, deliberately kept separate because they
# become useful at different times:
#
#   TEMPORAL  "this pairing recurs roughly every N days and is about due."
#             Needs real elapsed time and several sessions of occurrence history
#             before it can say anything. Returns nothing on a fresh graph, and
#             that is correct behaviour, not a bug.
#
#   CLUSTER   "you are talking about X, and Y is strongly co-recalled with X
#             from a different GRP domain." Works immediately against the
#             existing graph.
#
# Both return structured candidates, following run_maintenance()'s shape rather
# than emitting free text. Formatting is the server's job.


def _interval_stats(timestamps):
    """
    Gap statistics for a list of ISO timestamps.

    Returns (median_days, confidence) or (None, 0.0) when there is not enough
    history. Confidence is 1 - coefficient of variation over the gaps, clamped
    to [0, 1]: evenly spaced occurrences score high, erratic ones score low.
    Two data points give exactly one gap and therefore no variance information,
    so they are refused rather than reported as perfectly confident.
    """
    if not timestamps or len(timestamps) < 3:
        return None, 0.0
    try:
        ts = sorted(datetime.fromisoformat(t) for t in timestamps)
    except Exception:
        return None, 0.0

    gaps = [(ts[i + 1] - ts[i]).total_seconds() / 86400.0
            for i in range(len(ts) - 1)]
    gaps = [g for g in gaps if g > 0]
    if len(gaps) < 2:
        return None, 0.0

    gaps.sort()
    n = len(gaps)
    median = gaps[n // 2] if n % 2 else (gaps[n // 2 - 1] + gaps[n // 2]) / 2
    mean = sum(gaps) / n
    if mean <= 0:
        return None, 0.0
    var = sum((g - mean) ** 2 for g in gaps) / n
    cv = (var ** 0.5) / mean
    return round(median, 2), round(max(0.0, min(1.0, 1.0 - cv)), 3)


def detect_temporal_patterns(min_occurrences=4, min_confidence=0.5, limit=20):
    """
    CO_RECALLED pairings that recur on a consistent interval.

    Returns [{addr_a, addr_b, interval_days, confidence, last_seen, weight,
              days_since, due}] sorted by confidence.

    Expect an empty list until several sessions of occurrence history exist.
    Occurrence tracking started in Phase 10; edges created before it carry no
    history and are skipped rather than guessed at.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (a:Memory)-[r:CO_RECALLED]-(b:Memory)
                WHERE r.occurrences IS NOT NULL
                  AND size(r.occurrences) >= $minocc
                  AND a.address < b.address
                RETURN a.address AS addr_a, b.address AS addr_b,
                       r.occurrences AS occ, r.weight AS weight,
                       r.last_turn AS last_turn
            """, minocc=int(min_occurrences))

            now = datetime.now()
            out = []
            for rec in rows:
                interval, conf = _interval_stats(list(rec["occ"] or []))
                if interval is None or conf < min_confidence:
                    continue
                try:
                    since = (now - datetime.fromisoformat(rec["last_turn"])).total_seconds() / 86400.0
                except Exception:
                    since = None
                out.append({
                    "addr_a":        rec["addr_a"],
                    "addr_b":        rec["addr_b"],
                    "interval_days": interval,
                    "confidence":    conf,
                    "weight":        rec["weight"],
                    "last_seen":     rec["last_turn"],
                    "days_since":    round(since, 2) if since is not None else None,
                    "due":           since is not None and since >= interval,
                })
            out.sort(key=lambda d: -d["confidence"])
            return out[:int(limit)]
    except Exception as e:
        log.warning(f"detect_temporal_patterns failed: {e}")
        return []


def get_anticipated_context(seed_addrs=None, exclude_addrs=None, limit=3):
    """
    Memories worth surfacing that were NOT asked for.

    seed_addrs     what the conversation has touched (empty = start of session)
    exclude_addrs  never return these (already delivered this turn/session)
    limit          hard cap; proactive surfacing that floods context defeats itself

    Returns [{address, payload, color, src_label, reason, score}] where reason
    is "temporal" or "cluster" so the caller can label it honestly.

    Documents (src_type 2) are excluded. Reference material is retrieved on
    demand; a paragraph of a physics paper is not a thing to be reminded of.
    """
    driver = get_driver()
    if driver is None:
        return []

    seed_addrs    = list(seed_addrs or [])
    exclude       = set(exclude_addrs or []) | set(seed_addrs)
    limit         = max(0, int(limit))
    if limit == 0:
        return []

    picks, seen = [], set(exclude)

    try:
        with driver.session() as s:
            # ── Signal 1: temporal, "about due" ──
            for pat in detect_temporal_patterns():
                if not pat["due"]:
                    continue
                for addr in (pat["addr_a"], pat["addr_b"]):
                    if addr in seen:
                        continue
                    rec = s.run("""
                        MATCH (m:Memory {address:$a})
                        WHERE m.color <> 'Blue' AND m.src_type <> 2
                        RETURN m.address AS address, m.payload AS payload,
                               m.color AS color, m.src_label AS src_label
                    """, a=addr).single()
                    if rec:
                        d = dict(rec)
                        d["reason"] = "temporal"
                        d["score"]  = pat["confidence"]
                        picks.append(d)
                        seen.add(addr)
                    if len(picks) >= limit:
                        break
                if len(picks) >= limit:
                    break

            # ── Signal 2: cross-domain cluster ──
            # Same-domain neighbours are already reachable through expand();
            # surfacing them again would just duplicate ordinary recall. The
            # value here is the connection across domains that nobody asked for.
            if len(picks) < limit and seed_addrs:
                rows = s.run("""
                    MATCH (seed:Memory)-[r:CO_RECALLED]-(n:Memory)
                    WHERE seed.address IN $seeds
                      AND NOT n.address IN $exclude
                      AND n.color <> 'Blue' AND n.color <> 'Red'
                      AND n.src_type <> 2
                      // GRP domain differs. Uses split() rather than a fixed
                      // character offset: CON is variable width once it passes
                      // 999, which would shift every later field and make an
                      // offset silently read the wrong character.
                      AND toInteger(split(n.address, '.')[2]) / 100
                          <> toInteger(split(seed.address, '.')[2]) / 100
                    RETURN n.address AS address, n.payload AS payload,
                           n.color AS color, n.src_label AS src_label,
                           max(r.weight) AS w
                    ORDER BY w DESC LIMIT $lim
                """, seeds=seed_addrs, exclude=list(seen), lim=limit * 3)
                for rec in rows:
                    if rec["address"] in seen:
                        continue
                    d = dict(rec)
                    d["score"]  = float(d.pop("w") or 0)
                    d["reason"] = "cluster"
                    picks.append(d)
                    seen.add(rec["address"])
                    if len(picks) >= limit:
                        break

        return picks[:limit]
    except Exception as e:
        log.warning(f"get_anticipated_context failed: {e}")
        return []


# ═════════════════════════════════════════════
#  PHASE 9 — SEMANTIC EMBEDDING LAYER
# ═════════════════════════════════════════════
#
# A second, independent recall signal that runs ALONGSIDE the keyword gate,
# never replacing it. The gate stays the fast O(1) first pass; embeddings
# provide (a) a fallback when the gate finds nothing topical and (b) a
# similarity floor that pushes down stem-collision false positives.
#
# Blue (archived) memories are excluded from semantic search for the same
# reason expand() excludes them from CO_RECALLED hops: dormant memories stay
# dormant unless directly named. Red pinned nodes are excluded too - they are
# already injected unconditionally by the gate, so returning them here would
# duplicate rows and burn top_k slots.


def write_embedding(address, vector):
    """
    Attach an embedding vector to an existing Memory node.

    Called by /remember after a successful add_memory(), and by the backfill
    for memories that predate Phase 9. Returns True on success.

    Uses db.create.setNodeVectorProperty so the value is stored in Neo4j's
    native vector encoding rather than a plain list of floats - that is what
    the vector index actually reads.
    """
    driver = get_driver()
    if driver is None or not vector:
        return False
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (m:Memory {address: $addr})
                CALL db.create.setNodeVectorProperty(m, 'embedding', $vector)
                RETURN count(m) AS n
            """, addr=address, vector=[float(x) for x in vector]).single()
            return bool(rec and rec["n"])
    except Exception as e:
        log.warning(f"write_embedding failed for {address}: {e}")
        return False


def semantic_search(query_vector, top_k=10, exclude_blue=True, exclude_addrs=None):
    """
    Cosine nearest-neighbour search over Memory.embedding.

    Returns [{"address": str, "score": float}] sorted by descending cosine
    similarity, already filtered. Returns [] if Neo4j or the vector index is
    unavailable - the caller then simply has no semantic signal, and the
    keyword path is untouched.

    Over-fetches from the index before filtering, because Blue/Red exclusion
    happens after the ANN lookup and would otherwise silently shrink results.
    """
    driver = get_driver()
    if driver is None or not query_vector:
        return []

    exclude_addrs = set(exclude_addrs or ())
    fetch_k = max(int(top_k) * 4, 20)

    try:
        with driver.session() as s:
            result = s.run("""
                CALL db.index.vector.queryNodes($index, $k, $vector)
                YIELD node, score
                RETURN node.address AS address,
                       node.color   AS color,
                       score        AS score
            """, index=EMBEDDING_INDEX, k=fetch_k,
                 vector=[float(x) for x in query_vector])

            out = []
            for rec in result:
                addr  = rec["address"]
                color = rec["color"]
                if addr is None or addr in exclude_addrs:
                    continue
                if exclude_blue and color == "Blue":
                    continue
                if color == "Red":
                    continue          # already delivered by the pinned path
                out.append({"address": addr, "score": float(rec["score"])})
                if len(out) >= top_k:
                    break
            return out
    except Exception as e:
        log.warning(f"semantic_search failed: {e}")
        return []


def similarity_for_addresses(addresses, query_vector):
    """
    Cosine similarity between the query vector and specific Memory nodes.

    Used by the similarity floor in _v2_recall() to score keyword-gate hits
    that the ANN search did not return. Returns {address: score}; addresses
    with no embedding are simply absent from the map, and the caller must
    treat "absent" as "no opinion", never as "score zero" - otherwise every
    memory that predates the backfill would be dropped from recall.

    NOTE ON SCALE: Neo4j normalizes cosine into [0, 1], where 1.0 is
    identical, 0.5 is orthogonal, and 0.0 is diametrically opposed. A
    threshold expressed on the raw [-1, 1] cosine scale will NOT behave as
    intended here.
    """
    driver = get_driver()
    if driver is None or not addresses or not query_vector:
        return {}
    try:
        with driver.session() as s:
            result = s.run("""
                MATCH (m:Memory)
                WHERE m.address IN $addresses AND m.embedding IS NOT NULL
                RETURN m.address AS address,
                       vector.similarity.cosine(m.embedding, $vector) AS score
            """, addresses=list(addresses),
                 vector=[float(x) for x in query_vector])
            return {rec["address"]: float(rec["score"])
                    for rec in result if rec["score"] is not None}
    except Exception as e:
        log.warning(f"similarity_for_addresses failed: {e}")
        return {}


def count_unembedded_memories():
    """How many Memory nodes still have no embedding. Used by the backfill."""
    driver = get_driver()
    if driver is None:
        return 0
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (m:Memory)
                WHERE m.embedding IS NULL
                RETURN count(m) AS n
            """).single()
            return int(rec["n"]) if rec else 0
    except Exception as e:
        log.warning(f"count_unembedded_memories failed: {e}")
        return 0


def fetch_unembedded_memories(limit=50):
    """
    One page of memories still missing an embedding, as
    [{"address": str, "payload": str, "keywords": str}].

    Deliberately no SKIP offset: the backfill writes an embedding to every row
    it processes, so those rows drop out of this result set on the next call.
    Paging by SKIP against a shrinking set would silently skip records.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            result = s.run("""
                MATCH (m:Memory)
                WHERE m.embedding IS NULL
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                RETURN m.address AS address,
                       m.payload AS payload,
                       collect(k.term) AS kwList
                ORDER BY m.address
                LIMIT $limit
            """, limit=int(limit))
            return [{
                "address":  rec["address"],
                "payload":  rec["payload"] or "",
                "keywords": ",".join(rec["kwList"] or []),
            } for rec in result]
    except Exception as e:
        log.warning(f"fetch_unembedded_memories failed: {e}")
        return []


# ═════════════════════════════════════════════
#  PHASE 4 — SELF-CORRECTION
# ═════════════════════════════════════════════
#
# When Nova recalls a memory but finds it irrelevant, the MCP bridge
# calls /flag_recall which increments false_recall_count on that node.
# During the end-of-session reflection pass, flagged memories are
# included in the audit prompt so Nova can decide how to correct them:
#   - Update keywords to compound form (star%wars)
#   - Lower priority
#   - Demote color to Yellow
#   - Add audit_notes explaining the correction
#   - Reset false_recall_count after a successful fix
#
# This mirrors biological LTP weakening: associations that get
# retrieved but prove irrelevant gradually weaken over time.
# The CO_RECALLED weight system handles the strengthening side.
# Phase 4 adds the weakening and correction side.


def flag_memory(address, reason=""):
    """
    Increment false_recall_count on a Memory node.
    Called by the MCP bridge when Nova explicitly marks a recall as irrelevant.
    Returns the new count, or -1 if Neo4j is unavailable.
    """
    driver = get_driver()
    if driver is None:
        return -1
    try:
        with driver.session() as s:
            result = s.run("""
                MATCH (m:Memory {address: $addr})
                SET m.false_recall_count = coalesce(m.false_recall_count, 0) + 1,
                    m.last_flag_reason   = $reason,
                    m.last_flagged_at    = $now
                RETURN m.false_recall_count AS count
            """, addr=address, reason=reason,
                 now=datetime.now().isoformat()).single()
            count = result["count"] if result else -1
            log.info(f"flag_memory | {address} | count={count} | reason={reason[:60]}")
            return count
    except Exception as e:
        log.warning(f"flag_memory failed: {e}")
        return -1


def get_flagged_memories(threshold=3):
    """
    Return Memory nodes with false_recall_count >= threshold.
    Used by /flagged endpoint and by the reflect pass to include
    problematic memories in the audit prompt.
    Returns list of dicts with address, payload, color, keywords,
    false_recall_count, last_flag_reason.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            result = s.run("""
                MATCH (m:Memory)
                WHERE m.false_recall_count >= $threshold
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                WITH m, collect(k.term) AS kws
                RETURN m.address              AS address,
                       m.payload              AS payload,
                       m.color                AS color,
                       m.priority             AS priority,
                       m.false_recall_count   AS false_recall_count,
                       m.last_flag_reason     AS last_flag_reason,
                       m.last_flagged_at      AS last_flagged_at,
                       m.audit_notes          AS audit_notes,
                       kws                   AS keywords
                ORDER BY m.false_recall_count DESC
            """, threshold=threshold)
            rows = []
            for rec in result:
                d = dict(rec)
                d["keywords"] = ",".join(d.pop("keywords") or [])
                rows.append(d)
            return rows
    except Exception as e:
        log.warning(f"get_flagged_memories failed: {e}")
        return []


def reset_flag_count(address):
    """
    Reset false_recall_count to 0 after a successful audit.
    Also records when the audit occurred.
    """
    driver = get_driver()
    if driver is None:
        return
    try:
        with driver.session() as s:
            s.run("""
                MATCH (m:Memory {address: $addr})
                SET m.false_recall_count = 0,
                    m.last_audited       = $now
            """, addr=address, now=datetime.now().isoformat())
        log.info(f"reset_flag_count | {address}")
    except Exception as e:
        log.warning(f"reset_flag_count failed: {e}")


def apply_memory_correction(address, new_color=None, new_priority=None,
                             new_keywords=None, audit_note=""):
    """
    Apply Nova's correction to a flagged memory.
    Called by /audit_memory after the reflection pass decides what to fix.

    new_color     -- optional new color state (Yellow recommended for review)
    new_priority  -- optional new priority (higher number = lower priority)
    new_keywords  -- optional list of corrected keywords (replaces all existing)
    audit_note    -- Nova's explanation of what was wrong and what was changed

    After applying correction, resets false_recall_count to 0.
    """
    driver = get_driver()
    if driver is None:
        return False
    try:
        with driver.session() as s:
            # Build SET clauses dynamically for optional fields
            sets = ["m.audit_notes = $note", "m.last_audited = $now"]
            params = {"addr": address, "note": audit_note,
                      "now": datetime.now().isoformat()}

            if new_color is not None:
                sets.append("m.color = $color")
                params["color"] = new_color

            if new_priority is not None:
                sets.append("m.priority = $priority")
                params["priority"] = new_priority

            set_clause = ", ".join(sets)
            s.run(f"""
                MATCH (m:Memory {{address: $addr}})
                SET {set_clause}
            """, **params)

            # Replace keywords if provided
            if new_keywords:
                # Remove old HAS_KEYWORD edges
                s.run("""
                    MATCH (m:Memory {address: $addr})-[r:HAS_KEYWORD]->()
                    DELETE r
                """, addr=address)
                # Add new keywords
                kw_list = [k.strip().lower() for k in new_keywords if k.strip()]
                for term in kw_list:
                    _upsert_keyword_and_link(s, term, address)

            # Always reset flag count after a correction
            s.run("""
                MATCH (m:Memory {address: $addr})
                SET m.false_recall_count = 0
            """, addr=address)

        log.info(f"apply_memory_correction | {address} | note={audit_note[:60]}")
        return True

    except Exception as e:
        log.warning(f"apply_memory_correction failed: {e}")
        return False


# =============================================================================
# PHASE 5 -- Observability and Memory Versioning
# =============================================================================

def get_insights() -> dict:
    """
    Phase 5: Returns comprehensive memory graph insights.

    Used by the /insights REST endpoint and, in Phase 11, as the engine
    for crystallization candidate detection and routine proposal generation.

    Queries run in a single session; all are read-only and index-friendly.
    """
    driver = get_driver()
    if driver is None:
        return {}

    with driver.session() as session:

        # 1. Total node and edge counts
        totals_r = session.run("""
            MATCH (m:Memory)
            WITH count(m) AS tm
            OPTIONAL MATCH ()-[cr:CO_RECALLED]-()
            WITH tm, count(cr) / 2 AS te
            OPTIONAL MATCH (k:Keyword)
            WITH tm, te, count(k) AS tk
            OPTIONAL MATCH ()-[ev:EVOLVES_FROM]->()
            WITH tm, te, tk, count(ev) AS tev
            RETURN tm  AS total_memories,
                   te  AS total_edges,
                   tk  AS total_keywords,
                   tev AS total_evolutions
        """).single()

        totals = {
            "memories":          totals_r["total_memories"]   if totals_r else 0,
            "co_recalled_edges": totals_r["total_edges"]      if totals_r else 0,
            "keywords":          totals_r["total_keywords"]   if totals_r else 0,
            "audit_evolutions":  totals_r["total_evolutions"] if totals_r else 0,
        }

        # 2. Color distribution (Red / Green / Yellow / Blue)
        color_dist = {}
        for r in session.run("""
            MATCH (m:Memory)
            RETURN m.color AS color, count(m) AS cnt
            ORDER BY color
        """):
            color_dist[r["color"]] = r["cnt"]

        # 3. GRP domain activity -- count + color breakdown per domain code
        grp_activity = {}
        for r in session.run("""
            MATCH (m:Memory)
            WITH toInteger(split(m.address, '.')[2]) AS grp_code,
                 m.color AS color,
                 count(m) AS cnt
            RETURN grp_code, color, cnt
            ORDER BY grp_code, color
        """):
            code = r["grp_code"]
            if code not in grp_activity:
                grp_activity[code] = {"total": 0, "colors": {}}
            grp_activity[code]["colors"][r["color"]] = r["cnt"]
            grp_activity[code]["total"] += r["cnt"]

        # 4. Top CO_RECALLED edges by weight (fastest-growing associations)
        top_co_recalled = []
        for r in session.run("""
            MATCH (a:Memory)-[cr:CO_RECALLED]-(b:Memory)
            WHERE id(a) < id(b)
            RETURN a.address   AS addr_a,
                   substring(a.payload, 0, 55) AS preview_a,
                   b.address   AS addr_b,
                   substring(b.payload, 0, 55) AS preview_b,
                   cr.weight           AS weight,
                   cr.co_recall_count  AS count
            ORDER BY cr.weight DESC
            LIMIT 15
        """):
            top_co_recalled.append({
                "addr_a":    r["addr_a"],
                "preview_a": r["preview_a"] or "",
                "addr_b":    r["addr_b"],
                "preview_b": r["preview_b"] or "",
                "weight":    round(r["weight"] or 0.0, 4),
                "count":     r["count"] or 0,
            })

        # 5. Flagged memories (Phase 4 self-correction queue)
        flagged = []
        for r in session.run("""
            MATCH (m:Memory)
            WHERE m.false_recall_count > 0
            RETURN m.address            AS address,
                   m.false_recall_count AS count,
                   m.last_flag_reason   AS reason,
                   m.color              AS color,
                   substring(m.payload, 0, 80) AS preview
            ORDER BY m.false_recall_count DESC
            LIMIT 10
        """):
            flagged.append({
                "address": r["address"],
                "count":   r["count"],
                "reason":  r["reason"] or "",
                "color":   r["color"],
                "preview": r["preview"] or "",
            })

        # 6. Source type distribution
        src_labels = SOURCE_LABELS   # Phase 7: single source of truth, includes src_type=4
        source_dist = {}
        for r in session.run("""
            MATCH (m:Memory)
            RETURN m.src_type AS src_type, count(m) AS cnt
            ORDER BY src_type
        """):
            label = src_labels.get(r["src_type"], f"Type-{r['src_type']}")
            source_dist[label] = r["cnt"]

        # 7. Memory growth by calendar day
        growth = []
        for r in session.run("""
            MATCH (m:Memory)
            WITH date(datetime(m.created_at)) AS day, count(m) AS cnt
            RETURN toString(day) AS day, cnt
            ORDER BY day
        """):
            growth.append({"day": r["day"], "count": r["cnt"]})

        # 8. Crystallization candidates.
        #
        # Phase 13.1: this used to run its own query -- hub-shaped ("which
        # memory has strong neighbours"), with no source or colour filter and
        # its own scoring formula. find_routine_candidates() asks the question
        # that actually decides crystallization (mutual density across every
        # pair) and applies the real filters, and the two disagreed
        # systematically: /insights advertised a physics candidate at 0.7957,
        # above the documented propose-at-0.70 bar, that /routine_candidates was
        # structurally incapable of ever returning. Reporting a candidate the
        # confirm path cannot accept is worse than reporting none.
        #
        # One call, one answer. The endpoint is a preview of a real decision,
        # so it previews the real decision.
        crystal_candidates = find_routine_candidates(limit=10)

        # 9. Top keywords by frequency
        top_keywords = []
        for r in session.run("""
            MATCH (k:Keyword)
            RETURN k.term AS term, k.freq AS freq, k.stem AS stem
            ORDER BY k.freq DESC
            LIMIT 20
        """):
            top_keywords.append({
                "term": r["term"],
                "freq": r["freq"] or 0,
                "stem": r["stem"],
            })

        # 10. Phase 6.5 / 6.6: Valence distribution + bias analytics
        val_labels = {0: "unrated", 1: "like", 2: "dislike"}
        valence_dist = {}
        for r in session.run("""
            MATCH (m:Memory)
            RETURN coalesce(m.valence_type, 0) AS vt, count(m) AS cnt
            ORDER BY vt
        """):
            label = val_labels.get(r["vt"], f"type-{r['vt']}")
            valence_dist[label] = r["cnt"]

        # Phase 6.6: Negative weight suggestion and positivity bias flag
        total_liked    = valence_dist.get("like", 0)
        total_disliked = valence_dist.get("dislike", 0)
        total_rated    = total_liked + total_disliked

        # Inverse-frequency weight: rare dislikes should count more.
        # Cap at 5.0 so a single dislike doesn't dominate.
        suggested_neg_weight = round(
            min(total_liked / max(total_disliked, 1), 5.0), 2
        )

        # Bias flag fires when enough ratings exist but almost all are positive.
        positivity_bias = (
            total_rated >= 5
            and total_liked / total_rated > 0.85
        )

        # Top-rated memories (liked, intensity >= 5) + emotion label
        top_liked = []
        for r in session.run("""
            MATCH (m:Memory)
            WHERE m.valence_type = 1 AND m.valence_intensity >= 5
            RETURN m.address                AS address,
                   m.valence_intensity      AS intensity,
                   m.valence_emotion_label  AS emotion_label,
                   m.color                  AS color,
                   substring(m.payload, 0, 70) AS preview
            ORDER BY m.valence_intensity DESC
            LIMIT 10
        """):
            top_liked.append({
                "address":      r["address"],
                "intensity":    r["intensity"] or 0,
                "emotion_label": r["emotion_label"] or "",
                "color":        r["color"],
                "preview":      r["preview"] or "",
            })

        # Most-disliked memories (for retention-boost awareness) + emotion label
        top_disliked = []
        for r in session.run("""
            MATCH (m:Memory)
            WHERE m.valence_type = 2
            RETURN m.address                AS address,
                   m.valence_intensity      AS intensity,
                   m.valence_emotion_label  AS emotion_label,
                   m.color                  AS color,
                   substring(m.payload, 0, 70) AS preview
            ORDER BY m.valence_intensity DESC
            LIMIT 5
        """):
            top_disliked.append({
                "address":      r["address"],
                "intensity":    r["intensity"] or 0,
                "emotion_label": r["emotion_label"] or "",
                "color":        r["color"],
                "preview":      r["preview"] or "",
            })

        # Phase 6.6: Emotion label frequency breakdown (top 15, non-null)
        emotion_breakdown = []
        for r in session.run("""
            MATCH (m:Memory)
            WHERE m.valence_emotion_label IS NOT NULL
              AND m.valence_emotion_label <> ''
            RETURN m.valence_emotion_label AS label,
                   m.valence_type          AS vtype,
                   count(m)                AS n
            ORDER BY n DESC
            LIMIT 15
        """):
            emotion_breakdown.append({
                "label":    r["label"],
                "val_type": val_labels.get(r["vtype"], "?"),
                "count":    r["n"],
            })

        return {
            "totals":                     totals,
            "color_distribution":         color_dist,
            "grp_domain_activity":        grp_activity,
            "top_co_recalled_edges":      top_co_recalled,
            "flagged_memories":           flagged,
            "source_distribution":        source_dist,
            "memory_growth":              growth,
            "crystallization_candidates": crystal_candidates,
            "top_keywords":               top_keywords,
            "valence_distribution":       valence_dist,
            "top_liked_memories":         top_liked,
            "top_disliked_memories":      top_disliked,
            # Phase 6.6 additions
            "suggested_neg_weight":       suggested_neg_weight,
            "positivity_bias":            positivity_bias,
            "emotion_breakdown":          emotion_breakdown,
            "generated_at":               datetime.now().isoformat() + "Z",
        }


def snapshot_memory_before_audit(address: str) -> bool:
    """
    Phase 5: Snapshot a Memory node's current state before audit_memory
    overwrites it.  Creates a MemorySnapshot node connected via EVOLVES_FROM
    so audit lineage is permanently preserved.

    Direction: (MemorySnapshot) -[:EVOLVES_FROM]-> (Memory)
    Reading:   "this snapshot evolved into the current live node."

    MemorySnapshot nodes use a separate label so all existing queries
    that match on (m:Memory) are completely unaffected -- zero migration
    risk, zero performance impact on reads.

    Returns True if snapshot was created, False if address not found or on error.
    """
    driver = get_driver()
    if driver is None:
        return False
    try:
        with driver.session() as session:
            result = session.run("""
                MATCH (m:Memory {address: $address})
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                WITH m, collect(k.term) AS kw_terms
                CREATE (snap:MemorySnapshot {
                    address:              m.address,
                    payload:              m.payload,
                    color:                m.color,
                    priority:             m.priority,
                    src_label:            m.src_label,
                    src_type:             m.src_type,
                    false_recall_count:   m.false_recall_count,
                    audit_notes:          m.audit_notes,
                    note:                 m.note,
                    created_at:           m.created_at,
                    keywords_at_snapshot: kw_terms,
                    snapshotted_at:       $snapshotted_at
                })
                CREATE (snap)-[:EVOLVES_FROM]->(m)
                RETURN snap.address AS snapped
            """,
            address=address,
            snapshotted_at=datetime.now().isoformat())

            record = result.single()
            return record is not None
    except Exception as e:
        log.warning(f"snapshot_memory_before_audit failed silently: {e}")
        return False


# =============================================================================
# PHASE 6 -- Active Memory Maintenance (Consolidation)
# =============================================================================
#
# Runs four analysis passes over the Neo4j graph and returns a structured
# summary suitable for consumption by Phase 7 idle cognition prompts.
#
# No destructive operations are performed -- all findings are actionable
# suggestions for Nova or the user to act on via existing endpoints
# (/audit_memory, DELETE /memories/{address}, etc.).
#
# Phase 6 and Phase 7 sequencing (critical):
# When idle time is detected, Phase 6 /maintain fires FIRST and completes,
# THEN Phase 7 /idle_think fires with the maintenance summary as warm context.
# This mirrors sleep architecture: slow-wave consolidation (Phase 6) always
# precedes REM synthesis (Phase 7).

def run_maintenance(stale_days: int = 30,
                    cluster_weight_thresh: int = 5,
                    cluster_min_edges: int = 3) -> dict:
    """
    Phase 6: Active Memory Maintenance (Consolidation).

    Performs four analysis passes:
      1. Stem collision detection  -- keyword stem conflicts linked to flagged
         memories are compound keyword conversion candidates.
      2. Cross-GRP cluster detection -- dense CO_RECALLED pairs spanning
         different GRP domains signal emerging cross-domain patterns or
         spurious noise associations worth Nova's attention.
      3. Stale Blue memory detection -- Blue nodes whose last CO_RECALLED
         activity (or created_at) exceeds stale_days are pruning candidates.
      4. Domain gap detection -- GRP domains (1xx-8xx) with no active
         (Green/Yellow/Red) memories surface as context blind spots.

    Returns a structured dict including:
      - stem_collisions, cross_grp_clusters, stale_blue_memories, domain_gaps
      - stats:  quick numeric summary
      - summary_text: human-readable string injected into Phase 7 cognition
        prompt templates via {maintenance_summary_if_any}

    Parameters
    ----------
    stale_days           Days of inactivity before a Blue memory is flagged.
    cluster_weight_thresh Minimum CO_RECALLED weight to report a cross-GRP link.
    cluster_min_edges    Reserved for future cluster density filtering.
    """
    driver = get_driver()
    if driver is None:
        return {
            "error":                 "Neo4j unavailable",
            "summary_text":          "Maintenance skipped: Neo4j unavailable.",
            "maintenance_timestamp": datetime.now().isoformat() + "Z",
        }

    result = {
        "stem_collisions":     [],
        "cross_grp_clusters":  [],
        "stale_blue_memories": [],
        "domain_gaps":         [],
        "stats":               {},
        "maintenance_timestamp": datetime.now().isoformat() + "Z",
        "summary_text":        "",
    }

    try:
        with driver.session() as session:

            # ── Task 1: Stem collision detection ────────────────────────
            # Find keyword pairs sharing a 4-char stem where at least one
            # memory linked to either keyword has false_recall_count > 0.
            # These are compound keyword conversion candidates.
            # Example: stem "star" matches "star" (from Star Wars) and
            # "starter" (from sourdough starter) -- a known false-recall source.
            stem_rows = session.run("""
                MATCH (k1:Keyword), (k2:Keyword)
                WHERE k1.stem = k2.stem AND k1.term < k2.term
                WITH k1, k2
                MATCH (m:Memory)-[:HAS_KEYWORD]->(k1)
                WHERE m.false_recall_count > 0
                WITH k1, k2,
                     collect(m.address)                       AS flagged_addrs,
                     max(m.false_recall_count)                AS max_flag_count,
                     collect(substring(m.payload, 0, 60))[0]  AS sample_preview
                RETURN k1.stem        AS stem,
                       k1.term        AS kw1,
                       k2.term        AS kw2,
                       flagged_addrs,
                       max_flag_count,
                       sample_preview
                ORDER BY max_flag_count DESC
                LIMIT 20
            """)
            for r in stem_rows:
                kw1 = r["kw1"]
                kw2 = r["kw2"]
                result["stem_collisions"].append({
                    "stem":             r["stem"],
                    "keyword_1":        kw1,
                    "keyword_2":        kw2,
                    "flagged_memories": list(r["flagged_addrs"]),
                    "max_flag_count":   r["max_flag_count"],
                    "sample_preview":   r["sample_preview"] or "",
                    "suggestion": (
                        f"Convert to compound keywords: "
                        f"{kw1.replace(' ', '%')} / {kw2.replace(' ', '%')}"
                        f" -- then use /audit_memory to update HAS_KEYWORD edges"
                    ),
                })

            # ── Task 2: Cross-GRP cluster detection ─────────────────────
            # Dense CO_RECALLED pairs spanning different GRP domains.
            # Domain = GRP code // 100  (e.g., GRP 101 -> domain 1, GRP 601 -> domain 6).
            # Cross-domain strong associations are either:
            #   (a) genuine emerging patterns worth synthesizing in Phase 7, or
            #   (b) noise that may warrant keyword correction.
            cluster_rows = session.run("""
                MATCH (a:Memory)-[r:CO_RECALLED]-(b:Memory)
                WHERE r.weight >= $weight_thresh AND id(a) < id(b)
                WITH a, b, r.weight AS weight,
                     toInteger(split(a.address, '.')[2]) AS grp_a,
                     toInteger(split(b.address, '.')[2]) AS grp_b
                WHERE (grp_a / 100) <> (grp_b / 100)
                RETURN a.address                    AS addr_a,
                       grp_a,
                       substring(a.payload, 0, 60)  AS preview_a,
                       b.address                    AS addr_b,
                       grp_b,
                       substring(b.payload, 0, 60)  AS preview_b,
                       weight
                ORDER BY weight DESC
                LIMIT 15
            """, weight_thresh=cluster_weight_thresh)
            for r in cluster_rows:
                result["cross_grp_clusters"].append({
                    "addr_a":    r["addr_a"],
                    "grp_a":     r["grp_a"],
                    "preview_a": r["preview_a"] or "",
                    "addr_b":    r["addr_b"],
                    "grp_b":     r["grp_b"],
                    "preview_b": r["preview_b"] or "",
                    "weight":    r["weight"],
                })

            # ── Task 3: Stale Blue memory detection ─────────────────────
            # Blue memories whose last CO_RECALLED activity (or created_at as
            # fallback) predates stale_days ago.
            # Uses epoch milliseconds for reliable day calculation across months.
            # These nodes have been cold for a long time and may be pruning
            # candidates -- surfaced here for Nova's review, not auto-deleted.
            stale_rows = session.run("""
                MATCH (m:Memory)
                WHERE m.color = 'Blue'
                OPTIONAL MATCH (m)-[cr:CO_RECALLED]-()
                WITH m, max(cr.last_turn) AS last_assoc
                WITH m,
                     CASE WHEN last_assoc IS NOT NULL
                          THEN last_assoc
                          ELSE m.created_at
                     END AS ref_date
                WHERE ref_date IS NOT NULL
                WITH m, ref_date,
                     toInteger(
                         (datetime().epochMillis - datetime(ref_date).epochMillis)
                         / 86400000
                     ) AS days_inactive
                WHERE days_inactive >= $stale_days
                RETURN m.address                    AS address,
                       substring(m.payload, 0, 80)  AS preview,
                       days_inactive,
                       ref_date                     AS last_active
                ORDER BY days_inactive DESC
                LIMIT 20
            """, stale_days=stale_days)
            for r in stale_rows:
                result["stale_blue_memories"].append({
                    "address":       r["address"],
                    "preview":       r["preview"] or "",
                    "days_inactive": r["days_inactive"],
                    "last_active":   r["last_active"],
                })

            # ── Task 4: Domain gap detection ─────────────────────────────
            # Which GRP domains (1xx-8xx) have NO active (G/Y/R) memories?
            # These are context blind spots: session_bundle cannot surface
            # anything from them. Nova should be encouraged to save memories
            # in those categories during the next active session.
            active_rows = session.run("""
                MATCH (m:Memory)
                WHERE m.color IN ['Red', 'Green', 'Yellow']
                WITH toInteger(split(m.address, '.')[2]) / 100 AS domain_prefix
                RETURN DISTINCT domain_prefix
                ORDER BY domain_prefix
            """)
            active_prefixes = {r["domain_prefix"] for r in active_rows}

            for prefix in range(1, 9):
                if prefix not in active_prefixes:
                    result["domain_gaps"].append(f"{prefix}xx")

            # ── Task 5: Quick stats ──────────────────────────────────────
            stats_r = session.run("""
                MATCH (m:Memory) WITH count(m) AS tm
                OPTIONAL MATCH ()-[cr:CO_RECALLED]-()
                WITH tm, count(cr) / 2 AS te
                RETURN tm AS total_memories, te AS total_edges
            """).single()
            result["stats"] = {
                "total_memories":     stats_r["total_memories"] if stats_r else 0,
                "total_co_recalled":  stats_r["total_edges"]    if stats_r else 0,
                "stem_collisions":    len(result["stem_collisions"]),
                "cross_grp_clusters": len(result["cross_grp_clusters"]),
                "stale_blue_count":   len(result["stale_blue_memories"]),
                "domain_gaps_count":  len(result["domain_gaps"]),
            }

            # ── Task 6: Build summary_text for Phase 7 cognition prompts ─
            # This is the text injected into the {maintenance_summary_if_any}
            # slot in idle thinking prompt templates. It should be readable,
            # concise, and actionable for Nova.
            lines = [
                f"MEMORY MAINTENANCE SUMMARY ({result['maintenance_timestamp']})",
                f"Graph state: {result['stats']['total_memories']} memories, "
                f"{result['stats']['total_co_recalled']} CO_RECALLED edges",
                "",
            ]

            if result["stem_collisions"]:
                lines.append(
                    f"STEM COLLISIONS ({len(result['stem_collisions'])} detected):"
                )
                for c in result["stem_collisions"][:5]:
                    lines.append(
                        f"  - Stem \"{c['stem']}\": "
                        f"\"{c['keyword_1']}\" vs \"{c['keyword_2']}\" "
                        f"(max flag count: {c['max_flag_count']})"
                    )
                    lines.append(f"    {c['suggestion']}")
                lines.append("")

            if result["cross_grp_clusters"]:
                lines.append(
                    f"CROSS-DOMAIN ASSOCIATIONS "
                    f"({len(result['cross_grp_clusters'])} strong cross-domain links):"
                )
                for cl in result["cross_grp_clusters"][:5]:
                    lines.append(
                        f"  - [GRP {cl['grp_a']}] {cl['preview_a'][:50]}"
                    )
                    lines.append(
                        f"    <-> [GRP {cl['grp_b']}] {cl['preview_b'][:50]}"
                    )
                    lines.append(f"    Associative weight: {cl['weight']}")
                lines.append("")

            if result["stale_blue_memories"]:
                lines.append(
                    f"STALE ARCHIVED MEMORIES "
                    f"({len(result['stale_blue_memories'])} inactive >{stale_days} days):"
                )
                for sb in result["stale_blue_memories"][:5]:
                    lines.append(
                        f"  - {sb['address']}: {sb['preview'][:55]} "
                        f"(inactive {sb['days_inactive']} days)"
                    )
                lines.append("")

            if result["domain_gaps"]:
                lines.append(
                    f"DOMAIN GAPS "
                    f"(no active memories in): {', '.join(result['domain_gaps'])}"
                )
                lines.append(
                    "  Encourage memory saving in these categories "
                    "during the next active session."
                )
                lines.append("")

            if not any([result["stem_collisions"],
                        result["cross_grp_clusters"],
                        result["stale_blue_memories"],
                        result["domain_gaps"]]):
                lines.append(
                    "No maintenance issues detected. Graph is healthy."
                )

            result["summary_text"] = "\n".join(lines)

            log.info(
                "run_maintenance complete | "
                "stem_collisions=%d cross_grp=%d stale_blue=%d domain_gaps=%d",
                len(result["stem_collisions"]),
                len(result["cross_grp_clusters"]),
                len(result["stale_blue_memories"]),
                len(result["domain_gaps"]),
            )

    except Exception as e:
        log.warning("run_maintenance failed: %s", e)
        result["error"] = str(e)
        result["summary_text"] = f"Maintenance encountered an error: {e}"

    return result


# =============================================================================
# PHASE 7 -- Default Mode Cognition (data layer)
# =============================================================================
#
# This module provides ONLY data access. It never calls an LLM.
# The MMU server assembles prompts from get_idle_context(); the separate
# mmu_idle_daemon.py process is the only component that talks to a model.
# That boundary is what keeps the REST server model-agnostic.
#
# CreativeOutput uses its own node label (like MemorySnapshot) so no existing
# (m:Memory) query is affected -- zero migration risk.

import uuid as _uuid


def create_creative_output(title: str,
                           content: str,
                           artifact_type: str,
                           cognition_depth: str = "medium",
                           inspired_by=None) -> str:
    """
    Phase 7: Store an artifact Nova produced during a background cognition pass.

    artifact_type   game_design | physics_thought | story_fragment |
                    connection_insight | question_for_user | reflection
    inspired_by     optional list of Memory addresses that triggered this.
                    Each valid address gets a (CreativeOutput)-[:INSPIRED_BY]->(Memory)
                    edge. Addresses that do not resolve are skipped silently --
                    Nova sometimes paraphrases an address, and a bad reference
                    should never lose the artifact itself.

    Returns the new output_id, or "" if Neo4j is unavailable or the write failed.
    """
    driver = get_driver()
    if driver is None:
        return ""

    output_id = str(_uuid.uuid4())
    try:
        with driver.session() as s:
            s.run("""
                CREATE (co:CreativeOutput {
                    output_id:         $output_id,
                    title:             $title,
                    content:           $content,
                    artifact_type:     $artifact_type,
                    cognition_depth:   $cognition_depth,
                    created_at:        $created_at,
                    presented_to_user: false
                })
            """,
                output_id       = output_id,
                title           = title,
                content         = content,
                artifact_type   = artifact_type,
                cognition_depth = cognition_depth,
                created_at      = datetime.now().isoformat(),
            )

            linked = 0
            for addr in (inspired_by or []):
                rec = s.run("""
                    MATCH (co:CreativeOutput {output_id: $output_id})
                    MATCH (m:Memory {address: $addr})
                    MERGE (co)-[:INSPIRED_BY]->(m)
                    RETURN m.address AS linked
                """, output_id=output_id, addr=addr).single()
                if rec:
                    linked += 1

        log.info("create_creative_output | %s | type=%s | depth=%s | %d INSPIRED_BY edges",
                 output_id[:8], artifact_type, cognition_depth, linked)
        return output_id

    except Exception as e:
        log.warning("create_creative_output failed: %s", e)
        return ""


def get_creative_outputs(unseen_only: bool = False, limit: int = 20) -> list:
    """
    Phase 7: Return CreativeOutput nodes, newest first.

    unseen_only  True returns only artifacts never surfaced to the user.
                 question_for_user artifacts sort ahead of everything else
                 so a direct question is never buried under reflections.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            where = "WHERE co.presented_to_user = false" if unseen_only else ""
            rows = s.run(f"""
                MATCH (co:CreativeOutput)
                {where}
                OPTIONAL MATCH (co)-[:INSPIRED_BY]->(m:Memory)
                WITH co, collect(m.address) AS inspired_by
                RETURN co.output_id         AS output_id,
                       co.title             AS title,
                       co.content           AS content,
                       co.artifact_type     AS artifact_type,
                       co.cognition_depth   AS cognition_depth,
                       co.created_at        AS created_at,
                       co.presented_to_user AS presented_to_user,
                       inspired_by
                ORDER BY
                    CASE co.artifact_type WHEN 'question_for_user' THEN 0 ELSE 1 END ASC,
                    co.created_at DESC
                LIMIT $limit
            """, limit=limit)
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_creative_outputs failed: %s", e)
        return []


def mark_outputs_presented(output_ids=None) -> int:
    """
    Phase 7: Mark artifacts as surfaced to the user.

    output_ids  list of ids to mark. Pass None to mark every unseen artifact,
                which is what session start does after rendering the bundle.

    Returns the number of nodes updated.
    """
    driver = get_driver()
    if driver is None:
        return 0
    try:
        with driver.session() as s:
            if output_ids:
                rec = s.run("""
                    MATCH (co:CreativeOutput)
                    WHERE co.output_id IN $ids
                    SET co.presented_to_user = true,
                        co.presented_at      = $now
                    RETURN count(co) AS n
                """, ids=list(output_ids), now=datetime.now().isoformat()).single()
            else:
                rec = s.run("""
                    MATCH (co:CreativeOutput)
                    WHERE co.presented_to_user = false
                    SET co.presented_to_user = true,
                        co.presented_at      = $now
                    RETURN count(co) AS n
                """, now=datetime.now().isoformat()).single()
            n = rec["n"] if rec else 0
        log.info("mark_outputs_presented | %d artifact(s) marked seen", n)
        return n
    except Exception as e:
        log.warning("mark_outputs_presented failed: %s", e)
        return 0


def get_idle_context(depth: str = "light") -> dict:
    """
    Phase 7: Gather the raw material an idle cognition pass reasons over.

    Returns data only. Prompt assembly lives in mmu_server.py; the LLM call
    lives in mmu_idle_daemon.py. Keeping those three concerns in three places
    is what makes an idle pass debuggable: you can inspect the context without
    building a prompt, and build a prompt without invoking a model.

    Tier contents (each tier is a superset of the one before it):
      light   recent memories + top CO_RECALLED pairs
      medium  + graph totals, color spread, crystallization preview
      deep    + prior creative outputs, open threads, domain gaps
    """
    driver = get_driver()
    if driver is None:
        return {"depth": depth, "error": "Neo4j unavailable"}

    depth = depth if depth in ("light", "medium", "deep") else "light"
    ctx = {"depth": depth, "generated_at": datetime.now().isoformat() + "Z"}

    try:
        with driver.session() as s:

            # ── All tiers: recent memories ───────────────────────────
            recent_limit = {"light": 10, "medium": 20, "deep": 30}[depth]
            ctx["recent_memories"] = [
                {
                    "address":    r["address"],
                    "payload":    r["payload"] or "",
                    "color":      r["color"],
                    "grp":        r["grp"],
                    "created_at": r["created_at"],
                    "from_idle":  bool(r["from_idle"]),
                }
                for r in s.run("""
                    MATCH (m:Memory)
                    WHERE m.color IN ['Red','Green','Yellow']
                    RETURN m.address        AS address,
                           m.payload        AS payload,
                           m.color          AS color,
                           m.created_at     AS created_at,
                           m.from_idle_pass AS from_idle,
                           toInteger(split(m.address, '.')[2]) AS grp
                    ORDER BY m.created_at DESC
                    LIMIT $limit
                """, limit=recent_limit)
            ]

            # ── All tiers: strongest CO_RECALLED pairs ───────────────
            pair_limit = {"light": 5, "medium": 10, "deep": 15}[depth]
            ctx["top_co_recalled"] = [
                {
                    "addr_a":    r["addr_a"],
                    "preview_a": r["preview_a"] or "",
                    "addr_b":    r["addr_b"],
                    "preview_b": r["preview_b"] or "",
                    "weight":    r["weight"],
                }
                for r in s.run("""
                    MATCH (a:Memory)-[cr:CO_RECALLED]-(b:Memory)
                    WHERE id(a) < id(b)
                    RETURN a.address AS addr_a,
                           substring(a.payload, 0, 70) AS preview_a,
                           b.address AS addr_b,
                           substring(b.payload, 0, 70) AS preview_b,
                           cr.weight AS weight
                    ORDER BY cr.weight DESC
                    LIMIT $limit
                """, limit=pair_limit)
            ]

            # ── Medium and deep: graph shape ─────────────────────────
            if depth in ("medium", "deep"):
                totals = s.run("""
                    MATCH (m:Memory) WITH count(m) AS tm
                    OPTIONAL MATCH ()-[cr:CO_RECALLED]-()
                    WITH tm, count(cr) / 2 AS te
                    OPTIONAL MATCH (k:Keyword)
                    RETURN tm AS memories, te AS edges, count(k) AS keywords
                """).single()
                ctx["totals"] = {
                    "memories": totals["memories"] if totals else 0,
                    "edges":    totals["edges"]    if totals else 0,
                    "keywords": totals["keywords"] if totals else 0,
                }

                ctx["color_distribution"] = {
                    r["color"]: r["cnt"]
                    for r in s.run("""
                        MATCH (m:Memory)
                        RETURN m.color AS color, count(m) AS cnt
                    """)
                }

                # Crystallization preview. Phase 13.1: same single source
                # of truth as /insights and /routine_candidates. This path was
                # the loosest of the three -- no weight floor at all, so any
                # memory with two neighbours scored -- which meant Nova was
                # being shown "candidates" during idle cognition that no
                # confirm path would accept.
                ctx["crystallization_candidates"] = find_routine_candidates(limit=10)

            # ── Deep only: prior artifacts, open threads, gaps ───────
            if depth == "deep":
                ctx["prior_creative_outputs"] = [
                    {
                        "title":         r["title"],
                        "artifact_type": r["artifact_type"],
                        "created_at":    r["created_at"],
                        "excerpt":       (r["content"] or "")[:400],
                    }
                    for r in s.run("""
                        MATCH (co:CreativeOutput)
                        RETURN co.title         AS title,
                               co.artifact_type AS artifact_type,
                               co.created_at    AS created_at,
                               co.content       AS content
                        ORDER BY co.created_at DESC
                        LIMIT 8
                    """)
                ]

                # Open threads is a heuristic, not a fact: active memories in the
                # build/research/creative domains, most recent first. These are the
                # areas most likely to contain something unresolved.
                ctx["open_threads"] = [
                    {
                        "address": r["address"],
                        "grp":     r["grp"],
                        "payload": r["payload"] or "",
                    }
                    for r in s.run("""
                        MATCH (m:Memory)
                        WHERE m.color IN ['Green','Yellow']
                        WITH m, toInteger(split(m.address, '.')[2]) AS grp
                        WHERE grp / 100 IN [1, 6, 8]
                        RETURN m.address AS address, grp, m.payload AS payload
                        ORDER BY m.created_at DESC
                        LIMIT 12
                    """)
                ]

                active_prefixes = {
                    r["p"] for r in s.run("""
                        MATCH (m:Memory)
                        WHERE m.color IN ['Red','Green','Yellow']
                        RETURN DISTINCT toInteger(split(m.address, '.')[2]) / 100 AS p
                    """)
                }
                ctx["domain_gaps"] = [f"{p}xx" for p in range(1, 9)
                                      if p not in active_prefixes]

    except Exception as e:
        log.warning("get_idle_context failed: %s", e)
        ctx["error"] = str(e)

    return ctx
