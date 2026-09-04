# FINAL STAGE -- Shareable Release (Empty Memory, Clean Install)

Status: **final stage.** Runs after every numbered phase is complete (10, then 12+13).
Deliberately last: it hardens and packages whatever the system has become, so doing it
earlier means doing it twice.

Goal: someone who is not the user can clone the repo, run one command, and get a working MMU
with an **empty** memory graph. The user validates this by standing up a second instance from
scratch on the same machine and using it without touching the primary.

---

## Copy this to Claude Code to start

```
I'm ready to build the final shareable-release stage of the MMU project. Full design doc
is at C:\MMU\phase_packages\phase_final_shareable_release.md -- read it in full before
writing any code. The goal is a clean install with an empty memory graph that someone
other than me can run. It documents a decision I need flagged before you proceed past it
(search for "DECISION NEEDED"), the config extraction and hardening work, a first-run
bootstrap path that has never been exercised, and a validation sequence where you stand
up a SECOND instance alongside my live one without disturbing it. My primary instance and
its 954 memories must be completely untouched -- treat that as the hardest constraint in
the doc. Report back what you changed and the actual output of each validation step.
```

---

## Why this is last, and why it is not just "write a README"

Every phase so far was built by the person who already knew how it worked. That is the
one thing a shareable release cannot assume. Three categories of problem only appear here:

1. **The empty-graph path has never been executed.** Every code path in this system has
   only ever run against a populated graph. Recall against zero memories, `session_bundle`
   with no domains, `/insights` with nothing to report, the v2 index bootstrapping from
   nothing -- none of that has been exercised since very early development, and the
   codebase has changed enormously since.
2. **Defaults that are fine for the user are traps for a stranger.** The aging thresholds are
   the clearest case (see below), but so is a baked-in database password.
3. **A memory system that other people run needs an opinion about privacy** that a
   personal one never needed. See DECISION NEEDED #1.

---

## Current state (verify these still hold before starting)

Good news, established during Phase 11: **the Python is already portable.** Phases 9 and
11 both used the `MMU_*` env-var convention consistently. A grep for hardcoded absolute
paths and hosts across `*.py`, `*.yml` and `Dockerfile` (excluding `old/`) found nothing
in the server code -- only:

- `docker-compose.yml` -- one hardcoded personal mount,
  `C:/Users/<you>/Documents/Theory:/docs:ro`
- `docker-compose.yml` and `Dockerfile` -- `mmupassword` as a baked-in default
- `Extra/graph_viz.py`, `Extra/test_client.py` -- `http://localhost:8765` hardcoded
  (helper scripts, lower priority)

Re-run that grep before starting; Phase 10 and 12+13 will have added code.

Already in place and worth not re-solving:
- `.gitignore` excludes `Extra/apik.txt`, `*.bak`, `old/`, logs, and JSON exports.
- `GET /embedding_status` and `GET /health` already report enough to write a real
  post-install check against.
- `POST /backfill_embeddings` is idempotent and is the natural "repair" primitive.
- The MCP server (`mmu_mcp_server.py`) is the portable integration surface. MCP has real
  client support across Claude, LM Studio and others, so *that plus `docker compose up`*
  is the distributable package. `mmu_claude_bridge.py` is a convenience, not a second
  integration to maintain at parity.

---

## DECISION NEEDED #1: what privacy guarantees ship with it

A personal memory system does not need this. A shared one does. Right now anything said
to Nova can become a Memory node, and there is no retention policy, no export, and no
delete-everything.

Minimum honest options, pick before writing docs that make claims:

- **Option A (recommended): local-only, documented, with an escape hatch.** State plainly
  in the README that all data stays in the user's own Neo4j container and is never
  transmitted anywhere, that the only outbound calls are to the embedding/model endpoints
  the user configures, and ship `POST /forget_all` (guarded, requires an explicit
  confirmation token) plus a documented export. No policy engine, no retention timer --
  just truthful documentation and the ability to leave.
- **Option B: add retention controls.** Per-GRP or per-source retention windows,
  automatic expiry. More honest for a wider audience, materially more code, and it
  interacts with the aging state machine in ways that need thought.
- **Option C: say nothing.** Not acceptable for something handed to other people.

Note that the outbound-call claim must be **verified, not asserted**. `EmbeddingClient`
talks to `MMU_EMBEDDING_BASE` and the idle daemon talks to `LMSTUDIO_BASE`; confirm
nothing else egresses before writing that sentence into a README.

## DECISION NEEDED #2: what the default aging behaviour is for a new user

Phase 10 replaces count-only archiving with count + time-since-touch and a Yellow
pre-hold state. Whatever those defaults are, they were tuned against the user's usage.

A new user with a nearly empty graph has very different dynamics: with 5 memories, a
handful of recalls pushes everything toward the threshold at once. Decide whether the
shipped defaults differ from the user's, and if they do, say why in the config file rather
than leaving a stranger to discover it. Consider whether aging should simply not run
below some graph size.

---

## Implementation plan

### 1. Config extraction
- `.env.example` with every `MMU_*`, `NEO4J_*`, and embedding variable, each with a
  one-line comment on what it does and what a sane value is.
- `docker-compose.yml` reads from `.env`: `${NEO4J_PASS:?set NEO4J_PASS in .env}` so an
  unset password fails loudly at startup instead of silently defaulting to `mmupassword`.
- Remove `ENV NEO4J_PASS=mmupassword` from the `Dockerfile` entirely -- a default password
  baked into an image is worse than a missing one.
- The `/docs` mount becomes `${MMU_DOC_HOST:-./documents}:/docs:ro` with a committed empty
  `documents/` directory, so ingestion works out of the box with a local folder and the
  personal path disappears.

### 2. First-run bootstrap
- Verify `bootstrap_schema()` creates everything from nothing against a **fresh** Neo4j
  volume: constraints, indexes, and the vector index. The vector index is the risky one --
  it needs `MMU_EMBEDDING_DIM` correct before any memory exists.
- Verify `LightIndexV2` initialises with no index file present (`light_index_v2.py`
  handles a missing file, but confirm against a genuinely empty volume, not an assumption).
- Add a startup self-check that logs, in one clear block: Neo4j reachable, vector index
  present and its dimension, embedding backend reachable and the dimension it actually
  returns, and **a loud warning if those two dimensions disagree**. A dimension mismatch is
  the single most likely first-run failure and currently surfaces only as recall quietly
  not working.
- Decide whether to ship any seed memories at all. Recommendation: **no.** An empty graph
  is honest, and seeded examples become someone else's confusing memories.

### 3. Empty-graph correctness
Exercise every endpoint against zero memories and fix whatever breaks:
`/recall`, `/session_bundle`, `/insights`, `/maintain`, `/graph`, `/idle_prompt`,
`/unrated_memories`, `/embedding_status`, `/backfill_embeddings`.
These should return empty structures, not 500s and not misleading zeros presented as
findings. `/maintain` and `/insights` are the likeliest to assume non-empty input.

### 4. Known defects to fix before shipping
These are tolerable on a machine whose owner understands them, and not tolerable in
someone else's hands:
- **CON collisions.** `add_memory()` derives `con` from `n4j.get_memory_count() + 1`,
  which collides after deletions and violates the `memory_addr` UNIQUE constraint. It bit
  the Phase 11 restore script twice. For a stranger this is an unexplained 500.
- **Session nodes do not mean "conversation"** (`phase11_deploy_report.md` §6.5) --
  `/recall` mints a fresh UUID per call. If Phase 10 has not already fixed this, fix it
  here, because `/session_resume` misrepresents itself.
- Re-read the deferred `maybe_retrieval_control.md` for anything else recorded there.

### 5. Tests
There are none. The user has been the test harness, and that does not transfer.
Minimum worth having, favouring the paths a stranger will hit first:
- `chunk_text()` / `extract_keywords()` -- pure functions, no infrastructure needed
- address round-trip: `_gen_addr()` -> `_parse_addr()`, including the `~VAL` segment
- the aging state machine, with time mocked
- an empty-graph smoke test hitting every endpoint for a non-500
- one end-to-end: start empty, save, recall, verify `via` and `semantic_score`

### 6. Documentation
- `README.md`: what it is, requirements, install, first-run, connecting an MCP client,
  and a troubleshooting section led by the dimension-mismatch failure.
- Keep `MMU_Handoff_v13.md` as internal design history -- it is the user-specific and should
  not be the front door.
- `phase_packages/` is project history and can ship as-is; it is genuinely useful for
  explaining why the architecture is shaped the way it is.

---

## Validation: stand up a second instance

The real test is a from-scratch install, and it must not disturb the live one.

**Hard constraint: the primary instance and its memories stay untouched.** Different
container names, different host ports, different volumes, different project name. Verify
before and after that the primary's memory count and colour distribution are unchanged.

1. Record the primary's state first: `/embedding_status` and the colour distribution.
   This is the before-and-after evidence.
2. Fresh clone into a separate directory. Copy `.env.example` to `.env` and fill it in as
   a new user would -- **follow only the README**. Anything requiring knowledge not in the
   README is a documentation bug; note it rather than working around it from memory.
3. `docker compose -p mmu-test up -d` with distinct ports (e.g. 8766, 7475, 7688) and
   separate volumes.
4. Confirm the startup self-check block reports everything green against an empty graph.
5. `/health`, `/insights`, `/session_bundle`, `/recall` against zero memories -- no 500s.
6. Save a memory, recall it, confirm `via` and `semantic_score` are populated.
7. Ingest a small document into the test instance and confirm chunking, embedding and
   provenance work on a graph that started empty.
8. Connect an MCP client to the test instance and confirm the tools work end to end.
9. Re-check the primary: memory count and colour distribution identical to step 1.
10. Tear down the test instance and confirm its volumes are gone and the primary still runs.

---

## Acceptance checklist

- [ ] DECISION NEEDED #1 (privacy posture) answered and reflected in the README
- [ ] DECISION NEEDED #2 (default aging for a new user) answered
- [ ] No hardcoded personal paths, hosts, or passwords anywhere outside `.env`
- [ ] `Dockerfile` no longer carries a default `NEO4J_PASS`; unset password fails loudly
- [ ] Fresh install works from the README alone, with no undocumented steps
- [ ] Every endpoint returns a sane empty response against a zero-memory graph, no 500s
- [ ] Startup self-check reports Neo4j, vector index, embedding backend and dimension,
      and warns loudly on a dimension mismatch
- [ ] CON collision fixed
- [ ] Session-node semantics correct, or explicitly deferred with a reason
- [ ] Test suite exists and passes from a clean checkout
- [ ] Second instance validated end to end on different ports and volumes
- [ ] **Primary instance verified byte-for-byte unaffected: memory count and colour
      distribution identical before and after**
- [ ] Outbound network calls audited and the README's privacy claim verified, not asserted
- [ ] `--no-cache` rebuild + running-container verification performed on the test instance
