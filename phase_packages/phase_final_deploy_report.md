# FINAL STAGE — Shareable Release — DEPLOY REPORT

Status: **complete and validated** on the development machine, 2026-08-30.
Design doc: `C:\MMU\phase_packages\phase_final_shareable_release.md`

**Validated by cloning the repo, installing from the README alone, and running a second
instance alongside the live one. The primary was verified byte-for-byte unaffected.**

---

## 1. Decisions

| Decision | Answer |
|---|---|
| Privacy posture | **Local-only with an escape hatch.** `GET /export` and a guarded `POST /forget_all`, plus a README claim about data locality that is *verified* rather than asserted. No retention engine. |
| New-user aging defaults | **Skip aging entirely below a minimum graph size** (`MMU_AGING_MIN_MEMORIES`, default 25). On a small graph "unused" carries no information, and a new user's first experience should not be their memories going dormant for no perceptible reason. |

---

## 2. Config extraction

Starting position was better than expected: a grep for hardcoded paths, hosts and
credentials across all `*.py`, `*.yml` and the `Dockerfile` found **four lines**, all in
`docker-compose.yml` and `Dockerfile`. Phases 9 and 11 had already established the `MMU_*`
env-var convention throughout the Python.

- **`.env.example`** documents every setting with a working default and the reasoning
  behind the ones that bite (dimension matching, the Docker `localhost` trap, the
  normalized-cosine scale).
- **`NEO4J_PASS` has no default and the stack refuses to start without it:**
  ```
  required variable NEO4J_PASS is missing a value:
  NEO4J_PASS is not set. Copy .env.example to .env and set it.
  ```
- **`ENV NEO4J_PASS=mmupassword` removed from the Dockerfile entirely.** A default
  password baked into an image is worse than a missing one.
- The personal `/docs` mount became `${MMU_DOC_HOST:-./documents}` with a committed empty
  `documents/`.
- Host ports and container names became variables (see §6).

### A packaging bug caught before it shipped
`.gitignore` excluded `.env` and `.env.*` — and `.env.*` **silently swallowed
`.env.example`**. The README's very first instruction is `cp .env.example .env`, so the
release would have shipped an install guide referencing a file nobody receives. Caught by
noticing the file absent from `git status`, fixed with a `!.env.example` negation.

---

## 3. Privacy, verified rather than asserted

The README claims data stays local. That claim was checked against the code before being
written:

- `neo4j_layer.py`, `light_index_v2.py`, `ingest.py` — **zero** outbound network calls.
- `mmu_server.py` — **exactly two**, both inside `EmbeddingClient`, both to the
  user-configured `MMU_EMBEDDING_BASE`.
- `mmu_mcp_server.py` — only to the user's own server.

`GET /export` returns every memory as JSON. `POST /forget_all` erases everything but only
against the exact phrase `DELETE ALL MY MEMORIES`, and clears the v2 index too — leaving
it populated would resurrect phantoms into the gate and quietly cost every later recall a
slot (the Phase 11 §6.3 failure mode).

Both were exercised for real on the disposable test instance, which is the only place a
destructive path *can* honestly be tested:

```
export                                    -> 7 memories
POST /forget_all?confirm=DELETE ALL...    -> {"status":"erased","deleted":7}
after                                     -> index=0  neo4j=0     (both stores, no phantoms)
recall on the emptied graph               -> 200, count=0         (no crash)
```

Refusal path verified for the empty string, `yes`, a lowercase near-match, and
`DELETE ALL MY MEMORY` (singular) — all rejected with 400.

---

## 4. First-run experience

Startup now prints a self-check. A dimension mismatch is the single most likely first-run
failure and previously surfaced only as "semantic recall quietly does not work"; it now
fails loudly with the fix spelled out. Actual output from the fresh install:

```
MMU self-check
  Neo4j          : connected, 0 memories
  Vector index   : memory_embedding ONLINE, dim=768, COSINE
  Embedding      : reachable at http://host.docker.internal:1234/v1
  Model          : text-embedding-nomic-embed-text-v1.5
  Configured dim : 768
  Actual dim     : 768
  Empty graph -- this is a fresh install.
```

This matches the README's promised output exactly, which matters — a first-run guide that
doesn't match reality is worse than none.

**Empty-graph correctness.** Every endpoint was exercised against zero memories, a code
path that had not run since very early development:

```
/health /embedding_status /insights /session_bundle /graph /memories
/skill_candidates /skills /skill_tree /unrated_memories /activity
/export /flagged /creative_outputs          -> all 200, no 500s

POST /recall on an empty graph              -> 200, count=0,
                                               "No relevant memories found."
```

---

## 5. Tests

There were none; the user had been the test harness, and that does not transfer.

**32 tests, all passing.** Two tiers:

- **23 pure tests** — chunking, keyword extraction, the address codec across the 1000-CON
  boundary, and the aging state machine. No Docker, no Neo4j, no model.
- **9 live tests** — skipped automatically when nothing is listening, so `pytest` works on
  a clean checkout.

```
$ pytest tests/ -q            (nothing running)
23 passed, 9 skipped

$ MMU_TEST_BASE=http://.../8766 pytest tests/ -q
32 passed
```

The pure tier deliberately encodes the two worst bugs found in this project as regression
tests: `test_address_roundtrip_across_the_1000_boundary` and
`test_rapid_recalls_cannot_archive`.

One test bug was found and fixed during the run — a raw space in a URL query string, which
`http.client` rejects before the server ever sees it.

---

## 6. Validation: a second instance, from scratch

The real test, and it found a real bug.

### The bug: `container_name` is global
First attempt failed:

```
Container mmu-neo4j  Error: Conflict. The container name "/mmu-neo4j" is
already in use...
```

`container_name` was hardcoded, so `docker compose -p mmu-test` collided with the running
primary instead of isolating from it. **A compose project name namespaces volumes and
networks but NOT container names**, which are global to the Docker daemon. My own README's
second-instance instructions were wrong as written.

Fixed: names are now `${MMU_NEO4J_NAME:-mmu-neo4j}` / `${MMU_SERVER_NAME:-mmu-memory-server}`,
defaults preserving existing behaviour. `.env.example` and the README both corrected.

This is exactly what the second-instance exercise is for — it is not reachable by reading
the code.

### The run
Cloned from git (so the test instance received **only what a stranger receives**),
configured by following the README alone, and started:

```
Container mmu-test-neo4j  Healthy
Container mmu-test-server Started

docker ps:
  mmu-test-server    Up 11 seconds
  mmu-test-neo4j     Up 21 seconds
  mmu-memory-server  Up 14 minutes    <- primary, undisturbed
  mmu-neo4j          Up 6 days        <- primary, undisturbed
```

End to end on a graph that started empty:

```
save                -> {"address":"001.005.500.000,...","embedded":true}
                       (CON 001 -- correct allocation from empty)

paraphrase recall   "tell me about my animal companion"
                 -> via=semantic sem=0.823  "My dog is called Biscuit..."
                    no shared keywords with dog/pet/name

ingest (dry run)    -> chunks=5, words=1340, nothing written
ingest (real)       -> written=5 embedded=5 failed=0, 848ms
recall on ingested  -> via=direct sem=0.806, document chunk
```

### The hardest constraint: the primary
Recorded before, compared after:

```
BEFORE                          AFTER
memories=960 embedded=960       memories=960 embedded=960
Conv Yellow 87                  Conv Yellow 87
Conv Green  10                  Conv Green  10
Conv Red     2                  Conv Red     2
Doc  Green 860                  Doc  Green 860
Doc  Yellow  1                  Doc  Yellow  1

diff -> IDENTICAL
```

Teardown removed all three `mmu-test` volumes and the network; the primary continued
running at 960 memories.

---

## 7. Incidental confirmation from production

Partway through this stage the primary's count rose from 954 to 960. Those six were **real
memories Nova saved through the MCP bridge during a live conversation** — AI-Self source,
genuine content. Not test data, and left alone.

They arrived as CON 955–960: sequential, no collisions. That is unplanned production
confirmation that the Phase 12 CON allocator fix (`max(existing)+1` rather than
`count()+1`) works under real use.

---

## 8. Acceptance checklist

- [x] DECISION #1 (privacy posture) answered and reflected in the README
- [x] DECISION #2 (new-user aging) answered — aging floor at 25 memories
- [x] No hardcoded personal paths, hosts, or passwords outside `.env`
- [x] `Dockerfile` carries no default `NEO4J_PASS`; unset password fails loudly
- [x] Fresh install works from the README alone — one documentation bug found (§6) and
      fixed; nothing else required knowledge outside the README
- [x] Every endpoint returns a sane empty response against a zero-memory graph, no 500s
- [x] Startup self-check reports Neo4j, vector index, backend and both dimensions, and
      warns loudly on mismatch
- [x] CON collision fixed (Phase 12 §1) and confirmed in production (§7)
- [ ] Session-node semantics — **still deferred.** `/recall` mints a fresh UUID per call,
      so Session nodes are recall events rather than conversations
      (`phase11_deploy_report.md` §6.5). Changing it would strand 164 existing nodes, and
      that is the user's call, not a silent cleanup.
- [x] Test suite exists and passes from a clean checkout (32 passed)
- [x] Second instance validated end to end on separate ports, names and volumes
- [x] **Primary verified unaffected — memory count and colour distribution identical**
- [x] Outbound calls audited and the README's privacy claim verified, not asserted
- [x] `--no-cache` rebuild + running-container verification performed

---

## 9. What remains

The roadmap is complete: **9 → 11 → 10 → 12+13 → final release**, all shipped.

Known and deliberately open:

1. **5 duplicate CON pairs (030–034)** from the old allocator. Four are harmless. One
   leaves a memory frozen at `use=57` — clamping it would generate its twin's exact
   address. Functional, just unrenameable. Repair needs a stop/patch/start cycle.
2. **CON permanence.** `max()+1` guarantees uniqueness but reuses a number if the
   highest-numbered memory is deleted. Only matters if CON becomes a long-term external
   identifier; a monotonic counter node would fix it.
3. **Session nodes ≠ conversations** (checklist above).
4. **The crystallization write path is unexercised** — no cluster yet meets the
   3-member/0.6 bar. Verify it the first time a real candidate is confirmed.
5. **`maybe_retrieval_control.md`** — semantic zoom, emotion sieve, working-memory buffer.
   Deferred by choice; revisit if wanted.
6. **License.** The README ends with a placeholder. Pick one before sharing publicly.

Before publishing anywhere non-private, note that `mmupassword` remains in git history
(`docker-compose.yml`, `Dockerfile`, `MMU_Handoff_v13.md`, `migrate_add_valence.py`) from
the initial commit. It is a local dev container credential, harmless on a private repo,
worth rewriting or rotating if the history goes public.
