# Phase 10 — Proactive Memory + Aging Redesign — DEPLOY REPORT

Status: **deployed and validated** on the development machine, 2026-08-30.
Design doc: `C:\MMU\phase_packages\phase10_proactive_memory.md`
Prior phases: `phase9_deploy_report.md`, `phase11_deploy_report.md`

**Final state: 954 memories (93 conversational + 861 document chunks), 954 embedded,
0 missing.**

---

## 1. The OPEN ARCHITECTURAL QUESTION was answered by decision, not research

The design doc gates the build on researching whether LM Studio's MCP bridge can push
content into a live conversation without a tool call, and says to report findings before
proceeding.

**That research was not performed, deliberately, because the user settled the question on
different and stronger grounds.** He decided proactive surfacing stays **pull-based via
tool call**, reasoning that pausing for a tool call is the right behaviour and that
push is a later research topic if ever.

That decision holds regardless of what LM Studio happens to support, because the
governing constraint changed: the project is now aimed at being **shareable and
model-agnostic across local and cloud models**. Tool calls are the only primitive
supported universally. A push dependency would work on whatever was tested and silently
degrade everywhere else — disqualifying for something handed to other people.

Three further reasons recorded at the time:
- **Debuggability.** Content appearing mid-turn that neither side requested is the
  hardest possible thing to reproduce from a stranger's bug report.
- **Attribution.** Phase 6.6's principle is making Nova's internal state visible, which
  is why every row carries `via` and `semantic_score`. Unrequested injection blurs
  whether she recalled something or was handed it.
- **Context budget.** Pull lets the model spend its own context; push spends it for her.

Phase 10 therefore builds both approximations the doc describes, and neither is a
compromise: **pre-conversation anticipation** at `session_bundle` time and **ride-along
anticipation** on `recall`.

---

## 2. Aging redesign (added to Phase 10 at the user's direction)

### The defect
`_age_memories()` incremented `use` on every non-recalled memory on **every recall** and
archived at `MMU_ARCHIVE_THRESH = 20`. Driven by recall *count* and nothing else, twenty
recalls archived a memory whether they spanned three months or twenty minutes.

Not hypothetical: validation runs during Phase 9 and Phase 11 archived the conversational
graph **twice**, both needing a manual restore. The user's original design was count **and**
time-since-touch, with a holding state before archive. Only the count half shipped.

### The state machine
```
Green  --(use >= HOLD_THRESH)------------------------> Yellow   cooling, pre-hold
Yellow --(use >= ARCHIVE_THRESH AND stale >= N days)-> Blue     archived
Yellow --(recalled)---------------------------------> Green     reactivated
Blue   --(recalled)---------------------------------> Yellow    warm, unchanged
```

Archiving now requires **both** conditions. Yellow is reused rather than adding a fifth
colour: `MMU_Handoff_v13.md` defines it as "warm/re-activated", and a memory cooling
toward archive is warm in exactly the same sense as one returning from it. Yellow becomes
the transition zone in both directions.

Config:
```
MMU_HOLD_THRESH        default 20    Green -> Yellow (count only)
MMU_ARCHIVE_THRESH     existing 20   count half of the archive condition
MMU_ARCHIVE_MIN_DAYS   default 14    time half; BOTH must hold
```
`MMU_ARCHIVE_MIN_DAYS=0` reproduces the old behaviour exactly — the migration escape hatch.

### Schema
`m.last_touched_at` added, written in exactly one place (`write_recall_edges()`,
`neo4j_layer.py`) and read by both the aging state machine and temporal pattern
detection, so the two cannot drift. Backfilled idempotently in `bootstrap_schema()`.

**Backfilled to NOW, not `created_at`, on purpose.** We do not know when existing
memories were last recalled, and `created_at` would make a memory written weeks ago but
recalled yesterday look stale enough to archive on its next count trip. Seeding "now"
delays archiving rather than accelerating it.

The v2 index card carries a `touched_at` mirror as a unix float. The aging pass runs
in-memory over ~950 cards on every recall; reading the authoritative value from Neo4j per
memory per recall would be absurd. This is the same denormalisation the card already does
for `color`/`priority`/`src_type` — Neo4j stays the source of truth.

`days_since_touch()` returns `None` when unknown, and **None can never archive anything**.
Treating unknown as stale is precisely how a live memory gets silently archived.

### Validation — the headline test
The exact scenario that broke twice: 30 recalls in rapid succession, threshold 20.

```
BEFORE:  Green 91,  Red 2
AFTER:   Yellow 90, Green 1, Red 2      <- zero Blue
```

**Nothing archived.** This is a proof, not just an observation: memories reached Yellow,
which requires `use >= HOLD_THRESH` (20), so the counter demonstrably crossed the archive
threshold too. The only thing preventing Blue was the time gate. That rules out "the
counter is broken" without needing a destructive force-age run.

Recovery verified: a subsequent recall promoted memories Yellow -> Green, and Yellow rows
surfaced normally via `direct` with semantic scores. **Yellow is not dormant** — only Blue
is excluded from expansion and semantic search. No restore was needed after this phase's
testing, unlike the previous two.

Documents remain exempt entirely (Phase 11 §6.4): 860 Green throughout.

---

## 3. Pattern detection

### Occurrence history
`CO_RECALLED` carried only `weight` (a counter) and `last_turn` (overwritten each time) —
enough to say "the last time", never the pattern of gaps *between* times. Added
`r.occurrences`, a capped append-only list:

```cypher
r.occurrences = (coalesce(r.occurrences, []) + [$now])[-20..]
```

Capped at 20 so hot pairs cannot grow unbounded. Verified holding: 0 edges over cap.

### `detect_temporal_patterns()`
Computes median gap and a confidence score from the occurrence list. Confidence is
`1 - coefficient of variation` over the gaps, clamped to [0,1]: evenly spaced occurrences
score high, erratic ones low.

**Requires at least 3 timestamps.** Two data points give exactly one gap and therefore no
variance information; reporting that as a confident interval would be a lie dressed as a
statistic.

### `get_anticipated_context()`
Two signals, kept separate because they become useful at different times:

- **temporal** — "this pairing recurs every N days and is about due"
- **cluster** — "you are discussing X, and Y is strongly co-recalled with X **from a
  different GRP domain**"

The cross-domain requirement is deliberate. Same-domain neighbours are already reachable
through `expand()`; surfacing them again would just duplicate ordinary recall. The value
of an unrequested suggestion is the connection nobody asked for.

Documents (`src_type=2`) are excluded from anticipation. Reference material is retrieved
on demand — a paragraph of a physics paper is not a thing to be reminded of.

---

## 4. Wiring, and why the two channels look different

**`GET /session_bundle`** — temporal signal only (nothing has been discussed yet, so
there are no cluster seeds). Appends a `THIS MIGHT BE RELEVANT TODAY:` block, and only
when there is something to say — no empty labelled section, matching the Phase 7 and
Phase 8 blocks above it.

**`POST /recall`** — both signals, returned as its own `anticipated` list. Never merged
into `memories`, and deliberately absent from `context_block`. Two reasons: Nova asked for
`memories` and did not ask for these, so blending them would misrepresent why each
surfaced; and a caller that ignores the field keeps byte-identical behaviour to before
Phase 10.

### Rate limiting
```
MMU_ANTICIPATE_MAX          default 3    items per call; 0 disables anticipation
MMU_ANTICIPATE_PER_SESSION  default 5    firings per conversation; 0 disables the limit
MMU_ANTICIPATE_STATE_MAX    default 50   conversations tracked before eviction
```

Per-conversation state tracks both a firing count and which memories were already
surfaced. Both are needed — a cap alone still lets the same three memories be offered five
times running.

Keyed on the `X-MMU-Session` header, **not** the Session node, because `/recall` mints a
fresh UUID per call and Session nodes are per-recall rather than per-conversation
(`phase11_deploy_report.md` §6.5). With no header everything collapses to one "anonymous"
bucket, which rate-limits conservatively rather than not at all. In-process and not
persisted: it is conversation-scoped state and losing it on restart is correct.

---

## 5. Validation — actual output

### Ride-along anticipation
```
"Star Wars and video games"  memories=4  anticipated=3
   [cluster w=21] the user is building a two-way neural MMU: color matrix...
   [cluster w=18] The hero character: young, rugged but attractive...
   [cluster w=17] The game design is deeply connected to the research...
```
All three cross-domain from the 2xx/8xx seeds, with high CO_RECALLED weight.

### Rate limiting
```
SESSION D -- 7 recalls, per-session cap = 5
  call 1 (Star Wars)          memories=3 anticipated=3
  call 2 (video games)        memories=3 anticipated=3
  call 3 (physics theory)     memories=3 anticipated=3
  call 4 (game design)        memories=3 anticipated=3
  call 5 (Nova self observ.)  memories=3 anticipated=3
  call 6 (memory system)      memories=3 anticipated=0    <- cap reached
  call 7 (Portland)           memories=3 anticipated=0
  total rows=15  unique CONs=15  repeats=0

SESSION E (fresh conversation) anticipated=3   <- cap resets per conversation
```

### Pre-conversation anticipation
`/session_bundle` returns `anticipated=0` and omits the block. **This is correct, not a
failure.** It uses the temporal signal only, and occurrence history started today: 4 edges,
1 occurrence each, against a minimum of 4. The temporal half **cannot be validated until
real sessions accumulate over real days** — the design doc says to state this plainly
rather than manufacture a demo pattern, and no synthetic data was created.

---

## 6. Defects found and fixed

### 6.1 `/recall` returned addresses that no longer existed (FIXED — pre-existing, significant)
Found while debugging why anticipation returned nothing in one test session and three in
another with the same prompt.

`_age_memories()` rewrites the USE counter into the address, so a memory recalled after a
gap gets a **new address during that very call**. `results` was built before the aging
pass and never updated, so `/recall` returned pre-aging addresses — addresses that no
longer existed in either store by the time the caller read them.

This is not cosmetic. Nova takes those addresses straight back to `/rate` and
`/flag_recall`, where they silently miss. It also made Phase 10's cluster seeds match
nothing, which is how it surfaced: identical queries returning 3 anticipated rows or 0
depending on whether the graph happened to be churning.

**Fix:** `_age_memories()` now exposes its old→new mapping and `recall()` rewrites result
addresses after aging. Verified — all 4 addresses from a live `/recall` resolve in Neo4j.

### 6.2 Cross-turn dedup keyed on an unstable identifier (FIXED)
The first version of the per-session cap tracked surfaced *addresses*. Since aging
rewrites addresses, last turn's set no longer matched the same memories this turn, so the
identical three nudges were re-offered every turn — observed directly before the fix.

**Fix:** dedup keys on the **CON number** (first address field), which aging preserves
untouched. The anticipation call over-fetches `ANTICIPATE_MAX * 3` because the CON filter
removes rows after the query.

### 6.3 Still open, unchanged from Phase 11
- **CON numbers are not unique** — `get_memory_count() + 1` collides after deletions.
  Now load-bearing for §6.2's dedup, though only within a single conversation, where a
  collision is harmless. Still worth fixing before the shareable release.
- **Session nodes do not mean "conversation"** (`phase11_deploy_report.md` §6.5). Phase 10
  works around it by keying on the header rather than fixing it, because changing Session
  semantics would strand 164 existing per-recall nodes and that is the user's call.

---

## 7. Acceptance checklist

- [x] LM Studio push capability — **not researched; superseded by the user's decision** to
      stay pull-based on portability grounds, recorded in §1
- [x] Occurrence-tracking schema deployed and verified in the running container
- [x] Cluster-based anticipation validated against the current graph
- [x] Temporal pattern detection implemented and **explicitly flagged as needing real
      elapsed time**, not faked with synthetic data
- [x] Anticipated content structurally distinct from direct results in both channels
- [x] Hard cap in place and configurable, plus a per-session cap and repeat suppression
- [x] Aging redesign: `last_touched_at` added, backfilled idempotently, written from a
      single place shared with occurrence tracking
- [x] Archiving requires BOTH count and elapsed time — 30 rapid recalls archived nothing
- [x] Green → Yellow → Blue gradient works, and Yellow → Green on recall
- [x] Other consumers of Yellow audited (`run_maintenance()` domain-gap detection counts
      Green/Yellow/Red as active; still correct — a pre-hold memory *is* active)
- [x] Document chunks confirmed still exempt from aging
- [x] `--no-cache` rebuild + running-container verification performed, output included

---

## 8. What is left for Phase 12+13

Roadmap order is `9 → 11 → 10 → 12+13 → final shareable release`. Phases 9, 11 and 10 are
shipped; **Phase 12+13 (skill crystallization + skill tree) is next.**

The roadmap's argument for putting it last still holds and is now stronger: it wants a
mature graph with real CO_RECALLED weight worth compressing. The graph now has 954
memories and 355+ CO_RECALLED edges, and Phase 10 just added per-occurrence history, which
gives crystallization a genuine recurrence signal rather than a bare counter.

Two things to carry in:
- **CON uniqueness** should be fixed before Phase 12 rather than after. Crystallization
  demotes Memory nodes into a Skill's root system, which means creating and rewriting
  nodes in bulk — exactly the conditions that make a collision likely.
- **The address-rewrite class of bug (§6.1) is worth watching for.** Any component that
  captures an address and uses it after an aging pass has the same latent defect. Phase 12
  restructures nodes, so it should hold identifiers, not addresses.
