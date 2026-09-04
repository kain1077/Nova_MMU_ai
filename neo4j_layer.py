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
import time
import uuid
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
    # Phase 12 -- Skill nodes (procedural memory crystallization)
    "CREATE CONSTRAINT skill_id      IF NOT EXISTS FOR (sk:Skill) REQUIRE sk.skill_id IS UNIQUE",
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

    Keeps Skill nodes: they are a separate abstraction and erasing memories
    should not silently destroy crystallized skills. Callers wanting a truly
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
# Memories that cluster densely and cohere by domain compress into a Skill,
# mirroring declarative memory becoming procedural. Source memories are never
# deleted -- they are demoted to Blue and become the skill's root system.
#
# TWO-STEP BY DESIGN, and the split is structural rather than conventional:
#
#   find_skill_candidates()  reads. Writes nothing. Safe to poll.
#   crystallize_skill()      writes, atomically, and only on explicit human
#                            confirmation.
#
# This is the one phase that restructures Nova's own memory rather than adding
# capability beside it. Getting it wrong is not a bug, it is an identity-model
# change nobody approved. crystallize_skill() is deliberately NOT reachable
# from mmu_idle_daemon.py's IDLE_TOOLS.


def find_skill_candidates(min_cluster=3, min_pairwise_norm=0.6, limit=10):
    """
    Clusters where EVERY pair is densely co-recalled -- not merely anchored on
    one popular node.

    The preview queries in get_insights() and get_idle_context() answer a
    different question: "which single memory has strong neighbours." That is
    the right shape for showing a preview and the wrong shape for deciding to
    crystallize, because a hub with many weak-to-each-other neighbours would
    qualify while not being a coherent skill at all. This checks mutual
    density: all pairs among the candidate set clear the floor.

    Weights are normalized against the graph maximum, so min_pairwise_norm is
    on a 0-1 scale and means the same thing as skill_score elsewhere.

    Returns [] when nothing qualifies. On a young graph that is the expected
    answer, not a failure -- do not lower the threshold to manufacture one.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            rec = s.run("MATCH ()-[r:CO_RECALLED]-() RETURN max(r.weight) AS mx").single()
            max_w = float((rec and rec["mx"]) or 0.0) or 1.0
            floor = float(min_pairwise_norm) * max_w

            # Triangles first: the smallest cluster that can be mutually dense.
            # Larger clusters are grown from a qualifying triangle rather than
            # enumerated combinatorially, which would explode on a 954-node graph.
            rows = s.run("""
                MATCH (a:Memory)-[r1:CO_RECALLED]-(b:Memory)-[r2:CO_RECALLED]-(c:Memory)
                MATCH (a)-[r3:CO_RECALLED]-(c)
                WHERE a.address < b.address AND b.address < c.address
                  AND r1.weight >= $floor AND r2.weight >= $floor AND r3.weight >= $floor
                  AND a.color <> 'Blue' AND b.color <> 'Blue' AND c.color <> 'Blue'
                  AND a.src_type <> 2 AND b.src_type <> 2 AND c.src_type <> 2
                RETURN [a.address, b.address, c.address] AS members,
                       (r1.weight + r2.weight + r3.weight) / 3.0 AS avg_weight,
                       [a.payload, b.payload, c.payload] AS payloads,
                       [toInteger(split(a.address,'.')[2]),
                        toInteger(split(b.address,'.')[2]),
                        toInteger(split(c.address,'.')[2])] AS grps
                ORDER BY avg_weight DESC
                LIMIT $lim
            """, floor=floor, lim=int(limit) * 3)

            out, seen = [], set()
            for r in rows:
                members = list(r["members"])
                if len(members) < int(min_cluster):
                    continue
                key = tuple(sorted(members))
                if key in seen:
                    continue
                seen.add(key)

                grps = [g for g in (r["grps"] or []) if g is not None]
                # Coherence here is whole-cluster agreement on a GRP domain,
                # not the neighbour-fraction the preview queries compute. A
                # skill spanning four domains is not a skill.
                domains = [g // 100 for g in grps]
                coherence = (max(domains.count(d) for d in set(domains)) / len(domains)) if domains else 0.0
                avg_norm = float(r["avg_weight"] or 0.0) / max_w

                out.append({
                    "members":       members,
                    "avg_weight":    round(float(r["avg_weight"] or 0.0), 3),
                    "avg_weight_norm": round(avg_norm, 4),
                    "grp_coherence": round(coherence, 3),
                    "skill_score":   round((avg_norm * 0.5) + (coherence * 0.5), 4),
                    "grps":          grps,
                    "previews":      [(p or "")[:90] for p in (r["payloads"] or [])],
                })
                if len(out) >= int(limit):
                    break

            out.sort(key=lambda d: -d["skill_score"])
            return out
    except Exception as e:
        log.warning(f"find_skill_candidates failed: {e}")
        return []


def crystallize_skill(member_addresses, trigger, procedure, confidence=0.0):
    """
    The confirm step. ONE transaction, all-or-nothing.

    Creates the Skill, wires PROCEDURALIZED_FROM from every member, and demotes
    each member to Blue. Members are never deleted -- per the roadmap they are
    the skill's root system, and a memory that has been compressed is still the
    evidence the compression was drawn from.

    Returns the skill dict, or None on failure with nothing half-applied.
    """
    driver = get_driver()
    if driver is None or not member_addresses:
        return None

    skill_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    try:
        with driver.session() as s:
            # execute_write gives a real transaction: a failure part-way rolls
            # back rather than leaving memories demoted with no Skill to show
            # for it.
            def _tx(tx):
                found = tx.run("""
                    MATCH (m:Memory) WHERE m.address IN $addrs
                    RETURN count(m) AS n
                """, addrs=list(member_addresses)).single()["n"]
                if found != len(member_addresses):
                    raise ValueError(
                        f"expected {len(member_addresses)} members, matched {found}"
                    )

                tx.run("""
                    CREATE (sk:Skill {
                        skill_id: $sid, trigger: $trigger, procedure: $procedure,
                        confidence: $conf, invocation_count: 0, last_invoked: null,
                        created_at: $now, status: 'active'
                    })
                """, sid=skill_id, trigger=trigger, procedure=procedure,
                     conf=float(confidence), now=now)

                tx.run("""
                    MATCH (sk:Skill {skill_id: $sid})
                    MATCH (m:Memory) WHERE m.address IN $addrs
                    MERGE (m)-[:PROCEDURALIZED_FROM]->(sk)
                    SET m.color = 'Blue'
                """, sid=skill_id, addrs=list(member_addresses))

                # Clear any proposal marker for these members.
                tx.run("""
                    MATCH (m:Memory) WHERE m.address IN $addrs
                    REMOVE m.skill_candidate_id
                """, addrs=list(member_addresses))
                return True

            s.execute_write(_tx)

        log.info("Crystallized skill %s from %d memories", skill_id, len(member_addresses))
        return {
            "skill_id": skill_id, "trigger": trigger, "procedure": procedure,
            "confidence": float(confidence), "status": "active",
            "created_at": now, "members": list(member_addresses),
        }
    except Exception as e:
        log.warning(f"crystallize_skill failed (nothing applied): {e}")
        return None


def link_skills(child_id, parent_id):
    """
    Phase 13: child EXTENDS_SKILL parent.

    Refuses to create a cycle. A skill tree with a cycle is not a tree, and the
    deprecation guard below walks children -- a cycle would make that walk
    non-terminating.
    """
    driver = get_driver()
    if driver is None:
        return False
    if child_id == parent_id:
        raise ValueError("a skill cannot extend itself")
    try:
        with driver.session() as s:
            # Would the new edge close a loop? True if parent already reaches
            # child by following EXTENDS_SKILL upward.
            cyc = s.run("""
                MATCH (p:Skill {skill_id: $pid}), (c:Skill {skill_id: $cid})
                RETURN EXISTS((p)-[:EXTENDS_SKILL*1..]->(c)) AS cycles
            """, pid=parent_id, cid=child_id).single()
            if cyc and cyc["cycles"]:
                raise ValueError("that link would create a cycle in the skill tree")

            rec = s.run("""
                MATCH (c:Skill {skill_id: $cid}), (p:Skill {skill_id: $pid})
                MERGE (c)-[:EXTENDS_SKILL]->(p)
                RETURN count(*) AS n
            """, cid=child_id, pid=parent_id).single()
            return bool(rec and rec["n"])
    except ValueError:
        raise
    except Exception as e:
        log.warning(f"link_skills failed: {e}")
        return False


def get_skill_tree(root_skill_id=None):
    """
    Walk EXTENDS_SKILL. Returns the tree under root_skill_id, or the whole
    forest (every skill with no parent) when no root is given.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (sk:Skill)
                OPTIONAL MATCH (sk)-[:EXTENDS_SKILL]->(p:Skill)
                RETURN sk.skill_id AS skill_id, sk.trigger AS trigger,
                       sk.status AS status, sk.confidence AS confidence,
                       collect(DISTINCT p.skill_id) AS parents
            """)
            nodes = {}
            for r in rows:
                d = dict(r)
                d["parents"]  = [p for p in (d["parents"] or []) if p]
                d["children"] = []
                nodes[d["skill_id"]] = d

            for sid, d in nodes.items():
                for p in d["parents"]:
                    if p in nodes:
                        nodes[p]["children"].append(sid)

            def build(sid, seen):
                if sid in seen:          # defensive; link_skills blocks cycles
                    return {"skill_id": sid, "cycle": True}
                seen = seen | {sid}
                n = nodes[sid]
                return {
                    "skill_id":   sid,
                    "trigger":    n["trigger"],
                    "status":     n["status"],
                    "confidence": n["confidence"],
                    "children":   [build(c, seen) for c in n["children"]],
                }

            if root_skill_id:
                if root_skill_id not in nodes:
                    return []
                return [build(root_skill_id, set())]
            roots = [sid for sid, d in nodes.items() if not d["parents"]]
            return [build(r, set()) for r in roots]
    except Exception as e:
        log.warning(f"get_skill_tree failed: {e}")
        return []


def deprecate_skill(skill_id):
    """
    Phase 13: set a Skill to deprecated, but ONLY if no active child extends it.

    The roadmap states a parent cannot be deprecated while a child is active.
    Enforced here as a real check rather than a comment: a silent success would
    leave a live skill extending a dead parent, and the tree would be wrong in a
    way nothing later would notice.

    Returns (True, None) or (False, reason).
    """
    driver = get_driver()
    if driver is None:
        return False, "Neo4j unavailable"
    try:
        with driver.session() as s:
            exists = s.run("MATCH (sk:Skill {skill_id:$sid}) RETURN sk.status AS st",
                           sid=skill_id).single()
            if not exists:
                return False, "no such skill"

            blockers = [r["cid"] for r in s.run("""
                MATCH (child:Skill)-[:EXTENDS_SKILL]->(sk:Skill {skill_id: $sid})
                WHERE child.status = 'active'
                RETURN child.skill_id AS cid
            """, sid=skill_id)]
            if blockers:
                return False, (
                    f"{len(blockers)} active child skill(s) still extend this one: "
                    f"{', '.join(blockers)}. Deprecate or reparent them first."
                )

            s.run("MATCH (sk:Skill {skill_id:$sid}) SET sk.status = 'deprecated'",
                  sid=skill_id)
            return True, None
    except Exception as e:
        log.warning(f"deprecate_skill failed: {e}")
        return False, str(e)


def propose_meta_skill(min_shared=2):
    """
    Phase 13: skill pairs sharing >= min_shared source memories are candidates
    for a common parent.

    Proposes only. Creating the meta-skill is a human decision, same discipline
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
                MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(a:Skill)
                MATCH (m)-[:PROCEDURALIZED_FROM]->(b:Skill)
                WHERE a.skill_id < b.skill_id
                WITH a, b, count(DISTINCT m) AS shared
                WHERE shared >= $ms
                RETURN a.skill_id AS skill_a, a.trigger AS trigger_a,
                       b.skill_id AS skill_b, b.trigger AS trigger_b,
                       shared
                ORDER BY shared DESC
            """, ms=int(min_shared))
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"propose_meta_skill failed: {e}")
        return []


def get_skills(status=None):
    """List Skill nodes with their source-memory counts."""
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (sk:Skill)
                WHERE $status IS NULL OR sk.status = $status
                OPTIONAL MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk)
                OPTIONAL MATCH (sk)-[:EXTENDS_SKILL]->(parent:Skill)
                RETURN sk.skill_id AS skill_id, sk.trigger AS trigger,
                       sk.procedure AS procedure, sk.confidence AS confidence,
                       sk.status AS status, sk.created_at AS created_at,
                       sk.invocation_count AS invocation_count,
                       count(DISTINCT m) AS source_memories,
                       collect(DISTINCT parent.skill_id) AS extends
                ORDER BY sk.created_at DESC
            """, status=status)
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"get_skills failed: {e}")
        return []


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
    for crystallization candidate detection and skill proposal generation.

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

        # 8. Phase 11 preview: crystallization candidates
        #
        # Composite skill_score:
        #   avg_co_recall_weight * 0.35
        #   + min(connections / 10, 1.0) * 0.40
        #   + grp_coherence * 0.25
        #
        # grp_coherence = fraction of direct CO_RECALLED neighbors
        #                 sharing the same GRP code as the candidate node.
        # Threshold for human-confirmation proposal: skill_score >= 0.70

        # Phase 12 fix: avg_weight is normalized against the observed maximum
        # before scoring. It was used raw, and raw CO_RECALLED weights are
        # unbounded integers (6.04 observed here), so skill_score routinely
        # exceeded 1.0 -- 14 of 15 candidates did, topping out at 2.57. That
        # made the roadmap's "propose at >= 0.70" threshold meaningless on this
        # endpoint and made /insights disagree with /idle_prompt about the same
        # candidates. get_idle_context() already normalized correctly; this is
        # that formula, ported so both paths report the same thing.
        _raw = [
            {
                "address":       r["address"],
                "connections":   r["connections"] or 0,
                "avg_weight":    r["avg_weight"] or 0.0,
                "neighbor_grps": r["neighbor_grps"] or [],
                "self_grp":      r["self_grp"] or 0,
                "color":         r["color"],
                "preview":       r["preview"] or "",
            }
            for r in session.run("""
                MATCH (a:Memory)-[cr:CO_RECALLED]-(b:Memory)
                WHERE cr.weight > 0.35
                WITH a,
                     count(cr)                                         AS connections,
                     avg(cr.weight)                                    AS avg_weight,
                     collect(toInteger(split(b.address, '.')[2]))      AS neighbor_grps
                WHERE connections >= 2
                RETURN a.address                              AS address,
                       connections,
                       round(avg_weight * 10000) / 10000      AS avg_weight,
                       a.color                                AS color,
                       toInteger(split(a.address, '.')[2])    AS self_grp,
                       neighbor_grps,
                       substring(a.payload, 0, 70)            AS preview
                ORDER BY connections DESC, avg_weight DESC
                LIMIT 15
            """)
        ]

        max_w = max((c["avg_weight"] for c in _raw), default=0.0) or 1.0

        crystal_candidates = []
        for c in _raw:
            neighbors = c["neighbor_grps"]
            coherence = (
                sum(1 for g in neighbors if g == c["self_grp"]) / len(neighbors)
                if neighbors else 0.0
            )
            skill_score = ((c["avg_weight"] / max_w) * 0.35) \
                          + (min(c["connections"] / 10.0, 1.0) * 0.40) \
                          + (coherence * 0.25)
            crystal_candidates.append({
                "address":       c["address"],
                "connections":   c["connections"],
                "avg_weight":    c["avg_weight"],
                "grp_coherence": round(coherence, 3),
                "skill_score":   round(skill_score, 4),
                "color":         c["color"],
                "preview":       c["preview"],
            })

        crystal_candidates.sort(key=lambda x: x["skill_score"], reverse=True)

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

                # Crystallization preview -- normalized skill_score.
                # Raw CO_RECALLED weights are integers (observed 1-21), so the
                # avg_weight term is divided by the observed max before scoring.
                raw = [
                    {
                        "address":       r["address"],
                        "connections":   r["connections"] or 0,
                        "avg_weight":    r["avg_weight"] or 0.0,
                        "neighbor_grps": r["neighbor_grps"] or [],
                        "self_grp":      r["self_grp"] or 0,
                        "preview":       r["preview"] or "",
                    }
                    for r in s.run("""
                        MATCH (a:Memory)-[cr:CO_RECALLED]-(b:Memory)
                        WITH a, count(cr) AS connections, avg(cr.weight) AS avg_weight,
                             collect(toInteger(split(b.address, '.')[2])) AS neighbor_grps
                        WHERE connections >= 2
                        RETURN a.address AS address,
                               connections,
                               avg_weight,
                               neighbor_grps,
                               toInteger(split(a.address, '.')[2]) AS self_grp,
                               substring(a.payload, 0, 70) AS preview
                        ORDER BY connections DESC
                        LIMIT 10
                    """)
                ]
                max_w = max((c["avg_weight"] for c in raw), default=0.0) or 1.0
                candidates = []
                for c in raw:
                    nb = c["neighbor_grps"]
                    coherence = (sum(1 for g in nb if g == c["self_grp"]) / len(nb)) if nb else 0.0
                    score = ((c["avg_weight"] / max_w) * 0.35) \
                            + (min(c["connections"] / 10.0, 1.0) * 0.40) \
                            + (coherence * 0.25)
                    candidates.append({
                        "address":       c["address"],
                        "connections":   c["connections"],
                        "grp_coherence": round(coherence, 3),
                        "skill_score":   round(score, 4),
                        "preview":       c["preview"],
                    })
                candidates.sort(key=lambda x: x["skill_score"], reverse=True)
                ctx["crystallization_candidates"] = candidates

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
