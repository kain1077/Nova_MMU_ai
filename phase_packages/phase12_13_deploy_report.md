# Phase 12 + 13 — Crystallization + Skill Tree — DEPLOY REPORT

Status: **deployed and validated** on the development machine, 2026-08-30.
Design doc: `C:\MMU\phase_packages\phase12_13_procedural_memory.md`

**Final state: 954 memories, 954 embedded, 0 skills, 0 memories demoted.**
Crystallization is deployed and correct, and has nothing to crystallize yet. That is the
honest result, not a failure — see §4.

---

## 1. A critical bug found first, unrelated to but blocking this phase

Fixing CON uniqueness (the prerequisite the user approved) uncovered something worse.

`_gen_addr()` formats CON with `{con:03d}` — a **minimum** width. Memory #1000 therefore
produces `1000.005.602...`. But `_parse_addr()` demanded exactly three digits, and because
Phase 8 hardened it to `re.search` (so a pasted display line still parses), it did not
fail. **It matched one character in and returned `con=000`.**

```
con=954   -> parsed 954   OK
con=999   -> parsed 999   OK
con=1000  -> parsed 000   <-- silently wrong
con=1234  -> parsed 234   <-- silently wrong
```

Aging calls `_parse_addr()` then `_gen_addr()`, so any memory past CON 999 would have been
rewritten around a different identity — colliding with an existing node or corrupting
itself, with no error raised anywhere.

**The graph was at 954. The ceiling is 999. The next document ingest would have crossed it.**

Fixed: the CON group is now `(\d{3,})`, backward compatible with every existing address.
Verified through the real server codec, not an isolated regex — CON 1, 954, 999, 1000,
1500 and 9999 all round-trip with GRP, `src_type` and line intact, and the Phase 8
pasted-display-line behaviour still works.

### Related fixes in the same family
- **`substring(n.address, 8, 1)`** in the Phase 10 cross-domain query assumed a fixed
  character offset and breaks the moment CON widens. Now uses `split(address,'.')[2]`,
  matching every other query in the codebase. This was the only fixed-offset assumption;
  all others already used `split`.
- **USE was unbounded.** `new_use = p["use"] + 1` with no cap, and archived memories keep
  incrementing since only Red is skipped. Pre-existing, but the Phase 10 aging redesign
  made it far more reachable: memories now sit in Yellow instead of archiving, so `use`
  climbs longer. At 1000 it hits the same silent-corruption path. Now clamped at
  `ARCHIVE_THRESH` — nothing is lost, since past that point the count half of the archive
  condition is already satisfied, and it stops address churn once a memory settles.
  Verified: 92 of 93 conversational memories sit at ≤20 after further recalls.
- **`src_chunk` / `src_line`** remain 3-digit, per document. Largest paper so far: 220
  chunks, 114 pages, so ~4.5x headroom. They now clamp with a warning rather than silently
  corrupting — losing provenance precision on an enormous document is recoverable, writing
  a malformed address is not.

### CON uniqueness itself
`add_memory()` derived CON from `get_memory_count() + 1`, which collides after any
deletion: delete 5 of 100 and the next write claims 96, already taken. The graph carries
**5 duplicated CON pairs (030–034)** from exactly this. Now uses `get_next_con()` →
`max(existing) + 1`. Verified: the next save took 955.

**Still open:** the 5 existing duplicates. Four are harmless — they differ in another
address field. One (CON 033) has a twin identical in every field, which leaves that memory
frozen at `use=57`: clamping it to 20 would generate its twin's exact address, so the
rename cannot happen. It remains functional and still archives correctly. Repairing it
needs a stop/patch/start cycle like the Blue restore, and was not done unprompted.

**Note on CON permanence:** `max()+1` guarantees uniqueness but not permanence — deleting
the highest-numbered memory frees that number for reuse. That only matters if CON is ever
treated as a stable long-term identifier. A monotonic counter node would give that;
deferred to the final shareable stage.

---

## 2. Decisions

| Decision | Answer |
|---|---|
| GRP codes on Skill nodes (a 0xx Procedural domain)? | **No.** Skills are addressed by `skill_id` uuid, not by the Memory address schema, so a parallel taxonomy would be maintained for a handful of nodes. They are found by `trigger` text and by the Phase 13 tree. The measured coherence data supports this: real dense clusters span domains, so a single GRP per skill would often be false. |
| Fix CON before Phase 12? | **Yes**, done first — and it turned out to matter far more than expected (§1). |

---

## 3. The normalization fix (deploy step 1)

`get_insights()` scored candidates with **raw** `avg_weight`, while `get_idle_context()`
already normalized against the observed max. Raw CO_RECALLED weights are unbounded
integers, so `skill_score` escaped its 0-1 range and the roadmap's "propose at >= 0.70"
threshold was meaningless on that endpoint.

Confirmed live before fixing: **14 of 15 candidates exceeded 1.0**, topping out at 2.5696.

After porting `get_idle_context()`'s formula:
```
/insights     max score  2.5696 -> 0.8043      scores > 1.0:  14 of 15 -> 0
/idle_prompt  max score  0.8043
agreement:    EXACT on all 10 shared candidates
```
The 15-vs-10 count difference is the two queries' `LIMIT` values, not a disagreement.

---

## 4. Crystallization: deployed, correct, nothing to crystallize

### The mutual-density check is its own query, not the preview repurposed
The existing preview queries answer "which single memory has strong neighbours." That is
right for a preview and wrong for deciding to crystallize: a hub with many
weak-to-each-other neighbours would qualify while not being a coherent skill.

`find_skill_candidates()` requires **every pair** in the cluster to clear the normalized
weight floor. Triangles are matched first — the smallest mutually dense cluster — rather
than enumerating combinations, which would explode on 954 nodes.

### Result at the doc's threshold
```
/skill_candidates  (min_cluster=3, min_pairwise_norm=0.6)  ->  0 candidates
```

**Zero, and the threshold was not lowered to manufacture one.**

### Proving the query works rather than being silently broken
A broken query and an honest zero look identical, so the threshold was swept as a
diagnostic only:
```
min_pairwise=0.6 -> 0     min_pairwise=0.3 -> 5
min_pairwise=0.4 -> 5     min_pairwise=0.2 -> 5
```

The nearest miss is informative. A GRP-202 triangle (the user's biography — age, marriage,
career history) has `grp_coherence = 1.0` and averages **0.722 normalized**, above the 0.6
bar. It still fails, correctly. Its three pairwise weights are:

```
1.0     "in their forties, Portland, Star Wars"  <-> "in their forties, married to Sam"
0.833   "in their forties, Portland, Star Wars"  <-> "Videographer, then Photography"
0.333   "married to Sam"                  <-> "Videographer, then Photography"
```

Two of those memories are tightly linked; the third is loosely attached. An average-based
or anchor-based test would have accepted this cluster. The all-pairs test correctly
rejects it — which is exactly the distinction the design doc asked for.

This is the most likely first real candidate as co-recall accumulates.

### `/skill_candidates` writes nothing
Verified directly against Neo4j before and after: **954 memories, 0 Skill nodes, 0
PROCEDURALIZED_FROM edges**, unchanged.

---

## 5. The safety split is structural, not conventional

- `GET /skill_candidates` — reads only. Safe to poll, safe for the idle daemon to look at.
- `POST /crystallize` — the only write path, and **not reachable from
  `mmu_idle_daemon.py`'s `IDLE_TOOLS`**.

`crystallize_skill()` runs in a single `execute_write` transaction: create the Skill, wire
`PROCEDURALIZED_FROM` from every member, demote each member to Blue, clear the proposal
marker. It verifies every member address resolves before writing anything, so a stale
address aborts the whole thing rather than half-applying it. Source memories are **never
deleted** — per the roadmap they are the skill's root system.

Guards verified live:
```
POST /crystallize without confirmed=true  -> 400 "Refusing to crystallize without
                                                  confirmed=true..."
POST /crystallize with 1 member           -> 400 "A skill needs at least 2 source memories"
graph after both attempts                 -> 954 memories, 0 Blue, 0 skills
```

`CrystallizeIn` takes **explicit member addresses rather than a proposal id** on purpose:
aging rewrites addresses, so a cluster id resolved later could name different memories than
the ones a human reviewed.

The endpoint also syncs the v2 index colour after a successful crystallization; without it
the gate would keep treating demoted memories as active until the next cold-cache refill.

---

## 6. Phase 13 — skill tree

Built only after Phase 12 was validated, per the doc's instruction not to ship both in one
undifferentiated pass.

Tested with temporary `Skill` fixtures created directly in Neo4j, so **no real memory was
touched**, then removed.

```
1. tree structure
   [{"skill_id":"test-parent","status":"active",
     "children":[{"skill_id":"test-child","status":"active","children":[]}]}]

2. deprecate PARENT while child active   -> HTTP 409
   "1 active child skill(s) still extend this one: test-child.
    Deprecate or reparent them first."

3. deprecate the CHILD                    -> OK
4. deprecate the parent (child inactive)  -> OK
5. link parent -> child (cycle)           -> HTTP 400 "would create a cycle"
6. link skill to itself                   -> HTTP 400 "a skill cannot extend itself"
```

The guard **rejects loudly**. A silent success would leave a live skill extending a dead
parent — a broken tree nothing downstream would catch.

Cycle rejection is not decorative: `deprecate_skill()` walks children, and
`get_skill_tree()` recurses, so a cycle would make both non-terminating. `link_skills()`
refuses any edge that would close a loop, and the tree builder carries a defensive
`seen` set regardless.

`propose_meta_skill()` reports skill pairs sharing >= 2 source memories. It **proposes
only** — the tree encodes claims about how Nova's abilities relate, which is not a call to
make automatically.

---

## 7. Acceptance checklist

- [x] `skill_score` normalization ported to `get_insights()`, confirmed to match
      `get_idle_context()` exactly on shared candidates
- [x] Mutual-density check implemented as its own query, not repurposed from the
      anchor-node preview
- [x] `/skill_candidates` writes nothing — verified against Neo4j before and after
- [x] `/crystallize` requires explicit confirmation and is NOT wired into `IDLE_TOOLS`
- [ ] Source memories demoted to Blue after a real crystallization — **not exercised, no
      qualifying candidate exists.** The code path is written and transactional but has
      not run against real data; it should be verified the first time a genuine candidate
      is confirmed.
- [x] Honest report on whether real candidates exist — zero, with the threshold sweep
      showing the query works and no manufactured data
- [x] Parent-can't-deprecate-with-active-child tested and confirmed to reject
- [x] `--no-cache` rebuild + running-container verification at both build steps

---

## 8. What is left

**The roadmap's numbered phases are complete** (9, 11, 10, 12+13). Remaining:

1. **`phase_final_shareable_release.md`** — the final stage. Its checklist already carries
   the CON-uniqueness item; §1 above resolves the allocator but leaves the 5 existing
   duplicates and the CON-permanence question.
2. **Verify the crystallization write path** the first time a real candidate appears.
   Everything else in Phase 12 is validated; that one path is written but unexercised.
3. **`maybe_retrieval_control.md`** — deferred, revisit only if wanted.

Backups: `mmu_server.py.pre12.bak`, `neo4j_layer.py.pre12.bak`, plus the `.pre9`/`.pre10`/
`.pre11` sets.
