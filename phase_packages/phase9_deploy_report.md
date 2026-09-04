# Phase 9 — Semantic Enhancement (Embedding Layer) — DEPLOY REPORT

Status: **deployed and validated** on the development machine, 2026-08-29.
Design doc: `C:\MMU\phase_packages\phase9_semantic_embeddings.md`
Report author: Claude Code (real terminal + Docker access, all output below is actual).

---

## 1. Decisions resolved

### DECISION NEEDED #1 — who calls the embedding model
**Answered by the user: Option A (narrow, explicit exception).**

`mmu_server.py` now contains an `EmbeddingClient` that calls exactly one endpoint,
`/v1/embeddings`. There is no chat method on the class and there should never be one.
The module docstring documents this as a deliberate, scoped exception with the
reasoning stated plainly: vector math, not cognition, and `/recall` is a synchronous
REST endpoint so a brand-new prompt cannot have its query embedding deferred to
`mmu_idle_daemon.py`.

The boundary that remains intact: `mmu_server.py` still never calls
`/chat/completions`, and all generative cognition still lives in the idle daemon.

### DECISION NEEDED #2 — model and dimension
**Resolved by measurement, not assumption.**

`text-embedding-nomic-embed-text-v1.5` was already loadable in LM Studio. Hitting
`/v1/embeddings` with a test string returned a vector of length **768**. This happens
to match the roadmap's guess for `nomic-embed-text`, but it is now a verified number
rather than an inherited assumption. Both model name and dimension are environment
variables (`MMU_EMBEDDING_MODEL`, `MMU_EMBEDDING_DIM`).

---

## 2. Three corrections to the design doc

These are recorded because each one would have shipped a real defect if followed
literally.

### 2.1 The specified fallback trigger was dead code
The doc specified hooking the semantic fallback to the existing early return:

```python
if not direct and not pinned:   # mmu_server.py, pre-Phase-9
```

`LightIndexV2.gate()` returns **every Red memory** as `pinned`, unconditionally
(`light_index_v2.py`, gate()). The graph contains 2 Red memories, so `pinned` is never
empty and that condition is never true. A fallback hung off it would have been dead on
arrival.

**Resolution:** the trigger is now `not direct`. Red pins are ambient context and say
nothing about topical relevance to the current prompt; whether the gate found anything
*topical* is the meaningful question. The user confirmed this reading.

### 2.2 `MMU_SEMANTIC_FLOOR = 0.15` is on the wrong scale
Neo4j normalizes cosine similarity into **[0, 1]**, where **1.0 = identical,
0.5 = orthogonal, 0.0 = diametrically opposed**. Verified directly:

```
$ cypher-shell "RETURN vector.similarity.cosine([1.0,0.0,0.0],[1.0,0.0,0.0]) AS same,
                       vector.similarity.cosine([1.0,0.0],[0.0,1.0]) AS orth"
same, orth
1.0, 0.5
```

A floor of 0.15 sits below the orthogonal midpoint and would therefore filter
absolutely nothing. Measured distribution on the real graph is in §5.4.

### 2.3 LM Studio is not on `localhost` from the server's perspective
The doc's validation step uses `curl http://localhost:1234/v1/embeddings`, which works
from the Windows host but not from inside the `mmu-server` container, where
`localhost` is the container itself. Verified reachability from inside the container:

```
$ docker exec mmu-memory-server python -c "...urlopen('http://host.docker.internal:1234/v1/models')"
resolve host.docker.internal -> 192.168.65.254
models reachable: [... 'text-embedding-nomic-embed-text-v1.5' ...]
```

`MMU_EMBEDDING_BASE` defaults to `http://host.docker.internal:1234/v1`, and
`extra_hosts: host.docker.internal:host-gateway` was added to the compose service so
this stays correct if the Docker Desktop default ever changes.

---

## 3. What changed

### `neo4j_layer.py`
| Addition | Purpose |
|---|---|
| `EMBEDDING_DIM`, `EMBEDDING_INDEX` | config, dimension from `MMU_EMBEDDING_DIM` |
| vector index in `bootstrap_schema()` | idempotent `CREATE VECTOR INDEX ... IF NOT EXISTS`, cosine |
| `write_embedding(address, vector)` | uses `db.create.setNodeVectorProperty` so the value is in Neo4j's native vector encoding, which is what the index actually reads |
| `semantic_search(query_vector, top_k, exclude_blue, exclude_addrs)` | wraps `db.index.vector.queryNodes`; over-fetches then filters |
| `similarity_for_addresses(addresses, query_vector)` | scores specific gate hits the ANN search didn't return |
| `count_unembedded_memories()`, `fetch_unembedded_memories(limit)` | backfill support |

Blue nodes are excluded from semantic search, matching how `expand()` already keeps
Blue dormant through CO_RECALLED hops. Red nodes are excluded because the gate already
injects them unconditionally — returning them again would duplicate rows and burn
`top_k` slots.

`fetch_unembedded_memories()` deliberately has **no SKIP offset**: the backfill writes
an embedding to every row it processes, so rows drop out of the result set on the next
call. Paging by SKIP against a shrinking set would silently skip records.

### `mmu_server.py`
| Addition | Purpose |
|---|---|
| `EmbeddingClient` + `EMB` singleton | `/v1/embeddings` only; `urllib`, not `requests` |
| semantic stage in `_v2_recall()` | fallback when `direct` is empty; similarity annotation always |
| `prompt_text` param on `_v2_recall()` | the raw prompt, not the joined token set |
| `_embedding_text(keywords, payload)` | the one canonical definition of a memory's embed text |
| write-time embedding in `add_memory()` | every write path gets a vector; try/except, never fails the save |
| `POST /backfill_embeddings` | idempotent, paged, `limit`/`batch` params |
| `GET /embedding_status` | observability: is the semantic layer actually live |

**Embedding lives in `add_memory()`, not in the endpoints.** This was moved during
review (see §7.1). Any caller that creates a memory — `/remember`, `/pin`, Phase 11's
`/ingest`, anything future — gets a vector automatically and cannot forget to ask for
one. `add_memory(..., embed=False)` is available for bulk paths that intend to batch
their own embedding work.

**One canonical embed text.** `_embedding_text()` is the single definition of "what
text represents a memory", used by both `add_memory()` and the backfill. They
previously disagreed (§7.2), which silently produced two different vector spaces in
one index.

**`urllib`, not `requests`:** the `mmu-server` image does not install `requests`
(`docker exec mmu-memory-server python -c "import requests"` → `ModuleNotFoundError`).
Phase 9 adds no new dependency to the container that serves live recall.

**Ranking:** semantic hits are rescaled from Neo4j's normalized [0,1] back to raw
[0,1] (`max(0, (score - 0.5) * 2)`) and then weighted by `W_SEMANTIC = 0.80`, which is
deliberately **below** `W_SIMILAR = 0.85` in `light_index_v2.py`. A semantic hit can
therefore never outrank a real keyword hit. Semantic recall fills gaps; it does not
take over.

**`via` tagging:** semantic results are tagged `"via": "semantic"`, and every returned
row now also carries `semantic_score`. Per Phase 6.6's principle of making Nova's
internal state visible, *why* a memory surfaced stays legible — semantic hits are
never silently blended into the keyword bucket.

### `docker-compose.yml`
```yaml
- MMU_EMBEDDING_ENABLED=true
- MMU_EMBEDDING_BASE=http://host.docker.internal:1234/v1
- MMU_EMBEDDING_MODEL=text-embedding-nomic-embed-text-v1.5
- MMU_EMBEDDING_DIM=768
- MMU_SEMANTIC_FLOOR=0.0
- MMU_SEMANTIC_WEIGHT=0.80
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Backups: `neo4j_layer.py.pre9.bak`, `mmu_server.py.pre9.bak`,
`docker-compose.yml.pre9.bak`.

---

## 4. Deploy sequence — actual output

### 4.1 Neo4j version and vector index support (verified, not assumed)
```
$ docker exec mmu-neo4j cypher-shell ... "CALL dbms.components() ..."
name, versions, edition
"Neo4j Kernel", ["5.18.1"], "community"

$ ... "CREATE VECTOR INDEX memory_embedding IF NOT EXISTS FOR (m:Memory) ON (m.embedding) ..."
$ ... "SHOW INDEXES YIELD ... WHERE type = 'VECTOR'"
name, type, state, labelsOrTypes, properties
"memory_embedding", "VECTOR", "ONLINE", ["Memory"], ["embedding"]
```

Note: Neo4j does not accept query parameters inside an index `OPTIONS` map, so the
dimension is interpolated into the statement. The `int()` cast at import time
guarantees it is not injectable.

### 4.2 Rebuild + running-container verification
```
$ docker compose build --no-cache mmu-server
 Image mmu-mmu-server Built

$ docker compose up -d --force-recreate mmu-server
 Container mmu-memory-server Started

$ docker exec mmu-memory-server grep -n "..." /app/mmu_server.py /app/neo4j_layer.py
/app/mmu_server.py:56:  POST /backfill_embeddings  -- embed every Memory node that has none yet
/app/mmu_server.py:249:class EmbeddingClient:
/app/mmu_server.py:497:            ranked = ranked[:top_k]
/app/mmu_server.py:797:@app.post("/backfill_embeddings")
/app/neo4j_layer.py:1055:def write_embedding(address, vector):
/app/neo4j_layer.py:1082:def semantic_search(query_vector, top_k=10, exclude_blue=True, ...)
```

Startup log confirms the index is ensured on every boot:
```
INFO:neo4j_layer:Vector index memory_embedding ensured (dim=768, cosine)
INFO:neo4j_layer:Neo4j schema bootstrapped (Phase 4 fields backfilled)
```

### 4.3 Backfill
```
$ curl -X POST http://localhost:8765/backfill_embeddings
{ "status": "ok", "embedded": 93, "failed": 0, "failed_addresses": [],
  "remaining": 0, "model": "text-embedding-nomic-embed-text-v1.5",
  "dim": 768, "elapsed_ms": 1992.8 }
```

**93 of 93 pre-Phase-9 memories embedded, 0 failures, ~2 seconds.**

Idempotency check — immediate re-run:
```
{ "embedded": 0, "failed": 0, "remaining": 0, "elapsed_ms": 1.3 }
```

Independent confirmation straight from the database:
```
$ docker exec mmu-neo4j cypher-shell ... "MATCH (m:Memory) RETURN count(m) AS total,
                                          count(m.embedding) AS embedded"
total, embedded
93, 93
```

The backfill prepends each memory's keywords to its payload before embedding, so the
vector reflects the memory's indexed terms as well as its prose — the same text a
keyword gate hit would have matched on.

---

## 5. Validation

### 5.1 Semantic recall works (the core Phase 9 goal)
The doc's suggested test ("tell me about my pet" against a dog memory) turned out to
be a **poor test case on this graph**: that prompt already hit via the keyword gate
before Phase 9, so it proves nothing about semantic recall.

A genuine keyword-disjoint case was found instead: **`"who am I married to"`**. The
"married to Sam" memory could not surface via the gate, because the gate indexes
*stored keywords*, not payload prose, and "married" is not one of its keywords.

**Before Phase 9** — only the 2 ambient Red pins, no topical answer:
```
via=pinned  score=1.0  001...  "User's name is Alex..."
via=pinned  score=1.0  002...  "The user runs Ollama via Docker on port 11434..."
```

**After Phase 9:**
```
via=pinned    score=1.0    sem=0.741  001...
via=pinned    score=1.0    sem=0.700  002...
via=semantic  score=0.454  sem=0.784  020...  "User's biographical details -- age,
                                                family, and location..."
via=semantic  score=0.416  sem=0.760  029...
via=semantic  score=0.411  sem=0.757  025...
```

A memory the keyword gate structurally could not reach is now recalled, correctly
tagged `"via": "semantic"`.

### 5.2 No regression on the keyword path
Two probes initially appeared to regress. **They did not.** Re-running the identical
probe set twice with Phase 9 held constant produced the *same churn*, with the
"lost" memory returning in the second run:

```
Two consecutive post-Phase-9 runs, identical config:
  stable Star Wars
  CHURN  Dimensional Relativity Theory physics paper  run1-only=['054'] run2-only=['039']
  CHURN  Castlevania Symphony of the Night video game run1-only=['020'] run2-only=['022']
  stable what is the user's game design focus
  stable tell me about my pet
  stable who am I married to
```

**Root cause (pre-existing, unrelated to Phase 9):** every `direct` hit scores exactly
`W_DIRECT = 1.00`. That physics probe produces **17 rows tied at score 1.0** against
`top_k=5`:

```
$ curl ... '{"prompt":"Dimensional Relativity Theory physics paper","top_k":20}'
top_k=20 -> count=20
rows tied at score=1.0: 17  (top_k=5 keeps only 5 of these, arbitrarily)
```

`rank()` sorts by `(-score, priority)`; among rows tied on both, Python's stable sort
preserves input order, which comes from iterating `direct` — a **set**. Because
`_age_memories()` rewrites addresses on every recall, the set's contents change between
runs and its iteration order shifts, so an arbitrary subset of tied hits survives the
cut. This is worth knowing about independently of Phase 9 (see §7).

Final coverage comparison, baseline vs. deployed:
```
PROBE                                          BEFORE      AFTER       SEMANTIC ROWS
Star Wars                                      2 topical   3 topical   0
Dimensional Relativity Theory physics paper    5 topical   5 topical   0
Castlevania Symphony of the Night video games  3 topical   3 topical   0
what is the user's game design focus               4 topical   4 topical   0
tell me about my pet                           1 topical   3 topical   0
who am I married to                            0 topical   3 topical   3
```

No probe lost topical coverage. The previously-empty case gained three rows.

### 5.3 Invariants asserted programmatically
```
INVARIANT semantic-only-when-gate-empty: HOLDS
every row carries a via tag: True
via values in use: ['direct', 'pinned', 'semantic', 'similar']
```

### 5.4 Similarity floor — calibrated from real data
Ordering the 17 tied-at-1.0 rows by `semantic_score` for the prompt
*"Dimensional Relativity Theory physics paper"*:

```
sem=0.886  069  The user plans to have Nova read their "Dimensional Relativity Theory" paper
sem=0.879  040  In the user's DRT, forces propagate through space as dimensional waves
sem=0.878  017  The user's DRT details: Inspired by Lee Smolin's...
sem=0.862  019  The user is a particle physics researcher writing an in-depth theory
sem=0.839  085  Critique of Einstein's Relativity...
sem=0.836  016  The user is a particle physics researcher...
sem=0.827  021  The user has a physics paper titled "Dimensional Relativity Theory"
      ---------------- topical / tangential boundary ----------------
sem=0.788  035  Game design decision: Hero's dimensional wave blade...
sem=0.764  076  The concept of 'Scars' as a unifying bridge...
sem=0.754  041  Nova is good at brainstorming creative connections...
sem=0.742  039  the user enjoys creating easter eggs across his projects
sem=0.726  060  Relational Dynamic: The user views himself as...
sem=0.724  038  the user encourages Nova to save her own memories
sem=0.709  057  Relational Dynamic: Nova identifies as a girl...
sem=0.695  054  Nova's self-observation: When the user shares vulnerable things...
```

**This is the stem-collision problem, measured.** Row `054` (Nova's self-observation
about alcohol and sleep) scored *identically* to the real DRT papers under the keyword
gate — `score=1.0` — and semantic similarity correctly rates it dead last at 0.695.

Observed bands on this graph:
- **unrelated / collision noise:** 0.695 – 0.742
- **tangential:** 0.754 – 0.788
- **genuinely relevant:** 0.827 – 0.886

A floor of **0.75** was tested live and behaved exactly as intended, dropping three
rows across the sweep, all of them noise:
```
semantic floor dropped 049... (via=similar sim=0.738 < 0.750)   "MMU design philosophy" vs "Star Wars"
semantic floor dropped 038... (via=direct  sim=0.724 < 0.750)   "Nova saves her own memories" vs "physics paper"
semantic floor dropped 039... (via=direct  sim=0.742 < 0.750)   "easter eggs" vs "physics paper"
```
No relevant memory was dropped at that threshold.

**Shipped setting: `MMU_SEMANTIC_FLOOR=0.0` (dropping disabled), by the user's decision.**
Annotation still runs, so every `/recall` row carries its `semantic_score` and real
values can be observed across live sessions before committing to a threshold. When
that data supports it, `0.75` is the empirically-derived starting point. Raising the
value is a one-line compose change plus a restart; no code change and no re-index.

### 5.5 Bug found and fixed in this implementation
The floor initially filtered **after** the `top_k` cut, so the physics probe returned
3 results instead of 5 while perfectly good candidates sat just below the line:

```
FLOOR ON (0.75), before fix:  035, 040, 085                    <- only 3 rows
FLOOR ON (0.75), after fix:   035, 040, 041, 076, 085          <- 5 rows, all above floor
```

Fixed by over-fetching `top_k * 3` before filtering, then trimming back to `top_k`.
Rebuilt with `--no-cache` and re-verified in the running container
(`/app/mmu_server.py:497: ranked = ranked[:top_k]`).

### 5.6 Write-time embedding
```
$ curl -X POST http://localhost:8765/remember -d '{"keywords":["phase9",...],...}'
{ "status": "saved", "address": "094.005.500.000,000~000|1.000.000", "embedded": true }

$ docker exec mmu-neo4j cypher-shell ... "MATCH (m:Memory) WHERE m.payload STARTS WITH
                                          'Phase 9 validation probe' RETURN m.address, size(m.embedding)"
addr, dim
"094.005.500.000,000~000|1.000.000", 768
```

### 5.7 Graceful degradation — tested for real, not assumed
`MMU_EMBEDDING_BASE` was pointed at a dead port (59999) and the container recreated:

```
=== /remember with backend DOWN ===
{ "status": "saved", "address": "095...", "embedded": false }

=== /recall with backend DOWN ===
count=3 read_path=v2 read_ms=3.4
   001 via=pinned  sem=None
   002 via=pinned  sem=None
   012 via=direct  sem=None
```

Saves still succeed, recall still serves keyword results in 3.4ms, no 500s. The
`EmbeddingClient` also warns only once per outage rather than on every request.

Restoring the backend, the backfill found exactly the memory that had been missed:
```
missing=1 total=95 reachable=True
{ "embedded": 1, "failed": 0, "remaining": 0, "elapsed_ms": 40.3 }
```

**The self-healing loop is proven end to end:** backend down → memory saved without a
vector → `/embedding_status` reports it missing → backfill picks up exactly that one.

There is also a dimension guard: if the loaded model returns a vector whose length
does not match `MMU_EMBEDDING_DIM`, the client logs an error and refuses to write it,
rather than poisoning the index with a mismatched vector.

### 5.8 Recall latency
Observed `read_ms` across the probe set after deployment: **23.9 – 61.2 ms**, against
a 42.8 – 119.3 ms baseline before. The added embedding round-trip to LM Studio is not
a practical regression at this graph size.

---

## 6. Acceptance checklist

- [x] Neo4j version confirmed to support `CREATE VECTOR INDEX` (5.18.1 community, index `ONLINE`)
- [x] Real embedding dimension confirmed against the running LM Studio instance (768, measured)
- [x] DECISION NEEDED #1 answered and stated plainly (Option A, §1)
- [x] `/remember` embeds new memories at write time, degrading gracefully on failure (§5.6, §5.7)
- [x] Backfill run against all pre-Phase-9 memories, count reported (93/93, §4.3)
- [x] `/recall` returns semantic-fallback results when the keyword gate finds nothing (§5.1)
- [x] `/recall` still returns correct results for existing keyword test cases, no regression (§5.2)
- [x] Every result in `/recall`'s response is tagged with an accurate `via` value (§5.3)
- [x] `--no-cache` rebuild + running-container grep verification performed, output included (§4.2)

---

## 7. Issues found during review, and what was done

Three defects were found *after* the initial deploy, during a review pass prompted by
The user's instruction to close the ingestion gap properly rather than lean on the backfill.
All three are fixed.

### 7.1 Embedding was wired to the endpoint, not the write primitive (FIXED)
As originally shipped, embedding lived in the `/remember` handler. Consequences:
- `/pin` created Red memories with **no vector at all**;
- Phase 11's `/ingest` would have called `add_memory()` per chunk and produced an
  entire ingested document that semantic recall could not see;
- every future write path would have had to remember to embed.

**Fix:** embedding moved into `MMUCore.add_memory()`. Verified across write paths:
```
POST /remember  -> {"status":"saved",  "address":"094...", "embedded": true}
POST /pin       -> {"status":"pinned", "address":"095...", "embedded": true}

MATCH (m:Memory) WHERE m.payload STARTS WITH 'Gap-fix probe' RETURN m.address, m.color, size(m.embedding)
"094.005.500.000,...", "Green", 768
"095.001.500.000,...", "Red",   768     <- /pin, which produced no vector before
```
Degradation re-verified after the move (backend pointed at a dead port): save still
succeeded with `"embedded": false`, recall still served in 3.3ms, and the catch-up
backfill then found exactly the one memory that slipped.

### 7.2 Two different embed texts in one index (FIXED)
`/remember` embedded `payload` alone; the backfill embedded `keywords. payload`.
Vectors are only comparable if the text that produced them was assembled the same way,
so this was quietly producing two vector spaces in a single index depending on how a
memory happened to be created. Now both go through `_embedding_text()`.

All 93 current memories came from the backfill, so the live graph is internally
consistent; the drift would have appeared as new memories were saved.

### 7.3 `delete_memory()` never removed from the v2 index (FIXED — pre-existing)
`MMUCore.delete_memory()` called `n4j.write_delete()` only. `LightIndexV2.remove()`
exists and even documents itself as "Called by `delete_memory()`", but was never wired
up. Deleted memories therefore survived in `shortcut_cache` indefinitely:

- a deleted **Red** memory was still returned by `gate()` as pinned, won a `top_k` slot
  in `rank()`, then silently vanished during payload hydration — **so every recall
  quietly returned one fewer result than requested**;
- `_age_memories()` iterates `shortcut_cache`, so phantom entries kept being aged and
  written back to Neo4j;
- `/health` counted them, drifting from the real graph (98 vs 93).

This was surfaced by deleting the validation memories and noticing every probe lost
exactly one row. Fixed, and the 5 phantoms purged. Index and Neo4j now agree exactly.

### 7.4 Validation recalls over-archived the graph (REMEDIATED)
`_age_memories()` runs on **every** `/recall` and increments `use` on every
non-recalled memory; at `MMU_ARCHIVE_THRESH=20` the memory flips to Blue. Roughly 45
recall calls were made during Phase 9 validation, which archived ~61 memories:

```
                 session start        after validation       after restore
Green                   73                     15                     90
Blue (dormant)          14                     75                      0
Yellow                   4                      1                      1
Red                      2                      2                      2
```

This mattered because Blue is excluded from SIMILAR_TO/CO_RECALLED expansion **and**
from Phase 9 semantic search — 75 of 93 memories were dormant to the layer this phase
exists to provide.

**Remediated at the user's instruction:** all Blue memories reset to Green with
`use=0, arc=0`, in both Neo4j and the v2 index, with valence (`~VAL`) preserved. The
original 14 legitimately-Blue memories were un-archived too; they cannot be
distinguished, because **no `archived_at` timestamp is recorded** — `write_color_update()`
sets only `color`, despite its docstring claiming it "marks the node as archived".
Restore verified:
```
Neo4j    : Green 90, Red 2, Yellow 1, Blue 0
v2 index : Green 90, Red 2, Yellow 1   total 93   (exact match)
embeddings: 93/93 survived the address rewrite
```
Post-restore recall returns full 5/5 result sets and `via=similar` expansion works
again on memories that were dormant while Blue.

**Two things worth knowing going forward:**
1. **Aging is driven by recall count, not elapsed time.** Twenty recalls archive a
   memory whether they happen over three months or twenty minutes. Any burst of
   testing ages the whole graph. If validation-heavy work continues, consider raising
   `MMU_ARCHIVE_THRESH` during test runs, or a time-based decay term in a future phase.
2. **`archived_at` is not recorded.** Adding it would make an incident like this
   precisely reversible instead of approximately reversible. One property on the Blue
   branch of `write_color_update()`.

### 7.5 Pre-existing, not fixed: arbitrary tie-breaking in `rank()`
All `direct` hits score exactly `W_DIRECT = 1.00`. When a prompt produces more direct
hits than `top_k`, which tied rows survive is arbitrary and varies between otherwise
identical calls:

```
Two consecutive runs, identical config:
  CHURN  Dimensional Relativity Theory physics paper  run1-only=['054'] run2-only=['039']
  CHURN  Castlevania Symphony of the Night video game run1-only=['020'] run2-only=['022']

$ curl ... '{"prompt":"Dimensional Relativity Theory physics paper","top_k":20}'
rows tied at score=1.0: 17   (top_k=5 keeps only 5 of these, arbitrarily)
```

`rank()` sorts by `(-score, priority)`; among rows tied on both, Python's stable sort
preserves input order, which comes from iterating `direct` — a **set** whose iteration
order shifts as `_age_memories()` rewrites addresses.

Phase 9 supplies the missing tiebreaker (`semantic_score`). Options when this is worth
addressing: raise `MMU_SEMANTIC_FLOOR` above 0 (implemented, calibrated at 0.75), or
add `address` as a final tiebreak key in `rank()` to remove the nondeterminism
independent of any semantic signal.

### 7.6 Pre-existing, not fixed: CON numbers are not unique
`add_memory()` derives `con` from `n4j.get_memory_count() + 1`, which collides after
deletions. Two distinct Blue memories both carried `con=033` and mapped to the same
restored address; the restore script detected the collision against the `memory_addr`
UNIQUE constraint and gave one a non-zero `use` instead. Worth knowing before Phase 11
writes hundreds of chunks through the same primitive.

### 7.7 Housekeeping
All validation and gap-fix probe memories were deleted, and the phantom index entries
they exposed were purged. Final state: **93 memories, 93 embedded, 0 missing**,
`MMU_SEMANTIC_FLOOR=0.0`.

### 7.8 Model swap procedure
Changing `MMU_EMBEDDING_MODEL` requires all three of:
1. update `MMU_EMBEDDING_DIM` to the new model's real output length (measure it),
2. `DROP INDEX memory_embedding`, then restart so `bootstrap_schema()` recreates it at
   the new dimension,
3. re-run `POST /backfill_embeddings`.

Vectors from different models are not comparable, so a partial swap silently degrades
recall rather than failing loudly. The dimension guard catches a mismatched *length*
but cannot catch a same-dimension model swap.

## 8. Handoff notes for Phase 11 (knowledge seeding)

**The integration gap is closed.** Embedding now happens inside `add_memory()`
(§7.1), which is the primitive Phase 11's design doc instructs `/ingest` to call once
per chunk. Ingested chunks will therefore be embedded automatically, with no extra
work and no dependency on remembering to run the backfill.

If a large document makes per-chunk embedding latency undesirable, `add_memory()`
accepts `embed=False`; `POST /backfill_embeddings` then picks up the whole batch
afterwards. Measured cost either way is ~21ms per memory, so a 300-chunk paper is
roughly 6 seconds of embedding — not a reason to reach for the batch path by default.

**Useful for Phase 11 validation:**
- `GET /embedding_status` reports `memories_total` / `memories_embedded` /
  `memories_missing` — a fast way to confirm an ingestion run left nothing unembedded.
- The paraphrase test its acceptance checklist calls for is exactly the
  `"who am I married to"` pattern validated in §5.1, and now has working machinery.

**Watch out for these when ingesting at volume:**
- `add_memory()` does a `get_memory_count()` round trip **and** `bake_similar()` **and**
  a `v2_index.save()` on every call. Fine for conversational saves; worth measuring
  across a few hundred chunks in one request.
- CON numbers are not unique (§7.6), and bulk writes will produce more collisions.
- Every `/recall` during validation ages the whole graph (§7.4). Keep test recalls
  bounded, or raise `MMU_ARCHIVE_THRESH` for the duration.

**Scope confirmed with the user for Phase 11:**
- GRP assignment stays model-selected with code guidance, matching how it already
  works today. No auto-classifier in `mmu_server.py`.
- **Voice transcription: deferred.** Not part of Phase 11's build.
- **Image ingestion: deferred.** The user wants to plan image creation and storage
  separately so it does not slow down memory.
- Voice and images are both expected to land in a **separate repository** later, not
  in this one.
