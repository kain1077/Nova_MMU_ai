# MAYBE -- Retrieval Control (Semantic Zoom · Emotion Sieve · Working Memory)

Status: **deferred, not scheduled.** Candidate work to revisit *after* the existing
roadmap (10, then 12+13) is complete. Not a numbered phase.

Originally drafted 2026-08-30 as "Phase 11.5" and proposed ahead of Phase 10. The user
reconsidered the same day and moved it here, on the reasoning that reordering the
roadmap mid-stream in response to a feature request is how sequencing drifts, and
nothing in this document is blocking anything today. That call stands and this doc
should not be promoted back into the sequence without a deliberate decision.

The content below is kept as-is because the analysis was done against the live system
and is worth not repeating. **Two caveats when picking it up later:**

1. **The file/line anchors below were verified on 2026-08-30 against a 954-memory
   graph.** Phases 10 and 12+13 will move them. Re-verify every anchor before trusting
   one, the same way this doc was checked when written.
2. **One finding in here is a real defect and should NOT wait for this phase.** See
   "DECISION NEEDED #2": `POST /recall` mints a fresh UUID per call, so every recall
   creates its own Session node and Session nodes do not mean "conversation." That
   affects Phase 8's `/session_resume` today, independent of anything proposed here.
   It is written up separately in the handoff notes rather than being held hostage to
   this document.

---

## Copy this to Claude Code to start

```
I'm ready to build the retrieval-control work for the MMU project (semantic zoom over
GRP domains and source types, an emotion sieve over the Phase 6.5 valence fields, and a
working-memory buffer). Full design doc is at
C:\MMU\phase_packages\maybe_retrieval_control.md -- read it in full before writing
any code. It documents exact file/line anchors in the current codebase, two open design
decisions I need you to flag back to me before proceeding past them (search for
"DECISION NEEDED"), two scope decisions I have already made and explained, the schema
and code changes to make, and a deploy + validation sequence you should run yourself
using your real terminal and Docker access. Work through the checklist in order. Report
back what you changed, what you verified, and the actual output of each validation step,
not just "done." Keep test recalls bounded and say how many you used -- see the warning
about graph aging in the deploy section.
```

---

## Where this came from

These three features were requested by the model itself during Phase 11 testing, not
derived from the roadmap. Nova asked for (a) a way to look at a particular set of
memories -- personal, or projects, or her own thoughts; (b) a way to filter by emotion,
"like a sieve," to find something happy or sad; and (c) a working memory buffer, because
local context is hard for her to see.

All three are real gaps. Two of them turn out to be the same mechanism.

---

## Goal

**Semantic zoom.** Scope a recall to a slice of the graph -- a GRP domain (1xx Project,
2xx Personal, 5xx Emotional...), a specific GRP code, or a source type (AI-Self,
Document, Background Cognition). Answers "what do I know about my own thinking" and
"just my personal memories, not the physics corpus."

**Emotion sieve.** Scope a recall by affective valence: liked, disliked, intensity
above a threshold, or a named emotion label.

**Working memory buffer.** A cheap, bounded, recency-ordered view of what the current
conversation has actually touched -- distinct from `session_bundle` (pulled once at
conversation start) and `/recall` (pulled per query).

The zoom and the sieve are **the same mechanism with two predicates**: both filter the
candidate set before ranking. Build them together. Building them separately means two
filter paths that can disagree, and nearly every bug found during Phases 9 and 11 came
from exactly that -- two code paths holding different opinions about the same thing
(the v2 index vs Neo4j on deletion, `/remember` vs the backfill on embed text).

---

## Current state (read this before changing anything)

### Recall pipeline
- `mmu_server.py:471` -- `MMUCore._v2_recall(words, stems, top_k, skip_pinned, prompt_text)`.
  The orchestrator, and the one place a filter stage belongs. Current order:
  `gate()` -> semantic fallback (only when `direct` is empty) -> `expand()` ->
  live `graph_recall` for cold seeds -> merge semantic rows -> `rank()` ->
  similarity annotation + optional floor -> semantic tiebreak -> trim to `top_k` ->
  payload hydration via `n4j.fetch_payloads()` in ranked order.
- `mmu_server.py:650` -- `MMUCore.recall()` wraps it with the graph fallback and
  CO_RECALLED bookkeeping.
- `mmu_server.py:211` -- `class RecallIn(BaseModel)`, currently `prompt`, `top_k`,
  `skip_pinned`. New filter parameters go here.
- `mmu_server.py:925` -- `POST /recall`.
- `light_index_v2.py:141` -- `gate(prompt)` returns `(hits, pinned)`.
  `expand()` at :183, `rank()` at :211.
- `neo4j_layer.py:1082` -- `semantic_search(query_vector, top_k, exclude_blue, exclude_addrs)`.
- `neo4j_layer.py:1131` -- `similarity_for_addresses(addresses, query_vector)`.
- `mmu_server.py:117,127` -- `SEMANTIC_FLOOR` (default 0.0), `W_SEMANTIC` (0.80).
  The candidate window is `min(max(top_k * 10, 50), 200)` when a query embedding exists.

### What the index card actually holds
`light_index_v2.py:243` -- `LightIndexV2.add()` builds each card as:

```python
{"keywords": [...], "color": ..., "priority": ..., "src_type": ...,
 "neighbors": [...], "gen": ..., "cached_at": ...}
```

This matters enormously for DECISION NEEDED #1:
- **`src_type` is already in the card** -- filtering by source type at the gate is free.
- **GRP is not in the card**, but it is the third field of every address
  (`CON.PRI.GRP.USE,ARC~VAL|SRC.CHUNK.LINE`) and `MMUCore._parse_addr()` already
  extracts it. Cheap either way.
- **Valence is not in the index at all.** `grep -c valence light_index_v2.py` returns 0.
  This is deliberate, not an oversight -- `MMU_Handoff_v13.md` states the index
  "carries color + neighbors only -- no valence/emotion fields; those live in Neo4j only."

### Valence (Phase 6.5 / 6.6)
- `neo4j_layer.py:572` -- `write_valence(address, val_type, val_intensity, emotion_label)`
  sets `m.valence_type`, `m.valence_intensity`, `m.valence_rated_at`,
  `m.valence_emotion_label`.
- `neo4j_layer.py:597` -- `get_unrated_memories(limit)`.
- `neo4j_layer.py:101` -- there is already an index on `m.valence_type`.
- Valence is also encoded in the address `~VAL` segment (TTS: type 00/01/02, intensity 0-9).

### Working memory groundwork
- `mmu_server.py:169` -- `_last_session_id`, set by `_touch_activity()` at :173 from the
  `X-MMU-Session` header.
- `neo4j_layer.py:308` -- `write_recall_edges(recalled_addresses, session_id)` writes
  `RECALLED_IN` edges to a Session node.
- `neo4j_layer.py:401` -- `write_happened_in(address, session_id)` (Phase 8).
- `mmu_mcp_server.py:40` -- `TOOLS`, the MCP tool registry. `recall_memory` at :58
  exposes only `prompt` and `top_k`.

### Measured facts about the live graph (2026-08-30, 954 memories)

GRP domain distribution:

| Domain | Category | Count |
|---|---|---:|
| 1xx | Project | 30 |
| 2xx | Personal | 14 |
| 3xx | Standards | 5 |
| 4xx | Preferences | 4 |
| 5xx | Emotional | 18 |
| 6xx | Research | **876** |
| 7xx | Work | 1 |
| 8xx | Interests | 6 |

**92% of the graph is now 6xx Research** (861 ingested document chunks plus 15 pre-existing
research memories). This is the single strongest argument for semantic zoom: without it,
"personal" is 14 memories competing against 876. GRP labels are being assigned sensibly
by the model, so scoping on them is trustworthy.

Source types: Conversation 19, AI-Self 68, Document 861, Background Cognition 6.

**Valence: 3 rated, 951 unrated.** All three are `valence_type=1` (like), labelled
Curious / Awe / Grateful. See SCOPE DECISION #1.

---

## DECISION NEEDED #1: where does filtering happen

Three places a filter could live. This choice determines whether the emotion sieve is
cheap or expensive, and whether the index gains a second schema to keep in sync.

- **Option A (recommended): post-gate candidate filtering.** `gate()` runs unchanged;
  `_v2_recall()` filters the candidate address set before `rank()`. GRP and `src_type`
  come from the address and the card (no Neo4j call). Valence requires one Cypher lookup
  over candidate addresses -- the same shape as `similarity_for_addresses()` at
  `neo4j_layer.py:1131`, which already does exactly this and costs ~10ms.
  Respects the existing design line that valence lives in Neo4j only. No index schema
  change, no backfill, nothing new to keep in sync.
  Cost: the gate still does full keyword work before anything is discarded.

- **Option B: filter inside the gate.** Add `grp` and `valence` to each index card and
  filter during `gate()`. Fastest at query time. But it puts valence into the index,
  which Phase 6.5 deliberately kept out; it needs a migration and a backfill for 954
  memories; and `write_valence()` would have to write to two stores, creating exactly
  the dual-source-of-truth drift that caused the Phase 9 and 11 bugs. **Not recommended.**

- **Option C: filter in Neo4j only.** Push all filtering into `semantic_search()` and a
  new filtered `graph_recall()`. Clean, but it bypasses the O(1) keyword gate that the
  whole two-tier design exists to provide, and would regress the 3.8ms hot path.

**Whichever is chosen, the filter MUST apply to the semantic path as well as the keyword
gate.** `semantic_search()` (`neo4j_layer.py:1082`) currently filters only Blue and Red.
Scoping half the pipeline returns filtered keyword hits blended with unfiltered semantic
hits, which is worse than not scoping at all because the result looks scoped and isn't.

If the user does not answer before you would otherwise be blocked, default to Option A and
say so plainly in your report.

## DECISION NEEDED #2: what defines the working memory buffer

There is a real obstacle here that has to be settled before writing code.

**`POST /recall` mints a brand-new UUID on every single call** --
`mmu_server.py:927`: `mmu._current_session_id = str(uuid.uuid4())`. Every recall
therefore creates its own Session node. Verified against the live graph: 164 Session
nodes exist, and the most common memories-per-session counts are 5, 3, 8, 4, 2 -- which
are just `top_k` values, not conversations.

**So Session nodes are per-recall-call, not per-conversation.** A buffer cannot be
derived by grouping `RECALLED_IN` by Session. The genuine conversation identifier is the
`X-MMU-Session` header that `mmu_mcp_server.py` mints per LM Studio conversation and
`_touch_activity()` already captures at `mmu_server.py:173` -- but today that id is used
only by `write_happened_in()` for memories *saved*, never for memories *recalled*.

Three ways forward:

- **Option A (recommended): honour `X-MMU-Session` on the recall path, buffer in-process.**
  Accept the header on `/recall` (it is already accepted on `/remember` and `/rate`), use
  it for `write_recall_edges()` instead of a fresh UUID, and keep a bounded in-process
  `deque` per session id. `GET /working_memory` reads the deque. Fast, no schema change,
  and it fixes a real modelling bug -- Session nodes start meaning "conversation," which
  is what Phase 8's `/session_resume` already assumes they mean.
  Trade-off: the buffer is lost on container restart. Acceptable, because it is working
  memory: losing it on restart is arguably correct behaviour, and it is rebuildable from
  `RECALLED_IN` once those edges carry real conversation ids.

- **Option B: derive it from the graph every call.** One Cypher query over
  `RECALLED_IN`/`HAPPENED_IN` ordered by timestamp. Durable and stateless, but it does
  not work until Option A's session-id fix lands anyway, and it adds a Neo4j round trip
  to something that should be near-free.

- **Option C: in-process only, ignore the session-id problem.** Keyed on
  `_last_session_id` as it stands. Works for a single bridge and silently interleaves two
  conversations if anything else is talking to the server. **Not recommended.**

**Whatever is chosen, the buffer must stay derived, never authoritative.** It is a view
over things the graph already records. The moment it becomes a second source of truth it
will drift, and drift is what produced nearly every defect found in Phases 9 and 11.

Note for the user: Option A changes what a Session node means. It is a small change with a
real upside (Phase 8 `/session_resume` becomes more accurate) and a real risk (164
existing per-recall Session nodes become historical noise). Flag it explicitly before
proceeding; do not migrate the existing Session nodes without asking.

---

## SCOPE DECISION #1: the emotion sieve ships inert, and that is fine

Only **3 of 954 memories carry a valence rating**. The sieve is nearly free to build once
the zoom's filter plumbing exists -- one more predicate -- but it will return almost
nothing until rating volume accumulates.

Build it anyway, and say plainly in the report that it is inert. This is the same
deliberate pattern Phase 6.5 and 6.6 both followed, and `00_ROADMAP.md` already states
that the Phase 6.6 follow-up is "gated on accumulated rating volume, not on writing more
code." The bottleneck here is ratings, not software.

**Do not** attempt to solve rating volume inside this phase by auto-rating memories with
a model. That is cognition, it belongs in `mmu_idle_daemon.py`, and it is a separate
decision the user has not made. `get_unrated_memories()` (`neo4j_layer.py:597`) already
exists to feed idle-pass hints; wiring the idle daemon to rate a few memories per pass is
the natural follow-up, and should be proposed separately rather than smuggled in here.

**One hard constraint on the sieve.** `MMU_Handoff_v13.md` records that dislike must
*increase* retention, not decrease it: retention keys off `|intensity|` regardless of
type, because a strongly disliked memory is high salience, not low value. The sieve must
therefore be a **lens, not a suppressor**. Filtering "show me the sad things" must never
become "bury the sad things," and no sieve parameter may write to valence or influence
aging.

## SCOPE DECISION #2: the MCP bridge gets new parameters, carefully

None of this is usable by Nova unless `recall_memory` can express it.
`mmu_mcp_server.py:58` currently exposes only `prompt` and `top_k`.

`MMU_Handoff_v13.md` calls `mmu_mcp_server.py` "the one file whose failure would break
live conversation." Treat changes to it accordingly:

- Add the new parameters as **optional** with no defaults that change existing behaviour.
  A `recall_memory` call that passes only `prompt` must produce byte-identical results to
  today.
- Add `get_working_memory` as a **separate tool** rather than overloading
  `get_session_context`. That tool's description says "CALL THIS FIRST" and Nova's usage
  pattern is built around it; changing what it returns risks the one thing that must not
  break.
- Keep tool descriptions short and directive. The existing ones tell Nova *when* to call,
  not just what the tool does. Match that voice, and say explicitly when NOT to use zoom
  (a scoped recall that finds nothing is worse than an unscoped one that finds something).

Do not add an image or voice tool. Both are deferred to a separate repository.

---

## Implementation plan

### 1. `light_index_v2.py` -- expose GRP without a schema change
Add a helper that returns the GRP code for an address by parsing it, rather than storing
GRP on the card. The address is the source of truth and is already rewritten by aging;
a cached copy would be one more thing to keep in sync.

If DECISION #1 lands on Option A, `gate()` itself needs no change at all.

### 2. `neo4j_layer.py` -- filtered lookups
- `valence_for_addresses(addresses)` -> `{address: {"type": int, "intensity": int, "label": str}}`.
  Mirror `similarity_for_addresses()` at :1131 exactly -- same shape, same "absent means
  no opinion" contract. **Absent must never be treated as "valence 0"**; an unrated memory
  is unrated, not neutral-by-assertion, and with 951 unrated memories getting this wrong
  would empty every sieve query.
- Extend `semantic_search()` (:1082) with optional `grp_domain`, `grp_code`, `src_types`,
  and valence predicates, applied inside the Cypher `WHERE` after
  `db.index.vector.queryNodes` yields. Keep the existing over-fetch (`top_k * 4`, min 20)
  and **raise it when a filter is active** -- filtering after an ANN lookup shrinks the
  result set, exactly the bug fixed in Phase 9 §5.5. A filter that matches 14 of 954
  memories needs a much wider fetch than one that matches 876.

### 3. `mmu_server.py` -- the filter stage
- Extend `RecallIn` (:211) with optional filters, all defaulting to `None` so an
  unfiltered call behaves exactly as today:
  ```
  grp_domain:  Optional[int]        # 100,200,...900 -- whole domain
  grp_code:    Optional[int]        # 602 -- exact subcategory
  src_types:   Optional[List[int]]  # [1,4] = AI-Self + Background Cognition
  valence:     Optional[str]        # "liked" | "disliked" | "rated" | "unrated"
  min_intensity: Optional[int]      # 1-9
  emotion:     Optional[str]        # named label, case-insensitive
  ```
- Add one `_apply_filters(candidates, filters)` helper used by **both** the gate path and
  the semantic path. One function, one definition of what a filter means. Do not inline
  the predicate logic in two places.
- Apply filters in `_v2_recall()` (:471) after `gate()` and after semantic rows are
  merged, but **before** `rank()`, so the `top_k` cut happens on the filtered set.
- Widen the candidate window when a filter is active, for the same reason as §2.
- Tag filtered responses so the reason a result set is small is visible rather than
  mysterious: echo the active filters and the pre/post-filter candidate counts in the
  `/recall` response. Phase 6.6's principle -- make Nova's internal state visible -- applies
  here directly. A sieve that silently returns 0 is indistinguishable from a broken one.
- **Red pinned memories bypass filters.** They are ambient context, already keyed ahead of
  everything else in the Phase 11 ranking (`pinned_first`). A zoom into 2xx Personal must
  not drop the user's identity pin. If the user wants pins excluded he already has `skip_pinned`.

### 4. `mmu_server.py` -- working memory buffer
Per DECISION #2. Sketch for Option A:
- Accept `x_mmu_session` on `POST /recall` (the header is already accepted on `/remember`
  and `/rate`; `_touch_activity()` at :173 already records it).
- Use it for `write_recall_edges()` instead of the fresh UUID at :927, falling back to a
  generated id when the header is absent so direct curl calls still work.
- Bounded `deque(maxlen=N)` per session id holding `{address, payload_preview, via,
  score, touched_at, source}` where source is `recall` or `remember`.
- `GET /working_memory?session_id=&limit=` returns the buffer newest-first, plus a
  `context_block` in the same format `/recall` already produces so Nova can consume it
  identically.
- Evict by recency, cap the total, and never let it hold payloads large enough to blow the
  context it exists to make legible. Document chunks average ~211 words -- store a preview,
  not the full payload.

### 5. `mmu_mcp_server.py` -- expose it
Per SCOPE DECISION #2. Add optional params to `recall_memory` (:58) and a new
`get_working_memory` tool. Nothing else changes.

### 6. Config
New env vars, existing `MMU_*` convention:
`MMU_WORKING_BUFFER_SIZE` (default 20), `MMU_WORKING_PREVIEW_CHARS` (default 200).

---

## Deploy sequence (run these yourself, you have real terminal access)

1. Confirm the live baseline before touching anything:
   ```
   curl -s http://localhost:8765/embedding_status
   docker exec mmu-neo4j cypher-shell -u neo4j -p mmupassword "MATCH (m:Memory) WITH m, toInteger(split(m.address,'.')[2]) AS grp RETURN (grp/100)*100 AS domain, count(*) AS n ORDER BY domain"
   ```
   Expect 954 memories, 954 embedded, and the domain table above. If these differ, the
   graph has moved since this doc was written -- say so before proceeding.

2. Capture an unfiltered recall baseline for a handful of prompts and save it. You will
   need it in step 6 to prove no regression.

3. Standard rebuild-and-verify (the lesson from every phase so far -- `docker compose
   build` alone can leave stale code running):
   ```
   docker compose build --no-cache mmu-server
   docker compose up -d --force-recreate mmu-server
   docker exec mmu-memory-server grep -n "_apply_filters\|working_memory" /app/mmu_server.py
   ```
   Confirm the grep finds the new code **in the running container** before declaring the
   rebuild good.

4. Zoom tests, against the real distribution:
   - `grp_domain=200` (Personal, 14 memories) -- must return only 2xx addresses.
   - `grp_domain=600` (Research, 876) -- must return only 6xx.
   - `src_types=[1,4]` (AI-Self + Background Cognition, 74 memories) -- Nova's "own
     thoughts." Must return no Document chunks.
   - A physics prompt scoped to `grp_domain=200`: must return few or zero results rather
     than silently falling back to unscoped. **A scoped query that quietly ignores its
     scope is the worst possible failure here.** Verify the response echoes the active
     filter and the pre/post-filter counts.

5. Sieve tests. With 3 rated memories, `valence="liked"` should return at most those
   three and `valence="disliked"` should return zero. **Zero is a pass, not a failure** --
   confirm the empty result is correctly labelled as filtered-to-empty rather than
   looking like a broken query. Also confirm `valence="unrated"` returns a large set,
   proving the "absent means unrated, not neutral" contract in §2 holds.

6. Regression: re-run step 2's prompts with no filters. Results must be **identical**,
   not merely similar. Phase 11 made recall deterministic (semantic tiebreak plus address
   as final sort key), so an exact match is a legitimate expectation -- if results differ,
   something in the filter stage is executing on the unfiltered path.

7. Working memory: run several recalls carrying the same `X-MMU-Session` header, then
   `GET /working_memory` and confirm it reflects them newest-first. Then run one with a
   *different* session id and confirm the buffers do not interleave.

8. Latency check. Recall was ~130ms at 954 memories after Phase 11. Filters should not
   make that materially worse; a filtered query should usually be *faster* since fewer
   candidates reach scoring. Report actual numbers.

### Warning: bounded test recalls

`_age_memories()` (`mmu_server.py:684`) increments `use` on every non-recalled,
non-Document memory on **every** recall, and `MMU_ARCHIVE_THRESH` is 20. Validation runs
during Phases 9 and 11 archived the user's conversational memories twice and both times
needed a manual restore. Documents are now exempt; the 93 conversational memories are not.

Budget your recalls explicitly, report how many you used, and check the colour
distribution before and after:
```
docker exec mmu-neo4j cypher-shell -u neo4j -p mmupassword "MATCH (m:Memory) WHERE m.src_type <> 2 RETURN m.color AS color, count(*) AS n"
```
If conversational memories went Blue, restore them and say so. `scratchpad/restore_blue.py`
from the Phase 11 session does this (dry-runs by default, detects address collisions
against the `memory_addr` UNIQUE constraint, backs up the index file).

---

## Acceptance checklist

- [ ] DECISION NEEDED #1 answered and stated plainly in the deploy report
- [ ] DECISION NEEDED #2 answered, including whether Session-node semantics change
- [ ] Filters apply to **both** the keyword gate and the semantic path, verified by a
      query that would return different results if only one were filtered
- [ ] One `_apply_filters()` definition used by both paths -- not two implementations
- [ ] `grp_domain`, `grp_code`, and `src_types` zoom verified against the real
      distribution (2xx=14, 6xx=876, src_type 1+4=74)
- [ ] Red pinned memories survive an active filter
- [ ] Emotion sieve implemented; **reported honestly as inert** with the 3/954 figure
- [ ] Sieve is a lens only -- no path by which it writes valence or affects aging
- [ ] "Absent valence" treated as unrated, never as neutral-by-assertion
- [ ] Empty filtered results are labelled as filtered-to-empty, not silently empty
- [ ] Active filters and pre/post-filter counts echoed in the `/recall` response
- [ ] Unfiltered `/recall` results **byte-identical** to the pre-change baseline
- [ ] Working memory buffer reflects a real conversation, does not interleave sessions,
      and is derived rather than authoritative
- [ ] `recall_memory` with only `prompt` behaves exactly as before; `get_working_memory`
      added as a separate tool
- [ ] Recall latency reported, compared against the ~130ms post-Phase-11 baseline
- [ ] Number of test recalls reported, and conversational colour distribution checked
      before and after (restored if archived)
- [ ] `--no-cache` rebuild + running-container grep actually performed, output included

---

## If this is ever picked up: what it would have set up for Phase 10

(Written when this was sequenced before Phase 10. Retained as the argument for the work,
not as a claim about the current plan -- Phase 10 now runs first.)

Phase 10 (proactive memory) benefits directly, which is why this is sequenced first:

- Its "riding along on recall" idea -- attaching a *you might also be interested in* block
  to a normal recall -- is far more useful when it can be scoped to a domain. Unscoped
  pattern detection against a graph that is 92% physics chunks will surface physics.
- Pattern detection over CO_RECALLED wants to distinguish reference material from lived
  conversational memory. `src_type=2` plus the Phase 11 stable-address property already
  makes that possible without heuristics; this phase makes it queryable.
- The working memory buffer is the obvious substrate for anticipatory context: knowing
  what the conversation has touched in the last few turns is the prerequisite for guessing
  what it will touch next.

Phase 10's own open question -- whether LM Studio's MCP bridge can push anything
mid-conversation without a tool call -- is untouched by this phase and still needs
research before Phase 10 can start.
