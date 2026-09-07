# Phase 9 -- Semantic Enhancement (Embedding Layer)

Status: ready to build. This doc is self-contained: hand the whole file to Claude Code
running on the development machine with the prompt at the very top, and it has everything it needs to
implement, deploy, and validate without going back to the user for the codebase context.

---

## Copy this to Claude Code to start

```
I'm ready to build Phase 9 of the MMU project (semantic embedding layer for recall).
Full design doc is at C:\MMU\phase_packages\phase9_semantic_embeddings.md -- read it
in full before writing any code. It documents exact file/line anchors in the current
codebase, two open design decisions I need you to flag back to me before proceeding
past them (search for "DECISION NEEDED" in the doc), the schema and code changes to
make, and a deploy + validation sequence you should run yourself using your real
terminal and Docker access. Work through the doc's checklist in order. Report back
what you changed, what you verified, and the actual output of each validation step,
not just "done."
```

---

## Goal

Right now `LightIndexV2.gate()` (in `light_index_v2.py`) is pure keyword/stem matching.
If nothing in the prompt matches a stored keyword or stem, `MMUCore._v2_recall()` in
`mmu_server.py` (line ~287: `if not direct and not pinned: return [], ...`) returns
empty immediately -- there is no semantic fallback at all. This means a memory about
"my dog Max" is invisible to a prompt like "tell me about my pet," and it also means
stem collisions currently produce false positives (the project's own example:
"sourdough" and "star wars" can share a 4-char stem and gate-match each other with
nothing to tell them apart).

Phase 9 adds a second, independent signal: cosine similarity between a local embedding
of the prompt and stored embeddings on each Memory node. It does NOT replace the
keyword gate. It runs alongside it: the keyword gate stays the fast O(1) first pass,
and embeddings are used for (a) a semantic fallback when the gate returns nothing or
too little, and (b) reranking/filtering gate hits so stem-collision false positives can
be pushed down or dropped.

## Current state (read this before changing anything)

- `light_index_v2.py`: `LightIndexV2.gate(prompt)` returns `(hits, pinned)` from pure
  keyword/stem lookup. `expand(seed_addrs)` pulls pre-computed neighbors from
  `shortcut_cache`. `rank(direct, pinned, expanded, top_k)` merges and sorts by
  `(-score, priority)`. None of this touches Neo4j; Neo4j is only hit for payload
  hydration and cold-cache refills.
- `mmu_server.py`: `MMUCore._v2_recall()` (~line 274) is the orchestrator:
  `gate()` -> `expand()` -> live `graph_recall` for cold seeds -> `rank()` -> payload
  hydration via `n4j.fetch_payloads(addrs)`. `MMUCore.recall()` (~line 333) wraps this
  with the v2/graph fallback and CO_RECALLED bookkeeping. This is the one place a
  semantic stage needs to be spliced in.
- `neo4j_layer.py`: `write_memory()` (~line 189) is the Memory node upsert, called by
  `add_memory()`. It sets a fixed list of properties on `ON CREATE`. This is where an
  `embedding` property would get set.
- Neo4j is 5.18-community (`docker-compose.yml`). Native vector indexes
  (`CREATE VECTOR INDEX`, `db.index.vector.queryNodes`) are supported in Community
  edition from 5.11 onward, so this does not require an external vector database or an
  Enterprise license. Confirm this against the actual running instance as your first
  validation step below -- don't take my word for it, verify the version and that
  `CREATE VECTOR INDEX` succeeds.
- The design philosophy section of the handoff doc states plainly: "This server NEVER
  calls an LLM... mmu_idle_daemon.py is the only component that talks to a model." See
  DECISION NEEDED #1 below -- Phase 9 puts real pressure on that boundary and I want it
  resolved deliberately, not by accident.
- `SOURCE_LABELS` in `mmu_server.py` (~line 78) already has `2: "Document"` reserved,
  useful context for Phase 11 but not used here.
- GRP taxonomy, address schema, and the rest of the architecture are documented in
  `C:\MMU\MMU_Handoff_v13.md` if you need broader context.

## DECISION NEEDED #1: who calls the embedding model, and when

The project has kept a hard line so far: `mmu_server.py` never talks to a model,
`mmu_idle_daemon.py` is the only process that does, specifically so the REST server
stays model-agnostic and debuggable in isolated steps. Embeddings are not generative
and carry none of the framing/bias risk that line was drawn to contain, but they are
still a call out to a local model server, and `/recall` and `/remember` are
synchronous REST endpoints served by `mmu_server.py` -- a query-time embedding for a
brand-new prompt cannot be deferred to the idle daemon, because there is no daemon
in the loop for a live `/recall` call.

Two honest options, pick one before writing code:

- **Option A (recommended): narrow, explicit exception.** Give `mmu_server.py` a small
  `EmbeddingClient` (mirrors `LMStudioClient` in `mmu_idle_daemon.py` but calls only
  `/v1/embeddings`, never `/chat/completions`). Document in the module docstring that
  this is a deliberate, scoped exception: vector math, not cognition. Both `/remember`
  (embed the new memory's payload at write time) and `/recall` (embed the incoming
  prompt at read time) call it directly, synchronously.
- **Option B: keep the boundary literal.** `mmu_server.py` never calls any model, full
  stop. Memory embeddings are computed lazily by the idle daemon during a pass (a new
  small idle task: "find memories with no embedding, embed them, write them back").
  Query-time embedding for `/recall` still has to happen somewhere synchronous, so
  under this option `/recall` cannot do semantic fallback for a live query at all --
  it could only ever compare a query against pre-embedded memories using a
  pre-computed prompt cache, which does not generalize to arbitrary new prompts.
  This option effectively can't deliver the stated Phase 9 goal for live recall, only
  for a slower asynchronous enrichment. Flag this tradeoff to the user explicitly if he
  wants Option B anyway.

If the user doesn't answer before you'd otherwise be blocked, default to Option A and say
so plainly in your report, since Option B doesn't actually deliver working semantic
recall for live queries.

## DECISION NEEDED #2: embedding model choice and dimension

The roadmap note says `MMU_EMBEDDING_MODEL=nomic-embed-text`, but that's a name, not a
verified fact about what's loadable in the user's LM Studio right now. Before writing the
Neo4j vector index (which needs a fixed dimension at creation time), do this:

1. Check what embedding models are already downloaded/loadable in the user's LM Studio
   (ask him, or check via LM Studio's model list if reachable).
2. Load one and hit `/v1/embeddings` directly with a test string to see the actual
   vector length returned.
3. Use that real dimension in `vector.dimensions` when creating the index. Do not
   hardcode 768 on the assumption that it's nomic-embed-text's output size without
   checking, different embedding models vary (some are 384, some 1024+).

Make the model name AND the dimension both environment variables
(`MMU_EMBEDDING_MODEL`, `MMU_EMBEDDING_DIM`) so a future model swap doesn't require a
code change, just a re-index.

## Implementation plan

### 1. Neo4j schema

Add a vector index on `Memory.embedding`:

```cypher
CREATE VECTOR INDEX memory_embedding IF NOT EXISTS
FOR (m:Memory) ON (m.embedding)
OPTIONS { indexConfig: {
  `vector.dimensions`: $dim,
  `vector.similarity_function`: 'cosine'
}}
```

Add this to `bootstrap_schema()` in `neo4j_layer.py` (find it, it's the existing
schema-setup function called from `mmu_server.py`'s startup event) so it's created
idempotently on every server start, same pattern as any existing indexes there.

### 2. `neo4j_layer.py` additions

- `write_embedding(address, vector)`: `MATCH (m:Memory {address: $addr}) SET
  m.embedding = $vector`. Called after a memory is created, and by a backfill script
  for pre-Phase-9 memories that have no embedding yet.
- `semantic_search(query_vector, top_k=10, exclude_blue=True)`: wraps
  `db.index.vector.queryNodes('memory_embedding', $k, $vector) YIELD node, score`,
  filtering out Blue-colored nodes the same way `expand()` already excludes Blue from
  CO_RECALLED expansion (dormant memories stay dormant through semantic search too,
  for consistency with the existing design). Returns `[{address, score}]`.
- `count_unembedded_memories()` and a paged fetch of addresses+payloads still missing
  `embedding`, for the backfill script.

### 3. `mmu_server.py` additions

- `EmbeddingClient` class (see DECISION NEEDED #1): `.available()`, `.embed(text) ->
  list[float]`, mirroring `LMStudioClient`'s shape in `mmu_idle_daemon.py` for
  consistency, but talking to `/v1/embeddings` only.
- In `MemoryIn` handling inside `/remember`: after `add_memory()` succeeds, embed
  `mem.payload` and call `n4j.write_embedding(addr, vector)`. Wrap in try/except --
  a failed embedding call should degrade to "memory saved without embedding yet," not
  fail the whole save. Log a warning, don't raise.
- In `MMUCore._v2_recall()`: after the existing keyword-gate path produces `results`
  (or an empty list), add a semantic stage:
  - If `direct` and `pinned` are both empty (today's early-return case), embed the
    prompt and call `n4j.semantic_search()` for a fallback result set instead of
    returning empty.
  - If the gate DID produce hits, also run semantic search and use similarity to
    down-rank or drop gate hits whose embedding similarity to the prompt is very low
    (candidate mechanism for the stem-collision problem) -- tune a threshold, don't
    hardcode a guess; make it `MMU_SEMANTIC_FLOOR` (env var, default something
    permissive like 0.15) so it can be tuned after watching real behavior.
  - Tag results that came from the semantic path with `"via": "semantic"` in the
    response, same pattern the existing code already uses for `"direct"`,
    `"pinned"`, `"similar"`, `"corecall-live"`. This isn't cosmetic: The user and Nova
    both currently reason about *why* a memory surfaced, and Phase 6.6's whole
    design principle was "make Nova's internal state visible, don't hide it" --
    silently blending semantic hits into the same bucket as keyword hits would
    quietly erode that.
- New `POST /backfill_embeddings` endpoint (or a standalone script, your call) that
  pages through `count_unembedded_memories()` and embeds each one -- needed once at
  deploy time for every memory that predates Phase 9, and safe to re-run (idempotent,
  skips anything that already has an embedding).

### 4. Config

New env vars, follow the existing naming convention (`MMU_*`):
`MMU_EMBEDDING_MODEL`, `MMU_EMBEDDING_DIM`, `MMU_SEMANTIC_FLOOR`,
`MMU_EMBEDDING_BASE` (default same LM Studio base URL, override if the user ever runs the
embedding model on a different port/instance than the chat model).

## Deploy sequence (run these yourself, you have real terminal access)

1. Confirm Neo4j version and vector index support:
   `docker exec mmu-neo4j cypher-shell -u neo4j -p mmupassword "CALL dbms.components() YIELD versions RETURN versions"`
2. Load an embedding model in LM Studio (ask the user which one, or check what's already
   downloaded) and confirm `/v1/embeddings` responds:
   `curl http://localhost:1234/v1/embeddings -d '{"input":"test","model":"<name>"}'`
   and record the actual vector length from the response.
3. Set `MMU_EMBEDDING_DIM` to that real length before creating the index.
4. Standard rebuild-and-verify sequence (same lesson as every phase so far --
   `docker compose build` alone can leave stale code running):
   ```
   docker compose build --no-cache mmu-server
   docker compose up -d --force-recreate mmu-server
   docker exec mmu-memory-server grep -n "semantic_search\|memory_embedding" /app/mmu_server.py /app/neo4j_layer.py
   ```
   Confirm the grep actually finds the new code in the running container, not just on
   disk, before declaring the rebuild good.
5. Run the backfill against existing memories and record how many were embedded.
6. Live test: pick a real memory in the graph, then call `/recall` with a prompt that
   shares NO keywords or stems with that memory but is semantically related (e.g. if a
   memory says "I have a golden retriever named Max," recall with "tell me about my
   pet"). Confirm it now surfaces via `"via": "semantic"` where before Phase 9 it would
   have returned nothing.
7. Regression check: re-run a handful of existing keyword-based recalls the user already
   knows the right answer for (any memory address he can name from memory) and confirm
   they still surface via `"via": "direct"` unchanged -- Phase 9 must not regress the
   keyword path.

## Acceptance checklist

- [ ] Neo4j version confirmed to support `CREATE VECTOR INDEX` (not assumed)
- [ ] Real embedding dimension confirmed against the running LM Studio instance (not
      hardcoded from the roadmap doc's example model name)
- [ ] DECISION NEEDED #1 answered and stated plainly in the deploy report
- [ ] `/remember` embeds new memories at write time, degrading gracefully if the
      embedding call fails
- [ ] Backfill run against all pre-Phase-9 memories, count reported
- [ ] `/recall` returns semantic-fallback results when the keyword gate finds nothing
- [ ] `/recall` still returns identical results for existing keyword-only test cases
      (no regression)
- [ ] Every result in `/recall`'s response is tagged with an accurate `"via"` value
- [ ] `--no-cache` rebuild + running-container grep verification actually performed,
      output included in the report, not just described
