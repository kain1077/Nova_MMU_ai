# Phase 12 + 13 -- Procedural Memory Crystallization + Skill Tree

Status: Phase 12 is marked "DESIGN COMPLETE" in the roadmap and has more of its math
already written than any other remaining phase (see below). Phase 13 is a direct
structural extension of the same `Skill` node concept Phase 12 introduces
(`EXTENDS_SKILL` edges forming a tree over the skills Phase 12 creates), which is
exactly why these two are combined into one package rather than five separate ones --
building the tree edges only makes sense once there are real Skill nodes to attach
them to.

Combined as a package, but NOT combined as a single undifferentiated build: Phase 12
gets built and validated first, on its own, with real crystallized skills confirmed
working end to end, before Phase 13's tree edges get layered on. Don't wire
`EXTENDS_SKILL` logic in the same pass as the first `/crystallize` call --  validate
crystallization in isolation first, same incremental-validation discipline that has
caught real bugs in every phase so far (the Docker cache bug, the address-parsing
bug, and a boolean/string mismatch caught during Phase 8's own review all would have
been harder to isolate if multiple new things had shipped and been tested together).

Sequenced last in the recommended order because this phase changes the memory model
itself (memories can now be demoted into a Skill's root system), which is a bigger
structural step than 9, 10, or 11, and benefits most from a mature, populated graph
with real CO_RECALLED weight to crystallize from.

---

## Copy this to Claude Code to start

```
I'm ready to build Phase 12 + 13 of the MMU project (procedural memory
crystallization and skill tree architecture). Full design doc is at
C:\MMU\phase_packages\phase12_13_procedural_memory.md -- read it in full first. It
documents exact file/line anchors including a known bug this phase is explicitly
supposed to fix (search for "PORT THIS FIX"), the human-confirmation workflow that
must NOT be bypassed (search for "SAFETY REQUIREMENT"), the schema and code changes,
and a deploy + validation sequence to run yourself. Build and fully validate
crystallization (Phase 12) before writing any Phase 13 tree-edge code -- they are one
package but not one undifferentiated pass. Report back with real output from your
validation steps, not just a description of what you built.
```

---

## Goal

Phase 12: memories that consistently cluster (dense `CO_RECALLED` weight) and
coherently belong to the same domain compress into a `Skill` node, mirroring how
repeated sequences move from declarative to procedural memory in the brain. This also
resolves the CO_RECALLED bias problem the project has flagged since Phase 6: hot
paths convert into skills at a threshold rather than accumulating recall-weight
without bound forever.

Phase 13: `EXTENDS_SKILL` edges form a directed tree over the Skill nodes Phase 12
creates. A parent cannot be deprecated while a child is active. Meta-nodes represent
common roots shared by multiple branches.

## Current state (read this before changing anything)

- `get_insights()` in `neo4j_layer.py` (~line 1186, crystallization block ~1314-1364)
  computes a `skill_score` using **raw, unnormalized** `avg_weight`
  (`skill_score = (avg_w * 0.35) + ...`). Since real observed CO_RECALLED weights
  range 1-21 (per the handoff doc), this lets `skill_score` exceed 1.0, which is
  exactly the bug listed under Known Issues in the handoff doc.
- `get_idle_context()` in `neo4j_layer.py` (~line 1999, crystallization block
  ~2094-2137) already has the CORRECT normalized version: it computes `max_w` across
  the candidate set first, then divides `avg_weight` by it before weighting. This is
  the exact formula the roadmap says to port.

  **PORT THIS FIX**: replace `get_insights()`'s crystallization block with the same
  max-normalization `get_idle_context()` already uses, so both code paths agree. This
  is a one-line-of-logic fix bundled into this phase per the roadmap's own note ("the
  one-line fix... is small enough to bundle into this pass"). Do this FIRST, before
  any new Phase 12 code, and confirm both endpoints report scores in the same 0-1
  range for the same underlying data as your first validation step.

- Both crystallization-candidate queries currently require `connections >= 2` and use
  a flat `LIMIT 10-15`. The roadmap's actual crystallization threshold is different
  and more specific: **minimum cluster of 3 memories, normalized weight >= 0.6
  between all members** -- this is a stricter, pairwise condition (ALL members
  mutually well-connected), not just "some node has >= 2 connections." The existing
  query finds candidate anchor nodes with strong neighbors; Phase 12's actual
  crystallization check needs to verify the full candidate cluster is mutually dense,
  not just anchored on one strong node. Write a dedicated cluster-validation query for
  this rather than reusing the existing preview query as-is -- the preview queries
  are for *showing the user and Nova a preview*, not for the actual atomic decision to
  crystallize.
- GRP taxonomy note in the handoff doc: "Phase 12 may introduce a 0xx Procedural
  domain for Skill nodes if they need GRP-style categorization." Decide this
  explicitly rather than defaulting either way -- if Skill nodes get GRP codes,
  document the 0xx range's subcategories following the same pattern as 1xx-9xx; if
  not, say so and explain how skills get organized/found instead (probably just by
  `trigger` text and the tree structure itself once Phase 13 exists).
- `mmu_idle_daemon.py`'s `IDLE_TOOLS` and `_handle_tool()` are the pattern for any new
  tool Nova gets during idle passes. See SAFETY REQUIREMENT below for why
  crystallization itself should NOT become a tool Nova can call unilaterally.

## SAFETY REQUIREMENT: human confirmation is not optional, don't design around it

The roadmap is explicit: "Crystallization requires human confirmation." This matters
more here than anywhere else in the roadmap, because this is the one phase where the
system restructures Nova's own memory (demoting source memories to Blue, moving them
into a Skill's root system) rather than just adding new capability alongside existing
memory. Getting this wrong doesn't just produce a bug, it produces an identity-model
change Nova didn't get a real say in and the user didn't approve.

Concretely: do NOT expose `/crystallize` as something `mmu_idle_daemon.py`'s
`IDLE_TOOLS` can call directly and unilaterally complete. The two-step split:

1. **Propose** (`GET /skill_candidates`, and optionally a `propose_skill` idle tool
   Nova can call): surfaces clusters meeting the mutual-density threshold, with a
   `status: "proposed"` marker (a lightweight node or a property on a tracking node,
   your call) -- but writes NOTHING that touches the underlying Memory nodes yet. No
   color change, no new edges to real Skill nodes, nothing irreversible.
2. **Confirm** (`POST /crystallize`, called by the user directly or by Claude Code at his
   explicit request, never by the idle daemon's own automatic loop): takes a proposed
   cluster id (or explicit member addresses) and only THEN does the atomic node
   creation, edge wiring, and Memory demotion described below.

If you want Nova to be able to voice an opinion on a proposed skill ("I think this
should become a skill because...") that's a good, in-character addition (consistent
with how Phase 6.6 gave her a voice on her own emotional state) -- but her voicing an
opinion is not the same as her being able to execute the crystallization herself.
Keep those two things structurally separate in the code, not just by convention.

## Implementation plan

### 1. Schema

`Skill` node: `skill_id` (uuid), `trigger` (string, what pattern of recall/topic
invokes this skill), `procedure` (string, what the skill actually represents/does),
`confidence` (float), `invocation_count` (int, starts 0), `last_invoked`
(nullable timestamp), `created_at`, `status` (`candidate` | `active` | `deprecated`).

Edges:
- `PROCEDURALIZED_FROM` (Memory -> Skill): written when a proposal is confirmed.
  Source memories are demoted to Blue (per roadmap: "never deleted, they are the root
  system") -- use the existing `write_color_update()` pattern in `neo4j_layer.py`
  (search for it, already used for aging-triggered Blue transitions) rather than a
  new bespoke color-change query.
- `SKILL_CANDIDATE` (Memory -> a proposal tracking node, or a property-based marker,
  per the SAFETY REQUIREMENT above): represents the proposed-but-not-confirmed state.
- `EXTENDS_SKILL` (Skill -> Skill, Phase 13): forms the directed tree. Enforce "a
  parent cannot be deprecated while a child is active" as a real check in whatever
  endpoint changes a Skill's `status`, not just as a comment -- attempting to
  deprecate a Skill with an active `EXTENDS_SKILL` child should fail loudly, not
  silently succeed and leave the tree in a broken state.

### 2. `neo4j_layer.py` additions

- `find_skill_candidates(min_cluster=3, min_pairwise_weight_norm=0.6)`: the real
  mutual-density check described above (all pairs among a candidate cluster meet the
  normalized-weight floor), distinct from the existing preview queries. Returns
  proposed clusters with member addresses, computed `skill_score`, and a suggested
  `trigger`/`procedure` draft (can be a simple template from the shared keywords
  across the cluster, doesn't need to be clever).
- `write_skill_candidate(cluster)`: writes the proposal marker (step 1 of the SAFETY
  REQUIREMENT split). Purely additive, touches nothing on the Memory nodes.
- `crystallize_skill(member_addresses, trigger, procedure, confidence)`: the atomic
  confirm step. One transaction: create the `Skill` node, write `PROCEDURALIZED_FROM`
  from each member, demote each member to Blue via the existing color-update path,
  clear the `SKILL_CANDIDATE` proposal marker. All-or-nothing -- if any part fails,
  nothing should be left half-applied.
- `get_skill_tree(root_skill_id=None)`: walks `EXTENDS_SKILL` edges for Phase 13,
  returns the tree structure (or the whole forest if no root given).
- `propose_meta_skill(skill_id_a, skill_id_b)` (Phase 13): per the roadmap, "two
  skills sharing 2+ PROCEDURALIZED_FROM memories are flagged as related and can
  propose a meta-skill." Same human-confirmation discipline applies here too -- this
  proposes, it does not create the meta-skill unilaterally.

### 3. `mmu_server.py` endpoints

- `GET /skill_candidates`: runs `find_skill_candidates()`, returns proposals. Safe to
  poll, changes nothing.
- `POST /crystallize`: body is a proposed cluster id (or explicit member addresses +
  trigger/procedure/confidence for a manual override). Calls `crystallize_skill()`.
  This is the one endpoint in this whole phase that should feel deliberately heavier
  to invoke than everything else in the API -- consider requiring an explicit
  `confirmed: true` field in the body as a small extra guard against an accidental
  call, on top of the human-confirmation workflow already described.
- `GET /skill_tree`: Phase 13, wraps `get_skill_tree()`.
- `POST /skills/{skill_id}/deprecate`: enforces the "parent can't deprecate with an
  active child" rule described above.

### 4. Port the normalization fix

Apply the `max_w` normalization from `get_idle_context()`'s crystallization block to
`get_insights()`'s crystallization block, so `skill_score` means the same thing (and
stays in 0-1) everywhere it's reported.

## Deploy sequence (run these yourself, you have real terminal access)

1. Port the normalization fix first. Rebuild, verify the running container, and
   confirm via a real call to both `/insights` and `/idle_prompt` (which surfaces
   `get_idle_context()`) that `skill_score` for the same underlying candidates now
   agrees between the two and never exceeds 1.0.
2. Build and deploy the Phase 12 schema, functions, and endpoints. Standard
   `--no-cache` rebuild + running-container grep verification, same as every phase.
3. Call `GET /skill_candidates` against the real graph. Report what it finds --
   honestly, it may find zero clusters meeting the 3-member/0.6-normalized-weight bar
   this early in the project's life, and that is a legitimate, useful result to
   report, not a failure. Don't loosen the threshold just to manufacture a candidate.
4. If at least one real candidate exists, walk it through confirmation by hand: call
   `/crystallize` with the user's explicit go-ahead on that specific cluster, and verify
   afterward that the Skill node exists, `PROCEDURALIZED_FROM` edges point to it, and
   the source memories are now Blue but still present (not deleted) -- check this
   directly with a Cypher query against the running Neo4j instance, don't just trust
   the endpoint's return value.
5. If no real candidate exists yet, don't force one. Report that crystallization is
   deployed and correct but has nothing to crystallize yet, and that this should be
   revisited once the graph has grown denser -- this is the expected, honest state for
   a young graph, matching the same "don't manufacture a demo pattern" principle used
   for Phase 10's temporal detection.
6. Only after step 4 (or an honest step 5) is confirmed working, build Phase 13's
   tree edges and the deprecate-with-active-child guard. Test the guard directly:
   attempt to deprecate a Skill with an active child and confirm it's rejected, not
   silently allowed.

## Acceptance checklist

- [ ] `skill_score` normalization fix ported to `get_insights()`, confirmed to match
      `get_idle_context()`'s values on the same data
- [ ] Mutual-density cluster check (3 members, pairwise normalized weight >= 0.6)
      implemented as its own query, not repurposed from the anchor-node preview query
- [ ] `/skill_candidates` never writes anything to Memory nodes or creates a real
      Skill node -- confirmed by inspecting the graph before and after calling it
- [ ] `/crystallize` requires explicit confirmation and only the user (directly, or via
      Claude Code at his explicit request) can trigger it -- NOT wired into
      `mmu_idle_daemon.py`'s automatic `IDLE_TOOLS`
- [ ] Source memories are demoted to Blue, never deleted, after a real crystallization
      (verified directly against Neo4j, not just via API response)
- [ ] Honest report on whether the current graph actually has any real crystallization
      candidates yet, no manufactured demo data
- [ ] Phase 13's parent-can't-deprecate-with-active-child rule tested and confirmed
      to actually reject an invalid deprecation attempt
- [ ] `--no-cache` rebuild + running-container verification performed at each of the
      two build steps (normalization fix, then the full phase) and shown in the report
