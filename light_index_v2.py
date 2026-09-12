"""
light_index_v2.py — Two-Tier Light Index for the MMU
====================================================

Replaces the flat memory-first JSON card catalog with two maps:

  TIER 1  keyword_index   stem/term -> [addresses]
          O(1) dict lookup. This is the GATE. If nothing matches here,
          recall returns immediately with zero Cypher round-trips.

  TIER 2  shortcut_cache  address -> {card metadata, neighbors[]}
          Neighbors are pre-computed at write time from SIMILAR_TO and
          incrementally updated from CO_RECALLED bumps. A warm hit needs
          no live graph traversal at all.

Neo4j remains the source of truth. It is consulted for:
  - payload fetch (cards carry metadata only, not payload)
  - cache refill when an entry is stale or the neighbor set is too thin
  - full rebuild / backfill

SQLite is gone. If Neo4j is unreachable, the gate still answers from the
in-memory index; only payload hydration degrades.

Drop-in target: replaces _load_light_index / _save_light_index in
mmu_server.py and short-circuits graph_recall stages 2 and 3.
"""

import os
import json
import time
import logging
import threading
import functools
from difflib import SequenceMatcher

log = logging.getLogger("light_index_v2")

INDEX_PATH = os.environ.get("MMU_INDEX_PATH", "memory_index_v2.json")

STEM_LEN = 4
SIMILAR_THRESH = 0.72

# Cache entry is considered stale after this many seconds, or once its
# generation counter falls behind the global write generation.
CACHE_TTL_SEC = 3600

# If a shortcut set has fewer than this many neighbors, fall through to
# live Cypher traversal rather than trusting the cache.
MIN_NEIGHBORS = 2

# Scoring weights — mirror the values in neo4j_layer.py so cached and
# live paths produce comparable scores.
W_DIRECT = 1.00
W_SIMILAR = 0.85
W_CORECALL = 0.60
W_PINNED = 1.00

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "am", "i", "you", "we", "it", "to", "of", "in", "on",
    "for", "with", "at", "by", "from", "as", "that", "this", "what",
    "how", "do", "does", "did", "my", "me", "your", "our",
}


def stem(word):
    """4-char stem, matching the existing fuzzy matcher's behavior."""
    return word[:STEM_LEN].lower()


def tokenize(text):
    """Return (words, stems) with stopwords dropped. Mirrors mmu_server."""
    raw = [w.strip(".,!?;:'\"()[]{}").lower() for w in text.split()]
    words = [w for w in raw if w and w not in STOPWORDS]
    stems = list({stem(w) for w in words if len(w) >= 3})
    return words, stems


def _synchronized(method):
    """
    Serialize a LightIndexV2 method against the instance lock.

    Applied to the readers as well as the writers. A reader that iterates
    keyword_index while an aging pass rewrites it raises RuntimeError, and a
    reader that returns a half-applied rename is worse than one that waits:
    the index IS the read path, so a torn read is a memory that silently does
    not exist. Contention is negligible in practice -- one conversation plus a
    daemon polling every 30 seconds.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class LightIndexV2:

    # Every MMU endpoint is a sync `def`, which means Starlette runs it in a
    # worker thread rather than on the event loop -- so two requests really do
    # execute this class at the same time. There is also always a second
    # caller: the idle daemon polls every 30s and calls /maintain and
    # /remember against the same server a conversation is using.
    #
    # Reentrant because the mutators call each other: rename() calls _link(),
    # add() calls bake_similar(). A plain Lock would deadlock on the first
    # rename.
    def __init__(self, path=INDEX_PATH, neo4j_layer=None):
        self._lock = threading.RLock()
        self.path = path
        self.n4j = neo4j_layer
        self.keyword_index = {}      # term/stem -> set(address)  single words
        self.word_parts_index = {}   # kept for backward compat, no longer written
        self.compound_index = {}     # "part1|part2" -> set(address)  AND-required
        self.shortcut_cache = {}
        self.generation = 0
        self._load()

    # ─────────────────────────────────────────
    #  PERSISTENCE
    # ─────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self.path):
            log.info("No index at %s — starting empty", self.path)
            return
        try:
            with open(self.path, "r") as f:
                blob = json.load(f)
            self.keyword_index = {
                k: set(v) for k, v in blob.get("keyword_index", {}).items()
            }
            self.word_parts_index = {
                k: set(v) for k, v in blob.get("word_parts_index", {}).items()
            }
            self.compound_index = {
                k: set(v) for k, v in blob.get("compound_index", {}).items()
            }
            self.shortcut_cache = blob.get("shortcut_cache", {})
            self.generation = blob.get("generation", 0)

            # Phase 10 migration: cards written before the aging redesign have
            # no touched_at. Seed them to load time rather than leaving them
            # None -- None means "never archive", which for a memory that is
            # never recalled would mean never archiving it at all, quietly
            # disabling aging for exactly the memories aging exists for.
            # Seeding "now" is the conservative direction: it delays the first
            # possible archive by ARCHIVE_MIN_DAYS and no further.
            _seed = time.time()
            _migrated = 0
            for _card in self.shortcut_cache.values():
                if not _card.get("touched_at"):
                    _card["touched_at"] = _seed
                    _migrated += 1
            if _migrated:
                log.info("Phase 10: seeded touched_at on %d cards", _migrated)
            log.info(
                "Loaded index: %d keywords, %d cards, gen %d",
                len(self.keyword_index), len(self.shortcut_cache), self.generation
            )
        except Exception as e:
            log.warning("Index load failed (%s) — starting empty", e)
            self.keyword_index = {}
            self.word_parts_index = {}
            self.shortcut_cache = {}


    def save(self):
        # The blob is built under the lock for two reasons. json.dump() walks
        # these dicts lazily, so a concurrent write during serialization raises
        # "dictionary changed size during iteration" -- and even when it does
        # not raise, it can persist a keyword_index that disagrees with the
        # shortcut_cache it was captured beside.
        with self._lock:
            blob = {
                "keyword_index":    {k: sorted(v) for k, v in self.keyword_index.items()},
                "word_parts_index": {k: sorted(v) for k, v in self.word_parts_index.items()},
                "compound_index": {k: sorted(v) for k, v in self.compound_index.items()},
                "shortcut_cache": dict(self.shortcut_cache),
                "generation": self.generation,
                "saved_at": time.time(),
            }
            # Unique per call. os.replace is atomic, but a fixed ".tmp" name is
            # not the thing being replaced -- two savers sharing one temp file
            # interleave their bytes INTO it and then both rename the result.
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(blob, f)
                os.replace(tmp, self.path)   # atomic — no half-written index
            except Exception:
                # Never leave a partial temp file behind to be mistaken for an
                # index, and never mask the original error.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    

    # ─────────────────────────────────────────
    #  TIER 1 — THE GATE
    # ─────────────────────────────────────────

    @_synchronized
    def gate(self, prompt):
        words, stems = tokenize(prompt)
        prompt_word_set = set(words)
        hits = set()

        # Single word/stem matching — keyword_index only
        for w in words:
            hits |= self.keyword_index.get(w, set())
        for s in stems:
            hits |= self.keyword_index.get(s, set())

        # Compound keywords — ALL parts must be present as full words in prompt
        # stem("starter")="star" will NOT satisfy "star|wars" since "wars" is absent
        for compound_key, addrs in self.compound_index.items():
            if all(part in prompt_word_set for part in compound_key.split("|")):
                hits |= addrs

        pinned = {
            addr for addr, card in self.shortcut_cache.items()
            if card.get("color") == "Red"
        }
        return hits, pinned

    # ─────────────────────────────────────────
    #  TIER 2 — SHORTCUT EXPANSION
    # ─────────────────────────────────────────

    @_synchronized
    def _is_fresh(self, addr):
        card = self.shortcut_cache.get(addr)
        if not card:
            return False
        # Generation lag: how many writes have happened since this card
        # was last updated. Threshold of 20 is generous — avoids thrashing
        # cold on busy save sessions (e.g. a backfill run).
        if card.get("gen", -1) < self.generation - 20:
            return False
        if time.time() - card.get("cached_at", 0) > CACHE_TTL_SEC:
            return False
        if len(card.get("neighbors", [])) < MIN_NEIGHBORS:
            return False
        return True

    @_synchronized
    def expand(self, seed_addrs):
        """
        Pull pre-computed neighbors for the seed hits.

        Returns (scored, cold_seeds) where scored is
        {address: (score, via)} and cold_seeds is the list of addresses
        whose cache was stale/thin and need live Cypher traversal.
        """
        scored = {}
        cold = []

        for addr in seed_addrs:
            if not self._is_fresh(addr):
                cold.append(addr)
                continue
            for n in self.shortcut_cache[addr].get("neighbors", []):
                n_addr, n_score, via = n[0], n[1], n[2]
                weight = W_SIMILAR if via == "similar" else W_CORECALL
                s = n_score * weight
                # Blue stays dormant through expansion — direct hits only
                n_card = self.shortcut_cache.get(n_addr, {})
                if n_card.get("color") == "Blue":
                    continue
                if n_addr not in scored or s > scored[n_addr][0]:
                    scored[n_addr] = (s, via)

        return scored, cold

    @_synchronized
    def touch(self, addresses, when=None):
        """
        Mark addresses as recalled just now (Phase 10 aging).

        Mirrors what write_recall_edges() writes to Neo4j. Kept here so the
        aging pass never needs a graph round trip.
        """
        ts = when if when is not None else time.time()
        for a in addresses:
            card = self.shortcut_cache.get(a)
            if card is not None:
                card["touched_at"] = ts

    @_synchronized
    def days_since_touch(self, address, now=None):
        """
        Days since this memory was last recalled, or None if unknown.

        None means "no data", NEVER "infinitely stale". The caller must treat
        it as a reason not to archive -- an unknown touch time is exactly the
        case where guessing wrong silently archives a live memory.
        """
        card = self.shortcut_cache.get(address)
        if not card:
            return None
        ts = card.get("touched_at")
        if not ts:
            return None
        return ((now if now is not None else time.time()) - ts) / 86400.0

    def rank(self, direct, pinned, expanded, top_k=10):
        """
        Merge and rank without touching Neo4j. Uses card metadata
        (color, priority) so the cut happens before payload hydration.
        """
        merged = {}
        for addr in pinned:
            merged[addr] = (W_PINNED, "pinned")
        for addr in direct:
            merged[addr] = (W_DIRECT, "direct")
        for addr, (score, via) in expanded.items():
            if addr not in merged:
                merged[addr] = (score, via)

        def sort_key(item):
            addr, (score, via) = item
            card = self.shortcut_cache.get(addr, {})
            pri = card.get("priority", 5)
            return (-score, pri)

        ordered = sorted(merged.items(), key=sort_key)[:top_k]
        return [
            {"address": a, "score": round(s, 3), "via": v,
             **{k: self.shortcut_cache.get(a, {}).get(k)
                for k in ("color", "priority", "src_type")}}
            for a, (s, v) in ordered
        ]

    # ─────────────────────────────────────────
    #  WRITE PATH — KEEPING TIERS IN SYNC
    # ─────────────────────────────────────────

    @_synchronized
    def add(self, address, keywords, color="Green", priority=5,
            src_type=0, neighbors=None):
        self.generation += 1
        for kw in keywords:
            if '%' in kw:
                # Compound — Nova explicitly requires ALL parts to match
                parts = sorted(
                    p.strip().lower() for p in kw.split('%')
                    if len(p.strip()) >= 3 and p.strip().lower() not in STOPWORDS
                )
                if len(parts) >= 2:
                    self.compound_index.setdefault("|".join(parts), set()).add(address)
                elif parts:
                    # Degenerate case: only one valid part — treat as single word
                    self.keyword_index.setdefault(parts[0], set()).add(address)
                    self.keyword_index.setdefault(stem(parts[0]), set()).add(address)
            elif ' ' in kw:
                # Old-style space-separated — auto-convert to compound AND keep full phrase
                parts = sorted(
                    p for p in kw.lower().split()
                    if len(p) >= 3 and p not in STOPWORDS
                )
                if len(parts) >= 2:
                    # AND-required: "star wars" -> gate needs BOTH star AND wars
                    self.compound_index.setdefault("|".join(parts), set()).add(address)
                # Full phrase also indexed for exact match (rare but harmless)
                self.keyword_index.setdefault(kw.lower(), set()).add(address)
            else:
                # Single word — term + stem in keyword_index
                self.keyword_index.setdefault(kw.lower(), set()).add(address)
                self.keyword_index.setdefault(stem(kw), set()).add(address)

        self.shortcut_cache[address] = {
            "keywords": list(keywords),
            "color": color,
            "priority": priority,
            "src_type": src_type,
            "neighbors": neighbors or [],
            "gen": self.generation,
            "cached_at": time.time(),
            # Phase 10: local mirror of Neo4j's m.last_touched_at, as a unix
            # float. The aging pass runs over this in-memory index on every
            # recall; reading the authoritative value from Neo4j for ~950
            # memories per recall would be absurd. Same denormalisation the
            # card already does for color/priority/src_type -- Neo4j stays the
            # source of truth, this is a cache of it.
            "touched_at": time.time(),
        }

    @_synchronized
    def bake_similar(self, address, keywords):
        """
        Pre-compute SIMILAR_TO neighbors at write time by scanning the
        keyword index — the same fuzzy logic, but done once on write
        instead of on every recall.
        """
        neighbors = {}
        my_stems = {stem(k) for kw in keywords for k in kw.lower().split()}

        for token, addrs in self.keyword_index.items():
            if token in my_stems:
                for a in addrs:
                    if a != address:
                        neighbors[a] = max(neighbors.get(a, 0), 0.95)
                continue
            for ms in my_stems:
                ratio = SequenceMatcher(None, token, ms).ratio()
                if ratio >= SIMILAR_THRESH:
                    for a in addrs:
                        if a != address:
                            neighbors[a] = max(neighbors.get(a, 0), ratio)

        baked = [[a, round(s, 3), "similar"] for a, s in
                 sorted(neighbors.items(), key=lambda x: -x[1])[:20]]
        if address in self.shortcut_cache:
            self.shortcut_cache[address]["neighbors"] = baked
        return baked

    @_synchronized
    def bump_corecall(self, addresses):
        """
        Called after a recall. Adds/strengthens CO_RECALLED shortcuts
        between every pair that surfaced together. This is what makes
        the cache get smarter with use.
        Saves every 5 bumps to avoid losing co-recall data on restart
        without hammering disk on every single recall.
        """
        self.generation += 1
        addrs = list(dict.fromkeys(addresses))   # dedupe, keep order

        for i, a in enumerate(addrs):
            for b in addrs[i + 1:]:
                # CO_RECALLED is bidirectional — write both directions
                self._link(a, b)
                self._link(b, a)

        for a in addrs:
            card = self.shortcut_cache.get(a)
            if not card:
                continue
            card["neighbors"].sort(key=lambda n: -n[1])
            card["neighbors"] = card["neighbors"][:20]
            card["gen"] = self.generation
            card["cached_at"] = time.time()

        # Periodic save — every 5 bumps. Keeps data fresh without
        # hitting disk on every recall turn.
        if self.generation % 5 == 0:
            self.save()

    @_synchronized
    def _link(self, src, dst):
        """
        Add or strengthen a single co-recall shortcut.
        Increment uses diminishing returns so weak edges strengthen
        faster than already-strong ones — mirrors biological Hebbian
        plasticity and keeps scores from clustering at 1.0.
        Starting score 0.30 matches the normalized weight=1 baseline
        from rebuild_from_neo4j (weight/max_weight where max is typically
        3-5 in early sessions).
        """
        card = self.shortcut_cache.get(src)
        if not card:
            return
        found = next((n for n in card["neighbors"] if n[0] == dst), None)
        if found:
            current = found[1]
            # Diminishing returns: increment shrinks as score grows
            # score 0.3 -> +0.08, score 0.6 -> +0.05, score 0.9 -> +0.02
            increment = 0.10 * (1.0 - current)
            found[1] = min(1.0, round(current + increment, 3))
        else:
            # 0.30 = normalized weight=1 baseline (consistent with rebuild)
            card["neighbors"].append([dst, 0.30, "corecall"])

    @_synchronized
    def set_color(self, address, color):
        """Color matrix transitions must update the card, not just Neo4j."""
        if address in self.shortcut_cache:
            self.shortcut_cache[address]["color"] = color
            self.save()   # color changes affect gate filtering — persist immediately

    @_synchronized
    def rename(self, old_addr, new_addr):
        """
        Aging renames addresses. Both tiers must follow or the index
        silently rots — this is the same class of bug as the
        CO_RECALLED / _age_memories mismatch.

        Returns True when the card moved, False when the rename was refused.
        A refusal leaves BOTH tiers exactly as they were: the caller's memory
        keeps its old address and stays reachable.

        Two refusals, and neither used to exist:

        1. The target is already occupied. This is not hypothetical -- USE is
           encoded into the address, so a memory whose USE counter is climbing
           and one sharing its CON (duplicates survive from the old count()+1
           numbering) can converge on the same string. The line this replaced
           was `cache[new] = cache.pop(old)`, which resolves that collision by
           destroying the occupant's card: two memories in, one card out. The
           evicted memory is still in Neo4j and still counted by every graph
           query, and is invisible to recall, because this index IS the read
           path. That is the drift `/index_repair` kept reporting as one
           MISSING address with no matching PHANTOM -- a removal with no add,
           which ordinary drift cannot produce.

        2. There is no card at old_addr. The keyword rewrite below used to run
           regardless, which pointed live gate() terms at an address holding no
           card -- findable, unrankable, unhydratable.
        """
        if old_addr == new_addr:
            return True
        if old_addr not in self.shortcut_cache:
            log.warning("rename refused: no card at %s (target %s left alone)",
                        old_addr, new_addr)
            return False
        if new_addr in self.shortcut_cache:
            log.warning(
                "rename refused: %s is already occupied, so moving %s there "
                "would drop a memory out of the read path. Both keep their "
                "current addresses.", new_addr, old_addr
            )
            return False

        self.shortcut_cache[new_addr] = self.shortcut_cache.pop(old_addr)
        for idx_dict in (self.keyword_index, self.word_parts_index):
            for token, addrs in idx_dict.items():
                if old_addr in addrs:
                    addrs.discard(old_addr)
                    addrs.add(new_addr)
        for addrs in self.compound_index.values():
            if old_addr in addrs:
                addrs.discard(old_addr)
                addrs.add(new_addr)
        for card in self.shortcut_cache.values():
            for n in card.get("neighbors", []):
                if n[0] == old_addr:
                    n[0] = new_addr
        self.save()   # address renames must survive restart or index rots
        return True

    # ─────────────────────────────────────────
    #  BACKFILL FROM NEO4J
    # ─────────────────────────────────────────

    @_synchronized
    def rebuild_from_neo4j(self, driver):
        """
        One-time build of both tiers from the existing graph.
        Run this once after deploying; afterwards the index is
        maintained incrementally by add() and bump_corecall().
        """
        self.keyword_index = {}
        self.word_parts_index = {}
        self.shortcut_cache = {}
        self.generation = 0

        with driver.session() as s:
            rows = s.run("""
                MATCH (m:Memory)
                OPTIONAL MATCH (m)-[:HAS_KEYWORD]->(k:Keyword)
                RETURN m.address AS address,
                       m.color AS color,
                       m.priority AS priority,
                       m.src_type AS src_type,
                       collect(DISTINCT k.term) AS keywords
            """)
            for r in rows:
                self.add(
                    r["address"],
                    [k for k in (r["keywords"] or []) if k],
                    color=r["color"] or "Green",
                    priority=r["priority"] if r["priority"] is not None else 5,
                    src_type=r["src_type"] or 0,
                )

            # Bake SIMILAR_TO neighbors straight from the graph rather
            # than recomputing the fuzzy scores in Python.
            sim = s.run("""
                MATCH (a:Memory)-[:HAS_KEYWORD]->(k1:Keyword)
                      -[r:SIMILAR_TO]-(k2:Keyword)<-[:HAS_KEYWORD]-(b:Memory)
                WHERE a.address <> b.address AND r.score >= $thresh
                RETURN a.address AS src, b.address AS dst,
                       max(r.score) AS score
            """, thresh=SIMILAR_THRESH)
            for r in sim:
                card = self.shortcut_cache.get(r["src"])
                if card is not None:
                    card["neighbors"].append(
                        [r["dst"], round(r["score"], 3), "similar"]
                    )

            # And the CO_RECALLED weights, normalized to 0-1.
            co = s.run("""
                MATCH (a:Memory)-[r:CO_RECALLED]-(b:Memory)
                RETURN a.address AS src, b.address AS dst, r.weight AS weight
            """)
            co_rows = [dict(r) for r in co]
            max_w = max([r["weight"] or 1 for r in co_rows], default=1)
            for r in co_rows:
                card = self.shortcut_cache.get(r["src"])
                if card is not None:
                    card["neighbors"].append(
                        [r["dst"], round((r["weight"] or 0) / max_w, 3), "corecall"]
                    )

        for card in self.shortcut_cache.values():
            best = {}
            for n in card["neighbors"]:
                if n[0] not in best or n[1] > best[n[0]][1]:
                    best[n[0]] = n
            card["neighbors"] = sorted(best.values(), key=lambda n: -n[1])[:20]

        self.save()
        return {
            "keywords": len(self.keyword_index),
            "cards": len(self.shortcut_cache),
            "shortcuts": sum(len(c["neighbors"])
                             for c in self.shortcut_cache.values()),
        }

    @_synchronized
    def remove(self, address):
        """
        Remove a memory from both tiers and clean up neighbor references.
        Called by delete_memory() — keeps the index consistent without a full rebuild.
        """
        self.shortcut_cache.pop(address, None)
        for addrs in self.keyword_index.values():
            addrs.discard(address)
        for addrs in self.compound_index.values():
            addrs.discard(address)
        for card in self.shortcut_cache.values():
            card["neighbors"] = [n for n in card["neighbors"] if n[0] != address]
        self.save()

    # ─────────────────────────────────────────
    #  STATS
    # ─────────────────────────────────────────

    @_synchronized
    def stats(self):
        n = [len(c.get("neighbors", [])) for c in self.shortcut_cache.values()]
        fresh = sum(1 for a in self.shortcut_cache if self._is_fresh(a))
        return {
            "keywords": len(self.keyword_index),
            "cards": len(self.shortcut_cache),
            "shortcuts_total": sum(n),
            "shortcuts_avg": round(sum(n) / len(n), 2) if n else 0,
            "cards_fresh": fresh,
            "cards_cold": len(self.shortcut_cache) - fresh,
            "generation": self.generation,
        }
