# Phase 10 -- Proactive Memory (Nova Anticipates)

Status: ready to build, but read the OPEN ARCHITECTURAL QUESTION below before writing
any code -- it changes what "proactive" can actually mean given how LM Studio's MCP
bridge works today, and I'd rather Claude Code and the user settle that deliberately than
have it discovered halfway through.

Sequenced after Phase 9 and Phase 11 in the recommended order because pattern
detection is much more useful once there's both a semantic similarity signal (Phase 9)
and a document-ingestion path that can produce dense, related clusters worth noticing
(Phase 11). Building this first would mean testing pattern detection against a
thinner graph than it'll actually run against in practice.

---

## Copy this to Claude Code to start

```
I'm ready to build Phase 10 of the MMU project (proactive memory: pattern detection
over the CO_RECALLED graph, temporal memory tags, anticipatory context). Full design
doc is at C:\MMU\phase_packages\phase10_proactive_memory.md -- read it in full first.
It documents an open architectural question about what "mid-conversation, unprompted"
can actually mean given how the LM Studio MCP bridge works (search for "OPEN
ARCHITECTURAL QUESTION") -- I need you to actually check this against LM Studio's
real plugin/MCP behavior rather than assume, and report what you find before building
past that point. The rest of the doc has exact file/line anchors, the schema and code
changes, and a deploy + validation sequence to run yourself.
```

---

## Goal

Per the roadmap: "Pattern detection over the CO_RECALLED graph, temporal memory tags,
anticipatory context injection mid-conversation without being asked."

## OPEN ARCHITECTURAL QUESTION: what can "mid-conversation, unprompted" actually mean

`mmu_mcp_server.py`'s tool registry exposes exactly two things Nova can call:
`get_session_context` (maps to `GET /session_bundle`, called once at the start of a
conversation, "CALL THIS FIRST") and `recall_memory` (maps to `POST /recall`, called
whenever Nova decides she wants something specific). Both are pull-based: the model
has to decide to call the tool before anything comes back. MCP over stdio, as used
here, does not give the server a channel to push a notification into an ongoing
conversation on its own initiative -- there's no mechanism in this codebase today for
"say something to Nova mid-turn without her asking."

Before writing pattern-detection code, actually verify this against LM Studio's real
plugin/MCP support rather than trusting this doc's assumption. Check LM Studio's
current docs and, if needed, its actual behavior: does its MCP client support any
server push, resource-change notification, or sampling-request-style callback that
could inject content into a live conversation without a tool call? If the answer is
genuinely no (which matches everything the project has observed about this bridge so
far), then "anticipatory context injection mid-conversation without being asked" has
to be built as one of these two honest approximations, not as literal mid-turn push:

- **Pre-conversation anticipation**: prepare the anticipated content during idle time
  and surface it the next time `get_session_context`/`/session_bundle` is called --
  this is genuinely "before being asked," just delivered at conversation start rather
  than mid-conversation. `/session_bundle` already does exactly this pattern for
  Phase 7 artifacts and the Phase 8 session-close summary, so this is additive, not
  new architecture.
- **Riding along on recall**: when Nova calls `recall_memory` for one specific thing,
  the response can include an additional, clearly-labeled "you might also be
  interested in" block built from pattern detection, alongside the direct answer to
  what she actually asked. This is "unprompted" in the sense that she didn't ask for
  THAT specific content, even though the tool call itself was hers.

Recommend building both. If your research into LM Studio turns up a genuine push
mechanism this doc doesn't know about, that's worth a bigger conversation with the user
before building around it, since it would be new architecture, not incremental work.

## Current state (read this before changing anything)

- `write_recall_edges()` in `neo4j_layer.py` (~line 281) writes `CO_RECALLED` edges
  with only `weight` (an incrementing counter) and `last_turn` (a single timestamp,
  overwritten each time). There is no per-occurrence history. This is NOT enough data
  to detect a real recurrence interval ("this comes up roughly every 5 days") --
  you'd only ever know "the last time," never the pattern of gaps between times. See
  the schema addition below before attempting temporal pattern detection.
- The Phase 6 `maintain()` function in `neo4j_layer.py` (~line 1560 onward) is the
  best existing template for pattern-detection style: it already does cross-GRP
  cluster detection (dense `CO_RECALLED` pairs spanning domains) and stale-memory
  detection, returning structured results plus a `summary_text` folded into idle
  prompts. Phase 10's pattern detection should follow this same shape: a function
  that returns structured candidates, not a free-text narrative.
- `GET /session_bundle` in `mmu_server.py` (~line 580) already blends multiple
  proactive elements in sequence: base bundle, unseen `CreativeOutput`s (Phase 7),
  last closed session summary (Phase 8). A Phase 10 "anticipated" block is a fourth
  addition to this same function, following the same "append a labeled section,
  don't affect anything if there's nothing to add" pattern already established there.
- `mmu_idle_daemon.py`'s `IDLE_TOOLS` list and `_handle_tool()` dispatcher are where
  Nova's idle-time actions are defined (`save_memory`, `create_artifact`, `ask_user`,
  `rate_memory`). Anticipation candidates generated during an idle pass could be
  written as a new lightweight node type or property rather than going through the
  full `save_memory` path, since they're system-generated predictions, not Nova's own
  memories -- see schema below.

## ADDED 2026-08-30: Aging redesign (count + time-since-touch, with a pre-hold state)

The user's original intent for archiving was a **count threshold AND a time-since-touch
threshold**, with a holding state before archive. What shipped implements only the count
half, and that turns out to be a live defect rather than a simplification.

### Why this is in Phase 10 and not left alone

`_age_memories()` (`mmu_server.py:684`) increments `use` on every non-recalled,
non-Document memory on **every recall**, and archives at `MMU_ARCHIVE_THRESH = 20`.
Because the counter is driven by recall *count* and nothing else, twenty recalls archive
a memory whether they happen over three months or twenty minutes.

This is not hypothetical. Validation runs during Phase 9 and Phase 11 archived the user's
conversational memories **twice** -- 73 Green to 15 in the first incident, and again
afterwards -- and both times needed a manual restore (see `phase9_deploy_report.md` §7.4
and `phase11_deploy_report.md` §7). Any burst of activity does this. So would a busy day.

It belongs in Phase 10 because Phase 10 is already adding the temporal machinery this
needs: per-occurrence recall timestamps. Aging by time and detecting recurrence intervals
both want to know when a memory was last actually touched, and building two separate
notions of "when" would be the same duplicate-source-of-truth mistake that produced most
of the defects found in Phases 9 and 11.

### The state machine

Current: `Green --(use >= 20)--> Blue`, and `Blue --(recalled)--> Yellow`.

Proposed gradient, with Yellow becoming reachable from **both** directions:

```
Green  --(use >= HOLD_THRESH)------------------> Yellow    (cooling, pre-hold)
Yellow --(use >= ARCHIVE_THRESH AND
          days_since_touch >= ARCHIVE_MIN_DAYS)-> Blue     (archived)
Yellow --(recalled)---------------------------> Green     (reactivated)
Blue   --(recalled)---------------------------> Yellow    (warm, unchanged)
```

**Archiving now requires both conditions.** A memory that is unused by count but was
touched recently stays in the holding state. Only sustained disuse *over real elapsed
time* archives it.

Reusing Yellow rather than adding a fifth colour is deliberate. `MMU_Handoff_v13.md`
defines Yellow as "warm/re-activated," and a memory cooling toward archive is warm in
exactly the same sense as one returning from it -- Yellow becomes the transition zone in
both directions, which makes the matrix more coherent, not less.

**Check before relying on this:** anything that currently reads Yellow as specifically
"recently reactivated" needs review. The known case is Phase 6 domain-gap detection in
`run_maintenance()` (`neo4j_layer.py:1557`), which counts Green/Yellow/Red as "active" --
that stays correct under the new meaning, since a pre-hold memory *is* still active. Grep
for other Yellow consumers before assuming this is the only one.

### Schema

`Memory` needs a last-touch timestamp. There is `created_at`, and `CO_RECALLED` carries
`last_turn`, but nothing records when a *memory* was last recalled:

- Add `m.last_touched_at` (ISO string, matching `created_at`'s existing format).
- Set it in `write_recall_edges()` (`neo4j_layer.py:308`) for every recalled address, and
  in `write_memory()` on create.
- Backfill existing nodes in `bootstrap_schema()` (`neo4j_layer.py:97`), idempotently,
  following the Phase 4 backfill pattern already there:
  `MATCH (m:Memory) WHERE m.last_touched_at IS NULL SET m.last_touched_at = coalesce(m.created_at, $now)`

Note this is the same timestamp the occurrence-tracking change above wants. Write it once,
in one place, and have both features read it.

### Config

```
MMU_HOLD_THRESH        default 20    Green -> Yellow (count only)
MMU_ARCHIVE_THRESH     existing      count half of the archive condition
MMU_ARCHIVE_MIN_DAYS   default 14    time half; BOTH must hold to archive
```

Setting `MMU_ARCHIVE_MIN_DAYS=0` reproduces today's behaviour exactly, which is the
migration escape hatch if this misbehaves.

### Interaction with Phase 11

Document chunks (`src_type == 2`) are already exempt from aging entirely
(`mmu_server.py:718`). Nothing here changes that -- reference material does not decay.
This redesign governs conversational memories only.

### Validation

- Run `MMU_ARCHIVE_THRESH + 5` recalls in quick succession and confirm **nothing**
  archives, because the time condition cannot be satisfied. This is the exact scenario
  that broke twice; it is the headline test.
- Confirm memories do cross into Yellow on the count threshold alone.
- Force-age with `MMU_ARCHIVE_MIN_DAYS=0` and confirm the old behaviour returns, proving
  the time gate is what is holding archiving back rather than a broken counter.
- Confirm a recall promotes Yellow back to Green and resets `use`.
- Report the colour distribution before and after every validation run.

---

## Schema additions

### 1. Per-occurrence recall timestamps (needed for real temporal detection)

Add a capped, append-only list on the `CO_RECALLED` relationship (or, if that gets
unwieldy in Cypher, a separate lightweight `RecallEvent` node per occurrence, your
call, but a bare counter is not sufficient):

```cypher
MATCH (a:Memory)-[r:CO_RECALLED]-(b:Memory {address: $addr_b})
SET r.occurrences = coalesce(r.occurrences, []) + [$now]
```

Cap at the last 20 occurrences (trim the oldest when appending past that) so this
doesn't grow unbounded on hot paths. This is what makes "roughly every N days" a real
computed statistic (median or mean gap between consecutive timestamps) instead of a
guess.

### 2. Temporal memory tags

A `temporal_pattern` property on `Memory` or on the `CO_RECALLED` edge (prefer the
edge, since the pattern is really about a *pairing* recurring, not a single memory
recurring in isolation): `{interval_days: float, confidence: float, last_seen: str}`,
computed from the occurrence list above. Confidence should reflect how consistent the
gaps actually are (e.g., inverse of coefficient of variation across the last several
gaps) -- don't report a confident-sounding interval from two data points.

## Implementation plan

### 1. `neo4j_layer.py`: pattern detection functions

- `detect_temporal_patterns(min_occurrences=4, max_variance=0.4)`: pulls
  `CO_RECALLED` edges with enough occurrence history, computes interval statistics,
  returns candidates above a confidence floor. Follow the `maintain()` function's
  return shape (structured list + a stats summary).
- `get_anticipated_context(current_grp_domains=None)`: given the domains touched so
  far in the current session (or none, for the pre-conversation case), find memories
  whose temporal pattern suggests "about due" or whose CO_RECALLED cluster strongly
  overlaps what's already been discussed, excluding anything already surfaced this
  session (check against `RECALLED_IN`/`HAPPENED_IN` for the current session_id so
  this doesn't repeat itself). Cap the result count hard (3-5) -- proactive surfacing
  that floods context defeats its own purpose.

### 2. `mmu_server.py`: wire into the two channels

- `GET /session_bundle` (~line 580): after the existing Phase 7/8 additions, call
  `get_anticipated_context()` with no session-domain filter (nothing discussed yet)
  and append a clearly labeled block, something like "THIS MIGHT BE RELEVANT TODAY:"
  -- follow the existing lines' style exactly, don't invent a different formatting
  convention.
- `POST /recall` (~line 561): after computing the direct results, optionally call
  `get_anticipated_context()` scoped to the GRP domains of what was just recalled, and
  add a separate `anticipated` list in the response (not merged into `memories`, kept
  visibly distinct) so Nova and the user can both tell "this answered what I asked" from
  "this is a proactive nudge" -- same transparency principle Phase 6.6 and Phase 9
  both already follow (never blend a different kind of signal into a bucket that
  looks uniform).

### 3. Cap and rate-limit proactive surfacing

Add an env var `MMU_ANTICIPATE_MAX` (default 3) for how many anticipated items can
appear per call, and consider a simple per-session cap too (e.g., don't anticipate
more than N times in one conversation) so this doesn't become noisy. Ask the user after
the first week of real use whether the volume feels right; don't over-tune this
speculatively before there's real behavior to react to.

## Deploy sequence (run these yourself, you have real terminal access)

1. Complete the OPEN ARCHITECTURAL QUESTION research above and report findings before
   writing pattern-detection code.
2. Add the occurrence-tracking schema change to `write_recall_edges()`, rebuild with
   `--no-cache`, verify the running container has it
   (`docker exec mmu-memory-server grep -n "occurrences" /app/neo4j_layer.py`).
3. Since occurrence history starts empty for existing `CO_RECALLED` edges, temporal
   pattern detection will find nothing useful until real usage accumulates several
   more sessions. Say this plainly in your report rather than manufacturing a
   demo pattern -- this phase's temporal component needs real time to pass before it
   can be meaningfully validated, unlike the rest of Phase 10.
4. Cross-GRP cluster-based anticipation (not time-dependent) CAN be validated
   immediately against the existing graph: call `/session_bundle` and confirm the new
   anticipated block appears when the graph has strong enough CO_RECALLED clusters,
   and is absent (not an empty labeled section, just absent) when it doesn't.
5. Test the `/recall` side the same way: call it with a prompt that touches a GRP
   domain with known strong CO_RECALLED neighbors, confirm the `anticipated` list is
   populated and clearly separate from `memories` in the response.

## Acceptance checklist

- [ ] LM Studio's actual MCP/plugin push capability researched and reported, not
      assumed from this doc
- [ ] Occurrence-tracking schema change deployed and verified in the running
      container
- [ ] Cluster-based anticipation validated against the current graph (doesn't need to
      wait for new data)
- [ ] Temporal pattern detection implemented, but explicitly flagged as needing more
      real usage time before it can show a genuine result, not faked with synthetic
      data
- [ ] Anticipated content is visibly and structurally distinct from direct results in
      both `/session_bundle` and `/recall` responses
- [ ] A hard cap on anticipated item count is in place and configurable
- [ ] Aging redesign: `last_touched_at` added, backfilled idempotently, and written
      from a single place shared with occurrence tracking
- [ ] Archiving verified to require BOTH count and elapsed time -- a burst of
      `ARCHIVE_THRESH + 5` rapid recalls archives nothing
- [ ] Green -> Yellow -> Blue gradient works, and Yellow -> Green on recall
- [ ] Other consumers of Yellow audited before the meaning was widened
- [ ] Document chunks confirmed still exempt from aging entirely
- [ ] `--no-cache` rebuild + running-container verification actually performed and
      shown in the report
