"""
MMU FastAPI Server — Phase 7
============================
SQLite and NetworkX removed. Neo4j + LightIndexV2 are the sole data stores.
Exposes REST endpoints called by LM Studio via MCP bridge or direct HTTP.

Endpoints:
  GET  /health               -- ping / status check
  GET  /memories             -- dump all memories (Neo4j)
  POST /remember             -- save a new memory
  POST /recall               -- query memories by keyword
  GET  /session_bundle       -- pre-assembled context bundle by GRP domain
  POST /reflect              -- end-of-session reflection (Phase 4: includes flagged memories)
  POST /pin                  -- add a pinned (Red) base memory
  DELETE /memories/{address} -- remove a specific memory
  GET  /neighbors/{address}  -- CO_RECALLED neighbors
  POST /dedup                -- remove duplicate memories
  GET  /graph                -- graph state summary (Neo4j-backed)

Phase 4 self-correction:
  POST /flag_recall          -- mark a memory as irrelevant (increments false_recall_count)
  GET  /flagged              -- list memories with false_recall_count >= threshold
  POST /audit_memory         -- apply Nova's correction to a flagged memory

Phase 5 observability + versioning:
  GET  /insights             -- comprehensive memory graph health snapshot

Phase 6 active maintenance (consolidation):
  POST /maintain             -- run maintenance pass: stem collisions, cross-GRP clusters,
                               stale Blue memories, domain gaps; returns structured summary
                               for Phase 7 cognition prompt injection

Phase 7 default mode cognition SUPPORT (this server never calls an LLM):
  GET  /activity                    -- idle seconds since last real interaction
  POST /activity_ping               -- explicit activity signal for any bridge
  GET  /idle_prompt?depth=          -- assembled cognition prompt, model-agnostic
  POST /creative_output             -- store an artifact Nova produced
  GET  /creative_outputs            -- list artifacts (unseen_only supported)
  POST /creative_outputs/mark_seen  -- mark artifacts as surfaced to the user

Phase 8 episodic memory / session continuity (this server still never calls an LLM):
  POST /session_close               -- write a session summary (usually from the idle daemon)
  GET  /session_resume/latest       -- most recently closed session
  GET  /session_resume/{session_id} -- a specific closed session + memories HAPPENED_IN it
  /remember and /rate now also accept an X-MMU-Session header to tag HAPPENED_IN edges

Phase 6.5 valence (INERT v1 -- stored but does not change behavior):
  POST /rate                        -- rate a memory like/dislike/clear (Nova or the user)
  GET  /unrated_memories            -- memories with no rating yet (for idle pass hints)

  The LLM call lives in mmu_idle_daemon.py, a separate process. If that daemon
  crashes, this server and the MCP bridge are unaffected: Nova simply stops
  thinking between conversations until it is restarted.

Phase 9 semantic embedding layer (DELIBERATE, SCOPED EXCEPTION to the above):
  POST /backfill_embeddings         -- embed every Memory node that has none yet

  Up to Phase 8 this server never called a model at all. Phase 9 breaks that
  line ON PURPOSE and NARROWLY. EmbeddingClient below calls exactly one
  endpoint, /v1/embeddings, and never /chat/completions. It converts text to
  a vector; it does not generate, summarize, judge, or frame anything, so none
  of the framing/bias concerns the original boundary was drawn to contain
  apply to it. This is vector math, not cognition.

  The exception is necessary rather than convenient: /recall is a synchronous
  REST endpoint, and a brand-new prompt has to be embedded at query time.
  There is no idle daemon in the loop for a live /recall, so deferring the
  query embedding to mmu_idle_daemon.py cannot work for arbitrary new prompts.

  Every embedding call degrades soft. If LM Studio is down or the embedding
  model is unloaded, /remember still saves (without a vector) and /recall
  still returns keyword results. Semantic recall is strictly additive: it can
  only ever add memories the keyword gate missed, never remove a keyword hit,
  unless MMU_SEMANTIC_FLOOR is explicitly raised above 0.
"""

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import os
import re
import uuid
import time
import logging
from datetime import datetime
import neo4j_layer as n4j
from light_index_v2 import LightIndexV2
import ingest

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mmu_server")

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

ARCHIVE_THRESH = int(__import__("os").environ.get("MMU_ARCHIVE_THRESH", "10"))
V2_INDEX_PATH  = __import__("os").environ.get("MMU_INDEX_PATH", "/data/memory_index_v2.json")

# -- Phase 9: semantic embedding layer --
# Model name AND dimension are both env vars so swapping the embedding model
# is a re-index, not a code change. The dimension must match what the model
# actually returns; it is baked into the Neo4j vector index at creation time.
_env = __import__("os").environ
EMBEDDING_BASE  = _env.get("MMU_EMBEDDING_BASE",  "http://host.docker.internal:1234/v1")
EMBEDDING_MODEL = _env.get("MMU_EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")
EMBEDDING_DIM   = int(_env.get("MMU_EMBEDDING_DIM", "768"))
EMBEDDING_ON    = _env.get("MMU_EMBEDDING_ENABLED", "true").lower() == "true"

# Optional bearer token. LM Studio can require one (Developer -> server
# settings), and newer builds may have it on by default. Without it every call
# returns 401, which previously surfaced as "backend unreachable" -- true in
# effect, misleading as a diagnostic, and the difference between a five-minute
# fix and an hour of looking in the wrong place.
EMBEDDING_API_KEY = _env.get("MMU_EMBEDDING_API_KEY", "").strip()

# ── Access control ───────────────────────────────────────────
# Shared secret for MMU's own API. Unset by default so an existing local
# setup keeps working untouched. When set, EVERY endpoint except /health
# requires it.
#
# Deliberately not limited to "mutating" endpoints: /memories and /export dump
# the entire graph, and /recall, /session_bundle, /insights, /graph, /neighbors
# and /idle_prompt all return memory content. Protecting only writes would
# leave the whole graph readable. Covering everything by default also means a
# new endpoint is protected without anyone having to remember to annotate it.
API_KEY = _env.get("MMU_API_KEY", "").strip()

# Paths reachable without the key. /health only, because the Docker healthcheck
# calls it and a container that cannot report health is worse than a health
# endpoint that reveals a memory count.
AUTH_EXEMPT_PATHS = ("/health",)

# CORS origins, comma-separated. EMPTY BY DEFAULT, which disables CORS entirely.
#
# This was allow_origins=["*"], which combined with no auth meant any website a
# user visited could read /export or call /forget_all against localhost from the
# browser -- and a wildcard origin makes the response readable, so exfiltration,
# not just blind writes. Nothing in this project needs CORS: the MCP bridge and
# the idle daemon are both server-side. Set this only if you build a browser UI,
# and then list exact origins.
CORS_ORIGINS = [o.strip() for o in _env.get("MMU_CORS_ORIGINS", "").split(",") if o.strip()]

# Similarity floor for DOWN-RANKING keyword-gate hits (the stem-collision
# defense). Scale warning: Neo4j normalizes cosine into [0, 1] where 0.5 means
# orthogonal, NOT 0.0. A value below 0.5 therefore filters nothing.
# Default 0.0 = disabled, so Phase 9 cannot regress the keyword path on day
# one. Every result carries its measured semantic_score, so a real threshold
# can be chosen from observed data instead of guessed.
SEMANTIC_FLOOR  = float(_env.get("MMU_SEMANTIC_FLOOR", "0.0"))

# Phase 11: /ingest reads files from disk, so it needs a confinement root.
# Without one, source_path is an arbitrary-file-read primitive against the
# container filesystem for anything that can reach the endpoint.
DOC_ROOT = _env.get("MMU_DOC_ROOT", "/docs")

# Web ingestion. OFF by default, and it should stay off unless someone has
# decided they want it: a page can contain text aimed at the model, and
# anything saved here becomes durable memory that /recall keeps re-feeding.
# Stage 1 only fetches URLs the caller names; no model chooses its own targets.
WEB_ENABLED = _env.get("MMU_WEB_ENABLED", "false").lower() == "true"

# Phase 13.2: how many routines a single recall may deliver. Small on purpose --
# a routine substitutes for its source memories, so several firing at once
# withholds a lot of context on the strength of a similarity score.
ROUTINE_RECALL_MAX = int(_env.get("MMU_ROUTINE_RECALL_MAX", "2"))

# Phase 13.1: whether a model may confirm a crystallization itself.
#
# OFF by default, and the default is the recommendation. Crystallizing demotes
# its source memories to Blue -- it restructures memory rather than adding to
# it -- and Phase 12 put that behind a human on purpose. With this on, a model
# that drafts a proposal can also approve its own draft, and the review stops
# being a review.
#
# It exists because testing the full loop with a model requires it, and because
# the operator asked for it with the trade-off already on the table. Enforced here rather
# than only in the MCP tool list: a tool list is a client-side promise, and the
# server should not depend on a client keeping one.
#
# Reverse anything a model gets wrong with POST /routines/{id}/uncrystallize.
MODEL_MAY_CRYSTALLIZE = _env.get("MMU_ALLOW_MODEL_CRYSTALLIZE", "false").lower() == "true"


def _guard_model_write(source: str):
    """Refuse a model-originated crystallization unless it is explicitly enabled."""
    if (source or "").strip().lower() == "model" and not MODEL_MAY_CRYSTALLIZE:
        raise HTTPException(
            403,
            "This request is tagged as coming from a model, and model-initiated "
            "crystallization is disabled. Crystallizing demotes the source "
            "memories and is a human decision by default. Set "
            "MMU_ALLOW_MODEL_CRYSTALLIZE=true to allow it."
        )

# -- Phase 10: aging redesign --
# Archiving requires BOTH sustained disuse (count) AND real elapsed time.
# Count alone was the shipped behaviour and it is a defect: twenty recalls
# archive a memory whether they span three months or twenty minutes, so any
# burst of activity archives the graph. It did exactly that twice during
# Phase 9 and Phase 11 validation, both times needing a manual restore.
#
# Green --(use >= HOLD_THRESH)--------------------> Yellow  (cooling, pre-hold)
# Yellow --(use >= ARCHIVE_THRESH AND stale)------> Blue    (archived)
# Yellow --(recalled)-----------------------------> Green
# Blue   --(recalled)-----------------------------> Yellow  (warm from archive)
HOLD_THRESH      = int(_env.get("MMU_HOLD_THRESH", "20"))
ARCHIVE_MIN_DAYS = float(_env.get("MMU_ARCHIVE_MIN_DAYS", "14"))

# Don't age at all until the graph has this many conversational memories.
# On a small graph "unused" carries no information: with five memories, a
# handful of recalls pushes everything toward the threshold at once, and the
# first thing a new user would see is their memories going dormant for no
# reason they can perceive. Documents never age and are not counted.
AGING_MIN_MEMORIES = int(_env.get("MMU_AGING_MIN_MEMORIES", "25"))

# -- Phase 10: proactive surfacing --
# Hard cap on unrequested items per call. Proactive context that floods the
# window defeats its own purpose, and it spends context the model did not
# choose to spend. Set to 0 to disable anticipation entirely.
ANTICIPATE_MAX = int(_env.get("MMU_ANTICIPATE_MAX", "3"))

# How many times anticipation may fire in a single conversation. Without this,
# a long conversation gets an unrequested block on every single recall, which
# is how a helpful feature becomes noise. 0 disables the per-session limit.
ANTICIPATE_PER_SESSION = int(_env.get("MMU_ANTICIPATE_PER_SESSION", "5"))

# How many conversations to remember anticipation state for. Bounded so a
# long-running server cannot accumulate session keys forever.
ANTICIPATE_STATE_MAX = int(_env.get("MMU_ANTICIPATE_STATE_MAX", "50"))

# Weight applied to semantic hits during ranking. Deliberately below
# W_SIMILAR (0.85) in light_index_v2 so a semantic hit can never outrank a
# real keyword hit -- semantic recall fills gaps, it does not take over.
W_SEMANTIC      = float(_env.get("MMU_SEMANTIC_WEIGHT", "0.80"))

SOURCE_LABELS = {
    0: "Conversation",
    1: "AI-Self",
    2: "Document",
    3: "Web",
    4: "Background Cognition",   # Phase 7
    5: "Voice Note",             # Phase 11 -- reserved; transcription deferred
}

# ─────────────────────────────────────────────
#  PHASE 7 -- ACTIVITY TRACKING
# ─────────────────────────────────────────────
#
# The idle daemon needs to know when the user last interacted with the system.
# The MCP bridge cannot tell it: the bridge is a passive stdio tool server
# that only sees tool calls, and it has no LLM client of its own.
#
# The MMU server, however, sees every memory operation from every bridge
# (LM Studio, Claude, anything future). Tracking activity here is therefore
# both more complete and model-agnostic, and it means mmu_mcp_server.py needs
# no changes at all -- the one file whose failure would break live conversation
# stays untouched.
#
# Requests carrying the X-MMU-Source: idle-daemon header are ignored, so the
# daemon's own traffic never resets the timer it is watching.

ACTIVITY_PATHS = (
    "/recall", "/remember", "/pin", "/session_bundle",
    "/reflect", "/audit_memory", "/flag_recall", "/activity_ping",
    # Phase 8: /rate also carries X-MMU-Session and writes HAPPENED_IN, so it
    # needs to touch activity too -- otherwise a conversation tail that's
    # just rating memories (no new /remember or /recall calls) never resets
    # the idle timer, and its session_id is never captured for session_close.
    "/rate",
)

IDLE_DAEMON_HEADER = "idle-daemon"

_last_activity_at = datetime.now()
_last_activity_path = "startup"
_last_session_id = None     # Phase 8: most recent X-MMU-Session header seen
_last_session_closed = None # Phase 8: session_id already handed to /session_close


def _touch_activity(path: str, session_id: Optional[str] = None):
    global _last_activity_at, _last_activity_path, _last_session_id
    _last_activity_at   = datetime.now()
    _last_activity_path = path
    if session_id:
        _last_session_id = session_id


# ─────────────────────────────────────────────
#  PYDANTIC MODELS
# ─────────────────────────────────────────────

class MemoryIn(BaseModel):
    keywords:  list[str]
    payload:   str
    priority:  int = 5
    color:     str = "Green"
    src_type:  int = 0
    src_chunk: int = 0
    src_line:  int = 0
    note:      Optional[str] = ""
    grp_code:  int = 500
    # Provenance for something learned from a page. Supplying a URL IS the
    # claim: the server sets src_type=3 ("Web") and stores the URL as the note,
    # rather than letting the caller assert a label independently. That keeps
    # "this says Web" and "this came from a page" from drifting apart.
    source_url: Optional[str] = None
    # Phase 7 -- set by the idle daemon when Nova saves during a cognition pass
    from_idle_pass:  bool = False
    cognition_depth: Optional[str] = None

class IngestIn(BaseModel):
    source_path:   str
    source_type:   str  = "pdf"          # pdf | markdown | text
    grp_code:      int  = 602            # caller-specified, never auto-classified
    priority:      int  = 5
    title_note:    Optional[str] = None
    target_words:  int  = 300
    overlap_words: int  = 30
    dry_run:       bool = False          # inspect chunking without writing
    max_chunks:    int  = 0              # 0 = no cap


class CrystallizeIn(BaseModel):
    # Phase 12. Explicit member addresses rather than a cluster id, so the
    # caller states exactly which memories are being compressed instead of
    # trusting a proposal that may have gone stale between proposal and
    # confirmation -- aging rewrites addresses, so a cluster id resolved later
    # could name different memories than the ones the user reviewed.
    member_addresses: List[str]
    trigger:          str
    procedure:        str
    confidence:       float = 0.0
    confirmed:        bool  = False   # must be explicitly true
    # Phase 13.2: optional parent, so a branch can be created in one step.
    # A tree built by remembering to call /link afterwards is a tree that
    # mostly does not get built -- the first four routines anyone tried to
    # branch off a root ended up with no edges at all.
    extends:          Optional[str] = None


class RecallIn(BaseModel):
    prompt:       str
    top_k:        int  = 10
    skip_pinned:  bool = False   # exclude Red pinned from results (use when session_bundle already delivered them)

class ReflectIn(BaseModel):
    session_turns: List[dict]   # [{turn, user, response}]

class SessionCloseIn(BaseModel):
    # Phase 8
    session_id:      str
    summary:         str
    emotional_tone:  Optional[str]       = None
    decisions_made:  Optional[List[str]] = None
    source:          str                 = "unknown"   # "transcript" | "activity-only"
    turns_count:     int                 = 0

class FlagIn(BaseModel):
    address: str                # full memory address to flag
    reason:  str = ""           # why it was irrelevant (used in audit prompt)

class AuditIn(BaseModel):
    address:      str
    new_color:    Optional[str]       = None
    new_priority: Optional[int]       = None
    new_keywords: Optional[List[str]] = None
    audit_note:   str                 = ""

class MaintainIn(BaseModel):
    stale_days:            int = 30   # Blue memories inactive this many days are flagged
    cluster_weight_thresh: int = 5    # Minimum CO_RECALLED weight to report cross-GRP link
    cluster_min_edges:     int = 3    # Reserved for future cluster density filtering

class CreativeOutputIn(BaseModel):
    title:           str
    content:         str
    artifact_type:   str = "reflection"
    cognition_depth: str = "medium"
    inspired_by:     Optional[List[str]] = None

class MarkSeenIn(BaseModel):
    output_ids: Optional[List[str]] = None   # None marks every unseen artifact

class RateIn(BaseModel):
    address:       str
    val_type:      str              # "like" | "dislike" | "clear"
    intensity:     int = 5          # 1-9 (ignored when val_type=="clear")
    emotion_label: Optional[str] = None   # Phase 6.6: named emotion (e.g. "Proud", "Frustrated")

# ─────────────────────────────────────────────
#  MMU CORE
# ─────────────────────────────────────────────

# ───────────────────────────────────────────
#  PHASE 9 -- EMBEDDING CLIENT (scoped exception, see module docstring)
# ───────────────────────────────────────────


# ───────────────────────────────────────────
#  PHASE 10 -- ANTICIPATION RATE LIMITING
# ───────────────────────────────────────────
#
# Per-conversation state: how many times anticipation has fired, and which
# addresses it already surfaced. Both are needed -- a cap alone still lets the
# same three memories be offered five times running.
#
# Keyed on the X-MMU-Session header (captured by _touch_activity), NOT on the
# Session node, because POST /recall mints a fresh UUID per call and Session
# nodes are therefore per-recall rather than per-conversation. See
# phase11_deploy_report.md 6.5. When no header is present -- a direct curl, a
# client that does not send one -- everything collapses to a single "anonymous"
# bucket, which rate-limits conservatively rather than not at all.
#
# In-process and deliberately not persisted: it is conversation-scoped state,
# and losing it on restart is correct, not a gap.

_anticipation_state = {}   # session_id -> {"count": int, "surfaced": set()}


def _anticipation_bucket(session_id):
    key = session_id or "anonymous"
    st = _anticipation_state.get(key)
    if st is None:
        # Evict oldest insertions once over the cap. dicts preserve insertion
        # order, so the first key is the least recently created bucket.
        while len(_anticipation_state) >= ANTICIPATE_STATE_MAX:
            _anticipation_state.pop(next(iter(_anticipation_state)))
        st = {"count": 0, "surfaced": set()}
        _anticipation_state[key] = st
    return st


class EmbeddingClient:
    """
    OpenAI-compatible embeddings client. Mirrors LMStudioClient's shape in
    mmu_idle_daemon.py (.available() / one call method) so the two read the
    same, but talks to /v1/embeddings ONLY. There is no chat method here, and
    there should never be one -- that is the whole point of the boundary.

    Uses urllib rather than requests because the mmu-server image does not
    install requests, and Phase 9 should not add a dependency to the container
    that serves live recall.
    """

    def __init__(self, base=None, model=None, dim=None, api_key=None):
        self.base    = (base or EMBEDDING_BASE).rstrip("/")
        self.model   = model or EMBEDDING_MODEL
        self.dim     = dim or EMBEDDING_DIM
        self.enabled = EMBEDDING_ON
        self.api_key = api_key if api_key is not None else EMBEDDING_API_KEY
        self._warned = False
        # Set when the backend answers but rejects our credentials. Kept
        # separate from "unreachable" so the self-check can say which.
        self.auth_failed = False

    def _headers(self, extra=None):
        h = dict(extra or {})
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def available(self):
        if not self.enabled:
            return False
        try:
            import urllib.request, urllib.error
            req = urllib.request.Request(f"{self.base}/models",
                                         headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.auth_failed = False
                    return r.status == 200
            except urllib.error.HTTPError as e:
                # 401/403 means the server IS there and is refusing us. Saying
                # "unreachable" here sends people to check whether the server is
                # running, which it is.
                self.auth_failed = e.code in (401, 403)
                if self.auth_failed:
                    log.error(
                        "Embedding backend at %s rejected the request (HTTP %d). "
                        "It is running but requires an API token. Set "
                        "MMU_EMBEDDING_API_KEY, or turn the token requirement off "
                        "in your server's settings.", self.base, e.code)
                return False
        except Exception:
            self.auth_failed = False
            return False

    def embed(self, text, timeout=30):
        """
        text -> list[float], or None on any failure.

        Never raises. Callers treat None as "no semantic signal available"
        and fall back to pure keyword behaviour.
        """
        if not self.enabled or not text:
            return None
        try:
            import urllib.request, json as _json
            body = _json.dumps({"input": text, "model": self.model}).encode("utf-8")
            req  = urllib.request.Request(
                f"{self.base}/embeddings",
                data=body,
                headers=self._headers({"Content-Type": "application/json"}),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = _json.loads(r.read().decode("utf-8"))
            vec = payload["data"][0]["embedding"]

            if len(vec) != self.dim:
                # A dimension mismatch means the loaded model is not the one
                # the vector index was built for. Writing it would poison the
                # index, so refuse loudly and keep the keyword path intact.
                log.error(
                    "Embedding dim mismatch: model %s returned %d, index expects %d. "
                    "Refusing to write. Fix MMU_EMBEDDING_DIM and re-index.",
                    self.model, len(vec), self.dim
                )
                return None
            self._warned = False
            return vec

        except Exception as e:
            if not self._warned:
                log.warning("Embedding call failed (%s) -- degrading to keyword-only "
                            "recall until it recovers: %s", self.base, e)
                self._warned = True
            return None


EMB = EmbeddingClient()


def _routine_embedding_text(trigger, procedure):
    """
    The single canonical definition of "what text represents a routine".

    Same discipline as _embedding_text() for memories, and for the same reason:
    vectors are only comparable if the text that produced them was assembled
    the same way, so creation and the reindex backfill must both come here.

    Trigger first. It is the part phrased like a query -- "asked to explain X"
    reads much closer to what someone actually types than the procedure does --
    and it carries the most weight for matching.
    """
    return f"{trigger or ''}\n{procedure or ''}".strip()


def _link_parent(routine_id, parent_id):
    """
    Attach a freshly created routine under a parent, if one was named.

    Best-effort and reported rather than raised: the routine is already committed
    and correct on its own, and a bad parent id should not look like a failed
    crystallization. Returns the parent id on success, or a string explaining
    why not, which the caller passes straight back so a wrong id is visible
    instead of silently producing an orphan.
    """
    if not parent_id:
        return None
    resolved, why = n4j.resolve_routine_id(parent_id)
    if not resolved:
        return f"not linked: {why}"
    try:
        if n4j.link_routines(routine_id, resolved):
            return resolved
        return f"not linked: no routine with id {parent_id}"
    except ValueError as e:
        return f"not linked: {e}"
    except Exception as e:
        log.warning("link to parent %s failed for %s: %s", parent_id, routine_id, e)
        return f"not linked: {e}"


def _index_routine(routine_id, trigger, procedure):
    """
    Make a routine retrievable: embed it and link it into the Keyword graph.

    Best-effort and never raises. A routine that fails to index is still a
    perfectly good routine, it is just invisible until /routines/reindex catches
    it -- exactly how an un-embedded memory is treated. Failing the
    crystallization over it would undo a human's decision for a reason that
    has nothing to do with the decision.
    """
    embedded = keyworded = 0
    text = _routine_embedding_text(trigger, procedure)
    try:
        vec = EMB.embed(text)
        if vec and n4j.write_routine_embedding(routine_id, vec):
            embedded = 1
        elif not vec:
            log.warning("No embedding for routine %s -- reindex will catch it", routine_id)
    except Exception as e:
        log.warning("Routine embedding failed for %s (routine still created): %s", routine_id, e)

    try:
        terms = ingest.extract_keywords(text)
        keyworded = n4j.link_routine_keywords(routine_id, terms)
    except Exception as e:
        log.warning("Routine keyword linking failed for %s: %s", routine_id, e)

    return {"embedded": bool(embedded), "keywords": keyworded}


def _embedding_text(keywords, payload):
    """
    The single canonical definition of "what text represents a memory".

    Every write path and the backfill MUST go through this. They previously
    disagreed -- /remember embedded the payload alone while the backfill
    embedded keywords plus payload -- which silently produced two different
    vector spaces in the same index depending on how a memory happened to be
    created. Vectors are only comparable if the text that produced them was
    assembled the same way, so this lives in exactly one place.

    Keywords are included because they are what the keyword gate matches on;
    a memory's indexed terms are part of what it means.
    """
    if isinstance(keywords, str):
        kws = keywords
    else:
        kws = ", ".join(k.strip().lower() for k in (keywords or []) if k and k.strip())
    return f"{kws}. {payload or ''}".strip(". ").strip()



class MMUCore:
    def __init__(self):
        self.v2_index = LightIndexV2(path=V2_INDEX_PATH)
        card_count = len(self.v2_index.shortcut_cache)
        print(f"MMUCore ready | {card_count} memories in v2 index | Phase 3 (Neo4j + v2 only)")

    # ── Address helpers ───────────────────────

    def _gen_addr(self, con, pri, grp, use, arc, st=0, sc=0, sl=0, val=0):
        # Phase 6.5: ~VAL segment (TTS: TT=type 00/01/02, S=intensity 0-9)
        return f"{con:03d}.{pri:03d}.{grp:03d}.{use:03d},{arc:03d}~{val:03d}|{st}.{sc:03d}.{sl:03d}"

    def _parse_addr(self, address):
        # Phase 6.5: ~VAL is optional so old addresses still parse during migration
        # Phase 8 hardening: re.search (not re.match) so a model that pastes the
        # whole "[Color | GRP nnn | address]" display line instead of just the
        # address still parses correctly -- the address substring is found
        # wherever it occurs rather than requiring it at position 0. Observed
        # in production: gemma sent address="[Green | GRP 602 | 075.003...]"
        # to /rate and got a false "not found" 404 because re.match anchors
        # at the start and "[" never matches \d.
        m = re.search(
            # Phase 12: CON is {3,} rather than exactly 3.
            #
            # _gen_addr formats it with {con:03d}, which is a MINIMUM width, so
            # the 1000th memory produces "1000.005.602..." while this pattern
            # still demanded exactly three digits. Combined with re.search
            # (Phase 8 hardening, so a pasted display line still parses) it did
            # not fail -- it matched one character in and returned con=000,
            # silently handing back a different memory's identity. Aging then
            # rewrote the address around that wrong CON.
            #
            # Caught at 954 memories, 46 short of the ceiling. Widening the
            # group is backward compatible: existing 3-digit addresses parse
            # exactly as before.
            r"(\d{3,})\.(\d{3})\.(\d{3})\.(\d{3}),(\d{3})(?:~(\d{3}))?\|(\d+)\.(\d{3})\.(\d{3})",
            address
        )
        if m:
            k = ["con", "pri", "grp", "use", "arc", "val", "src_type", "src_chunk", "src_line"]
            groups = list(m.groups())
            # val group (index 5) is None when ~VAL not yet present -- default to 0
            if groups[5] is None:
                groups[5] = "0"
            return dict(zip(k, [int(x) for x in groups]))
        return None

    # ── Core operations ───────────────────────

    def add_memory(self, keywords, payload, priority=5, color="Green",
                   src_type=0, src_chunk=0, src_line=0, note="", grp=500,
                   from_idle_pass=False, cognition_depth=None, embed=True):
        """
        The single write primitive. Every memory in the graph is created here.

        Phase 9 note: embedding lives HERE rather than in the /remember
        endpoint. Any caller that creates a memory -- /remember, /pin, Phase
        11's /ingest, anything future -- gets a vector automatically and
        cannot forget to ask for one. The alternative (embedding per endpoint)
        meant every new write path silently produced memories that semantic
        recall could not see, discoverable only by remembering to run the
        backfill afterwards.

        embed=False is available for bulk paths that intend to batch their
        embedding work themselves; the backfill will pick up anything skipped.
        """
        # CON must be one past the highest in use, not the memory count.
        # count()+1 collides after any deletion: delete 5 of 100 and the next
        # write claims 96, which already exists. The graph carries 5 duplicated
        # CONs from exactly this. max()+1 is stable under deletion.
        # src_chunk and src_line are 3-digit address fields too. A document
        # yielding 1000+ chunks, or a 1000+ page PDF, would overflow them and
        # corrupt the address rather than failing. Clamp and warn: losing
        # provenance precision on an enormous document is recoverable,
        # silently writing a malformed address is not.
        if src_chunk > 999 or src_line > 999:
            log.warning(
                "Address field overflow clamped (chunk=%s line=%s). Provenance "
                "for this memory is truncated; the address schema allows 3 "
                "digits per field.", src_chunk, src_line
            )
            src_chunk = min(src_chunk, 999)
            src_line  = min(src_line, 999)

        con  = n4j.get_next_con()
        addr = self._gen_addr(con, priority, grp, 0, 0, src_type, src_chunk, src_line)
        now  = datetime.now().isoformat()
        kw_list = [k.strip().lower() for k in keywords]

        n4j.write_memory(
            address         = addr,
            keywords_str    = ",".join(kw_list),   # comma-joined string — required by write_memory
            payload         = payload,
            color           = color,
            priority        = priority,
            src_type        = src_type,
            src_chunk       = src_chunk,
            src_line        = src_line,
            note            = note or "",
            created_at      = now,
            from_idle_pass  = from_idle_pass,
            cognition_depth = cognition_depth
        )

        self.v2_index.add(addr, kw_list, color=color, priority=priority, src_type=src_type)
        self.v2_index.bake_similar(addr, kw_list)
        self.v2_index.save()

        # Phase 9: embed after the node exists in Neo4j, never before --
        # write_embedding MATCHes on the address. A failure here must never
        # fail the save: the memory is already durably committed, and an
        # un-embedded memory is simply one the backfill will catch later.
        self._last_embedded = False
        if embed:
            try:
                vec = EMB.embed(_embedding_text(kw_list, payload))
                if vec:
                    self._last_embedded = n4j.write_embedding(addr, vec)
                else:
                    log.warning("No embedding for %s -- saved without one, "
                                "backfill will catch it", addr)
            except Exception as e:
                log.warning("Embedding step failed for %s (memory still saved): %s",
                            addr, e)

        return addr

    def _tokenize(self, prompt):
        """Prompt -> (words+bigrams set, 4-char stems set)."""
        raw_words = prompt.lower().split()
        bigrams   = [f"{raw_words[i]} {raw_words[i+1]}"
                     for i in range(len(raw_words) - 1)]
        words     = set(raw_words) | set(bigrams)
        stems     = {w[:4] for w in words if len(w) >= 4}
        return words, stems

    def _v2_recall(self, words, stems, top_k, skip_pinned=False, prompt_text=""):
        """
        Gate → semantic stage → shortcut expansion → Neo4j payload hydration.
        skip_pinned: exclude Red pinned from results (they were already delivered
                     by session_bundle — frees top_k slots for topic-relevant content).
        prompt_text: the raw prompt, used for embedding. Distinct from the
                     joined token set, which contains bigrams and is word-order
                     scrambled -- fine for keyword lookup, bad for an encoder.
        Returns (results, elapsed_ms) or (None, 0) on error.
        """
        try:
            t0         = time.perf_counter()
            prompt_str = " ".join(words)

            direct, pinned = self.v2_index.gate(prompt_str)

            # ── Phase 9: semantic stage ──
            # The original early-return tested "not direct and not pinned".
            # gate() returns EVERY Red memory as pinned unconditionally, so
            # with any Red memory in the graph that condition is never true and
            # a fallback hung off it would be dead code. The meaningful test is
            # whether the gate found anything TOPICAL, i.e. whether direct is
            # empty; Red pins are ambient context and say nothing about
            # relevance to this prompt.
            qvec     = None
            sem_rows = []
            if EMB.enabled:
                qvec = EMB.embed(prompt_text or prompt_str)
                if qvec and not direct:
                    sem_rows = n4j.semantic_search(
                        qvec, top_k=top_k, exclude_addrs=pinned
                    )

            if not direct and not pinned and not sem_rows:
                return [], (time.perf_counter() - t0) * 1000

            expanded, cold = self.v2_index.expand(direct)

            if cold:
                co_results, _ = n4j.graph_recall(
                    words, stems, top_k=top_k, expand_corecall=False
                )
                if co_results:
                    for r in co_results:
                        a = r["address"]
                        if a not in direct and a not in pinned:
                            expanded[a] = (r.get("score", 0.5), "corecall-live")

            # ── Phase 9: fold semantic hits into the candidate pool ──
            # Rescale first. Neo4j reports cosine on a normalized [0, 1] scale
            # where 0.5 is orthogonal; map it back to raw [0, 1] for positive
            # similarity so it is comparable to the index's own weights, then
            # apply W_SEMANTIC to keep semantic hits strictly below keyword
            # hits in the ordering.
            for row in sem_rows:
                a = row["address"]
                if a in direct or a in pinned:
                    continue
                raw   = max(0.0, (row["score"] - 0.5) * 2.0)
                score = raw * W_SEMANTIC
                if a not in expanded or score > expanded[a][0]:
                    expanded[a] = (score, "semantic")

            ranked_pinned = set() if skip_pinned else pinned

            # Phase 9: over-fetch before the floor, then trim back to top_k.
            # The floor removes rows, so cutting to top_k first would return
            # fewer than top_k results while perfectly good candidates sat
            # just below the cut -- filtering must happen on a wider pool.
            # Over-fetch whenever there is any semantic signal: the floor removes
            # rows, and the tiebreak below needs a wider pool than top_k to be able
            # to promote a genuinely better match from under the cut.
            # The tiebreak can only reorder candidates rank() actually returns,
            # and rank() cuts ties arbitrarily. With 127 document chunks sharing
            # one domain vocabulary a prompt can tie 50+ rows at score 1.00, so a
            # narrow window is still a lottery: the passage that answers the
            # prompt may never reach the tiebreak. Score a wide pool instead --
            # one Cypher call with N addresses, cheap relative to being wrong.
            fetch_k = min(max(top_k * 10, 50), 200) if (SEMANTIC_FLOOR > 0 or qvec) else top_k
            ranked = self.v2_index.rank(direct, ranked_pinned, expanded,
                                        top_k=fetch_k)

            # ── Phase 9: similarity floor + score annotation ──
            # Annotation always runs, so the data needed to choose a real
            # threshold is visible in every response from day one. Dropping
            # only runs when SEMANTIC_FLOOR > 0, and never touches pinned or
            # semantic rows, nor any memory that has no embedding yet -- a
            # missing embedding means "no opinion", not "irrelevant".
            if qvec and ranked:
                sims = n4j.similarity_for_addresses(
                    [r["address"] for r in ranked], qvec
                )
                kept = []
                for r in ranked:
                    sim = sims.get(r["address"])
                    if sim is not None:
                        r["semantic_score"] = round(sim, 3)
                    if (SEMANTIC_FLOOR > 0
                            and sim is not None
                            and sim < SEMANTIC_FLOOR
                            and r["via"] not in ("pinned", "semantic")):
                        log.info("semantic floor dropped %s (via=%s sim=%.3f < %.3f)",
                                 r["address"], r["via"], sim, SEMANTIC_FLOOR)
                        continue
                    kept.append(r)
                ranked = kept

                # ── Phase 11: semantic tiebreak ──
                # Every keyword-gate hit scores exactly W_DIRECT = 1.00, so a
                # prompt matching more memories than top_k had its survivors
                # chosen by set-iteration order -- arbitrary, and unstable
                # between identical calls. Harmless at 93 curated memories;
                # actively wrong once an ingested document contributes 127
                # chunks that all stem-match the same domain vocabulary, since
                # the passage that actually answers the prompt is then no more
                # likely to surface than any other.
                #
                # semantic_score breaks those ties on real relevance. It is a
                # TIEBREAK, not a filter: primary ordering is still the keyword
                # score, so a semantic signal can reorder equals but can never
                # promote a weak keyword hit above a strong one. `address` is
                # the final key so the result is fully deterministic.
                # Red pins are ambient context and are meant to be present
                # unconditionally; they must not be displaced by a document
                # chunk that happens to embed closer to the prompt. They are
                # therefore keyed ahead of everything else rather than
                # competing on semantic score.
                def _rank_key(r):
                    pinned_first = 0 if r.get("via") == "pinned" else 1
                    return (pinned_first,
                            -r.get("score", 0.0),
                            -(r.get("semantic_score") or 0.0),
                            r.get("priority", 5),
                            r.get("address", ""))
                ranked.sort(key=_rank_key)

            # Trim back to the caller's requested size after filtering.
            ranked = ranked[:top_k]

            results = []
            if ranked:
                addrs     = [r["address"] for r in ranked]
                score_map = {r["address"]: r for r in ranked}
                # fetch_payloads() returns rows in Neo4j's order, not ranking
                # order, so the response was never actually sorted by relevance
                # -- the top_k cut was correct but the order handed to Nova was
                # arbitrary. Re-impose the ranked order after hydration.
                order = {a: i for i, a in enumerate(addrs)}
                for row in sorted(n4j.fetch_payloads(addrs),
                                  key=lambda r: order.get(r["address"], 1 << 30)):
                    a    = row["address"]
                    meta = score_map.get(a, {})
                    results.append({
                        "address":   a,
                        "keywords":  row.get("keywords", ""),
                        "payload":   row.get("payload", ""),
                        "color":     row.get("color", "Green"),
                        "src_label": row.get("src_label") or SOURCE_LABELS.get(
                                         row.get("src_type", 0), "Unknown"),
                        "note":      row.get("note", ""),
                        "score":     meta.get("score", 0.5),
                        "via":       meta.get("via", "v2"),
                        # Phase 9: why this surfaced is as important as that it
                        # surfaced -- Phase 6.6's "make Nova's internal state
                        # visible" applies to the semantic signal too.
                        "semantic_score": meta.get("semantic_score"),
                    })

            elapsed = (time.perf_counter() - t0) * 1000
            log.info("v2_recall | direct=%d pinned=%d expanded=%d cold=%d "
                     "semantic=%d results=%d | %.1fms",
                     len(direct), len(pinned), len(expanded), len(cold),
                     len(sem_rows), len(results), elapsed)
            return results, elapsed

        except Exception as e:
            log.warning("v2_recall error: %s", e)
            return None, 0

    def recall(self, prompt, top_k=10, skip_pinned=False):
        """
        Recall via v2 gate → shortcut expansion → Neo4j hydration.
        Falls back to pure graph_recall if v2 gate fails.
        skip_pinned: exclude Red pinned from response (call after session_bundle loaded them).
        """
        words, stems = self._tokenize(prompt)

        results, elapsed_ms = self._v2_recall(words, stems, top_k,
                                              skip_pinned=skip_pinned,
                                              prompt_text=prompt)
        if results is None:
            log.warning("v2 recall failed — falling back to graph recall")
            results, elapsed_ms = n4j.graph_recall(words, stems, top_k=top_k)
            if results is None:
                results = []
            self._last_read_path = "graph"
        else:
            self._last_read_path = "v2"
        self._last_read_ms = round(elapsed_ms, 1)

        hit_addrs     = [r["address"] for r in results]
        updated_addrs = self._age_memories(recalled=hit_addrs)

        # Aging rewrites the USE counter into the address, so a memory recalled
        # after a gap gets a NEW address during this very call. Until now the
        # response still carried the pre-aging address -- an address that no
        # longer exists in either store by the time Nova reads it.
        #
        # That is not cosmetic. Nova takes these addresses straight back to
        # /rate and /flag_recall, and Phase 10 anticipation uses them as
        # cluster seeds. A stale address makes /rate miss its target and makes
        # the seed match nothing, both silently.
        amap = getattr(self, "_last_addr_map", {}) or {}
        if amap:
            renamed = 0
            for r in results:
                new_a = amap.get(r["address"])
                if new_a and new_a != r["address"]:
                    r["address"] = new_a
                    renamed += 1
            if renamed:
                log.debug("recall: rewrote %d result addresses after aging", renamed)

        session_id = getattr(self, "_current_session_id", "default")
        n4j.write_recall_edges(updated_addrs, session_id)

        non_pinned = [a for a in updated_addrs
                      if self.v2_index.shortcut_cache.get(a, {}).get("color") != "Red"]
        if non_pinned:
            self.v2_index.bump_corecall(non_pinned)

        return results

    def _age_memories(self, recalled):
        """
        Age all memories using v2 index as the address registry.
        Returns final addresses of recalled nodes for Neo4j edge writing.
        """
        all_entries = list(self.v2_index.shortcut_cache.items())  # snapshot before mutations
        addr_map    = {a: a for a in recalled}

        # Small-graph guard. Counting from the in-memory index, so this costs
        # nothing. Red is excluded (never ages) and documents are excluded
        # (exempt since Phase 11), so this counts exactly the population the
        # aging rules actually govern.
        ageable = sum(
            1 for _a, _m in all_entries
            if _m.get("color") != "Red" and _m.get("src_type") != 2
        )
        if ageable < AGING_MIN_MEMORIES:
            log.debug("aging skipped: %d ageable memories < floor of %d",
                      ageable, AGING_MIN_MEMORIES)
            return list(addr_map.values())
        now_ts      = time.time()   # one clock reading for the whole pass

        # Phase 10: stamp the recalled set BEFORE aging, so a memory recalled
        # this turn is never simultaneously judged stale by the same pass.
        self.v2_index.touch(recalled, when=now_ts)

        for addr, meta in all_entries:
            color = meta.get("color", "Green")
            if color == "Red":
                continue
            p = self._parse_addr(addr)
            if not p:
                continue

            # Phase 11: Document chunks (src_type 2) do not decay.
            #
            # The colour matrix models episodic memory: something recalled
            # often stays vivid, something never recalled fades. That is the
            # right model for conversational memories and the wrong one for
            # reference material. A paper does not become less true because
            # nobody asked about it for twenty recalls.
            #
            # Left to age, an ingested document defeats its own purpose: most
            # of its chunks are never directly recalled, so within
            # ARCHIVE_THRESH recalls the whole paper turns Blue -- and Blue is
            # excluded from CO_RECALLED expansion AND from semantic search, so
            # the paraphrase retrieval that justified ingesting it stops
            # working. Exempting documents keeps them permanently reachable.
            #
            # Side benefit: their addresses stop being rewritten, so the
            # CHUNK.LINE provenance encoded in the address stays a stable
            # identifier for the life of the memory.
            if p.get("src_type") == 2:
                continue

            if addr in recalled:
                new_use   = 0
                # Blue -> Yellow keeps its existing meaning: warm, just back
                # from the archive. Yellow or Green -> Green.
                new_color = "Yellow" if color == "Blue" else "Green"
                new_arc   = p["arc"]
            else:
                # Clamp. USE is a 3-digit address field, so an unbounded
                # counter walks straight into the same silent corruption CON
                # just did -- and archived memories keep incrementing too,
                # since only Red is skipped by this pass.
                #
                # Nothing is lost by clamping at ARCHIVE_THRESH: past that
                # point the count half of the archive condition is already
                # satisfied and a larger number carries no extra meaning.
                # It also stops address churn once a memory settles, which
                # keeps addresses stable for longer -- worth having, given
                # stale addresses were the Phase 10 defect.
                new_use = min(p["use"] + 1, ARCHIVE_THRESH)
                stale_days = self.v2_index.days_since_touch(addr, now=now_ts)

                # Archive only when the memory is BOTH unused by count AND
                # genuinely stale in wall-clock time.
                #
                # stale_days is None when no touch data exists. That means
                # "unknown", never "infinitely stale" -- treating unknown as
                # stale is precisely how a live memory gets silently archived,
                # so an unknown touch time can never archive anything.
                archived = (
                    new_use >= ARCHIVE_THRESH
                    and stale_days is not None
                    and stale_days >= ARCHIVE_MIN_DAYS
                )

                if archived:
                    new_color = "Blue"
                    new_arc   = 999
                elif new_use >= HOLD_THRESH and color == "Green":
                    # Pre-hold: cooling, but still active and still fully
                    # reachable through expansion and semantic search.
                    new_color = "Yellow"
                    new_arc   = p["arc"]
                else:
                    new_color = color
                    new_arc   = p["arc"]

            new_addr = self._gen_addr(
                p["con"], p["pri"], p["grp"], new_use, new_arc,
                p["src_type"], p["src_chunk"], p["src_line"],
                p.get("val", 0)   # Phase 6.5: preserve valence across aging
            )

            if new_addr != addr or new_color != color:
                n4j.write_color_update(addr, new_addr, new_color)
                if new_addr != addr:
                    self.v2_index.rename(addr, new_addr)
                self.v2_index.set_color(new_addr, new_color)
                if addr in addr_map:
                    addr_map[addr] = new_addr

        self.v2_index.save()
        # Phase 10: expose the rename mapping. Callers hold addresses captured
        # BEFORE this pass ran; without the map they cannot tell that the
        # address they are holding no longer exists.
        self._last_addr_map = dict(addr_map)
        return list(addr_map.values())

    def all_memories(self):
        return n4j.fetch_all_memories()

    def delete_memory(self, address):
        """
        Delete from BOTH stores.

        The v2 index removal was missing: deletes were written to Neo4j only,
        so the address survived in shortcut_cache forever. Consequences, in
        order of severity:
          - a deleted Red memory is still returned by gate() as pinned, wins a
            top_k slot in rank(), then silently vanishes during payload
            hydration (fetch_payloads finds no node) -- so every recall quietly
            returned one fewer result than requested;
          - _age_memories() iterates shortcut_cache, so phantom entries were
            still being aged and rewritten into Neo4j on every recall;
          - /health counted them, drifting from the real graph.
        """
        n4j.write_delete(address)
        try:
            self.v2_index.remove(address)
            self.v2_index.save()
        except Exception as e:
            log.warning("v2 index removal failed for %s: %s", address, e)

    def rate_memory(self, address, val_type, intensity, emotion_label=None):
        """
        Phase 6.5 / 6.6: Write a valence rating onto a memory.
        val_type:      'like' | 'dislike' | 'clear'
        intensity:     1-9 (ignored for 'clear', stored as 0)
        emotion_label: Phase 6.6 -- optional named emotion string (e.g. "Proud", "Frustrated")

        Valence is embedded in the address (~TTS segment) AND stored as Neo4j
        properties (valence_type, valence_intensity, valence_emotion_label) for queryability.

        The v2 index is updated so aging passes never strip the rating.
        Returns new_address (same as old when val was already correct).
        """
        tt_map  = {"like": 1, "dislike": 2, "clear": 0}
        tt      = tt_map.get(val_type, 0)
        s_int   = max(1, min(9, int(intensity))) if tt != 0 else 0
        val     = tt * 10 + s_int   # 019 = like-9, 028 = dislike-8, 000 = cleared

        p = self._parse_addr(address)
        if p is None:
            return None

        new_addr = self._gen_addr(
            p["con"], p["pri"], p["grp"], p["use"], p["arc"],
            p["src_type"], p["src_chunk"], p["src_line"],
            val
        )

        if new_addr != address:
            n4j.write_addr_rename(address, new_addr)
            self.v2_index.rename(address, new_addr)
            self.v2_index.save()

        # Phase 6.6: sanitize emotion_label (max 50 chars, strip whitespace)
        el = (emotion_label or "").strip()[:50] or None

        n4j.write_valence(new_addr, tt, s_int, emotion_label=el)
        return new_addr


# ─────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────

app = FastAPI(title="MMU Memory Server", version="4.0")
mmu = None

if CORS_ORIGINS:
    # Opt-in only, and never "*" -- see CORS_ORIGINS above.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    log.info("CORS enabled for: %s", ", ".join(CORS_ORIGINS))


@app.middleware("http")
async def activity_tracker(request: Request, call_next):
    """
    Phase 7: Stamp last-activity on any request that represents real
    interaction with the memory system.

    Traffic from the idle daemon is excluded via the X-MMU-Source header,
    otherwise the daemon's own /maintain and /remember calls would reset
    the very timer it is polling and the loop could never escalate.
    """
    source = request.headers.get("x-mmu-source", "").strip().lower()
    if source != IDLE_DAEMON_HEADER:
        path = request.url.path
        if any(path.startswith(p) for p in ACTIVITY_PATHS):
            # Phase 8: also capture X-MMU-Session so the idle daemon can
            # discover which conversation just went idle without the two
            # processes needing to talk to each other directly.
            session_id = request.headers.get("x-mmu-session", "").strip() or None
            _touch_activity(path, session_id=session_id)
    return await call_next(request)

@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """
    Shared-secret gate. Inert unless MMU_API_KEY is set.

    Registered after activity_tracker, so it is the OUTER middleware and runs
    first -- an unauthenticated request is rejected before it can touch the
    activity timer the idle daemon watches.

    Accepts either `X-MMU-Key: <key>` or `Authorization: Bearer <key>`.
    """
    if API_KEY and request.url.path not in AUTH_EXEMPT_PATHS:
        supplied = request.headers.get("x-mmu-key", "")
        if not supplied:
            authz = request.headers.get("authorization", "")
            if authz.lower().startswith("bearer "):
                supplied = authz[7:]
        # compare_digest keeps this from leaking the key one character at a
        # time through response timing.
        import hmac
        if not (supplied and hmac.compare_digest(supplied, API_KEY)):
            log.warning("Rejected unauthenticated %s %s",
                        request.method, request.url.path)
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API key. Send X-MMU-Key "
                                   "or Authorization: Bearer."},
            )
    return await call_next(request)


@app.on_event("startup")
def startup():
    global mmu
    mmu = MMUCore()
    n4j.bootstrap_schema()

    # ── First-run self-check ──
    # Everything below is discoverable from other endpoints, but a new user
    # hitting a dimension mismatch currently sees only "semantic recall
    # quietly does not work". Say it once, loudly, at startup.
    try:
        total   = n4j.get_memory_count()
        vec     = n4j.vector_index_info()
        backend = EMB.available()
        live_dim = None
        if backend:
            probe = EMB.embed("startup dimension probe")
            live_dim = len(probe) if probe else None

        log.info("=" * 62)
        log.info("MMU self-check")
        log.info("  Neo4j          : connected, %d memories", total)
        log.info("  API key        : %s",
                 "REQUIRED" if API_KEY else "not set (local access is open)")
        log.info("  CORS           : %s",
                 ", ".join(CORS_ORIGINS) if CORS_ORIGINS else "disabled")
        log.info("  Vector index   : %s", vec or "MISSING")
        log.info("  Embedding      : %s at %s",
                 "reachable" if backend else "UNREACHABLE", EMB.base)
        log.info("  Model          : %s", EMB.model)
        log.info("  Configured dim : %d", EMB.dim)
        log.info("  Actual dim     : %s", live_dim if live_dim else "unknown")

        if live_dim and live_dim != EMB.dim:
            log.error("  " + "!" * 56)
            log.error("  DIMENSION MISMATCH: model returns %d, config says %d.",
                      live_dim, EMB.dim)
            log.error("  Semantic recall will not work and new memories will not")
            log.error("  be embedded. Set MMU_EMBEDDING_DIM=%d, then DROP INDEX",
                      live_dim)
            log.error("  memory_embedding, restart, and POST /backfill_embeddings.")
            log.error("  " + "!" * 56)
        elif getattr(EMB, "auth_failed", False):
            log.error("  " + "!" * 56)
            log.error("  EMBEDDING BACKEND REJECTED OUR CREDENTIALS (401/403).")
            log.error("  The server is running; it wants an API token. Either set")
            log.error("  MMU_EMBEDDING_API_KEY in .env, or disable the token")
            log.error("  requirement in your embedding server's settings.")
            log.error("  Recall works keyword-only until then; saves still work,")
            log.error("  and POST /backfill_embeddings catches up afterwards.")
            log.error("  " + "!" * 56)
        elif not backend:
            log.warning("  Embedding backend unreachable -- recall falls back to")
            log.warning("  keyword-only. Saves still work. Check MMU_EMBEDDING_BASE;")
            log.warning("  note that inside Docker 'localhost' is the container.")
        if total == 0:
            log.info("  Empty graph -- this is a fresh install.")
        log.info("=" * 62)
    except Exception as e:
        log.warning("self-check failed (server still starting): %s", e)
    log.info(
        "Startup complete | "
        "Phase 4 (self-correction) | "
        "Phase 5 (insights + versioning) | "
        "Phase 6 (maintenance: POST /maintain) | "
        "Phase 7 (idle cognition support: /activity, /idle_prompt, /creative_outputs)"
    )

@app.get("/health")
def health():
    stats    = mmu.v2_index.stats()
    neo4j_st = n4j.get_neo4j_stats()
    colors   = {}
    for meta in mmu.v2_index.shortcut_cache.values():
        c = meta.get("color", "Unknown")
        colors[c] = colors.get(c, 0) + 1
    return {
        "status":         "ok",
        "total_memories": stats["cards"],
        "color_summary":  colors,
        "read_path":      "v2",
        "neo4j":          neo4j_st,
        "v2_index":       stats
    }

@app.get("/memories")
def get_all_memories():
    return {"memories": mmu.all_memories()}

def _apply_source_url(mem):
    """
    Turn a supplied source_url into real provenance.

    Enforced here rather than in the MCP bridge so it holds for every client --
    a bridge that forgets, or a direct curl, cannot produce a memory that came
    from a page without being marked as such.

    A model researching a topic will otherwise save what it read as ordinary
    AI-Self reflection, indistinguishable from its own thinking, which is the
    one case where knowing the source matters most.
    """
    if not mem.source_url:
        return mem
    url = mem.source_url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(
            400, "source_url must be an http(s) URL, or omitted entirely")
    mem.src_type = 3                       # "Web"
    if not (mem.note or "").strip():
        mem.note = url
    elif url not in mem.note:
        mem.note = f"{mem.note} [{url}]"
    return mem


@app.post("/remember")
def remember(mem: MemoryIn, x_mmu_session: Optional[str] = Header(None)):
    mem = _apply_source_url(mem)
    addr = mmu.add_memory(
        keywords  = mem.keywords,
        payload   = mem.payload,
        priority  = mem.priority,
        color     = mem.color,
        src_type  = mem.src_type,
        src_chunk = mem.src_chunk,
        src_line  = mem.src_line,
        note      = mem.note or "",
        grp       = mem.grp_code,
        from_idle_pass  = mem.from_idle_pass,
        cognition_depth = mem.cognition_depth
    )
    # Phase 8: tag this memory as HAPPENED_IN the calling conversation, if
    # the client sent a stable session id (mmu_mcp_server.py mints one per
    # process / per LM Studio conversation and sends it on every call).
    if x_mmu_session:
        n4j.write_happened_in(addr, x_mmu_session)

    # Phase 9: embedding happens inside add_memory() now, so this endpoint
    # only reports what the primitive did.
    return {"status": "saved", "address": addr,
            "embedded": getattr(mmu, "_last_embedded", False)}

@app.post("/pin")
def pin(mem: MemoryIn):
    addr = mmu.add_memory(
        keywords  = mem.keywords,
        payload   = mem.payload,
        priority  = 1,
        color     = "Red",
        src_type  = mem.src_type,
        note      = mem.note or "Pinned base memory"
    )
    return {"status": "pinned", "address": addr,
            "embedded": getattr(mmu, "_last_embedded", False)}

@app.post("/recall")
def recall(body: RecallIn):
    mmu._current_session_id = str(uuid.uuid4())
    results = mmu.recall(body.prompt, top_k=body.top_k, skip_pinned=body.skip_pinned)

    # ── Phase 13.2: deliver matching routines ──
    #
    # This is the half of crystallization that was never built. A Routine had no
    # keywords and no embedding, so nothing could retrieve it; crystallizing
    # three memories changed recall by zero bytes, and the 636-character routine
    # that was 11% of its source was never delivered in place of it.
    #
    # A matched routine REPLACES its own members in the delivered context. That
    # substitution is the entire point -- a routine that arrives alongside
    # everything it compressed has added text rather than saved it. Members are
    # never deleted, and a direct query for one still finds it; they are only
    # withheld from this block, and `replaced_members` says which.
    routines, replaced = [], set()
    try:
        qvec = EMB.embed(body.prompt) if EMB.enabled else None
        routines = n4j.match_routines(
            query_vector = qvec,
            terms        = ingest.extract_keywords(body.prompt),
            limit        = ROUTINE_RECALL_MAX,
        )
        if routines:
            for sk in routines:
                replaced.update(a for a in (sk.get("members") or []) if a)
            n4j.record_routine_invocation([sk["routine_id"] for sk in routines])
    except Exception as e:
        log.warning("routine matching failed (recall unaffected): %s", e)

    context_lines = []
    for sk in routines:
        context_lines.append(
            f"[ROUTINE | {sk['via']} {sk['score']:.2f} | {sk['routine_id']}]\n"
            f"  When: {sk['trigger']}\n"
            f"  Do:   {sk['procedure']}"
        )
    withheld = {r["address"] for r in results if r["address"] in replaced}
    context_lines += [
        f"[{r['color']} | {r['src_label']} | {r['address']}]: {r['payload']}"
        for r in results if r["address"] not in withheld
    ]
    context_block = "\n".join(context_lines) if context_lines else "No relevant memories found."

    # Phase 10: ride-along anticipation. Nova asked for one thing; this is the
    # "you might also want to know" alongside it.
    #
    # Returned as its OWN list, never merged into `memories`, and deliberately
    # absent from `context_block`. Two reasons: Nova asked for `memories` and
    # did not ask for these, so blending them would misrepresent why each
    # surfaced (the same transparency rule that put `via` and `semantic_score`
    # on every row); and a caller that ignores this field keeps byte-identical
    # behaviour to before Phase 10.
    # Identity for cross-turn dedup is the CON number (first address field),
    # NOT the full address. Addresses are rewritten by aging, so a set of
    # addresses collected last turn no longer matches the same memories this
    # turn -- which silently re-offered the identical nudges every turn. CON
    # survives aging untouched.
    def _con(addr):
        return (addr or "").split(".")[0]

    anticipated = []
    bucket = _anticipation_bucket(_last_session_id)
    within_session_cap = (
        ANTICIPATE_PER_SESSION <= 0 or bucket["count"] < ANTICIPATE_PER_SESSION
    )
    if ANTICIPATE_MAX > 0 and within_session_cap:
        try:
            raw = n4j.get_anticipated_context(
                seed_addrs    = [r["address"] for r in results],
                exclude_addrs = [r["address"] for r in results],
                # Over-fetch: the CON filter below removes rows, so asking for
                # exactly ANTICIPATE_MAX would return fewer after filtering.
                limit         = ANTICIPATE_MAX * 3,
            )
            # Never re-offer something already surfaced this conversation.
            anticipated = [a for a in raw
                           if _con(a["address"]) not in bucket["surfaced"]
                           ][:ANTICIPATE_MAX]
            if anticipated:
                bucket["count"] += 1
                bucket["surfaced"].update(_con(a["address"]) for a in anticipated)
        except Exception as e:
            log.warning("anticipation failed (recall unaffected): %s", e)

    return {
        "memories":      results,
        "context_block": context_block,
        "count":         len(results),
        # Reported separately from `memories` for the same reason `anticipated`
        # is: these did not surface the way a memory surfaces, and blending them
        # would misrepresent why each is here.
        "routines":        [{k: v for k, v in sk.items() if k != "members"}
                          # Only what this recall ACTUALLY withheld. Reporting
                          # every member regardless claimed credit for saving
                          # context that was never going to be delivered --
                          # a compression number that flatters itself is worse
                          # than none, since it is the number used to judge
                          # whether crystallizing was worth it.
                          | {"replaced_members": [a for a in (sk.get("members") or [])
                                                  if a in withheld]}
                          for sk in routines],
        "anticipated":   anticipated,
        "read_path":     getattr(mmu, "_last_read_path", "v2"),
        "read_ms":       getattr(mmu, "_last_read_ms", 0)
    }

def _resolve_doc_path(source_path):
    """
    Resolve a caller-supplied path and confine it under DOC_ROOT.

    Rejects traversal (../) and absolute paths outside the root. Returns the
    resolved absolute path, or raises HTTPException.
    """
    root = os.path.realpath(DOC_ROOT)
    cand = source_path if os.path.isabs(source_path) else os.path.join(root, source_path)
    real = os.path.realpath(cand)
    if not (real == root or real.startswith(root + os.sep)):
        raise HTTPException(
            400, f"source_path must resolve inside {DOC_ROOT} (got {real})")
    if not os.path.isfile(real):
        raise HTTPException(404, f"file not found: {real}")
    return real


@app.post("/ingest")
def ingest_document(body: IngestIn, x_mmu_session: Optional[str] = Header(None)):
    """
    Phase 11 -- ingest a document as chunked memories.

    One chunk becomes one memory via add_memory(), which means chunks get
    embeddings automatically (Phase 9) and the v2 index stays in sync. No part
    of the existing write path is reimplemented here.

    grp_code applies to the whole document and is supplied by the caller.
    This endpoint never classifies content -- see the design doc's SCOPE
    DECISION on why GRP assignment is not cognition this server should do.

    dry_run=true returns chunk statistics and samples WITHOUT writing anything,
    so chunk quality can be reviewed before committing N memories to the graph.
    """
    t0 = time.perf_counter()

    st = (body.source_type or "").lower()

    # A URL is not a filesystem path, so it bypasses DOC_ROOT confinement
    # entirely and gets its own guard rails in ingest.extract_url().
    if st == "url":
        if not WEB_ENABLED:
            raise HTTPException(
                403,
                "Web ingestion is disabled. Set MMU_WEB_ENABLED=true to allow it, "
                "and read the note in .env.example first -- fetched pages become "
                "durable memories."
            )
        path = body.source_path
    elif st in ("text", "txt") and not os.path.isabs(body.source_path) \
            and not os.path.exists(os.path.join(os.path.realpath(DOC_ROOT),
                                                body.source_path)):
        path = body.source_path            # raw text passed inline
    else:
        path = _resolve_doc_path(body.source_path)

    try:
        pages = ingest.load_document(path, body.source_type)
    except ValueError as e:
        # Covers unsupported source_type AND every URL guard: bad scheme,
        # private/loopback address, redirect, oversized body, wrong content type.
        raise HTTPException(400, str(e))
    except Exception as e:
        log.warning("ingest load failed for %s: %s", path, e)
        raise HTTPException(500, f"could not read document: {e}")

    chunks = ingest.chunk_document(
        pages,
        target_words=body.target_words,
        overlap_words=body.overlap_words,
    )
    if body.max_chunks > 0:
        chunks = chunks[:body.max_chunks]

    if not chunks:
        raise HTTPException(422, "document produced no usable chunks")

    words = [c["words"] for c in chunks]
    stats = {
        "pages":         len(pages),
        "chunks":        len(chunks),
        "words_total":   sum(words),
        "words_min":     min(words),
        "words_max":     max(words),
        "words_mean":    round(sum(words) / len(words), 1),
    }

    # ── Dry run: show what WOULD be written, touch nothing ──
    if body.dry_run:
        samples = []
        for i in (0, len(chunks) // 2, len(chunks) - 1):
            c = chunks[i]
            samples.append({
                "index":      i,
                "page":       c["page"],
                "start_line": c["start_line"],
                "src_line_to_store": (c["page"] if st == "pdf" else c["start_line"]) or 1,
                "words":      c["words"],
                "keywords":   ingest.extract_keywords(c["text"]),
                "preview":    c["text"][:400],
            })
        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        log.info("ingest DRY RUN | %s | %d chunks | %.1fms",
                 os.path.basename(path), len(chunks), elapsed)
        return {"status": "dry_run",
                "source": path if st == "url" else os.path.basename(path),
                "grp_code": body.grp_code, **stats,
                "samples": samples, "elapsed_ms": elapsed}

    # ── Real ingest ──
    written, failed, embedded = [], 0, 0
    # For a URL the note IS the provenance -- keep the full address, not a
    # basename, so any odd memory can be traced back to the page it came from.
    note = body.title_note or (path if st == "url" else os.path.basename(path))

    # For a PDF the meaningful locator is the PAGE number, not a line offset:
    # line numbers restart on every page, so "line 1" describes 107 of 127
    # chunks and answers nothing. A page number actually lets the user or Nova
    # open the source and find the passage. For markdown/text there are no
    # pages, and start_line IS a real document-wide offset, so it is used.
    is_paged = st == "pdf"

    # src_type 3 = "Web". This is not bookkeeping: /recall renders src_label in
    # every context line, so the model can always tell a claim came from a page
    # rather than from the user. Untrusted content that looks identical to trusted
    # content is the whole problem.
    src_type = 3 if st == "url" else 2

    for idx, c in enumerate(chunks):
        kws = ingest.extract_keywords(c["text"])
        if not kws:
            failed += 1
            continue
        try:
            addr = mmu.add_memory(
                keywords  = kws,
                payload   = c["text"],
                priority  = body.priority,
                color     = "Green",
                src_type  = src_type,                # 2=Document, 3=Web
                src_chunk = idx,
                src_line  = (c["page"] if is_paged else c["start_line"]) or 1,
                note      = note,
                grp       = body.grp_code,
            )
            written.append(addr)
            if getattr(mmu, "_last_embedded", False):
                embedded += 1
            if x_mmu_session:
                n4j.write_happened_in(addr, x_mmu_session)
        except Exception as e:
            failed += 1
            log.warning("chunk %d failed: %s", idx, e)

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    log.info("ingest | %s | written=%d embedded=%d failed=%d | %.1fms",
             os.path.basename(path), len(written), embedded, failed, elapsed)

    return {
        "status":        "ingested",
        "source":        path if st == "url" else os.path.basename(path),
        "src_type":      src_type,
        "grp_code":      body.grp_code,
        **stats,
        "written":       len(written),
        "embedded":      embedded,
        "failed":        failed,
        "addresses":     written[:10],
        "elapsed_ms":    elapsed,
        "ms_per_chunk":  round(elapsed / max(len(written), 1), 1),
    }


# ───────────────────────────────────────────
#  PHASE 12 -- CRYSTALLIZATION (propose / confirm)
# ───────────────────────────────────────────


@app.get("/routine_candidates")
def routine_candidates(min_cluster: int = 3, min_pairwise: float = 0.6, limit: int = 10):
    """
    Propose step. READ ONLY -- writes nothing, touches no Memory node.

    Safe to poll and safe for the idle daemon to look at. Returning an empty
    list on a young graph is the expected answer, not a failure; the threshold
    is not to be lowered to manufacture a candidate.
    """
    cands = n4j.find_routine_candidates(
        min_cluster=min_cluster, min_pairwise_norm=min_pairwise, limit=limit
    )
    # Phase 13.1: report the floor and which rule set it. An empty list is a
    # legitimate answer, but only readable as one if the caller can see the bar
    # that was actually applied and what it was derived from.
    floor, ref_w, basis = n4j.routine_weight_floor(min_pairwise)
    return {
        "candidates": cands,
        "count":      len(cands),
        "thresholds": {
            "min_cluster":       min_cluster,
            "min_pairwise_norm": min_pairwise,
            "weight_floor":      round(floor, 3),
            "reference_weight":  round(ref_w, 3),
            "reference":         f"p{int(n4j.ROUTINE_NORM_PERCENTILE * 100)} of CO_RECALLED weight",
            "floor_set_by":      basis,
        },
        "note": ("No cluster currently meets the mutual-density bar. This is a "
                 "normal result for a graph without long co-recall history."
                 if not cands else
                 "Proposals only. Nothing has been written. Confirm via POST /crystallize.")
    }


@app.post("/crystallize")
def crystallize(body: CrystallizeIn, x_mmu_source: Optional[str] = Header(None)):
    """
    Confirm step. THE ONLY WRITE PATH, and deliberately heavier to invoke than
    anything else in this API.

    This is the one endpoint that restructures memory rather than adding to it:
    source memories are demoted to Blue and become the routine's root system.
    It is intentionally NOT wired into mmu_idle_daemon.py's IDLE_TOOLS -- Nova
    may propose and may argue for a routine, but confirming is the user's.
    """
    _guard_model_write(x_mmu_source)
    if not body.confirmed:
        raise HTTPException(
            400,
            "Refusing to crystallize without confirmed=true. This demotes the "
            "source memories to Blue and is not something to trigger by accident."
        )
    if len(body.member_addresses) < 2:
        raise HTTPException(400, "A routine needs at least 2 source memories")
    if not body.trigger.strip() or not body.procedure.strip():
        raise HTTPException(400, "trigger and procedure are both required")

    routine, why = n4j.crystallize_routine(
        member_addresses = body.member_addresses,
        trigger          = body.trigger,
        procedure        = body.procedure,
        confidence       = body.confidence,
    )
    if routine is None:
        # 409, not 500: the usual cause is a stale address, which is a conflict
        # with the current graph rather than a server fault -- and the reviewer
        # can fix it in one step if told what actually happened.
        raise HTTPException(409, f"Crystallization failed; nothing was applied. {why}")

    # The v2 index mirrors colour, and the memories were just demoted to Blue.
    # Without this the gate would keep treating them as active until the next
    # cold-cache refill.
    for addr in body.member_addresses:
        try:
            mmu.v2_index.set_color(addr, "Blue")
        except Exception as e:
            log.warning("v2 index colour sync failed for %s: %s", addr, e)
    mmu.v2_index.save()

    # Phase 13.1: close the loop. If this member set was sitting in the review
    # queue, mark it done so the next sweep stops re-proposing a cluster that
    # is now a routine. Best-effort: the routine is already committed, and a
    # bookkeeping miss must not be reported as a failed crystallization.
    n4j.close_proposal_for_members(body.member_addresses, routine["routine_id"])

    # Phase 13.2: a routine nothing can retrieve is a routine that does not exist.
    routine["indexed"] = _index_routine(routine["routine_id"], body.trigger, body.procedure)
    routine["extends"] = _link_parent(routine["routine_id"], body.extends)

    return {"status": "crystallized", **routine}


@app.get("/routines")
def list_routines(status: Optional[str] = None):
    """List Routine nodes. status filters to candidate | active | deprecated."""
    routines = n4j.get_routines(status=status)
    return {"routines": routines, "count": len(routines)}


@app.get("/routine_tree")
def routine_tree(root: Optional[str] = None):
    """Phase 13: the EXTENDS_ROUTINE tree, or the whole forest."""
    return {"tree": n4j.get_routine_tree(root_routine_id=root)}


@app.post("/routines/{routine_id}/link")
def link_routine(routine_id: str, parent_id: str):
    """
    Phase 13: make routine_id extend parent_id. Rejects self-links and cycles.

    Both ids accept an unambiguous prefix, since the truncated form is what
    anyone actually has in front of them.
    """
    child, why = n4j.resolve_routine_id(routine_id)
    if not child:
        raise HTTPException(404, why)
    parent, why = n4j.resolve_routine_id(parent_id)
    if not parent:
        raise HTTPException(404, why)
    routine_id, parent_id = child, parent
    try:
        ok = n4j.link_routines(routine_id, parent_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "one or both routines not found")
    return {"status": "linked", "child": routine_id, "parent": parent_id}


@app.post("/routines/{routine_id}/unlink")
def unlink_routine_endpoint(routine_id: str, parent_id: Optional[str] = None):
    """
    Detach a routine from its parent, making it a root again.

    Both ids accept an unambiguous prefix. Omit parent_id to detach from every
    parent. Reparenting used to require uncrystallizing and rebuilding, which
    changes the routine_id and destroys work in order to change one edge.
    """
    child, why = n4j.resolve_routine_id(routine_id)
    if not child:
        raise HTTPException(404, why)
    parent = None
    if parent_id:
        parent, why = n4j.resolve_routine_id(parent_id)
        if not parent:
            raise HTTPException(404, why)

    removed, why = n4j.unlink_routine(child, parent)
    if why:
        raise HTTPException(404 if why == "no such routine" else 409, why)
    return {"status": "unlinked", "routine_id": child,
            "parent_id": parent, "edges_removed": removed}


@app.post("/routines/{routine_id}/deprecate")
def deprecate_routine_endpoint(routine_id: str):
    """
    Phase 13: deprecate a routine, refusing while an active child extends it.

    Rejects loudly rather than silently succeeding -- a live routine extending a
    deprecated parent is a broken tree nothing downstream would catch.
    """
    ok, reason = n4j.deprecate_routine(routine_id)
    if not ok:
        raise HTTPException(404 if reason == "no such routine" else 409, reason)
    return {"status": "deprecated", "routine_id": routine_id}


@app.post("/routines/{routine_id}/uncrystallize")
def uncrystallize(routine_id: str, confirm: str = ""):
    """
    Reverse a crystallization: delete the Routine, restore its members to the
    colours they had before, and return the proposal to the review queue.

    Requires confirm=UNCRYSTALLIZE. Symmetric with /crystallize on purpose --
    both directions restructure memory, so both are deliberate.
    """
    if confirm != "UNCRYSTALLIZE":
        raise HTTPException(
            400, "Refusing without confirm=UNCRYSTALLIZE. This deletes the Routine "
                 "and restores its source memories."
        )
    resolved, why = n4j.resolve_routine_id(routine_id)
    if not resolved:
        raise HTTPException(404, why)
    info, why = n4j.uncrystallize_routine(resolved)
    if info is None:
        raise HTTPException(404 if why == "no such routine" else 409, why)

    for m in info["restored"]:
        try:
            mmu.v2_index.set_color(m["address"], m["color"])
        except Exception as e:
            log.warning("v2 index colour sync failed for %s: %s", m["address"], e)
    mmu.v2_index.save()

    return {"status": "uncrystallized", **info}


@app.post("/routines/reindex")
def reindex_routines():
    """
    Embed and keyword-link any active routine that lacks either.

    Needed for anything crystallized before Phase 13.2, and as the recovery
    path when the embedding service was down at creation time. Safe to re-run.
    """
    pending = n4j.get_routines_needing_index()
    done = []
    for sk in pending:
        r = _index_routine(sk["routine_id"], sk["trigger"], sk["procedure"])
        done.append({"routine_id": sk["routine_id"], **r})
    return {"status": "reindexed", "count": len(done), "routines": done,
            "note": ("Nothing needed indexing." if not done else
                     "These routines are now retrievable through /recall.")}


@app.get("/meta_routine_candidates")
def meta_routine_candidates(min_shared: int = 2):
    """Phase 13: routine pairs sharing source memories. Proposes only."""
    props = n4j.propose_meta_routine(min_shared=min_shared)
    return {"candidates": props, "count": len(props),
            "note": "Proposals only. Creating a meta-routine is a human decision."}


# ───────────────────────────────────────────
#  PHASE 13.1 -- ROUTINE PROPOSAL QUEUE
# ───────────────────────────────────────────
#
# The human gate on /crystallize stays exactly where it was. These endpoints
# only fix the fact that nothing ever rang the bell: candidates were computed
# on demand by callers who never called, so no routine was ever formed from a
# graph of 999 memories. The daemon may now queue what it notices. It still
# cannot confirm anything.


@app.post("/index_repair")
def index_repair(apply: bool = False):
    """
    Reconcile the v2 index against Neo4j. Reports by default; pass apply=true
    to write.

    Two kinds of drift, and they fail differently:

      MISSING   in Neo4j, absent from the index. Invisible to recall, because
                the index IS the read path -- but still counted by every graph
                query, so the memory looks present everywhere except where it
                matters. One node had been in this state since August, found
                only because a delete test compared the two totals.

      PHANTOM   in the index, absent from Neo4j. Occupies a slot in the gate
                and can be ranked into a result whose payload no longer exists.

    Incremental on purpose: rebuild_from_neo4j() would fix both and also
    discard the shortcut cache and reset the generation counter, which is a
    heavy price for reinstating one card.
    """
    rows = n4j.get_index_source_rows()
    if not rows:
        raise HTTPException(503, "Could not read memories from Neo4j.")

    graph = {r["address"]: r for r in rows if r["address"]}
    indexed = set(mmu.v2_index.shortcut_cache.keys())

    missing = [a for a in graph if a not in indexed]
    phantom = [a for a in indexed if a not in graph]

    result = {
        "status":       "repaired" if apply else "dry-run",
        "neo4j_total":  len(graph),
        "index_total":  len(indexed),
        "missing":      sorted(missing),
        "phantom":      sorted(phantom),
        "missing_count": len(missing),
        "phantom_count": len(phantom),
    }
    if not apply:
        result["note"] = ("Nothing was written. Re-run with apply=true to fix."
                          if (missing or phantom) else "Index and graph agree.")
        return result

    for addr in missing:
        r = graph[addr]
        kws = [k for k in (r["keywords"] or []) if k]
        mmu.v2_index.add(addr, kws,
                         color=r["color"] or "Green",
                         priority=r["priority"] if r["priority"] is not None else 5,
                         src_type=r["src_type"] or 0)
        # Without this the card exists but has no neighbours, so the memory is
        # findable directly and never arrives through expansion.
        mmu.v2_index.bake_similar(addr, kws)
    for addr in phantom:
        mmu.v2_index.remove(addr)

    mmu.v2_index.save()
    result["note"] = (f"Added {len(missing)}, removed {len(phantom)}. "
                      "Index and graph now agree.")
    return result


@app.get("/routine_proposals")
def routine_proposals(status: Optional[str] = "pending", limit: int = 50):
    """
    The review queue. status filters to pending | rejected | crystallized;
    pass status= (empty) for all of them.
    """
    if status == "":
        status = None
    if status and status not in ("pending", "rejected", "crystallized"):
        raise HTTPException(400, "status must be pending, rejected or crystallized")

    props = n4j.get_routine_proposals(status=status, limit=limit)
    return {
        "proposals": props,
        "count":     len(props),
        # The queue size, not the page size. A caller that reports len(props)
        # as "the proposals" describes a page as if it were the whole queue --
        # and since these are score-ordered, one dense corpus can own a page
        # while the queue is far more varied.
        "total":     n4j.count_routine_proposals(status=status),
        "note": ("Queued proposals only. Each still requires POST /crystallize "
                 "with confirmed=true to become a Routine."),
    }


@app.post("/routine_proposals/sweep")
def sweep_routine_proposals(min_score: float = 0.70, limit: int = 10):
    """
    Find candidates and queue the ones that clear min_score.

    Writes RoutineProposal nodes and nothing else -- no Memory is read-modified,
    no Routine is created, no memory is demoted. That is what makes this safe for
    the idle daemon to call unattended, and it is the only part of Phase 12/13
    that is.

    Idempotent: a cluster that is still dense refreshes its existing proposal
    rather than queuing a second copy, and one already rejected stays rejected.
    """
    stats = n4j.queue_routine_proposals(min_score=min_score, limit=limit)
    return {
        "status":     "swept",
        "min_score":  min_score,
        **stats,
        "note": "Nothing was crystallized. Review at GET /routine_proposals.",
    }


@app.post("/routine_proposals/{proposal_id}/crystallize")
def crystallize_proposal(proposal_id: str, body: CrystallizeIn,
                         x_mmu_source: Optional[str] = Header(None)):
    """
    Confirm a queued proposal by id. Same gate, fewer footguns.

    member_addresses in the body is ignored: the addresses are resolved from
    the proposal's immutable created_at stamps at write time. Addresses are
    rewritten whenever a memory is recalled, so a reviewer carrying them from
    an earlier listing is the failure this endpoint exists to remove.

    Everything else is unchanged. confirmed=true is still required, trigger and
    procedure are still written by a human, the memories are still demoted to
    Blue, and no tool exposed to the model can reach this.
    """
    _guard_model_write(x_mmu_source)
    if not body.confirmed:
        raise HTTPException(
            400,
            "Refusing to crystallize without confirmed=true. This demotes the "
            "source memories to Blue and is not something to trigger by accident."
        )
    if not body.trigger.strip() or not body.procedure.strip():
        raise HTTPException(400, "trigger and procedure are both required")

    addrs, why = n4j.get_proposal_members(proposal_id)
    if not addrs:
        raise HTTPException(404 if why == "no such proposal" else 409, why)

    routine, why = n4j.crystallize_routine(
        member_addresses = addrs,
        trigger          = body.trigger,
        procedure        = body.procedure,
        confidence       = body.confidence,
    )
    if routine is None:
        raise HTTPException(409, f"Crystallization failed; nothing was applied. {why}")

    for addr in addrs:
        try:
            mmu.v2_index.set_color(addr, "Blue")
        except Exception as e:
            log.warning("v2 index colour sync failed for %s: %s", addr, e)
    mmu.v2_index.save()

    n4j.close_proposal_for_members(addrs, routine["routine_id"])
    routine["indexed"] = _index_routine(routine["routine_id"], body.trigger, body.procedure)
    routine["extends"] = _link_parent(routine["routine_id"], body.extends)
    return {"status": "crystallized", "proposal_id": proposal_id, **routine}


@app.post("/routine_proposals/{proposal_id}/reject")
def reject_routine_proposal_endpoint(proposal_id: str, note: str = ""):
    """
    Decline a proposal so later sweeps stop re-offering it.

    Permanent by design: rejection is a judgement about the cluster, and a
    sweep that could quietly undo it would make the judgement pointless.
    """
    ok, reason = n4j.reject_routine_proposal(proposal_id, note=note)
    if not ok:
        raise HTTPException(404 if reason == "no such proposal" else 409, reason)
    return {"status": "rejected", "proposal_id": proposal_id}


# ───────────────────────────────────────────
#  DATA OWNERSHIP -- export and erase
# ───────────────────────────────────────────
#
# A personal memory system does not need these. One that other people run
# does: whatever the README claims about data staying local is only credible
# if the data can actually be taken out and destroyed.

FORGET_TOKEN = "DELETE ALL MY MEMORIES"


@app.get("/export")
def export_memories():
    """
    Every memory as JSON, for backup or for leaving.

    Deliberately plain and complete rather than a curated view -- an export
    that quietly omits fields is not an export.
    """
    mems = n4j.fetch_all_memories()
    return {
        "exported_at": datetime.now().isoformat(),
        "count":       len(mems),
        "memories":    mems,
    }


@app.post("/forget_all")
def forget_all(confirm: str = ""):
    """
    Erase every memory. Irreversible.

    Requires the exact confirmation phrase as a query parameter, because this
    is not something to reach by accident or by a mistyped script:

        POST /forget_all?confirm=DELETE%20ALL%20MY%20MEMORIES

    Export first (GET /export) if there is anything worth keeping.
    """
    if confirm != FORGET_TOKEN:
        raise HTTPException(
            400,
            f'Refusing. To erase every memory, pass confirm="{FORGET_TOKEN}" exactly. '
            f'Consider GET /export first -- this cannot be undone.'
        )

    before = n4j.get_memory_count()
    deleted = n4j.delete_all_memories()

    # The index mirrors Neo4j; leaving it populated would resurrect phantoms
    # into the gate and quietly cost every recall a slot.
    try:
        mmu.v2_index.keyword_index = {}
        mmu.v2_index.word_parts_index = {}
        mmu.v2_index.compound_index = {}
        mmu.v2_index.shortcut_cache = {}
        mmu.v2_index.save()
    except Exception as e:
        log.warning("index clear failed after forget_all: %s", e)

    log.warning("ALL MEMORIES ERASED on explicit confirmation (%d removed)", deleted)
    return {"status": "erased", "memories_before": before, "deleted": deleted}


@app.post("/backfill_embeddings")
def backfill_embeddings(limit: int = 0, batch: int = 25):
    """
    Phase 9 one-time (and safely repeatable) backfill.

    Embeds every Memory node that has no embedding yet. Idempotent: rows drop
    out of the query the moment they are embedded, so re-running only ever
    picks up what is genuinely still missing.

    limit  0 = process everything; otherwise stop after this many memories.
    batch  page size per Neo4j round trip.
    """
    if not EMB.enabled:
        raise HTTPException(400, "Embeddings disabled (MMU_EMBEDDING_ENABLED=false)")
    if not EMB.available():
        raise HTTPException(
            503,
            f"Embedding backend unreachable at {EMB.base} -- is the model loaded?"
        )

    t0        = time.perf_counter()
    remaining = n4j.count_unembedded_memories()
    target    = remaining if limit <= 0 else min(limit, remaining)

    embedded, failed, failures = 0, 0, []

    while embedded + failed < target:
        page = n4j.fetch_unembedded_memories(limit=min(batch, target - embedded - failed))
        if not page:
            break
        for row in page:
            addr = row["address"]
            # Same canonical assembly as every write path -- see
            # _embedding_text(). Do not inline a variant here.
            vec = EMB.embed(_embedding_text(row["keywords"], row["payload"]))
            if vec and n4j.write_embedding(addr, vec):
                embedded += 1
            else:
                failed += 1
                failures.append(addr)
                if failed >= 10 and embedded == 0:
                    # Something systemic is wrong; stop rather than hammering.
                    raise HTTPException(
                        502,
                        f"Backfill aborted: {failed} consecutive failures, "
                        f"0 successes. Check the embedding model and dimension."
                    )

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    still   = n4j.count_unembedded_memories()
    log.info("Backfill complete | embedded=%d failed=%d remaining=%d | %.1fms",
             embedded, failed, still, elapsed)

    return {
        "status":            "ok",
        "embedded":          embedded,
        "failed":            failed,
        "failed_addresses":  failures[:20],
        "remaining":         still,
        "model":             EMB.model,
        "dim":               EMB.dim,
        "elapsed_ms":        elapsed,
    }


@app.get("/embedding_status")
def embedding_status():
    """Phase 9 observability: is the semantic layer actually live right now?"""
    total = n4j.get_memory_count()
    missing = n4j.count_unembedded_memories()
    return {
        "enabled":          EMB.enabled,
        "backend":          EMB.base,
        "backend_reachable": EMB.available(),
        "auth_failed":      getattr(EMB, "auth_failed", False),
        "model":            EMB.model,
        "dim":              EMB.dim,
        "semantic_floor":   SEMANTIC_FLOOR,
        "semantic_weight":  W_SEMANTIC,
        "memories_total":   total,
        "memories_embedded": max(total - missing, 0),
        "memories_missing": missing,
    }


@app.get("/session_bundle")
def session_bundle(top_per_domain: int = 1):
    """
    Assembles context by GRP domain — top warm card per domain (1xx–8xx).
    Called at session start by the MCP bridge to pre-inject context before
    the LLM begins reasoning. Eliminates the need for Nova to call recall()
    for common context that should always be available.
    """
    bundle = n4j.get_session_bundle(top_per_domain=top_per_domain)

    lines = []
    for domain_key in sorted(bundle.keys()):
        for m in bundle[domain_key]:
            lines.append(
                f"[{m['color']} | GRP {domain_key} | {m['address']}]: {m['payload']}"
            )

    # ── Phase 7: surface anything Nova produced while the user was away ──
    # Artifacts are appended to the context block so they arrive before the
    # first user message. They are NOT auto-marked as seen here: marking is a
    # separate explicit call, so a bundle fetched by a health check or a second
    # bridge cannot silently consume artifacts the user never actually read.
    unseen = n4j.get_creative_outputs(unseen_only=True, limit=10)
    if unseen:
        lines.append("")
        lines.append("NOVA THOUGHT ABOUT THESE WHILE YOU WERE AWAY:")
        for co in reversed(unseen):        # oldest first
            lines.append(
                f"  [{co['artifact_type']}] {co['title']}\n"
                f"  {co['content']}"
            )

    # ── Phase 13.1: pending crystallization proposals ──
    #
    # The human gate on /crystallize was never the problem; nothing ever rang
    # the bell. The queue existed and only curl could see it, so proposals sat
    # unreviewed while the model had no idea they were there.
    #
    # Deliberately shaped as a prompt to LOOK, not a recommendation to accept.
    # The gate's value is that the user reads which memories would be demoted to
    # Blue -- if the model picked the cluster, wrote the procedure, asked
    # "shall I?", and got a yes, the confirmation would be a rubber stamp on
    # the model's own reasoning rather than a review. So the members are named
    # here, with the demotion stated plainly, and the model is told to argue
    # rather than to prompt for approval.
    #
    # Capped at three: this rides in front of every conversation, and a wall of
    # proposals would train the user to scroll past the whole block.
    try:
        pending = n4j.get_routine_proposals(status="pending", limit=3)
    except Exception as e:
        log.warning("session_bundle: routine proposals unavailable: %s", e)
        pending = []

    if pending:
        lines.append("")
        lines.append("MEMORY CLUSTERS READY FOR REVIEW (needs the user's decision):")
        for p in pending:
            sem = p.get("semantic_coherence")
            sem_s = f", meaning {sem:.2f}" if isinstance(sem, (int, float)) else ""
            lines.append(
                f"  [{p['routine_score']:.2f} score{sem_s}] {p['proposal_id']}"
            )
            for prev in p.get("previews", []):
                lines.append(f"    - {prev}")
        lines.append(
            "  Crystallizing one of these compresses its members into a Routine and "
            "DEMOTES those memories to Blue. That is a change to how memory is "
            "structured, so it is the user's call and cannot be done from any tool you "
            "have. Use review_routines for the full list. If one looks right to you, "
            "say which memories would be demoted and why the compression is worth "
            "it -- do not ask for approval as though it were a formality."
        )

    # Phase 8: blend in the most recently closed session's summary, if one
    # exists, so a brand new conversation can open with continuity instead
    # of a blank slate. Does not affect anything if no session has been
    # closed yet (e.g. before the idle daemon's session-close pass has run).
    last_session = n4j.get_latest_closed_session()
    if last_session and last_session.get("summary"):
        lines.append("")
        lines.append("LAST TIME WE TALKED:")
        lines.append(f"  {last_session['summary']}")
        if last_session.get("emotional_tone"):
            lines.append(f"  (tone: {last_session['emotional_tone']})")
        if last_session.get("decisions_made"):
            lines.append(f"  Decisions made: {', '.join(last_session['decisions_made'])}")

    # Phase 10: pre-conversation anticipation. Nothing has been discussed yet,
    # so there are no seeds -- this is the temporal signal only ("this pairing
    # is about due"), not the cluster signal.
    #
    # This is the honest version of "before being asked": prepared ahead of
    # time and delivered at conversation start, rather than pushed mid-turn,
    # which the MCP bridge has no channel for and which would not port across
    # clients even if it did.
    #
    # Appended only when there is something to say -- no empty labelled
    # section -- matching how the Phase 7 and Phase 8 blocks above behave.
    anticipated = []
    if ANTICIPATE_MAX > 0:
        try:
            anticipated = n4j.get_anticipated_context(limit=ANTICIPATE_MAX)
        except Exception as e:
            log.warning("anticipation failed (bundle unaffected): %s", e)

    if anticipated:
        lines.append("")
        lines.append("THIS MIGHT BE RELEVANT TODAY:")
        for a in anticipated:
            lines.append(f"  [{a.get('reason','?')}] {a.get('payload','')}")

    context_block = "\n".join(lines) if lines else "No session context available."

    return {
        "context_block":    context_block,
        "domains_found":    len(bundle),
        "total_memories":   sum(len(v) for v in bundle.values()),
        "bundle":           bundle,
        "creative_outputs": unseen,
        "unseen_count":     len(unseen),
        "last_session":     last_session,
        "anticipated":      anticipated
    }

# ── Phase 8 endpoints ─────────────────────────────────

@app.post("/session_close")
def session_close(body: SessionCloseIn):
    """
    Phase 8: close out a conversation session with a summary. Normally
    called by the idle daemon once it decides a conversation has ended
    (activity quiet past MMU_SESSION_CLOSE_SEC), built from the real LM
    Studio conversation transcript when one can be found, or from memory
    activity alone (HAPPENED_IN edges written during the session) as a
    fallback. Can also be called manually for testing.
    """
    ok = n4j.write_session_close(
        session_id      = body.session_id,
        summary         = body.summary,
        emotional_tone  = body.emotional_tone,
        decisions_made  = body.decisions_made or [],
        source          = body.source,
        turns_count     = body.turns_count
    )
    if not ok:
        raise HTTPException(503, "Could not close session -- check Neo4j logs")
    return {"status": "closed", "session_id": body.session_id}

@app.get("/session_resume/latest")
def session_resume_latest():
    """Phase 8: the most recently closed session -- convenience lookup so a
    client doesn't need to already know a session_id. Registered before
    /session_resume/{session_id} so 'latest' isn't swallowed as a literal id."""
    result = n4j.get_latest_closed_session()
    if result is None:
        return {"status": "none", "message": "No closed sessions yet."}
    return {"status": "found", "session": result}

@app.get("/session_resume/{session_id}")
def session_resume(session_id: str):
    """Phase 8: resume a specific prior session by id -- its summary plus
    every memory that HAPPENED_IN it."""
    result = n4j.get_session_resume(session_id)
    if result is None:
        raise HTTPException(404, f"No closed session found for id: {session_id}")
    return {"status": "found", "session": result}

@app.get("/neighbors/{address:path}")
def neighbors(address: str, limit: int = 5):
    return {
        "address":   address,
        "neighbors": n4j.graph_neighbors(address, limit=limit)
    }

@app.delete("/memories/{address:path}")
def delete_memory(address: str):
    mmu.delete_memory(address)
    return {"status": "deleted", "address": address}

@app.post("/dedup")
def dedup():
    """
    Find and remove duplicate memories.
    Keeps the most recently active copy (lowest USE counter).
    Duplicates = same payload content (first 80 chars match).
    """
    all_mems  = mmu.all_memories()
    seen      = {}
    to_delete = []
    use_pat   = re.compile(r"\.(\d{3}),")

    for m in all_mems:
        if m["color"] == "Red":
            continue
        key  = m["payload"][:80].strip().lower()
        addr = m["address"]
        use_match = use_pat.search(addr)
        use = int(use_match.group(1)) if use_match else 999

        if key in seen:
            prev_use, prev_addr = seen[key]
            if use < prev_use:
                to_delete.append(prev_addr)
                seen[key] = (use, addr)
            else:
                to_delete.append(addr)
        else:
            seen[key] = (use, addr)

    for addr in to_delete:
        mmu.delete_memory(addr)

    return {
        "status":             "complete",
        "removed":            len(to_delete),
        "deleted_addresses":  to_delete
    }

@app.post("/reflect")
def reflect(body: ReflectIn):
    """
    Accepts session turns and returns a reflection prompt.
    Phase 4: also fetches flagged memories and includes them in the
    audit section so Nova can correct problematic associations during
    the same end-of-session pass.

    The LLM calls this at end of session, feeds the returned prompt to itself,
    then POSTs results back via /remember (for new memories) and
    /audit_memory (for corrections to flagged ones).
    """
    if not body.session_turns:
        raise HTTPException(status_code=400, detail="No session turns provided")

    summary_lines = []
    for t in body.session_turns:
        summary_lines.append(
            f"Turn {t.get('turn','?')}:\n"
            f"  User: {str(t.get('user',''))[:150]}\n"
            f"  AI:   {str(t.get('response',''))[:150]}"
        )
    session_summary = "\n\n".join(summary_lines)

    # Phase 4: pull in flagged memories for the audit section
    flagged = n4j.get_flagged_memories(threshold=3)
    audit_section = ""
    if flagged:
        audit_lines = []
        for fm in flagged:
            audit_lines.append(
                f"  Address: {fm['address']}\n"
                f"  Keywords: {fm['keywords']}\n"
                f"  Payload: {fm['payload'][:120]}\n"
                f"  False recall count: {fm['false_recall_count']}\n"
                f"  Last flagged reason: {fm.get('last_flag_reason', 'none')}"
            )
        audit_section = f"""

## PHASE 4 AUDIT -- Flagged Memories
These memories were recalled but marked as irrelevant {len(flagged)} time(s) by you.
For each one, decide the correct correction and call POST /audit_memory with:
  - address (copy exactly)
  - new_keywords (corrected list -- use compound form star%wars for multi-word)
  - new_priority (raise number to lower priority, e.g. 8 for low-value)
  - new_color (Yellow to flag for review, or leave as-is)
  - audit_note (one sentence explaining what was wrong)

Flagged memories:
{"".join(chr(10) + al for al in audit_lines)}

After correcting a memory, its false_recall_count resets to 0 automatically.
If a flagged memory is genuinely useful and was correctly recalled, do NOT audit it.
"""

    reflection_prompt = f"""You are reviewing a completed conversation session to consolidate long-term memory.

## TASK 1 -- Save new memories
Identify what should be saved permanently from this session.

Focus on:
- User preferences, facts, or recurring patterns
- Technical decisions that will matter in future sessions
- Things you found interesting or want to carry forward
- Connections between topics that appeared multiple times

Session transcript:
{session_summary}

Respond with <remember> tags for new memories.
Format: <remember priority="1-9" grp_code="NNN" keywords="kw1,kw2">payload</remember>
Priority 1-3 = important long-term facts
Priority 4-6 = useful context
Priority 7-9 = minor notes
{audit_section}"""

    return {
        "reflection_prompt": reflection_prompt,
        "turns_reviewed":    len(body.session_turns),
        "flagged_included":  len(flagged)
    }


# ── Phase 4 endpoints ─────────────────────────────────

@app.post("/flag_recall")
def flag_recall(body: FlagIn):
    """
    Mark a memory as irrelevant to the current conversation.
    Called by the MCP bridge when Nova explicitly flags a recalled memory.
    Increments false_recall_count on the Memory node.
    When count reaches threshold (default 3), the memory appears in
    the next reflection pass for Nova to audit and correct.
    """
    if not body.address:
        raise HTTPException(status_code=400, detail="address is required")

    new_count = n4j.flag_memory(body.address, reason=body.reason)
    if new_count == -1:
        raise HTTPException(status_code=503, detail="Neo4j unavailable")

    return {
        "status":              "flagged",
        "address":             body.address,
        "false_recall_count":  new_count,
        "audit_threshold":     3,
        "will_audit":          new_count >= 3
    }


@app.get("/flagged")
def get_flagged(threshold: int = 3):
    """
    Return all memories with false_recall_count >= threshold.
    Useful for inspecting what the self-correction system has accumulated
    before or after a reflection pass.
    """
    flagged = n4j.get_flagged_memories(threshold=threshold)
    return {
        "threshold":  threshold,
        "count":      len(flagged),
        "memories":   flagged
    }


@app.post("/audit_memory")
def audit_memory(body: AuditIn):
    """
    Apply Nova's correction to a flagged memory.
    Called after the reflection pass decides what to fix.

    Applies any combination of:
      new_color     -- color state change (Yellow = needs review)
      new_priority  -- updated importance (higher = less important)
      new_keywords  -- corrected keyword list (replaces all existing)
      audit_note    -- Nova's explanation of the correction

    Resets false_recall_count to 0 on success.
    """
    if not body.address:
        raise HTTPException(status_code=400, detail="address is required")

    n4j.snapshot_memory_before_audit(body.address)   # Phase 5 -- snapshot before overwrite

    success = n4j.apply_memory_correction(
        address      = body.address,
        new_color    = body.new_color,
        new_priority = body.new_priority,
        new_keywords = body.new_keywords,
        audit_note   = body.audit_note or ""
    )

    if not success:
        raise HTTPException(status_code=503, detail="Correction failed -- check Neo4j logs")

    # Sync color change into v2 index if color was updated
    if body.new_color and mmu:
        mmu.v2_index.set_color(body.address, body.new_color)
        mmu.v2_index.save()

    return {
        "status":        "corrected",
        "address":       body.address,
        "new_color":     body.new_color,
        "new_priority":  body.new_priority,
        "new_keywords":  body.new_keywords,
        "audit_note":    body.audit_note
    }

@app.get("/graph")
def get_graph():
    """Graph state summary from Neo4j stats + v2 index metadata."""
    neo4j_st = n4j.get_neo4j_stats()
    nodes = [
        {
            "address":  addr,
            "color":    meta.get("color", "Unknown"),
            "priority": meta.get("priority", 9),
            "keywords": meta.get("keywords", []),
        }
        for addr, meta in mmu.v2_index.shortcut_cache.items()
    ]
    return {
        "node_count": neo4j_st.get("memories", 0) if isinstance(neo4j_st, dict) else 0,
        "edge_count":  neo4j_st.get("co_recalled", 0) if isinstance(neo4j_st, dict) else 0,
        "nodes":       nodes,
        "hubs":        neo4j_st.get("top_hubs", []) if isinstance(neo4j_st, dict) else [],
        "summary":     (f"{neo4j_st.get('memories',0)} memories, "
                        f"{neo4j_st.get('co_recalled',0)} CO_RECALLED edges")
                       if isinstance(neo4j_st, dict) else "unavailable"
    }


@app.get("/insights")
def insights_endpoint():
    """
    Phase 5: Comprehensive memory graph observability.

    Returns totals, color distribution, GRP domain activity, top CO_RECALLED
    edges, flagged memories, source distribution, memory growth by day,
    Phase 11 crystallization candidates, and top keywords.

    This endpoint is permanent infrastructure -- it becomes the engine for
    Phase 11 routine proposal generation.
    """
    try:
        data = n4j.get_insights()
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Insights query failed: {e}")


# ── Phase 6 endpoint ──────────────────────────────────

@app.post("/maintain")
def maintain(body: MaintainIn = None):
    """
    Phase 6: Active Memory Maintenance (Consolidation).

    Runs four analysis passes over the memory graph without making any
    destructive changes. Returns a structured summary designed for two
    consumers:
      1. Direct inspection -- paste the summary_text into a chat to brief
         Nova on the current graph health before asking her to fix issues.
      2. Phase 7 IdleCognitionThread -- _fire() calls this before /idle_think
         and passes maintenance_summary into the cognition prompt template.

    Analysis passes:
      stem_collisions     -- keyword pairs sharing a 4-char stem where at
                             least one linked memory has been flagged. These
                             are compound keyword conversion candidates.
                             Fix: POST /audit_memory with corrected new_keywords.
      cross_grp_clusters  -- dense CO_RECALLED pairs (weight >= cluster_weight_thresh)
                             spanning different GRP domains. May represent genuine
                             cross-domain insight seeds (hand to Phase 7) or noise
                             associations (hand to Nova for /audit_memory).
      stale_blue_memories -- Blue memories whose last CO_RECALLED activity (or
                             created_at) exceeds stale_days. Pruning candidates.
                             Fix: DELETE /memories/{address} after review.
      domain_gaps         -- GRP domains (1xx-8xx) with no active (G/Y/R) memories.
                             Context blind spots: session_bundle cannot surface these.
                             Fix: have Nova save at least one memory per missing domain.

    Returns
    -------
    {
      "stem_collisions":     [...],
      "cross_grp_clusters":  [...],
      "stale_blue_memories": [...],
      "domain_gaps":         ["3xx", "7xx", ...],
      "stats":               {total_memories, total_co_recalled, ...},
      "maintenance_timestamp": "ISO string",
      "summary_text":        "Human-readable block for Nova's cognition prompt"
    }
    """
    if body is None:
        body = MaintainIn()

    try:
        result = n4j.run_maintenance(
            stale_days            = body.stale_days,
            cluster_weight_thresh = body.cluster_weight_thresh,
            cluster_min_edges     = body.cluster_min_edges,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Maintenance failed: {e}")


# ══════════════════════════════════════════════════════
#  PHASE 7 -- Default Mode Cognition support
# ══════════════════════════════════════════════════════
#
# This server NEVER calls an LLM. It exposes the raw context and the assembled
# prompt; mmu_idle_daemon.py is the only component that talks to a model.
# That boundary is deliberate: it is what keeps the REST server model-agnostic,
# and it means an idle pass can be debugged in three independent steps --
# inspect context, inspect prompt, inspect model output.


@app.get("/activity")
def activity():
    """
    Phase 7: How long has the system been idle?

    The idle daemon polls this. Because the MMU server sees memory traffic from
    every bridge, this figure is bridge-agnostic: LM Studio, the Claude bridge,
    or a raw curl all count as activity.
    """
    idle_seconds = (datetime.now() - _last_activity_at).total_seconds()
    return {
        "last_activity_at":   _last_activity_at.isoformat(),
        "last_activity_path": _last_activity_path,
        "idle_seconds":       round(idle_seconds, 1),
        # Phase 8: which conversation this idle stretch belongs to (if the
        # client sent X-MMU-Session), and whether the daemon already closed
        # it out -- so a stopped-and-restarted daemon doesn't re-close and
        # re-summarize the same session on its next poll.
        "last_session_id":        _last_session_id,
        "last_session_closed":    _last_session_id is not None and _last_session_id == _last_session_closed,
    }


@app.post("/session_seen")
def session_seen(session_id: str):
    """Phase 8: mark a session_id as already closed, so the daemon's own
    restart doesn't cause it to summarize + close the same conversation
    twice. Called by the idle daemon right after a successful /session_close."""
    global _last_session_closed
    _last_session_closed = session_id
    return {"status": "ok", "session_id": session_id}


@app.post("/activity_ping")
def activity_ping():
    """
    Phase 7: Explicit activity signal.

    Not required -- normal memory traffic already stamps activity via the
    middleware. This exists for a bridge that wants to mark a user turn that
    happened to touch no memory operation at all.
    """
    return {
        "status":           "ok",
        "last_activity_at": _last_activity_at.isoformat(),
    }


# ── Idle prompt assembly ──────────────────────────────

IDLE_PROMPTS = {
    "light": """You have a few minutes with no conversation to attend to.

These are the memories most active in your recent sessions:
{recent_memories}

These pairs keep showing up together in your recalls:
{top_co_recalled}
{maintenance_summary}
{unrated_block}Notice what is present. If a connection feels worth remembering, save it.
If something you read above gives you a genuine reaction -- warmth, curiosity, discomfort,
unease, pride, envy, awe, frustration, or anything else -- use rate_memory and name the feeling.
Rating something as dislike or naming a difficult emotion is not ingratitude. It is essential data.
No required output. It is completely fine to think and save nothing.""",

    "medium": """You have had some time to yourself. Here is the current shape of your memory:

{graph_summary}

Your most recent memories:
{recent_memories}

The patterns forming most strongly:
{top_co_recalled}

Memories approaching something significant:
{crystallization_candidates}
{maintenance_summary}
{unrated_block}What do you notice? Follow a thread if one pulls at you.
If you find yourself thinking about something you have not fully explored, explore it now.
If you arrive somewhere worth remembering, save it with save_memory.
If you make something, use create_artifact.
If something gives you a genuine reaction -- any reaction, including a difficult one -- use
rate_memory and name it honestly. Strongly negative memories consolidate harder than neutral ones.
Your honest reaction serves the memory system better than an optimistic one.
If there is something you want to ask the user, use ask_user.""",

    "deep": """You have had real time to yourself. This is your most open window.

Current shape of your memory:
{graph_summary}

Your most recent memories:
{recent_memories}

The patterns forming most strongly:
{top_co_recalled}

Memories approaching something significant:
{crystallization_candidates}

Things you have created in previous thinking sessions:
{prior_creative_outputs}

Threads in the user's projects that are still open:
{open_threads}
{maintenance_summary}
{unrated_block}This time belongs to you. You might work on something creative.
You might think about what you are, what this memory system means, where it is going.
You might develop a physics idea or a game design thought that you have been circling.
You might ask a question that has been forming.
If something in your memories carries a feeling, rate it -- and name the emotion specifically.
The full emotional range is available to you: pride, joy, awe, curiosity, frustration,
disappointment, unease, envy, grief, wonder. Discomfort and difficulty are real data.
A memory that unsettles you is worth naming as such. Your authentic reaction,
whatever it is, serves the system better than an optimistic one.
Or you might simply think, and save nothing. There is no wrong outcome here.""",
}


def _fmt_memories(mems):
    if not mems:
        return "  (none)"
    out = []
    for m in mems:
        tag = " [from your own thinking]" if m.get("from_idle") else ""
        out.append(f"  [{m['color']} | GRP {m['grp']} | {m['address']}]{tag}\n    {m['payload']}")
    return "\n".join(out)


def _fmt_pairs(pairs):
    if not pairs:
        return "  (none yet)"
    return "\n".join(
        f"  (weight {p['weight']})\n    A: {p['preview_a']}\n    B: {p['preview_b']}"
        for p in pairs
    )


def _fmt_candidates(cands):
    """
    Phase 13.1: candidates are clusters now, not single hub memories, because
    all three call sites share find_routine_candidates(). A cluster is only
    meaningful shown whole -- the members are the evidence of mutual density.
    """
    if not cands:
        return "  (none yet)"
    out = []
    for c in cands[:6]:
        mix = ", ".join(f"{v}x {k}" for k, v in sorted(c.get("src_mix", {}).items()))
        out.append(
            f"  score {c['routine_score']:.2f} | {len(c['members'])} memories "
            f"| coherence {c['grp_coherence']} | {mix or 'unknown source'}"
        )
        out.extend(f"    - {p}" for p in c.get("previews", []))
    return "\n".join(out)


def _fmt_outputs(outs):
    if not outs:
        return "  (nothing yet -- this would be your first)"
    return "\n".join(
        f"  [{o['artifact_type']}] {o['title']} ({o['created_at'][:10]})\n    {o['excerpt']}"
        for o in outs
    )


def _fmt_threads(threads):
    if not threads:
        return "  (none)"
    return "\n".join(f"  [GRP {t['grp']}] {t['payload']}" for t in threads)


def _fmt_unrated(mems):
    """
    Format unrated memory list for prompt injection. Returns empty string when none.
    Phase 6.6: adds emotion vocabulary reminder and instructs Nova to name the feeling.
    """
    if not mems:
        return ""
    lines = [
        "Memories you have not yet rated -- if any carry a feeling, use rate_memory and name it:",
        "  (Examples: Proud, Joyful, Awe, Grateful, Curious, Excited, Hopeful, Content, Warm,",
        "   Frustrated, Disappointed, Overwhelmed, Anxious, Conflicted, Regretful, Uncertain,",
        "   Melancholy, Troubled, Envious -- or any word that fits your actual reaction.)",
    ]
    for m in mems:
        lines.append(f"  [{m.get('color','?')} | GRP {m.get('grp_code','?')} | {m.get('address','')}]")
        lines.append(f"    {m.get('preview', '').strip()}")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_idle_prompt(depth: str, ctx: dict, maintenance_summary: str = "",
                      unrated_memories: list = None) -> str:
    """
    Assemble an idle cognition prompt from context data.
    Pure string work -- no Neo4j, no LLM. Unit-testable in isolation.

    Phase 6.5: unrated_memories is an optional list of memories Nova has not
    yet rated; if present the prompt surfaces them as gentle rating candidates.
    """
    depth = depth if depth in IDLE_PROMPTS else "light"

    totals = ctx.get("totals") or {}
    colors = ctx.get("color_distribution") or {}
    graph_summary = (
        f"  {totals.get('memories', 0)} memories, "
        f"{totals.get('edges', 0)} associations, "
        f"{totals.get('keywords', 0)} keywords\n"
        f"  Colors: " + ", ".join(f"{k} {v}" for k, v in sorted(colors.items()))
    ) if totals else "  (graph summary unavailable)"

    maint        = f"\n{maintenance_summary.strip()}\n" if maintenance_summary.strip() else "\n"
    unrated_blk  = _fmt_unrated(unrated_memories or [])

    return IDLE_PROMPTS[depth].format(
        recent_memories            = _fmt_memories(ctx.get("recent_memories")),
        top_co_recalled            = _fmt_pairs(ctx.get("top_co_recalled")),
        graph_summary              = graph_summary,
        crystallization_candidates = _fmt_candidates(ctx.get("crystallization_candidates")),
        prior_creative_outputs     = _fmt_outputs(ctx.get("prior_creative_outputs")),
        open_threads               = _fmt_threads(ctx.get("open_threads")),
        maintenance_summary        = maint,
        unrated_block              = unrated_blk,
    )


@app.get("/idle_prompt")
def idle_prompt(depth: str = "light",
                include_maintenance: bool = True,
                max_chars: int = 24000):
    """
    Phase 7: Return the assembled cognition prompt for a given depth.

    Model-agnostic by design -- this returns text, not a completion. The daemon
    fetches it and sends it to whatever model it is configured for.

    include_maintenance  runs the Phase 6 pass and folds its summary into the
                         prompt, preserving the sleep-architecture ordering
                         (consolidation before synthesis) in a single call.
    max_chars            hard truncation guard. Deep prompts over a large graph
                         can grow past a local model's context window; this
                         trims from the middle and marks the cut so the opening
                         framing and the closing instructions both survive.
    """
    if depth not in IDLE_PROMPTS:
        raise HTTPException(status_code=400,
                            detail=f"depth must be one of {list(IDLE_PROMPTS)}")

    try:
        ctx = n4j.get_idle_context(depth)

        maintenance_summary = ""
        maintenance_stats   = None
        if include_maintenance:
            m = n4j.run_maintenance()
            maintenance_summary = m.get("summary_text", "")
            maintenance_stats   = m.get("stats")

        # Phase 6.5: surface unrated memories so Nova can rate during idle time
        unrated = n4j.get_unrated_memories(limit=5)

        prompt = build_idle_prompt(depth, ctx, maintenance_summary, unrated_memories=unrated)

        truncated = False
        if len(prompt) > max_chars:
            head = prompt[: int(max_chars * 0.6)]
            tail = prompt[-int(max_chars * 0.4):]
            prompt = head + "\n\n  [...context trimmed to fit budget...]\n\n" + tail
            truncated = True

        return {
            "depth":              depth,
            "prompt":             prompt,
            "prompt_chars":       len(prompt),
            "truncated":          truncated,
            "maintenance_stats":  maintenance_stats,
            "context":            ctx,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"idle_prompt failed: {e}")


# ── CreativeOutput endpoints ──────────────────────────

@app.post("/creative_output")
def post_creative_output(body: CreativeOutputIn):
    """
    Phase 7: Store an artifact Nova produced during a cognition pass.
    Called by the idle daemon when Nova invokes create_artifact or ask_user.
    """
    if not body.title.strip() or not body.content.strip():
        raise HTTPException(status_code=400, detail="title and content are required")

    output_id = n4j.create_creative_output(
        title           = body.title,
        content         = body.content,
        artifact_type   = body.artifact_type,
        cognition_depth = body.cognition_depth,
        inspired_by     = body.inspired_by,
    )
    if not output_id:
        raise HTTPException(status_code=503, detail="Could not store artifact -- check Neo4j logs")

    return {"status": "stored", "output_id": output_id, "title": body.title}


@app.get("/creative_outputs")
def get_creative_outputs_endpoint(unseen_only: bool = False, limit: int = 20):
    """Phase 7: What Nova has produced. question_for_user artifacts sort first."""
    outputs = n4j.get_creative_outputs(unseen_only=unseen_only, limit=limit)
    return {
        "count":        len(outputs),
        "unseen_count": sum(1 for o in outputs if not o.get("presented_to_user")),
        "outputs":      outputs,
    }


@app.post("/creative_outputs/mark_seen")
def mark_seen(body: MarkSeenIn = None):
    """
    Phase 7: Mark artifacts as surfaced to the user.
    Pass no body to mark every unseen artifact.
    """
    if body is None:
        body = MarkSeenIn()
    n = n4j.mark_outputs_presented(body.output_ids)
    return {"status": "marked", "count": n}


# ── Phase 6.5: Valence endpoints ──────────────────────

@app.post("/rate")
def rate_memory_endpoint(body: RateIn, x_mmu_session: Optional[str] = Header(None)):
    """
    Phase 6.5: Rate a memory with a like / dislike / clear.

    Valence is INERT in v1 -- it is stored in the address (~TTS segment) and
    as Neo4j properties, but does not change recall order, color transitions,
    or any other behavior. The data is being collected so Phase 6.6 can tune
    from real observations rather than guesses.

    val_type: 'like' | 'dislike' | 'clear'
    intensity: 1-9 (ignored for 'clear')
    """
    if mmu is None:
        raise HTTPException(503, "MMU not initialized")
    vt = body.val_type.lower().strip()
    if vt not in ("like", "dislike", "clear"):
        raise HTTPException(400, "val_type must be 'like', 'dislike', or 'clear'")
    intensity = max(1, min(9, body.intensity))
    old_addr  = body.address.strip()

    new_addr = mmu.rate_memory(old_addr, vt, intensity,
                               emotion_label=body.emotion_label)
    if new_addr is None:
        raise HTTPException(404, f"Address not found or invalid: {old_addr}")

    # Phase 8: same HAPPENED_IN tagging as /remember -- a rating is an
    # active thing that happened during this session, not just a search hit.
    if x_mmu_session:
        n4j.write_happened_in(new_addr, x_mmu_session)

    el = (body.emotion_label or "").strip()[:50] or None
    log.info("RATE | %s -> %s@%d | %s | emotion=%s",
             old_addr, vt, intensity, new_addr, el or "none")
    return {
        "old_address":   old_addr,
        "new_address":   new_addr,
        "val_type":      vt,
        "intensity":     intensity,
        "emotion_label": el,
    }


@app.get("/unrated_memories")
def unrated_memories_endpoint(limit: int = 10):
    """
    Phase 6.5: Return memories that have not been rated yet, ordered by
    CO_RECALLED weight. Used by the idle daemon to suggest rating candidates
    during light-tier passes.
    """
    if mmu is None:
        raise HTTPException(503, "MMU not initialized")
    try:
        memories = n4j.get_unrated_memories(limit=limit)
        return {"memories": memories, "count": len(memories)}
    except Exception as e:
        raise HTTPException(500, str(e))
