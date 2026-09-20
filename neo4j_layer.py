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
SKILL_EMBEDDING_INDEX = "skill_embedding"

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
    # Phase 13.1 -- queued crystallization proposals awaiting human review.
    # member_key is unique so a repeated sweep MERGEs onto the same proposal
    # instead of stacking duplicates of a cluster that is simply still dense.
    "CREATE CONSTRAINT proposal_id   IF NOT EXISTS FOR (p:SkillProposal) REQUIRE p.proposal_id IS UNIQUE",
    "CREATE CONSTRAINT proposal_key  IF NOT EXISTS FOR (p:SkillProposal) REQUIRE p.member_key IS UNIQUE",
    # Monotonic counters (currently just 'con'). One node per counter name;
    # the constraint is what stops a race from creating two of the same one.
    "CREATE CONSTRAINT counter_name  IF NOT EXISTS FOR (c:Counter) REQUIRE c.name IS UNIQUE",
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

                # Phase 13.2: the same treatment for Skill. A separate index
                # rather than a shared one -- skills and memories are ranked
                # against each other only after both are retrieved, and mixing
                # labels in one ANN index would make "top k memories" and "top
                # k skills" compete for the same k.
                s.run(f"""
                    CREATE VECTOR INDEX {SKILL_EMBEDDING_INDEX} IF NOT EXISTS
                    FOR (sk:Skill) ON (sk.embedding)
                    OPTIONS {{ indexConfig: {{
                        `vector.dimensions`: {int(EMBEDDING_DIM)},
                        `vector.similarity_function`: 'cosine'
                    }} }}
                """)
                log.info("Vector index %s ensured", SKILL_EMBEDDING_INDEX)
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
        log.warning(f"Neo4j write failed (index unaffected): {e}")


def write_recall_edges(recalled_addresses, session_id):
    """
    After a recall hit:
      - RECALLED_IN edges from each Memory to the Session node
      - CO_RECALLED edges between every pair of co-recalled memories that are
        neither pinned nor compressed into a Skill
        (bidirectional MERGE, weight increments each time)
    Called by recall() after the aging pass.

    Crystallized members are excluded from PAIRING, and that exclusion is the
    point of crystallizing. The roadmap's stated purpose is that a hot path
    converts into a skill "rather than accumulating recall-weight without
    bound forever" -- but this function paired Blue like any other colour, so a
    crystallized cluster went on thickening its own edges on every recall,
    exactly as if it had never been compressed. Two of the three highest-degree
    hubs in the graph this was found on were crystallized members.

    Note it is MEMBERSHIP that excludes, not colour. Reading it off Blue would
    also silence archived memories, which are meant to re-warm when they
    resurface, and would still miss a member that had already leaked back to
    Yellow. Membership also makes this independent of when skills are matched:
    this runs before the caller knows which skills matched, and does not need
    to know.

    RECALLED_IN is still written for members. "This surfaced during this
    session" stays true, and it is episodic history rather than the weight
    that biases retrieval.
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

            # Step 1: decide who may pair, in ONE query.
            #
            # This was a round trip per recalled address -- a fresh Cypher
            # query each, on the path that runs on every single recall. The
            # membership check rides along in the same pass rather than
            # doubling that count.
            #
            # A node absent from Neo4j returns no row and is still allowed to
            # pair, which preserves the old behaviour: not yet written is not
            # the same as pinned.
            state = {}
            for r in s.run("""
                MATCH (m:Memory) WHERE m.address IN $addrs
                RETURN m.address AS address,
                       m.color   AS color,
                       EXISTS {
                           MATCH (m)-[:PROCEDURALIZED_FROM]->(sk:Skill)
                           WHERE sk.status <> 'deprecated'
                       } AS in_skill
            """, addrs=list(recalled_addresses)):
                state[r["address"]] = (r["color"], bool(r["in_skill"]))

            non_pinned, compressed = [], []
            for addr in recalled_addresses:
                color, in_skill = state.get(addr, (None, False))
                if color == "Red":
                    continue
                if in_skill:
                    compressed.append(addr)
                    continue
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
                 f"{len(compressed)} compressed into skills | "
                 f"{pairs_written} CO_RECALLED edges written")

    except Exception as e:
        log.warning(f"Neo4j recall edges failed (recall unaffected): {e}")


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

    Colour is the only thing written. Blue IS the archived state -- there is no
    separate `archived` property, and this docstring used to claim one.

    Prefer write_color_updates_batch() for the aging pass, which is the only
    caller that has more than one update in hand. This single-update form
    remains for one-off changes.

    Returns THE ADDRESS THE NODE CARRIES AFTERWARDS, or None if nothing was
    written at all. The caller must key its own index update off that return
    value rather than off `new_address`.

    This used to return nothing, and _age_memories() renamed the v2 index
    unconditionally afterwards -- so any rewrite Neo4j declined still moved the
    card. The address is not a free identifier: USE is encoded into it, so two
    memories sharing a CON (duplicates from the old count()+1 numbering) can
    CONVERGE on one address as their USE counters meet. m.address is UNIQUE, so
    the second one's rename is rejected here while the index went ahead --
    which is exactly how a memory ends up in the graph, counted by every query,
    and absent from the read path.

    The collision is now detected rather than raised: OPTIONAL MATCH looks for
    an occupant and the address is only moved when there is none. The colour
    change still lands either way, because the colour is not part of the
    contested identity. A memory that loses the race simply keeps its old
    address -- its USE counter fails to advance this pass, which costs nothing
    and is visible in the log.
    """
    s = neo4j_session()
    if s is None:
        return None
    try:
        with s:
            if old_address == new_address:
                # Just color change
                rec = s.run("""
                    MATCH (m:Memory {address: $addr})
                    SET m.color = $color
                    RETURN m.address AS address
                """, addr=old_address, color=new_color).single()
            else:
                # Address changed — move it only if the target is free.
                # FOREACH over a one-or-zero element list is the conditional
                # write: no occupant, one SET; occupant, none.
                rec = s.run("""
                    MATCH (old:Memory {address: $old_addr})
                    OPTIONAL MATCH (clash:Memory {address: $new_addr})
                    SET old.color = $new_color
                    FOREACH (_ IN CASE WHEN clash IS NULL THEN [1] ELSE [] END |
                        SET old.address = $new_addr)
                    RETURN old.address AS address
                """, old_addr=old_address, new_addr=new_address,
                     new_color=new_color).single()
            if rec is None:
                # No node at old_address. Nothing was written; the caller must
                # not touch its index either.
                log.warning("Neo4j color update found no node at %s", old_address)
                return None
            landed = rec["address"]
            if landed != new_address:
                log.warning(
                    "Address %s could not move to %s -- that address is already "
                    "taken. Colour applied, USE counter held back.",
                    old_address, new_address
                )
            return landed
    except Exception as e:
        log.warning(f"Neo4j color update failed: {e}")
        return None


def write_color_updates_batch(updates):
    """
    Apply many aging updates in ONE round trip.

    `updates` is a list of (old_address, new_address, new_color). Returns
    {old_address: landed_address} containing only the rows that actually
    wrote -- an address missing from the result means no node was found, which
    is the batch equivalent of write_color_update() returning None, and the
    caller must leave its index alone for that memory.

    WHY THIS EXISTS. The aging pass runs on EVERY recall and walks every
    memory in the graph; each one whose USE counter advances needed an address
    rewrite. Done one at a time, that was a separate Cypher query AND a
    separate Bolt session per memory -- neo4j_session() opens a new one per
    call -- so a single recall on a 500-memory graph could issue 500 sequential
    round trips before returning. The counters clamp at ARCHIVE_THRESH so it
    settles, but every newly added memory restarts its own climb, and a growing
    graph therefore never fully quiesces.

    The collision rule is unchanged and still enforced here, not just by the
    caller's in-memory pre-check. UNWIND processes rows in order within one
    transaction, and each row's OPTIONAL MATCH sees the writes of the rows
    before it -- so a row moving INTO an address another row is vacating
    behaves exactly as it did when these were sequential calls. A row that
    loses the race keeps its old address, its colour still lands, and the
    returned map says where it actually ended up.
    """
    if not updates:
        return {}

    s = neo4j_session()
    if s is None:
        return {}

    rows = [{"old_addr": o, "new_addr": n, "new_color": c} for o, n, c in updates]
    try:
        with s:
            # MATCH rather than OPTIONAL MATCH on `old`: a row whose node is
            # gone should produce no result row, so the caller sees it as
            # "nothing written" and leaves its index untouched.
            #
            # When old_addr == new_addr the OPTIONAL MATCH finds the node
            # itself, so the address SET is skipped -- correct, since it is
            # already the address being asked for -- and the colour still
            # lands.
            result = s.run("""
                UNWIND $rows AS row
                MATCH (old:Memory {address: row.old_addr})
                OPTIONAL MATCH (clash:Memory {address: row.new_addr})
                SET old.color = row.new_color
                FOREACH (_ IN CASE WHEN clash IS NULL THEN [1] ELSE [] END |
                    SET old.address = row.new_addr)
                RETURN row.old_addr AS requested, old.address AS landed
            """, rows=rows)
            landed = {r["requested"]: r["landed"] for r in result}
    except Exception as e:
        log.warning(f"Neo4j batched colour update failed: {e}")
        return {}

    missing = len(rows) - len(landed)
    if missing:
        log.warning("Neo4j colour batch: %d of %d addresses matched no node",
                    missing, len(rows))
    held = [(o, n) for o, n, _c in updates
            if o in landed and landed[o] != n]
    if held:
        log.warning(
            "Neo4j colour batch: %d address(es) could not move -- target taken. "
            "Colour applied, USE counter held back. First: %s -> %s",
            len(held), held[0][0], held[0][1])
    return landed


def write_addr_rename(old_address, new_address):
    """
    Phase 6.5: Rename a Memory node's address without changing color or any
    other property. Used when a valence update changes only the ~VAL segment.

    Returns the address the node carries afterwards, or None if nothing was
    written. Same contract, and same reason, as write_color_update(): the v2
    index must only follow a move the graph actually made. ~VAL is a smaller
    target than USE -- rating two memories the same way is not enough to
    collide, they would also have to share a CON -- but the seam is identical
    and there is no reason for it to behave differently.
    """
    s = neo4j_session()
    if s is None:
        return None
    try:
        with s:
            rec = s.run("""
                MATCH (m:Memory {address: $old})
                OPTIONAL MATCH (clash:Memory {address: $new})
                FOREACH (_ IN CASE WHEN clash IS NULL THEN [1] ELSE [] END |
                    SET m.address = $new)
                RETURN m.address AS address
            """, old=old_address, new=new_address).single()
            if rec is None:
                log.warning("Neo4j addr rename found no node at %s", old_address)
                return None
            landed = rec["address"]
            if landed != new_address:
                log.warning("Address %s could not move to %s -- already taken.",
                            old_address, new_address)
            return landed
    except Exception as e:
        log.warning(f"Neo4j addr rename failed: {e}")
        return None


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
            # Three independent COUNT subqueries, NOT a chain of MATCHes.
            #
            # The chained form this replaces returned ZERO ROWS whenever any
            # link in it matched nothing -- and the CO_RECALLED link matches
            # nothing until the first recall creates an edge. A brand new graph
            # therefore reported "connected, 0 memories, 0 keywords" while
            # holding a full set of both. /health is the first thing anyone
            # looks at, and it lied on exactly the graphs whose state is least
            # obvious. It also silently defeated any tool that reads graph size
            # from /health, mmu_validate.py's integrity check among them.
            #
            # COUNT {} evaluates each pattern separately, so an empty one
            # contributes 0 instead of erasing the other two.
            counts = s.run("""
                RETURN COUNT { MATCH (m:Memory)  RETURN m } AS mem,
                       COUNT { MATCH (k:Keyword) RETURN k } AS kw,
                       COUNT { MATCH ()-[r:CO_RECALLED]-() RETURN r } / 2 AS co
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
    The next CON (address identity) number, from a counter that only ever
    goes up.

    This used to be max(existing) + 1, which is unique at write time but not
    stable over time: delete the highest-numbered memory and the next write
    hands that same number back out. Anything outside MMU that had recorded the
    old CON -- a note, an export, an external index -- then silently points at a
    different memory. So the number lives in a :Counter node instead and is
    never recomputed from the population.

    (count() + 1 is worse still and was the original bug here: delete 5 of 100
    and it returns 96, which is already taken, so it collides against the
    memory_addr UNIQUE constraint.)

    An existing graph seeds the counter from max(existing) the first time
    through, so upgrading doesn't renumber anything or collide with what's
    already there.

    Returns 1 on an empty graph or if Neo4j is unavailable -- the caller is
    creating a memory either way, and a low CON is recoverable while a crash is
    not.
    """
    driver = get_driver()
    if driver is None:
        return 1
    try:
        with driver.session() as s:
            # Common path: the counter exists, so just claim the next value.
            # The increment happens inside a write transaction, which holds a
            # lock on the node, so concurrent callers serialize rather than
            # both reading the same value.
            rec = s.run("""
                MATCH (c:Counter {name: 'con'})
                SET c.value = c.value + 1
                RETURN c.value AS con
            """).single()
            if rec and rec["con"] is not None:
                return int(rec["con"])

            # First run on this graph (new install or an upgrade from the old
            # max()+1 scheme). Seed above the highest CON already in use.
            rec = s.run("""
                MATCH (m:Memory)
                RETURN max(toInteger(split(m.address, '.')[0])) AS max_con
            """).single()
            seed = int(rec["max_con"] or 0) if rec else 0

            rec = s.run("""
                MERGE (c:Counter {name: 'con'})
                  ON CREATE SET c.value = $seed
                SET c.value = c.value + 1
                RETURN c.value AS con
            """, seed=seed).single()
            log.info(f"CON counter initialized at {seed}; first issued CON is {seed + 1}")
            return int(rec["con"]) if rec else 1
    except Exception as e:
        # Last resort. This can collide, but the caller is mid-write and a
        # reused number beats losing the memory.
        log.warning(f"get_next_con failed, falling back to max()+1: {e}")
        try:
            with driver.session() as s:
                rec = s.run("""
                    MATCH (m:Memory)
                    RETURN max(toInteger(split(m.address, '.')[0])) AS max_con
                """).single()
                return int((rec["max_con"] or 0)) + 1 if rec else 1
        except Exception:
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


def get_index_source_rows():
    """
    Every memory as the v2 index needs it: address, colour, priority, src_type
    and keyword terms.

    Same projection rebuild_from_neo4j() uses, exposed separately so drift can
    be repaired incrementally. A full rebuild also discards the shortcut cache
    and resets the generation counter, which is a heavy price for reinstating
    one card.

    Returns None when the graph could not be read, and [] when it was read and
    is empty. Both used to be [], so /index_repair called an empty graph a
    503 -- which is exactly what a fresh second instance is, and therefore what
    every drift test run against one saw before it had written anything.
    """
    driver = get_driver()
    if driver is None:
        return None
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
# THREE-STEP BY DESIGN, and the split is structural rather than conventional:
#
#   find_skill_candidates()  reads. Writes nothing. Safe to poll.
#   queue_skill_proposals()  writes SkillProposal nodes only. Never touches a
#                            Memory node. Safe for the idle daemon.
#   crystallize_skill()      writes, atomically, and only on explicit human
#                            confirmation.
#
# This is the one phase that restructures Nova's own memory rather than adding
# capability beside it. Getting it wrong is not a bug, it is an identity-model
# change nobody approved. crystallize_skill() is deliberately NOT reachable
# from mmu_idle_daemon.py's IDLE_TOOLS.
#
# ── Phase 13.1: two bugs that made crystallization unreachable ──
#
# Symptom: 999 memories, 724 co-recall edges, zero Skill nodes ever formed.
#
# 1. DOCUMENTS WERE EXCLUDED. This function carried `src_type <> 2`, copied
#    from get_anticipated_context() where it is correct -- a paragraph of a
#    physics paper is not a thing to be proactively *reminded* of, and that
#    filter stays exactly where it is. The reasoning does not survive
#    the trip. Crystallization is the opposite operation, and compressing a
#    large reference corpus into one procedural node is exactly what a graph
#    of 861 documents needs. The filter made 86% of memories permanently
#    ineligible, so the densest region of the graph could never form a skill
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
SKILL_NORM_PERCENTILE = 0.90

# Absolute floor, applied under the normalized one. On a young or sparse graph
# p90 can itself be 1-2, and 0.6 * that would admit noise. A pair that has not
# been co-recalled at least this often is not evidence of anything, whatever
# the rest of the distribution happens to look like.
SKILL_MIN_ABS_WEIGHT = 3.0


def skill_weight_floor(min_pairwise_norm=0.6):
    """
    The co-recall weight a pair must clear to count toward a skill cluster.

    Returns (floor, reference_weight, basis) where reference_weight is the
    percentile the floor came from and basis names which of the two rules
    actually bound. Callers report these rather than a bare number, so a
    "no candidates" answer can be read as evidence instead of a shrug.
    """
    driver = get_driver()
    if driver is None:
        return SKILL_MIN_ABS_WEIGHT, 0.0, "absolute"
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH ()-[r:CO_RECALLED]-()
                RETURN percentileCont(r.weight, $p) AS ref
            """, p=float(SKILL_NORM_PERCENTILE)).single()
            ref = float((rec and rec["ref"]) or 0.0)
    except Exception as e:
        log.warning(f"skill_weight_floor failed, using absolute floor: {e}")
        return SKILL_MIN_ABS_WEIGHT, 0.0, "absolute"

    scaled = float(min_pairwise_norm) * ref
    if scaled >= SKILL_MIN_ABS_WEIGHT:
        return scaled, ref, "percentile"
    return SKILL_MIN_ABS_WEIGHT, ref, "absolute"


def find_skill_candidates(min_cluster=3, min_pairwise_norm=0.6, limit=10,
                          max_per_domain=2, max_member_reuse=2):
    """
    Clusters where EVERY pair is densely co-recalled -- not merely anchored on
    one popular node.

    The previews in get_insights() and get_idle_context() used to answer a
    different question ("which single memory has strong neighbours"), which is
    the right shape for a preview and the wrong shape for deciding to
    crystallize: a hub with many weak-to-each-other neighbours would qualify
    while not being a coherent skill at all. Both previews now call this
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
        floor, ref_w, _basis = skill_weight_floor(min_pairwise_norm)
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
                               + (semantic * 0.3) END                 AS skill_score,
                       [a.payload, b.payload, c.payload]              AS payloads,
                       [a.src_type, b.src_type, c.src_type]           AS src_types,
                       [ga, gb, gc]                                   AS grps
                ORDER BY skill_score DESC, avg_weight DESC
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
                    "skill_score":        round(float(r["skill_score"] or 0.0), 4),
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
        log.warning(f"find_skill_candidates failed: {e}")
        return []


def crystallize_skill(member_addresses, trigger, procedure, confidence=0.0):
    """
    The confirm step. ONE transaction, all-or-nothing.

    Creates the Skill, wires PROCEDURALIZED_FROM from every member, and demotes
    each member to Blue. Members are never deleted -- per the roadmap they are
    the skill's root system, and a memory that has been compressed is still the
    evidence the compression was drawn from.

    Returns (skill_dict, None) on success, or (None, reason) on failure with
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

    skill_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    try:
        with driver.session() as s:
            # execute_write gives a real transaction: a failure part-way rolls
            # back rather than leaving memories demoted with no Skill to show
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
                        "re-read GET /skill_proposals and use the addresses it "
                        "returns now, or confirm by proposal_id instead."
                    )

                # A memory already compressed into an active Skill cannot be
                # compressed into a second one. Colour is single-valued, so two
                # skills claiming the same member disagree about what it should
                # be the moment either is undone -- which is exactly how this
                # was found: a stale proposal was confirmed twice, and undoing
                # the second restored a member the first still owned.
                owners = [dict(r) for r in tx.run("""
                    MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk:Skill)
                    WHERE m.address IN $addrs AND sk.status <> 'deprecated'
                    RETURN sk.skill_id AS skill_id,
                           sk.trigger  AS trigger,
                           collect(DISTINCT m.address) AS addrs
                """, addrs=list(member_addresses))]
                if owners:
                    # Name the owner, and do not suggest uncrystallizing.
                    #
                    # The old wording said "either uncrystallize the skill that
                    # owns them, or pick a different proposal" without naming
                    # which skill -- so the reader had to guess an id, and the
                    # advice itself was the trap. Overlapping proposals are
                    # alternatives: freeing the members lets exactly one of them
                    # ever be crystallized, not both. A model told to
                    # uncrystallize does so, re-crystallizes the other, hits the
                    # same refusal from the opposite side, and loops -- which is
                    # precisely what happened, for as long as the message
                    # recommended it.
                    taken = sorted({a for o in owners for a in o["addrs"]})
                    lines = [
                        f"{len(taken)} of these memories already belong to an "
                        f"active skill, so this proposal cannot be crystallized "
                        f"as it stands. Retrying it will fail identically."
                    ]
                    for o in owners:
                        trig = (o.get("trigger") or "").strip()
                        lines.append(
                            f"  skill {o['skill_id']}"
                            + (f" ({trig[:60]})" if trig else "")
                            + f" owns: {', '.join(sorted(o['addrs']))}"
                        )
                    lines.append(
                        "Do NOT uncrystallize to free them. Two proposals over "
                        "the same memories are alternatives, not a queue: "
                        "whichever you crystallize, the other stays impossible, "
                        "so undoing and retrying only swaps which one refuses. "
                        "To put these memories under a broader skill, crystallize "
                        "a different proposal with `extends` set to the owning "
                        "skill id, or link the owning skill under a parent. A "
                        "human can also add the free members to the owning skill "
                        "directly with POST /skills/{id}/members. The review "
                        "queue marks which proposals are blocked, and which "
                        "exclude each other."
                    )
                    raise ValueError(chr(10).join(lines))

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
                    // Remember what this was before demoting it. Members here
                    // are a mix of colours -- Green and Yellow in the first
                    // real crystallization -- so an undo that assumed one
                    // would corrupt the aging state of the rest. coalesce
                    // keeps the ORIGINAL colour if this memory was somehow
                    // demoted once before.
                    SET m.pre_skill_color = coalesce(m.pre_skill_color, m.color),
                        m.color           = 'Blue'
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
        }, None
    except Exception as e:
        log.warning(f"crystallize_skill failed (nothing applied): {e}")
        return None, str(e)


def uncrystallize_skill(skill_id):
    """
    Reverse a crystallization: delete the Skill and restore its members.

    Returns (info, None) on success or (None, reason) on failure, with nothing
    half-applied.

    This exists because crystallization is the one operation in the system that
    restructures memory rather than adding to it, and it was the one operation
    with no way back. deprecate_skill() marks a Skill dead but leaves every
    member sitting in Blue, which is the state the recall gate treats as
    inactive -- so a skill judged wrong afterwards left its evidence buried.

    Refuses while another skill extends this one, for the same reason
    deprecate_skill() does: a live child pointing at a deleted parent is a
    broken tree, and silently orphaning it would be worse than refusing.

    Members are restored to pre_skill_color. A member crystallized before that
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
                MATCH (sk:Skill {skill_id: $sid}) RETURN sk.trigger AS trigger
            """, sid=skill_id).single()
            if not rec:
                return None, "no such skill"

            blockers = [r["cid"] for r in s.run("""
                MATCH (child:Skill)-[:EXTENDS_SKILL]->(sk:Skill {skill_id: $sid})
                WHERE child.status <> 'deprecated'
                RETURN child.skill_id AS cid
            """, sid=skill_id)]
            if blockers:
                return None, (f"{len(blockers)} active child skill(s) still extend "
                              f"this one: {', '.join(blockers)}")

            def _tx(tx):
                # Only members that this skill alone owns get restored. One
                # still compressed into another active skill stays Blue and
                # keeps its recorded colour -- restoring it would contradict
                # the skill that still claims it.
                rows = [dict(r) for r in tx.run("""
                    MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk:Skill {skill_id: $sid})
                    OPTIONAL MATCH (m)-[:PROCEDURALIZED_FROM]->(other:Skill)
                    WHERE other.skill_id <> $sid AND other.status <> 'deprecated'
                    WITH m, count(other) AS others
                    RETURN m.address AS address,
                           m.pre_skill_color AS pre,
                           m.pre_skill_color IS NULL AS guessed,
                           others > 0 AS still_owned
                """, sid=skill_id)]

                keep = [r["address"] for r in rows if r["still_owned"]]
                restore = [r["address"] for r in rows if not r["still_owned"]]

                if restore:
                    tx.run("""
                        MATCH (m:Memory) WHERE m.address IN $addrs
                        SET m.color = coalesce(m.pre_skill_color, 'Green')
                        REMOVE m.pre_skill_color
                    """, addrs=restore)

                tx.run("""
                    MATCH (m:Memory)-[r:PROCEDURALIZED_FROM]->(sk:Skill {skill_id: $sid})
                    DELETE r
                """, sid=skill_id)

                rows = [dict(r, kept=(r["address"] in keep)) for r in rows]

                # Reopen the proposal so the cluster returns to review rather
                # than vanishing: undoing a crystallization is a statement that
                # the skill was wrong, not that the pattern was imaginary.
                tx.run("""
                    MATCH (p:SkillProposal {skill_id: $sid})
                    SET p.status      = 'pending',
                        p.skill_id    = null,
                        p.reviewed_at = null
                """, sid=skill_id)

                tx.run("MATCH (sk:Skill {skill_id: $sid}) DETACH DELETE sk", sid=skill_id)
                return rows

            restored = s.execute_write(_tx)

        log.info("Uncrystallized skill %s; restored %d memories", skill_id, len(restored))
        return {
            "skill_id": skill_id,
            "trigger":  rec["trigger"],
            "restored": [{"address": r["address"], "color": r["pre"] or "Green"}
                         for r in restored if not r["kept"]],
            # Left demoted on purpose: another active skill still owns these.
            "still_demoted": [r["address"] for r in restored if r["kept"]],
            "colors_guessed": [r["address"] for r in restored
                               if r["guessed"] and not r["kept"]],
        }, None
    except Exception as e:
        log.warning(f"uncrystallize_skill failed (nothing applied): {e}")
        return None, str(e)


# ═════════════════════════════════════════════
#  PHASE 13.3 — SKILL GROWTH
# ═════════════════════════════════════════════
#
# Crystallization could create a skill and delete a skill, and nothing in
# between. crystallize_skill() always CREATEs, and it refuses any memory an
# active skill already owns -- so the only way to add a fourth memory to a
# three-memory skill was to uncrystallize it and build it again.
#
# That round trip is not a rebuild, it is a replacement. It mints a new
# skill_id, resets invocation_count to zero, discards created_at, drops the
# embedding and the keyword edges, and cannot run at all while an active child
# extends the skill -- so the tree has to be dismantled first. A skill that
# earned seven invocations came back claiming none, and the honest record of
# which skills are actually used was the thing being destroyed to add one
# memory.
#
# Measured on the graph this was written against: all twelve skills held
# exactly three members, the min_cluster floor. Not one had grown past its
# birth size, because nothing could make it.
#
# These two functions are the missing middle. Membership changes; identity,
# history and tree position do not.


def add_skill_members(skill_id, member_addresses):
    """
    Add memories to an existing skill. ONE transaction, all-or-nothing.

    The validation is deliberately the same as crystallize_skill()'s, because
    the hazards are the same ones: addresses go stale between reading a list
    and acting on it, and a memory owned by two active skills has two
    disagreeing opinions about what colour it should be when either is undone.

    Returns (info, None) or (None, reason) with nothing half-applied.

    Re-adding a memory this skill already owns is a no-op, not an error --
    reported in `already_members` so a caller that sent a superset sees what
    actually changed rather than a failure it has to interpret.
    """
    driver = get_driver()
    if driver is None:
        return None, "no database connection"
    if not member_addresses:
        return None, "no member addresses given"

    member_addresses = list(dict.fromkeys(member_addresses))

    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (sk:Skill {skill_id: $sid})
                RETURN sk.status AS status, sk.trigger AS trigger
            """, sid=skill_id).single()
            if not rec:
                return None, "no such skill"
            if rec["status"] == "deprecated":
                return None, (
                    "that skill is deprecated. Adding members would demote them "
                    "to Blue and bury them under a skill nothing can match -- "
                    "which is the exact state uncrystallize_skill() exists to "
                    "get out of."
                )

            def _tx(tx):
                live = [r["a"] for r in tx.run("""
                    MATCH (m:Memory) WHERE m.address IN $addrs
                    RETURN m.address AS a
                """, addrs=member_addresses)]
                if len(live) != len(member_addresses):
                    missing = [a for a in member_addresses if a not in live]
                    raise ValueError(
                        f"{len(missing)} of {len(member_addresses)} member "
                        f"addresses matched no memory: {', '.join(missing)}. "
                        "MMU addresses are rewritten in place when a memory is "
                        "recalled, so a stale address is the usual cause -- "
                        "re-read the skill's members and send the addresses "
                        "they have now."
                    )

                # Owned by a DIFFERENT active skill. This skill's own members
                # are excluded: re-sending one is the no-op above, not a
                # conflict with itself.
                taken = [r["a"] for r in tx.run("""
                    MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk:Skill)
                    WHERE m.address IN $addrs
                      AND sk.skill_id <> $sid
                      AND sk.status <> 'deprecated'
                    RETURN DISTINCT m.address AS a
                """, addrs=member_addresses, sid=skill_id)]
                if taken:
                    raise ValueError(
                        f"{len(taken)} memory(ies) already belong to another "
                        f"active skill: {', '.join(taken)}. Remove them from "
                        "that skill first, or uncrystallize it -- retrying "
                        "this call will fail identically every time."
                    )

                existing = {r["a"] for r in tx.run("""
                    MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(:Skill {skill_id: $sid})
                    WHERE m.address IN $addrs
                    RETURN m.address AS a
                """, addrs=member_addresses, sid=skill_id)}
                fresh = [a for a in member_addresses if a not in existing]

                if fresh:
                    tx.run("""
                        MATCH (sk:Skill {skill_id: $sid})
                        MATCH (m:Memory) WHERE m.address IN $addrs
                        MERGE (m)-[:PROCEDURALIZED_FROM]->(sk)
                        // Same coalesce as crystallize_skill: keep the ORIGINAL
                        // colour if this memory was ever demoted before, so an
                        // undo restores what it actually was rather than Blue.
                        SET m.pre_skill_color = coalesce(m.pre_skill_color, m.color),
                            m.color           = 'Blue'
                    """, sid=skill_id, addrs=fresh)

                    tx.run("""
                        MATCH (m:Memory) WHERE m.address IN $addrs
                        REMOVE m.skill_candidate_id
                    """, addrs=fresh)

                total = tx.run("""
                    MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(:Skill {skill_id: $sid})
                    RETURN count(DISTINCT m) AS n
                """, sid=skill_id).single()["n"]
                return fresh, sorted(existing), int(total)

            added, already, total = s.execute_write(_tx)

        log.info("Added %d memories to skill %s (now %d members)",
                 len(added), skill_id, total)
        return {
            "skill_id":        skill_id,
            "trigger":         rec["trigger"],
            "added":           added,
            "already_members": already,
            "member_count":    total,
        }, None
    except Exception as e:
        log.warning(f"add_skill_members failed (nothing applied): {e}")
        return None, str(e)


def remove_skill_members(skill_id, member_addresses):
    """
    Take memories back out of a skill and restore their colour.

    Refuses to remove the last member. A Skill with no root system is a claim
    with no evidence behind it -- still matchable, still delivered, and no
    longer traceable to anything that justified it. Emptying a skill is what
    uncrystallize_skill() is for, and it says so rather than leaving the caller
    to discover the difference.

    A memory another active skill still owns stays Blue and keeps its recorded
    colour, reported in `still_demoted` -- restoring it would contradict the
    skill that still claims it. Same rule uncrystallize_skill() applies.

    Returns (info, None) or (None, reason).
    """
    driver = get_driver()
    if driver is None:
        return None, "no database connection"
    if not member_addresses:
        return None, "no member addresses given"

    member_addresses = list(dict.fromkeys(member_addresses))

    try:
        with driver.session() as s:
            if not s.run("MATCH (sk:Skill {skill_id:$sid}) RETURN count(sk) AS n",
                         sid=skill_id).single()["n"]:
                return None, "no such skill"

            def _tx(tx):
                rows = [dict(r) for r in tx.run("""
                    MATCH (m:Memory)-[rel:PROCEDURALIZED_FROM]->(sk:Skill {skill_id: $sid})
                    WHERE m.address IN $addrs
                    OPTIONAL MATCH (m)-[:PROCEDURALIZED_FROM]->(other:Skill)
                    WHERE other.skill_id <> $sid AND other.status <> 'deprecated'
                    WITH m, count(other) AS others
                    RETURN m.address AS address,
                           m.pre_skill_color AS pre,
                           m.pre_skill_color IS NULL AS guessed,
                           others > 0 AS still_owned
                """, sid=skill_id, addrs=member_addresses)]

                found = {r["address"] for r in rows}
                not_members = [a for a in member_addresses if a not in found]
                if not_members:
                    raise ValueError(
                        f"{len(not_members)} address(es) are not members of this "
                        f"skill: {', '.join(not_members)}. Addresses are rewritten "
                        "on recall -- re-read the skill's members and use the "
                        "addresses it reports now."
                    )

                total = tx.run("""
                    MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(:Skill {skill_id: $sid})
                    RETURN count(DISTINCT m) AS n
                """, sid=skill_id).single()["n"]
                if len(found) >= int(total):
                    raise ValueError(
                        f"that would remove all {total} members and leave the "
                        "skill with no root system. Use uncrystallize to retire "
                        "the skill itself, which also deletes the Skill node "
                        "instead of leaving one that can still be matched and "
                        "delivered with nothing behind it."
                    )

                tx.run("""
                    MATCH (m:Memory)-[rel:PROCEDURALIZED_FROM]->(:Skill {skill_id: $sid})
                    WHERE m.address IN $addrs
                    DELETE rel
                """, sid=skill_id, addrs=member_addresses)

                restore = [r for r in rows if not r["still_owned"]]
                for r in restore:
                    tx.run("""
                        MATCH (m:Memory {address: $a})
                        SET m.color = $c
                        REMOVE m.pre_skill_color
                    """, a=r["address"], c=r["pre"] or "Green")

                return rows, restore, int(total) - len(found)

            rows, restore, remaining = s.execute_write(_tx)

        log.info("Removed %d memories from skill %s (%d remain)",
                 len(rows), skill_id, remaining)
        return {
            "skill_id":       skill_id,
            "removed":        [{"address": r["address"], "color": r["pre"] or "Green"}
                               for r in restore],
            "still_demoted":  [r["address"] for r in rows if r["still_owned"]],
            "colors_guessed": [r["address"] for r in restore if r["guessed"]],
            "member_count":   remaining,
        }, None
    except Exception as e:
        log.warning(f"remove_skill_members failed (nothing applied): {e}")
        return None, str(e)


def update_skill_text(skill_id, trigger=None, procedure=None):
    """
    Rewrite a skill's trigger and/or procedure in place.

    Growth without this is half an operation: a skill that gains a fourth
    member usually needs its procedure to say so, and the only way to change
    that text used to be the same uncrystallize-and-rebuild that loses the
    skill's identity.

    Returns (changed_fields, None) or (None, reason). The caller re-indexes --
    the embedding and keyword edges are derived from this text, and leaving
    them pointing at the old wording makes the skill retrievable by what it
    used to say.
    """
    driver = get_driver()
    if driver is None:
        return None, "no database connection"

    sets, params = [], {"sid": skill_id}
    if trigger is not None and str(trigger).strip():
        sets.append("sk.trigger = $trigger")
        params["trigger"] = str(trigger).strip()
    if procedure is not None and str(procedure).strip():
        sets.append("sk.procedure = $procedure")
        params["procedure"] = str(procedure).strip()
    if not sets:
        return None, "nothing to update"

    try:
        with driver.session() as s:
            rec = s.run(f"""
                MATCH (sk:Skill {{skill_id: $sid}})
                SET {', '.join(sets)}
                RETURN sk.trigger AS trigger, sk.procedure AS procedure
            """, **params).single()
            if not rec:
                return None, "no such skill"
        return {"trigger": rec["trigger"], "procedure": rec["procedure"],
                "changed": [s.split("=")[0].strip().split(".")[1] for s in sets]}, None
    except Exception as e:
        log.warning(f"update_skill_text failed: {e}")
        return None, str(e)



# ═════════════════════════════════════════════
#  PHASE 13.2 — SKILL DELIVERY
# ═════════════════════════════════════════════
#
# Crystallization was write-only. A Skill node carried trigger and procedure
# and nothing else: no keywords, no embedding, no edge into the Keyword graph.
# Every retrieval path in this system reaches a memory through keywords or
# through the vector index, so a Skill was unreachable by all of them -- and
# invocation_count, written as 0 at creation, was never incremented because
# nothing ever invoked anything.
#
# Measured before this existed: crystallizing three memories changed recall by
# zero bytes. The 636-character skill was 11% of its 5,377 characters of source
# and was never delivered in place of them, so the compression was real on
# paper and absent in practice.
#
# The model noticed before the code did. Asked about skills, it saved an
# ordinary Memory titled "SKILL NODE: ..." with a trigger phrase and a
# provenance footer -- reimplementing the mechanism at the only layer that was
# actually retrievable.


def write_skill_embedding(skill_id, vector):
    """Attach an embedding to a Skill, in Neo4j's native vector encoding."""
    driver = get_driver()
    if driver is None or not vector:
        return False
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (sk:Skill {skill_id: $sid})
                CALL db.create.setNodeVectorProperty(sk, 'embedding', $vector)
                RETURN count(sk) AS n
            """, sid=skill_id, vector=[float(x) for x in vector]).single()
            return bool(rec and rec["n"])
    except Exception as e:
        log.warning(f"write_skill_embedding failed: {e}")
        return False


def link_skill_keywords(skill_id, terms, replace=False):
    """
    Link a Skill into the same Keyword graph memories use.

    Deliberately the existing HAS_KEYWORD edge and the existing Keyword nodes
    rather than a parallel vocabulary: a skill about dimensional relativity and
    a memory about dimensional relativity should match the same term, or the
    keyword gate would need to learn about skills as a special case.

    replace=True drops the skill's existing HAS_KEYWORD edges first. This
    function only ever MERGEd, which is right when indexing a new skill and
    wrong when re-indexing one whose procedure was rewritten: the terms from
    the old wording stay linked, and the skill keeps matching prompts about
    text it no longer contains. Default stays False so existing callers
    behave exactly as before.
    """
    driver = get_driver()
    if driver is None or not terms:
        return 0
    try:
        n = 0
        with driver.session() as s:
            if replace:
                s.run("""
                    MATCH (sk:Skill {skill_id: $sid})-[r:HAS_KEYWORD]->(:Keyword)
                    DELETE r
                """, sid=skill_id)
            for term in terms:
                term = (term or "").strip().lower()
                if not term:
                    continue
                s.run("""
                    MERGE (k:Keyword {term: $term})
                    ON CREATE SET k.freq = 0, k.stem = $term
                    WITH k
                    MATCH (sk:Skill {skill_id: $sid})
                    MERGE (sk)-[:HAS_KEYWORD]->(k)
                """, term=term, sid=skill_id)
                n += 1
        return n
    except Exception as e:
        log.warning(f"link_skill_keywords failed: {e}")
        return 0


def project_skill_associations(skill_id):
    """
    Carry a skill's members' outward co-recall onto the skill itself, as
    ASSOCIATED_WITH edges. Returns how many edges it wrote.

    Crystallizing severed the associative pathway it was built from. The
    cluster existed BECAUSE those memories were recalled together with things
    around them; compression then demoted the members to Blue and -- since the
    pairing exclusion -- stopped them accumulating at all, while the Skill got
    no graph position of its own. Its only edges were HAS_KEYWORD,
    PROCEDURALIZED_FROM and EXTENDS_SKILL. On the graph where this was found,
    356 co-recall edges worth 855 in total ran from crystallized members out to
    99 still-live memories, and none of them could reach the skill that
    replaced them.

    So the weight is projected: for each outside memory, the sum of its
    co-recall weight to any member becomes one edge to the Skill. Summed rather
    than averaged because a memory tied to three members of a cluster is more
    strongly about it than one tied to a single member, and averaging would
    erase exactly that.

    Members are read from PROCEDURALIZED_FROM rather than passed in, because
    that edge IS the membership -- threading addresses through the caller only
    creates a second version of the truth that can disagree with the graph.
    That also makes this safe to re-run after add_skill_members() or
    remove_skill_members(): it SETS the seed rather than adding to it, and
    carries forward whatever has accumulated since, so re-projecting a skill
    cannot inflate it.
    """
    driver = get_driver()
    if driver is None or not skill_id:
        return 0
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk:Skill {skill_id: $sid})
                WITH sk, collect(m) AS members
                UNWIND members AS mem
                MATCH (o:Memory)-[c:CO_RECALLED]-(mem)
                WHERE NOT o IN members
                WITH sk, o, sum(c.weight) AS w
                MERGE (o)-[a:ASSOCIATED_WITH]->(sk)
                ON CREATE SET a.weight = w, a.seeded = w,
                              a.created_at = $now, a.last_at = $now
                ON MATCH  SET a.weight = w + (coalesce(a.weight, 0)
                                              - coalesce(a.seeded, 0)),
                              a.seeded = w, a.last_at = $now
                RETURN count(*) AS n
            """, sid=skill_id, now=datetime.now().isoformat()).single()
            n = rec["n"] if rec else 0
        if n:
            log.info("Projected %d association(s) onto skill %s", n, skill_id[:8])
        return n
    except Exception as e:
        log.warning(f"project_skill_associations failed: {e}")
        return 0


def project_all_skill_associations():
    """
    Backfill the projection across every active skill.

    Returns {"skills": n, "edges": n} or None if the graph could not be read.
    Idempotent for the same reason the single-skill version is.
    """
    driver = get_driver()
    if driver is None:
        return None
    try:
        with driver.session() as s:
            ids = [r["sid"] for r in s.run("""
                MATCH (sk:Skill) WHERE sk.status = 'active'
                RETURN sk.skill_id AS sid
            """)]
        total = sum(project_skill_associations(sid) for sid in ids)
        log.info("Projected %d association(s) across %d skill(s)", total, len(ids))
        return {"skills": len(ids), "edges": total}
    except Exception as e:
        log.warning(f"project_all_skill_associations failed: {e}")
        return None


def bump_skill_associations(skill_ids, memory_addresses):
    """
    Strengthen ASSOCIATED_WITH for memories delivered alongside a skill.

    This is the live half, and it is now the ONLY way the edge can grow.
    Members cannot carry it: they are Blue after crystallization and, since the
    pairing exclusion, are left out of co-recall entirely -- deliberately, so a
    compressed cluster stops thickening its own edges. An association that only
    grew when a member was recalled would therefore never grow at all.

    What does still happen is that a skill is DELIVERED in a recall, beside
    other memories -- and that is the same evidence co-recall captures between
    two memories, one level up.

    It is worth keeping because a memory that keeps arriving with a skill is a
    candidate for belonging to it. The edge is not only a retrieval path, it is
    the record of a cluster still growing after it was compressed.
    """
    driver = get_driver()
    if driver is None or not skill_ids or not memory_addresses:
        return 0
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (sk:Skill) WHERE sk.skill_id IN $sids AND sk.status = 'active'
                MATCH (o:Memory) WHERE o.address IN $addrs
                // Never to its own members: a skill already contains them, and
                // an edge back would make every delivery look like growth.
                AND NOT (o)-[:PROCEDURALIZED_FROM]->(sk)
                MERGE (o)-[a:ASSOCIATED_WITH]->(sk)
                ON CREATE SET a.weight = 1, a.seeded = 0,
                              a.created_at = $now, a.last_at = $now
                ON MATCH  SET a.weight = coalesce(a.weight, 0) + 1, a.last_at = $now
                RETURN count(*) AS n
            """, sids=list(skill_ids), addrs=list(memory_addresses),
                 now=datetime.now().isoformat()).single()
            return rec["n"] if rec else 0
    except Exception as e:
        log.warning(f"bump_skill_associations failed: {e}")
        return 0


def clear_skill_associations(skill_id):
    """Drop a skill's projected associations. Used when it is undone."""
    driver = get_driver()
    if driver is None:
        return 0
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (:Memory)-[a:ASSOCIATED_WITH]->(sk:Skill {skill_id: $sid})
                DELETE a RETURN count(a) AS n
            """, sid=skill_id).single()
            return rec["n"] if rec else 0
    except Exception as e:
        log.warning(f"clear_skill_associations failed: {e}")
        return 0


def skill_growth_candidates(skill_id=None, limit=5, min_weight=3):
    """
    Memories strongly associated with a skill that are not part of it.

    The reason the live edge is worth maintaining: a memory that keeps being
    delivered alongside a skill is evidence the skill should grow to include
    it -- and since #15 that is a thing the system can actually do, with
    add_skill_members(). Without this the association is only a retrieval path;
    with it, a compressed cluster can still be observed accumulating.

    Read-only. Nothing here changes a skill -- it reports what a reviewer or a
    sweep could act on.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (o:Memory)-[a:ASSOCIATED_WITH]->(sk:Skill)
                WHERE sk.status = 'active'
                  AND ($sid IS NULL OR sk.skill_id = $sid)
                  AND a.weight >= $minw
                  AND NOT (o)-[:PROCEDURALIZED_FROM]->(sk)
                  AND o.color <> 'Blue'
                RETURN sk.skill_id AS skill_id, sk.trigger AS trigger,
                       o.address   AS address,
                       substring(coalesce(o.payload, ''), 0, 90) AS preview,
                       a.weight    AS weight,
                       coalesce(a.seeded, 0) AS seeded,
                       a.weight - coalesce(a.seeded, 0) AS grown
                ORDER BY a.weight DESC
                LIMIT $lim
            """, sid=skill_id, minw=int(min_weight), lim=int(limit))]
    except Exception as e:
        log.warning(f"skill_growth_candidates failed: {e}")
        return []


def match_skills(query_vector=None, terms=None, limit=3, min_semantic=0.75,
                 matched_addresses=None, assoc_ref=None):
    """
    Find active skills relevant to a query, by embedding and by keyword.

    Returns [{skill_id, trigger, procedure, confidence, members, score, via}]
    where via is "semantic" or "keyword", so a caller can report WHY a skill
    surfaced -- the same transparency rule that puts `via` on every memory row.

    Deprecated skills are never matched. A skill with no embedding falls back
    to the keyword path rather than being invisible, which is what keeps a
    skill crystallized before this phase from silently disappearing.

    min_semantic is deliberately high. A skill substitutes for its source
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
                        RETURN node.skill_id AS sid, score AS score
                    """, index=SKILL_EMBEDDING_INDEX, k=max(int(limit) * 3, 10),
                         vector=[float(x) for x in query_vector],
                         floor=float(min_semantic)):
                        found[r["sid"]] = (float(r["score"]), "semantic")
                except Exception as e:
                    # No vector index (older Neo4j), or no embedded skills yet.
                    log.debug("skill vector match unavailable: %s", e)

            if terms:
                for r in s.run("""
                    MATCH (sk:Skill)-[:HAS_KEYWORD]->(k:Keyword)
                    WHERE sk.status = 'active' AND k.term IN $terms
                    WITH sk, count(DISTINCT k) AS hits
                    RETURN sk.skill_id AS sid, hits
                    ORDER BY hits DESC LIMIT $lim
                """, terms=terms, lim=max(int(limit) * 3, 10)):
                    sid, hits = r["sid"], r["hits"]
                    # Fraction of the query's terms this skill carries. Kept on
                    # the same 0-1 scale as the cosine score so one threshold
                    # and one ordering apply to both paths.
                    score = hits / float(len(terms))
                    if sid not in found or score > found[sid][0]:
                        found[sid] = (score, "keyword")

            # Association: the skills the memories THIS query already matched
            # point at. Kept on the same 0-1 scale as the other two so one
            # ordering covers all three, and ranked BESIDE them rather than as
            # a tiebreak -- a skill reached because the conversation is
            # demonstrably in its neighbourhood is not weaker evidence than one
            # reached by wording, and treating it as a tiebreak would leave the
            # co-recall graph decorative.
            if matched_addresses:
                ref = float(assoc_ref) if assoc_ref else ASSOC_REF
                for r in s.run("""
                    MATCH (o:Memory)-[a:ASSOCIATED_WITH]->(sk:Skill)
                    WHERE sk.status = 'active' AND o.address IN $addrs
                    WITH sk, sum(a.weight) AS w
                    RETURN sk.skill_id AS sid, w
                    ORDER BY w DESC LIMIT $lim
                """, addrs=list(matched_addresses), lim=max(int(limit) * 3, 10)):
                    sid, w = r["sid"], float(r["w"] or 0)
                    score = w / (w + ref) if w > 0 else 0.0
                    # Floored for the same reason min_semantic is high: a
                    # matched skill WITHHOLDS its members from the delivered
                    # context, so a loose match does not merely add noise, it
                    # removes evidence. Every memory in a hot neighbourhood
                    # carries some association to some skill; only a strong one
                    # should be allowed to substitute.
                    if score < ASSOC_FLOOR:
                        continue
                    if sid not in found or score > found[sid][0]:
                        found[sid] = (score, "association")

            if not found:
                return []

            rows = s.run("""
                MATCH (sk:Skill) WHERE sk.skill_id IN $sids
                OPTIONAL MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk)
                RETURN sk.skill_id   AS skill_id,
                       sk.trigger    AS trigger,
                       sk.procedure  AS procedure,
                       sk.confidence AS confidence,
                       collect(m.address) AS members
            """, sids=list(found.keys()))

            out = []
            for r in rows:
                score, via = found[r["skill_id"]]
                d = dict(r)
                d["score"] = round(score, 4)
                d["via"]   = via
                out.append(d)
            out.sort(key=lambda d: -d["score"])
            return out[:int(limit)]
    except Exception as e:
        log.warning(f"match_skills failed: {e}")
        return []


def record_skill_invocation(skill_ids):
    """
    Count a delivery. invocation_count existed from Phase 12 and was never
    incremented, which meant there was no way to tell a skill that earns its
    place from one that has never once been used.
    """
    driver = get_driver()
    if driver is None or not skill_ids:
        return False
    try:
        with driver.session() as s:
            s.run("""
                MATCH (sk:Skill) WHERE sk.skill_id IN $sids
                SET sk.invocation_count = coalesce(sk.invocation_count, 0) + 1,
                    sk.last_invoked     = $now
            """, sids=list(skill_ids), now=datetime.now().isoformat())
        return True
    except Exception as e:
        log.warning(f"record_skill_invocation failed: {e}")
        return False


def get_skill_member_addresses():
    """
    Every memory compressed into an active Skill.

    The v2 index carries this as a per-card flag, because the read path cannot
    afford a graph query to decide whether a memory may age. This is the
    authority that flag is reconciled against by /index_repair -- the two tiers
    disagreeing about membership is the same class of drift as one having a
    card the other has no node for, and it fails more quietly.

    Returns None (not []) when the graph is unreachable, so a caller can tell
    "no members" from "could not ask" and refuse to repair on the strength of
    an answer it never got.
    """
    driver = get_driver()
    if driver is None:
        return None
    try:
        with driver.session() as s:
            return [r["a"] for r in s.run("""
                MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk:Skill)
                WHERE sk.status <> 'deprecated'
                RETURN DISTINCT m.address AS a
            """)]
    except Exception as e:
        log.warning(f"get_skill_member_addresses failed: {e}")
        return None


def get_skills_needing_index():
    """
    Active skills with no embedding or no keywords -- everything crystallized
    before delivery existed, plus anything whose embedding write failed.
    """
    driver = get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (sk:Skill)
                WHERE sk.status = 'active'
                  AND (sk.embedding IS NULL
                       OR NOT (sk)-[:HAS_KEYWORD]->(:Keyword))
                RETURN sk.skill_id  AS skill_id,
                       sk.trigger   AS trigger,
                       sk.procedure AS procedure
            """)]
    except Exception as e:
        log.warning(f"get_skills_needing_index failed: {e}")
        return []


def resolve_skill_id(value):
    """
    Accept a full skill_id or an unambiguous prefix. Returns (skill_id, error).

    Skill ids are UUIDs and get displayed truncated almost everywhere -- tree
    views, summaries, logs. Requiring the full 36 characters means the id
    someone actually has in front of them is the one form that does not work,
    which is how a branch ends up as a second root.

    An ambiguous prefix is an error, never a guess: silently picking one of two
    matching skills would attach a branch to the wrong parent.
    """
    value = (value or "").strip()
    if not value:
        return None, "no skill id given"
    driver = get_driver()
    if driver is None:
        return None, "no database connection"
    try:
        with driver.session() as s:
            hits = [r["sid"] for r in s.run("""
                MATCH (sk:Skill)
                WHERE sk.skill_id = $v OR sk.skill_id STARTS WITH $v
                RETURN sk.skill_id AS sid
                ORDER BY CASE WHEN sk.skill_id = $v THEN 0 ELSE 1 END
                LIMIT 5
            """, v=value)]
        if not hits:
            return None, f"no skill with id {value}"
        if hits[0] == value or len(hits) == 1:
            return hits[0], None
        return None, (f"{len(hits)} skills start with {value}: "
                      f"{', '.join(h[:12] for h in hits)}. Use more characters.")
    except Exception as e:
        log.warning(f"resolve_skill_id failed: {e}")
        return None, str(e)


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


def unlink_skill(child_id, parent_id=None):
    """
    Detach a skill from its parent, making it a root again.

    Reparenting previously meant uncrystallizing and rebuilding, which changes
    the skill_id, re-enters the overlap checks, and destroys work to change one
    edge. Detaching is the cheap operation and should be reachable as one.

    parent_id=None removes every EXTENDS_SKILL edge from this skill.
    Returns (removed_count, error).
    """
    driver = get_driver()
    if driver is None:
        return 0, "no database connection"
    try:
        with driver.session() as s:
            if not s.run("MATCH (sk:Skill {skill_id:$cid}) RETURN count(sk) AS n",
                         cid=child_id).single()["n"]:
                return 0, "no such skill"
            if parent_id:
                rec = s.run("""
                    MATCH (c:Skill {skill_id:$cid})-[r:EXTENDS_SKILL]->(p:Skill {skill_id:$pid})
                    DELETE r RETURN count(r) AS n
                """, cid=child_id, pid=parent_id).single()
            else:
                rec = s.run("""
                    MATCH (c:Skill {skill_id:$cid})-[r:EXTENDS_SKILL]->(:Skill)
                    DELETE r RETURN count(r) AS n
                """, cid=child_id).single()
        return int(rec["n"]) if rec else 0, None
    except Exception as e:
        log.warning(f"unlink_skill failed: {e}")
        return 0, str(e)


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
#  PHASE 13.1 — SKILL PROPOSAL QUEUE
# ═════════════════════════════════════════════
#
# The human gate on crystallize_skill() is correct and stays. What it lacked
# was a doorbell: nothing ever surfaced a candidate for review, so in practice
# the gate was never approached and no skill was ever formed. find_skill_
# candidates() is read-only and safe to poll, but a poll nobody runs proposes
# nothing.
#
# A SkillProposal is a durable, deduplicated note that a cluster looked ready.
# The daemon may create and refresh them. It may not act on them. Turning one
# into a Skill still requires POST /crystallize with confirmed=true, which is
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
# Reference weight at which an association scores 0.5, via w/(w+ref).
#
# Saturating rather than linear because association weight has no ceiling: a
# long-lived pair keeps accumulating, and dividing by a maximum would let one
# hot neighbourhood outrank everything reached by meaning.
#
# 40 comes off the real distribution. Individual edges run median 3, p90 15,
# max 59, but a score sums every edge from the memories one query matched, so
# per-query totals land in the tens to low hundreds. A first cut used 8 and
# effectively everything saturated -- a query scored 0.947 against semantic
# matches at 0.86, which is not "ranks beside" but "ranks above everything".
# At 40, that same query scores 0.78: competitive with a strong semantic match
# without displacing it.
ASSOC_REF = float(os.environ.get("MMU_ASSOC_REF", "40"))

# Minimum association score that may deliver a skill. Present for the same
# reason min_semantic is high: a matched skill WITHHOLDS its members from the
# delivered context, so a weak match subtracts evidence rather than adding
# noise. 0.35 is about a summed weight of 21 at the reference above.
ASSOC_FLOOR = float(os.environ.get("MMU_ASSOC_FLOOR", "0.35"))

MAX_PENDING_PROPOSALS = 25

# Semantic floor a MERGED cluster has to clear to be offered as one proposal.
#
# find_skill_candidates is a triangle query -- MATCH (a)-(b)-(c) -- so every
# candidate it can produce has exactly three members. Clusters larger than that
# were not rejected by the design; they were unreachable by it. Merging the
# overlapping triples back together is what gets past three without rewriting
# the traversal.
#
# Merging is also what makes the queue reviewable at all. Overlapping proposals
# are alternatives: crystallizing one makes every proposal sharing a member
# permanently impossible. Offered as 26 separate items, 24 of which shared
# members with another, the queue read as a list of tasks and behaved as a
# minefield.
#
# 0.45 because measured merges on a real graph landed between 0.64 and 0.86 and
# cost only 0.03-0.07 against the fragments they replaced -- the union of
# overlapping triples stays about as coherent as the triples themselves, which
# is what "they share members" was evidence of in the first place. The floor is
# here to catch the case where it does not.
MERGE_COHERENCE_FLOOR = float(os.environ.get("MMU_MERGE_COHERENCE_FLOOR", "0.45"))

# An absolute floor alone does not hold, because a growing cluster ratchets
# through it. Each admission moves a mean taken over every pair, so the larger
# the cluster the less any single member can shift it -- 35 triples merged into
# one 18-member cluster spanning six domains, and no individual step ever
# dropped below 0.45. The floor was never crossed; it was outrun.
#
# So growth is bounded three ways, and a merge has to satisfy all of them:
#   FLOOR  the union is coherent in absolute terms (above)
#   DROP   the union is not much worse than the seed it grew from, which is
#          what actually stops the ratchet -- the comparison is against where
#          the cluster started rather than against a constant
#   MAX    a hard ceiling on members, because a skill is a procedure someone
#          reads, and there is a size past which that stops being true no
#          matter how well it scores
MERGE_COHERENCE_DROP = float(os.environ.get("MMU_MERGE_COHERENCE_DROP", "0.08"))
MERGE_MAX_MEMBERS    = int(os.environ.get("MMU_MERGE_MAX_MEMBERS", "8"))


def retire_unconfirmable_proposals(session, exclude_key=None):
    """
    Mark pending proposals that no skill leaves any room for.

    A proposal whose members were crystallized into some OTHER cluster cannot
    be confirmed: colour is single-valued, so a memory belongs to one skill.
    Nothing retired those, so they stayed pending and unconfirmable -- 22 of 26
    on the graph where this was found, 14 of them with no route at all. A
    reviewer works through that queue hitting refusal after refusal, which is
    how the crystallize/uncrystallize loop started.

    "No room" means fewer than two unclaimed members, since a skill needs two
    sources. A proposal with two or more free members is deliberately left
    pending: it can still be crystallized UNDER the owning skill via `extends`,
    and that is a live option the queue should keep offering.

    Returns how many were retired.
    """
    rec = session.run("""
        MATCH (p:SkillProposal {status: 'pending'})
        WHERE size(p.member_created) > 0
          AND ($key IS NULL OR p.member_key <> $key)
        OPTIONAL MATCH (m:Memory)-[:PROCEDURALIZED_FROM]->(sk:Skill)
        WHERE m.created_at IN p.member_created AND sk.status <> 'deprecated'
        WITH p, count(DISTINCT m) AS claimed
        WHERE claimed > 0 AND size(p.member_created) - claimed < 2
        SET p.status      = 'superseded',
            p.updated_at  = $now,
            p.review_note = 'Members were crystallized into another skill'
        RETURN count(p) AS n
    """, key=exclude_key, now=datetime.now().isoformat()).single()
    return rec["n"] if rec else 0


def cluster_metrics(session, member_created, ref_weight=None):
    """
    Score an arbitrary-size cluster the way find_skill_candidates scores a
    triple. Returns a dict of the same quantities, or None if the members
    cannot be resolved.

    The triangle query hardcodes three of everything -- three cosines over
    three pairs, and a CASE with one arm per way three domains can agree. None
    of that generalises, so the same quantities are computed here for N:

      semantic      mean pairwise cosine over all N*(N-1)/2 pairs, rescaled off
                    Neo4j's 0.5-is-orthogonal convention exactly as the sweep
                    does. Null when any member lacks an embedding -- reported,
                    not silently treated as zero.
      grp_coherence share of members in the cluster's most common GRP domain,
                    which for a triple is max(domain count)/3 and is what the
                    hardcoded CASE was computing all along.
      avg_weight    mean CO_RECALLED weight over the edges that exist between
                    members. Absent edges are not counted as zero: a merged
                    cluster is not required to be a clique, and scoring it as
                    though it should be would penalise exactly the large
                    clusters this function exists to make possible.
    """
    rows = list(session.run("""
        MATCH (m:Memory) WHERE m.created_at IN $created
        RETURN m.address AS address, m.payload AS payload, m.color AS color,
               m.created_at AS created_at, m.src_type AS src_type,
               toInteger(split(m.address, '.')[2]) AS grp,
               m.embedding IS NOT NULL AS has_emb
        ORDER BY m.created_at
    """, created=list(member_created)))
    if len(rows) < 2:
        return None

    addrs = [r["address"] for r in rows]
    grps  = [r["grp"] for r in rows if r["grp"] is not None]
    domains = [g // 100 for g in grps]
    grp_coh = (max((domains.count(d) for d in set(domains)), default=0) / len(domains)
               if domains else 0.0)

    sem = None
    if all(r["has_emb"] for r in rows):
        rec = session.run("""
            MATCH (m:Memory) WHERE m.created_at IN $created
            WITH collect(m) AS ms
            UNWIND range(0, size(ms) - 2) AS i
            UNWIND range(i + 1, size(ms) - 1) AS j
            RETURN avg(vector.similarity.cosine(ms[i].embedding,
                                                ms[j].embedding)) AS raw
        """, created=list(member_created)).single()
        raw = rec["raw"] if rec else None
        if raw is not None:
            sem = max(0.0, min(1.0, (float(raw) - 0.5) * 2))

    rec = session.run("""
        MATCH (a:Memory)-[r:CO_RECALLED]-(b:Memory)
        WHERE a.created_at IN $created AND b.created_at IN $created
          AND a.address < b.address
        RETURN avg(r.weight) AS w
    """, created=list(member_created)).single()
    avg_w = float(rec["w"]) if rec and rec["w"] is not None else 0.0

    src_mix = {}
    for r in rows:
        if r["src_type"] is None:
            continue
        label = SOURCE_LABELS.get(r["src_type"], f"Type-{r['src_type']}")
        src_mix[label] = src_mix.get(label, 0) + 1

    return {
        "members":            addrs,
        "member_created":     [r["created_at"] for r in rows],
        "previews":           [(r["payload"] or "")[:90] for r in rows],
        "grps":               grps,
        "colors":             [r["color"] for r in rows],
        "src_mix":            src_mix,
        "avg_weight":         round(avg_w, 3),
        "grp_coherence":      round(grp_coh, 3),
        "semantic_coherence": (round(sem, 3) if sem is not None else None),
    }


def merge_overlapping_candidates(session, candidates, ref_weight,
                                 floor=MERGE_COHERENCE_FLOOR):
    """
    Fold candidates that share a member into larger candidates, and return a
    set of clusters no two of which share a memory.

    Two triples sharing a memory are not two pieces of work. Only one of them
    can ever be crystallized -- colour is single-valued, so the first to claim
    a member locks every other cluster containing it out permanently. Offered
    separately they produce a queue whose items silently cancel each other, and
    a reader working through it hits a refusal that looks repairable and is not.

    Disjointness is the contract, not a side effect. A cluster is grown,
    emitted, and then every remaining candidate that still touches it is
    DROPPED rather than offered -- because once that cluster is crystallized
    those candidates are unconfirmable, and listing them would recreate the
    problem this exists to remove.

    Growth is greedy from the strongest remaining candidate and bounded by
    MERGE_COHERENCE_FLOOR, MERGE_COHERENCE_DROP and MERGE_MAX_MEMBERS together;
    see those constants for why one bound is not enough. Greedy and not optimal
    on purpose: components here are small, and choosing between partitions that
    score within noise of each other is not worth the machinery.

    Safe on a candidate list with no overlaps at all -- each comes back
    unchanged.
    """
    if not candidates:
        return []

    stamps = [set(str(t) for t in (c.get("member_created") or [])) for c in candidates]
    order  = sorted(range(len(candidates)),
                    key=lambda i: -float(candidates[i].get("skill_score", 0.0)))
    remaining = list(order)
    out, merges, dropped = [], 0, 0

    while remaining:
        seed = remaining[0]
        current = set(stamps[seed])
        absorbed = 1

        seed_m   = cluster_metrics(session, sorted(current), ref_weight)
        seed_sem = (seed_m or {}).get("semantic_coherence")

        # Re-sweep after each admission: a candidate that did not touch the
        # seed may touch what the cluster has since grown into.
        changed = True
        while changed and len(current) < MERGE_MAX_MEMBERS:
            changed = False
            for i in remaining[1:]:
                if not (stamps[i] & current):
                    continue                      # keep the cluster connected
                trial = current | stamps[i]
                if trial == current:
                    absorbed += 1                 # wholly contained
                    continue
                if len(trial) > MERGE_MAX_MEMBERS:
                    continue
                m = cluster_metrics(session, sorted(trial), ref_weight)
                if m is None:
                    continue
                sem = m["semantic_coherence"]
                # An unembedded member cannot be judged, so it is not admitted.
                # Refusing to grow is recoverable; a merge nothing measured is
                # not.
                if sem is None or sem < floor:
                    continue
                if seed_sem is not None and sem < seed_sem - MERGE_COHERENCE_DROP:
                    continue
                current, absorbed, changed = trial, absorbed + 1, True

        m = cluster_metrics(session, sorted(current), ref_weight)
        if m is not None and len(m["members"]) >= 2:
            avg_norm = min(1.0, m["avg_weight"] / ref_weight) if ref_weight else 0.0
            sem = m["semantic_coherence"]
            score = ((avg_norm * 0.5) + (m["grp_coherence"] * 0.5) if sem is None
                     else (avg_norm * 0.4) + (m["grp_coherence"] * 0.3) + (sem * 0.3))
            domains = [g // 100 for g in m["grps"]]
            m.update({
                "skill_score":     round(float(score), 4),
                "avg_weight_norm": round(avg_norm, 4),
                "scored_without_embeddings": sem is None,
                "domain":          (max(set(domains), key=domains.count) if domains else None),
                "merged_from":     absorbed,
            })
            out.append(m)
            if absorbed > 1:
                merges += 1
                log.info("Merged %d candidate(s) into a %d-member cluster (meaning %s, domain %s)",
                         absorbed, len(m["members"]),
                         f"{sem:.3f}" if sem is not None else "n/a", m["grp_coherence"])

        # Drop the seed and everything still touching the emitted cluster.
        before = len(remaining)
        remaining = [i for i in remaining[1:] if not (stamps[i] & current)]
        dropped += before - 1 - len(remaining)

    if merges or dropped:
        log.info("Candidate merge: %d -> %d disjoint cluster(s) (%d absorbed or excluded)",
                 len(candidates), len(out), dropped)
    return out


def queue_skill_proposals(candidates=None, min_score=0.70, limit=10,
                          max_pending=MAX_PENDING_PROPOSALS):
    """
    Write pending SkillProposal nodes for candidates that clear min_score.

    Touches no Memory node and creates no Skill. The default min_score matches
    the threshold the roadmap always documented for proposing; it finally
    means something now that skill_score is clamped to 0-1.

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
                "deferred": 0, "superseded": 0, "pending": 0}

    if candidates is None:
        candidates = find_skill_candidates(limit=limit)

    ranked = [c for c in candidates if float(c.get("skill_score", 0)) >= float(min_score)]
    created = refreshed = skipped = deferred = superseded = 0
    now = datetime.now().isoformat()

    try:
        with driver.session() as s:
            # Fold overlapping candidates together BEFORE the score filter is
            # applied to the result, so a merged cluster is judged as the thing
            # that will actually be offered rather than as its best fragment.
            _, ref_w, _ = skill_weight_floor()
            ranked = merge_overlapping_candidates(s, ranked, ref_w)
            ranked = [c for c in ranked
                      if float(c.get("skill_score", 0)) >= float(min_score)]
            ranked.sort(key=lambda c: -float(c.get("skill_score", 0.0)))

            # Retire the unconfirmable before counting room. The ceiling is a
            # bound on unreviewed WORK, and a proposal no skill leaves space for
            # is not work -- counting it holds the queue shut against the very
            # clusters that would clear it.
            retired = retire_unconfirmable_proposals(s)
            if retired:
                log.info("Retired %d unconfirmable proposal(s) before sweeping", retired)
            superseded += retired

            pending_now = s.run("""
                MATCH (p:SkillProposal {status: 'pending'}) RETURN count(p) AS n
            """).single()["n"]

            for c in ranked:
                # Room check is per-candidate: rejecting something mid-sweep
                # should let the next one through rather than wait a pass.
                if pending_now >= int(max_pending):
                    known = s.run("""
                        MATCH (p:SkillProposal {member_key: $key}) RETURN count(p) AS n
                    """, key=_member_key(c["member_created"])).single()["n"]
                    # A cluster that absorbs what is already queued does not
                    # add unreviewed work, it consolidates it -- so the ceiling
                    # must not block it. Otherwise merging deadlocks exactly
                    # when it is most needed: a queue full of fragments has no
                    # room for the merged proposal that would supersede them,
                    # so the sweep defers every one and the fragments stay
                    # forever. Observed as created=0, deferred=4, pending=26
                    # against a ceiling of 25, with nothing able to change.
                    #
                    # The ceiling was always documented as a bound on
                    # unreviewed work rather than on the graph; this is that
                    # sentence applied to a case it did not anticipate.
                    absorbs = 0
                    if not known:
                        rec = s.run("""
                            MATCH (p:SkillProposal {status: 'pending'})
                            WHERE size(p.member_created) > 0
                              AND all(t IN p.member_created WHERE t IN $created)
                            RETURN count(p) AS n
                        """, created=list(c["member_created"])).single()
                        absorbs = rec["n"] if rec else 0
                    if not known and absorbs < 1:
                        deferred += 1
                        continue
                key = _member_key(c["member_created"])
                rec = s.run("""
                    MERGE (p:SkillProposal {member_key: $key})
                    ON CREATE SET p.proposal_id   = $pid,
                                  p.members       = $members,
                                  p.member_created = $created,
                                  p.status        = 'pending',
                                  p.created_at    = $now,
                                  p.updated_at    = $now,
                                  p.reviewed_at   = null,
                                  p.review_note   = '',
                                  p.skill_id      = null,
                                  p.skill_score   = $score,
                                  p.avg_weight    = $avgw,
                                  p.grp_coherence = $coh,
                                  p.semantic_coherence = $sem,
                                  p.grps          = $grps,
                                  p.previews      = $previews,
                                  p.src_mix       = $srcmix,
                                  // How many overlapping triples this cluster
                                  // absorbed. 1 means the sweep found it whole.
                                  p.merged_from   = $mergedfrom
                    // Refresh scores on a still-pending proposal so the queue
                    // reflects the graph now. Never touch status: a rejected
                    // proposal that is still dense must stay rejected.
                    // Addresses drift as members are recalled; refresh the
                    // display copy so a reviewer never sees a stale one.
                    ON MATCH SET  p.members       = CASE WHEN p.status = 'pending'
                                                        THEN $members ELSE p.members END,
                                  p.updated_at    = CASE WHEN p.status = 'pending'
                                                        THEN $now ELSE p.updated_at END,
                                  p.skill_score   = CASE WHEN p.status = 'pending'
                                                        THEN $score ELSE p.skill_score END,
                                  p.avg_weight    = CASE WHEN p.status = 'pending'
                                                        THEN $avgw ELSE p.avg_weight END,
                                  p.grp_coherence = CASE WHEN p.status = 'pending'
                                                        THEN $coh ELSE p.grp_coherence END,
                                  p.semantic_coherence = CASE WHEN p.status = 'pending'
                                                        THEN $sem ELSE p.semantic_coherence END,
                                  p.previews      = CASE WHEN p.status = 'pending'
                                                        THEN $previews ELSE p.previews END,
                                  p.src_mix       = CASE WHEN p.status = 'pending'
                                                        THEN $srcmix ELSE p.src_mix END,
                                  p.merged_from   = CASE WHEN p.status = 'pending'
                                                        THEN $mergedfrom ELSE p.merged_from END
                    RETURN p.status AS status, p.created_at = $now AS is_new
                """,
                    key=key, pid=str(uuid.uuid4()), members=list(c["members"]),
                    created=list(c["member_created"]),
                    now=now, score=float(c.get("skill_score", 0.0)),
                    avgw=float(c.get("avg_weight", 0.0)),
                    coh=float(c.get("grp_coherence", 0.0)),
                    sem=(float(c["semantic_coherence"])
                         if c.get("semantic_coherence") is not None else None),
                    grps=list(c.get("grps") or []),
                    previews=list(c.get("previews") or []),
                    # Neo4j stores no maps on properties; JSON keeps the mix
                    # readable without inventing a node per source class.
                    srcmix=json.dumps(c.get("src_mix") or {}),
                    mergedfrom=int(c.get("merged_from", 1) or 1),
                ).single()

                if rec and rec["is_new"]:
                    created += 1
                    pending_now += 1
                elif rec and rec["status"] == "pending":
                    refreshed += 1
                else:
                    skipped += 1

                # Retire the fragments this cluster now contains.
                #
                # Without this the merge makes the queue worse rather than
                # better: the merged proposal is added, the triples it was
                # built from stay pending beside it, and now they conflict with
                # their own union as well as with each other. A proposal whose
                # members are all inside a larger pending one is not a second
                # opinion, it is the same finding at lower resolution.
                #
                # Superseded rather than rejected. Rejected means a reviewer
                # said no and the sweep must never re-offer it; this is
                # bookkeeping, and stamping it as a human judgement would make
                # the two indistinguishable later.
                if rec and rec["status"] == "pending" and len(c["member_created"]) > 3:
                    gone = s.run("""
                        MATCH (p:SkillProposal {status: 'pending'})
                        WHERE p.member_key <> $key
                          AND size(p.member_created) > 0
                          AND all(t IN p.member_created WHERE t IN $created)
                        SET p.status        = 'superseded',
                            p.updated_at    = $now,
                            p.superseded_by = $key,
                            p.review_note   = 'Absorbed into a larger cluster'
                        RETURN count(p) AS n
                    """, key=key, created=list(c["member_created"]), now=now).single()
                    n = gone["n"] if gone else 0
                    if n:
                        superseded += n
                        pending_now = max(0, pending_now - n)

            pending = s.run("""
                MATCH (p:SkillProposal {status: 'pending'}) RETURN count(p) AS n
            """).single()["n"]

        if created:
            log.info("Queued %d new skill proposal(s); %d refreshed, %d already rejected",
                     created, refreshed, skipped)
        if deferred:
            log.info("Deferred %d proposal(s): %d already pending review (ceiling %d)",
                     deferred, pending, max_pending)
        return {"created": created, "refreshed": refreshed,
                "skipped_rejected": skipped, "deferred": deferred,
                "superseded": superseded,
                "pending": pending}
    except Exception as e:
        log.warning(f"queue_skill_proposals failed: {e}")
        return {"created": 0, "refreshed": 0, "skipped_rejected": 0,
                "deferred": 0, "superseded": 0, "pending": 0}


def get_skill_proposals(status="pending", limit=50):
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
                MATCH (p:SkillProposal)
                {where}
                OPTIONAL MATCH (m:Memory) WHERE m.created_at IN p.member_created
                WITH p, collect({{created_at: m.created_at,
                                 address:    m.address,
                                 payload:    m.payload,
                                 color:      m.color,
                                 grp: toInteger(split(m.address, '.')[2])}}) AS live
                // A member already compressed into an active Skill cannot be
                // compressed into a second one, so this proposal can never be
                // confirmed while that skill exists. Saying so here is the
                // difference between a queue and a list of things to try.
                OPTIONAL MATCH (bm:Memory)-[:PROCEDURALIZED_FROM]->(bsk:Skill)
                WHERE bm.created_at IN p.member_created
                  AND bsk.status <> 'deprecated'
                // Filter the nulls OUT here, not in Python. An OPTIONAL
                // MATCH that finds nothing still collects one all-null map, so
                // size(blockers) was 1 for every proposal and the ordering
                // below silently did nothing.
                //
                // Carries the owned member's own timestamp now, so the caller
                // can work out which members are still free. A cluster that
                // overlaps an existing skill is not necessarily dead -- its
                // unclaimed members can still be crystallized UNDER that skill,
                // which is how the tree grows instead of stalling.
                WITH p, live,
                     [b IN collect(DISTINCT {{skill_id: bsk.skill_id,
                                             trigger:  bsk.trigger,
                                             taken:    bm.created_at}})
                      WHERE b.skill_id IS NOT NULL] AS blockers
                // Proposals that claim a member of this one. They are
                // ALTERNATIVES, not queue entries: crystallizing this makes
                // every one of them permanently impossible. Nothing said so,
                // so a reader worked through the queue item by item, hit the
                // ownership refusal, and read it as something to repair.
                //
                // Matched on member_created, not address: addresses are
                // rewritten in place on recall, and the proposal stores the
                // timestamps for exactly that reason.
                //
                // Always against pending, whatever `status` this call asked
                // for -- a confirmed or superseded proposal is not an
                // alternative to anything.
                OPTIONAL MATCH (q:SkillProposal)
                WHERE q.status = 'pending'
                  AND q.proposal_id <> p.proposal_id
                  AND any(t IN q.member_created WHERE t IN p.member_created)
                WITH p, live, blockers,
                     [x IN collect(DISTINCT q.proposal_id)
                      WHERE x IS NOT NULL] AS excludes
                RETURN blockers         AS blockers,
                       excludes         AS excludes,
                       p.proposal_id    AS proposal_id,
                       p.member_key     AS member_key,
                       p.member_created AS member_created,
                       live             AS live_members,
                       p.status         AS status,
                       p.skill_score    AS skill_score,
                       p.avg_weight     AS avg_weight,
                       p.grp_coherence  AS grp_coherence,
                       p.semantic_coherence AS semantic_coherence,
                       p.src_mix        AS src_mix,
                       coalesce(p.merged_from, 1) AS merged_from,
                       p.created_at     AS created_at,
                       p.updated_at     AS updated_at,
                       p.reviewed_at    AS reviewed_at,
                       p.review_note    AS review_note,
                       p.skill_id       AS skill_id
                // Actionable proposals first. Score still orders within each
                // group, but a review queue that leads with items nothing can
                // confirm wastes the reviewer's attention on the ones ranked
                // highest -- which is exactly what happened: the top three by
                // score were all blocked.
                ORDER BY size(blockers) ASC, p.skill_score DESC, p.created_at ASC
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

                raw_blockers = [b for b in (d.pop("blockers", None) or [])
                                if b.get("skill_id")]

                # Collapse to one entry per owning skill, keeping which member
                # timestamps each one took.
                by_skill = {}
                for b in raw_blockers:
                    e = by_skill.setdefault(b["skill_id"],
                                            {"skill_id": b["skill_id"],
                                             "trigger":  b.get("trigger"),
                                             "taken":    []})
                    if b.get("taken") is not None:
                        e["taken"].append(b["taken"])
                blockers = list(by_skill.values())
                for b in blockers:
                    b["taken_count"] = len(b["taken"])

                d["blocked_by"] = blockers
                d["blocked"]    = bool(blockers)
                d["excludes"]   = list(d.get("excludes") or [])

                # The members no active skill has claimed, and where they could
                # hang if some of them are claimed.
                #
                # A blocked proposal used to be a dead end that read like a
                # repairable one. It is not dead: crystallizing the free
                # members with `extends` set to the owning skill puts them in
                # the tree underneath it, which is the outcome the overlap was
                # evidence for in the first place. Two members is the floor
                # because a skill needs two sources.
                taken_stamps = {t for b in blockers for t in b["taken"]}
                free = [m for m in live if m.get("created_at") not in taken_stamps]
                d["free_members"] = [m["address"] for m in free]
                d["suggested_parent"] = (
                    max(blockers, key=lambda b: b["taken_count"])["skill_id"]
                    if blockers and len(free) >= 2 else None
                )

                try:
                    d["src_mix"] = json.loads(d.get("src_mix") or "{}")
                except (TypeError, ValueError):
                    d["src_mix"] = {}
                out.append(d)
            return out
    except Exception as e:
        log.warning(f"get_skill_proposals failed: {e}")
        return []


def count_skill_proposals(status="pending"):
    """How many proposals exist, independent of any page limit."""
    driver = get_driver()
    if driver is None:
        return 0
    try:
        with driver.session() as s:
            where = "WHERE p.status = $status" if status else ""
            rec = s.run(f"MATCH (p:SkillProposal) {where} RETURN count(p) AS n",
                        status=status).single()
            return int(rec["n"]) if rec else 0
    except Exception as e:
        log.warning(f"count_skill_proposals failed: {e}")
        return 0


def reject_skill_proposal(proposal_id, note=""):
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
                MATCH (p:SkillProposal {proposal_id: $pid})
                RETURN p.status AS status
            """, pid=proposal_id).single()
            if not rec:
                return False, "no such proposal"
            if rec["status"] == "crystallized":
                return False, "that proposal already became a skill"

            now = datetime.now().isoformat()
            s.run("""
                MATCH (p:SkillProposal {proposal_id: $pid})
                SET p.status      = 'rejected',
                    p.reviewed_at = $now,
                    p.review_note = $note,
                    p.updated_at  = $now
            """, pid=proposal_id, now=now, note=(note or ""))

            # Give back the fragments this cluster absorbed.
            #
            # Merging retires the smaller clusters a large one contains, which
            # is right while the large one is live: they are the same finding
            # at lower resolution, and offering both makes the queue conflict
            # with itself. It stops being right the moment the large one is
            # rejected. Without this, saying no to an 8-member cluster silently
            # says no to the 3-member clusters inside it as well -- and those
            # were never reviewed, so the rejection asserts a judgement nobody
            # made. Worse, the sweep will not rebuild them: superseded is not
            # pending, so ON MATCH leaves it alone forever.
            #
            # Only the ones THIS proposal superseded, and only if they are
            # still superseded -- a fragment that has since been rejected on
            # its own merits keeps that rejection.
            back = s.run("""
                MATCH (p:SkillProposal {proposal_id: $pid})
                MATCH (f:SkillProposal {status: 'superseded',
                                        superseded_by: p.member_key})
                SET f.status        = 'pending',
                    f.superseded_by = null,
                    f.updated_at    = $now,
                    f.review_note   = 'Released when the larger cluster was rejected'
                RETURN count(f) AS n
            """, pid=proposal_id, now=now).single()
            released = back["n"] if back else 0
            if released:
                log.info("Rejected %s; released %d absorbed fragment(s) back to pending",
                         proposal_id[:8], released)
        return True, ("rejected" if not released
                      else f"rejected; {released} absorbed proposal(s) returned to the queue")
    except Exception as e:
        log.warning(f"reject_skill_proposal failed: {e}")
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
                MATCH (p:SkillProposal {proposal_id: $pid})
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


def close_proposal_for_members(member_addresses, skill_id):
    """
    Close the loop after a human crystallizes: if the confirmed member set
    matches a queued proposal, mark it crystallized so the sweep stops
    proposing a cluster that already became a skill.

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
                MATCH (p:SkillProposal {member_key: $key})
                SET p.status      = 'crystallized',
                    p.skill_id    = $sid,
                    p.reviewed_at = $now,
                    p.updated_at  = $now
                RETURN p.proposal_id AS pid
            """, key=_member_key(created), sid=skill_id,
                 now=datetime.now().isoformat()).single()

            # Retire every OTHER pending proposal this skill just made
            # impossible.
            #
            # Only the exactly-matching proposal was ever closed, so a cluster
            # that merely overlapped the new skill stayed pending forever while
            # being unconfirmable -- its members belong to a skill now, and a
            # memory cannot be compressed into two. On the graph where this was
            # found, 22 of 26 pending proposals were blocked that way and 14
            # could never be confirmed by any route. That queue is what a
            # reviewer works through, hitting refusal after refusal.
            n = retire_unconfirmable_proposals(s, exclude_key=_member_key(created))
            if n:
                log.info("Retired %d pending proposal(s) made unconfirmable by skill %s",
                         n, skill_id[:8])
        return bool(res)
    except Exception as e:
        log.warning(f"close_proposal_for_members failed (skill was created): {e}")
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

        # 8. Crystallization candidates.
        #
        # Phase 13.1: this used to run its own query -- hub-shaped ("which
        # memory has strong neighbours"), with no source or colour filter and
        # its own scoring formula. find_skill_candidates() asks the question
        # that actually decides crystallization (mutual density across every
        # pair) and applies the real filters, and the two disagreed
        # systematically: /insights advertised a physics candidate at 0.7957,
        # above the documented propose-at-0.70 bar, that /skill_candidates was
        # structurally incapable of ever returning. Reporting a candidate the
        # confirm path cannot accept is worse than reporting none.
        #
        # One call, one answer. The endpoint is a preview of a real decision,
        # so it previews the real decision.
        crystal_candidates = find_skill_candidates(limit=10)

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


def get_creative_output(output_id: str):
    """
    Phase 7 (revised): fetch one artifact in full.

    Accepts a PREFIX, because the session bundle digest prints only the first
    eight characters of the id and a model can only ask for what it was shown.
    An ambiguous prefix returns None rather than an arbitrary winner -- handing
    back the wrong artifact silently is worse than saying the id was no good.
    """
    driver = get_driver()
    if driver is None or not output_id:
        return None
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (co:CreativeOutput)
                WHERE co.output_id = $exact OR co.output_id STARTS WITH $prefix
                OPTIONAL MATCH (co)-[:INSPIRED_BY]->(m:Memory)
                WITH co, collect(m.address) AS inspired_by
                RETURN co.output_id        AS output_id,
                       co.title            AS title,
                       co.content          AS content,
                       co.artifact_type    AS artifact_type,
                       co.cognition_depth  AS cognition_depth,
                       co.created_at       AS created_at,
                       co.presented_to_user AS presented_to_user,
                       inspired_by
                LIMIT 2
            """, exact=output_id, prefix=output_id)
            hits = [dict(r) for r in rows]

        # An exact hit wins outright even if other ids share it as a prefix.
        for h in hits:
            if h["output_id"] == output_id:
                return h
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            log.warning("get_creative_output: prefix %r is ambiguous", output_id)
        return None
    except Exception as e:
        log.warning("get_creative_output failed: %s", e)
        return None


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
                # of truth as /insights and /skill_candidates. This path was
                # the loosest of the three -- no weight floor at all, so any
                # memory with two neighbours scored -- which meant Nova was
                # being shown "candidates" during idle cognition that no
                # confirm path would accept.
                ctx["crystallization_candidates"] = find_skill_candidates(limit=10)

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
