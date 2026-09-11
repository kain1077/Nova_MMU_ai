# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org),
with the caveat that MMU is pre-1.0 — the HTTP API and the graph schema can still change
between minor versions, and will say so here when they do.

## [Unreleased]

An audit pass: two concurrency bugs with real data loss behind them, one
write-amplification fix on the hottest path, and the removal of code that
could not run.

### Fixed

- **Concurrent requests could corrupt the v2 index, and did.** Every MMU
  endpoint is a sync `def`, so Starlette runs it in a worker threadpool and two
  requests genuinely overlap — and the idle daemon polls every 30 seconds
  against the same server a conversation is using. There was not one lock in
  the codebase. `json.dump()` walking a live index dict raised "dictionary
  changed size during iteration", and `save()` built its temp path as a fixed
  `self.path + ".tmp"`, so two savers wrote into one file and both renamed the
  result. A stress run of four writers and four readers put **172 cards on disk
  out of 909 added**. The index is the read path, so a lost card is a memory
  that no longer exists as far as recall is concerned. `LightIndexV2` now takes
  a reentrant lock, readers included — a torn read of a half-applied rename is
  the same silent disappearance — and saves to a pid- and thread-unique temp
  file.

- **Per-request state leaked between concurrent recalls.** The session id, the
  aging pass's rename map, and the read path and timing were plain attributes
  on the one shared `MMUCore`. Each is written during a request and read later
  in that same request, so an overlapping recall overwrote them in between: in
  a six-thread test, four threads read a fifth's session id. The damaging case
  is the rename map, because `/recall` rewrites its result addresses through it
  and those addresses go straight back to `/rate` and seed anticipation — both
  miss silently when the address is wrong. Now `threading.local`, behind
  properties that keep every call site unchanged.

- **A `docker run` without compose wrote a v1-named index.** The Dockerfile set
  `MMU_INDEX_PATH=/data/memory_index.json`; only compose's override made it the
  v2 path. It also set `MMU_ARCHIVE_THRESH=10`, contradicting both compose and
  `.env.example` at 20.

### Changed

- **The aging pass writes once per recall, not once per memory.** It runs on
  every `/recall` and walks the whole graph; each address rewrite was its own
  Cypher query and, since `neo4j_session()` opens a session per call, its own
  Bolt session — so a recall on a 500-memory graph could issue 500 sequential
  round trips before returning. `write_color_updates_batch()` applies the pass
  in one `UNWIND`. Three aging passes over a 60-memory fixture went from **104
  round trips to 3**. The collision rule is unchanged and still enforced in the
  database rather than only by the caller's in-memory pre-check.

- **Every tool that writes to a graph now refuses the port a real one lives
  on.** `tests/test_mmu.py` got this guard in 0.1.2, after a bare `pytest` aged
  four of someone's real memories. The other two tools that write were never
  given it: `mmu_recall_speed_test.py` defaulted to 8765, and
  `Extra/test_client.py` **hardcoded** 8765 with no override at all while
  pinning memories, saving memories, and firing a deliberate burst of unrelated
  recalls to demonstrate aging. Both now default to 8766 and refuse 8765
  without an explicit opt-in.

### Added

- **`mmu_validate.py`** — captures a JSON snapshot of a running instance
  (health, index-vs-graph agreement, embedding coverage, recall latency) and
  diffs two of them, so "I optimized it and nothing broke" is a measurement
  rather than a claim. Calls `/index_repair` in dry-run only: a tool that
  repairs while it observes cannot be trusted to report what it found. Refuses
  8765 and also 7474, which is not an MMU endpoint at all — it is the Neo4j
  browser, and it grants full database access.

- **Six regression tests** covering index corruption under concurrency,
  per-request state isolation, aging round-trip count, index/graph agreement
  across aging passes with duplicate CONs forcing real collisions, and an
  unreachable graph leaving the index untouched. All fail against the previous
  commit.

### Removed

- **`neo4j_backfill.py`** and **`Extra/graph_viz.py`** — 445 lines that both
  `import sqlite3` and read `memory_system.db`, a store removed in Phase 3.
  Neither could run. Same reasoning that removed `Extra/light_index_v2.py` in
  0.1.2.

- **Four dead Dockerfile `ENV` lines.** `NEO4J_READS` and `MMU_USE_V2_INDEX`
  are read by no code in the repository; `MMU_DB_PATH` pointed at the removed
  SQLite file; `MMU_V2_INDEX_PATH` is a host-side migration script's variable,
  not one the server reads.

### Known, not changed

- **`/recall` mints a fresh session id per call**, so every recall MERGEs a new
  `:Session` node that nothing ever reaps, and `RECALLED_IN` cannot group a
  conversation. The MCP bridge already mints a stable per-conversation id and
  the server's middleware already captures it; `mmu_recall()` is also the one
  bridge call that does not send it. Left alone deliberately for now — the
  bridge describes `RECALLED_IN` as per-query bookkeeping, so changing it is a
  design decision rather than a bug fix.

## [0.1.2] — 2026-09-07

Second pass on outside review. Four issues filed by @kumilange; two of them turned out to
have already been fixed by pushing the local history, which is its own finding — see below.

### Changed

- **"Skill" is now "Routine"** on every surface a human or a model reads
  ([#2](https://github.com/kain1077/Nova_MMU_ai/issues/2)). The old name collided with
  Agent Skills — the `SKILL.md` files a person writes to instruct a model — and since MMU
  is an MCP server usually attached to a model that also has those, both meanings landed
  in one context window. They are near-opposites: an Agent Skill is authored and lives in
  a file, a Routine is emergent and lives only as a graph node. "Routine" also describes
  the node better, being literally a trigger paired with a procedure.

  MCP tools renamed: `review_skills` → `review_routines`, `crystallize_skill` →
  `crystallize_routine`, `link_skill` → `link_routine`, `unlink_skill` →
  `unlink_routine`, `uncrystallize_skill` → `uncrystallize_routine`. **Update your MCP
  client config if you had these tools enabled.** README and `mmu_review.py` output
  follow.

  **The HTTP API and the graph are unchanged** — `/skills`, `/skill_proposals`,
  `skill_id`, the `:Skill` label. Renaming the wire format would break every stored URL
  and need a graph migration for a cosmetic gain. `review_routines` says so in its own
  description, so a model that sees `skill_id` come back knows what it is.

### Added

- **Three more Mermaid diagrams in the README**
  ([#1](https://github.com/kain1077/Nova_MMU_ai/issues/1)): recall flow, remember/ingest
  flow, and a deployment view, alongside the existing memory-lifecycle diagram. The
  recall diagram makes explicit something the prose never said: the semantic stage runs
  **only when the keyword gate finds nothing topical**, so semantic recall fills gaps
  rather than competing with keyword hits. All four are parse-checked against Mermaid
  itself, not eyeballed.

- **LongMemEval benchmark harness** in `bench/`. Runs MMU against two baselines —
  BM25 and flat vector search over the same embeddings with the graph switched off —
  over identical haystacks with identical scoring. Reports retrieval recall@k, which
  needs no model and is exactly reproducible, separately from QA accuracy, which
  depends on a reader and a judge and is not comparable across runs.

  The flat-vector baseline is the one that matters: the gap between it and MMU is what
  the graph layer is worth, and if there is no gap that gets published too.

  The harness erases the graph it runs against, so it refuses to touch one that
  hasn't been explicitly marked disposable — port 8765 is refused outright, a graph
  with no sentinel memory is refused, and a graph over 200 memories with no sentinel
  is refused regardless. 22 tests cover the loader, scoring and the guards, none of
  which need a running service. Not yet run against the real dataset.

### Fixed

- **Aging no longer drops a memory out of the read path when it rewrites an address.**
  `use` is encoded into the address by `_gen_addr`, so incrementing it changes the
  address, and the v2 index is keyed by address. Two memories that share a `con` —
  duplicates left over from the old `count() + 1` numbering — converge on one address
  as their `use` counters climb, because `use` clamps at `MMU_ARCHIVE_THRESH`. When
  they collided, `LightIndexV2.rename()` resolved it with
  `cache[new] = cache.pop(old)`, destroying the occupant's card, while Neo4j rejected
  the identical rename against the `UNIQUE` constraint on `m.address` and
  `write_color_update()` swallowed the exception. Two memories in, one card out — and
  the evicted one stayed in the graph, counted by `/health` and by every Cypher query,
  and invisible to recall, because the index *is* the read path.

  Three changes, at the two seams that were wrong:

  - `LightIndexV2.rename()` returns a bool and refuses two cases it used to perform:
    a target that is already occupied, and a source address that has no card. The
    second one used to move the gate's keyword pointers anyway, leaving live terms
    aimed at an address holding no card.
  - `write_color_update()` and `write_addr_rename()` return **the address the node
    actually carries afterwards**, and detect the collision with an `OPTIONAL MATCH`
    instead of leaving it to the constraint. The colour change still lands; only the
    address is held back.
  - `_age_memories()` and `rate_memory()` check the index before asking the graph to
    move anything, and then follow *what landed* rather than what they asked for. A
    held address costs one `use` increment — a decay counter, not a timestamp — and
    says so in the log, once per contested pair rather than once per `/recall`.

  Found by `POST /index_repair` on a 1,009-memory graph reporting
  `missing_count: 1, phantom_count: 0`. That asymmetry is the signature: a removal
  with no matching add is not ordinary drift. It is very likely the same mechanism
  behind the node `index_repair`'s own docstring describes as having sat in that state
  since August. Existing drift is not repaired by this change — run
  `POST /index_repair?apply=true` once to reinstate anything already lost. Regression
  tests cover the collision, the cardless-source case, the uncontested rename, and an
  end-to-end `use` increment that asserts `index_total == neo4j_total` afterwards.

- **`POST /index_repair` no longer calls an empty graph a 503.**
  `get_index_source_rows()` returned `[]` both when Neo4j could not be read and when
  it was read and held nothing, and `index_repair` treated the falsy result as a
  failure. An empty graph is exactly what a fresh second instance is, so the drift
  tests failed on the setup the README tells people to use. The two cases are now
  distinct: `None` for unreadable, `[]` for empty.

- **The live tests no longer default to the production port.** `MMU_TEST_BASE`
  defaulted to `http://127.0.0.1:8765`, so a bare `pytest tests/` on a machine running
  MMU normally silently exercised the user's own graph — writing memories and aging
  every memory each `/recall` didn't return. It now defaults to 8766, and aiming the
  live tests at 8765 is refused before any request unless `MMU_TEST_ALLOW_PRODUCTION=1`
  is set. Found by walking into it: four memories went Green → Yellow before anyone
  noticed.

### Removed

- **`Extra/light_index_v2.py`** ([#3](https://github.com/kain1077/Nova_MMU_ai/issues/3)),
  a stale duplicate of the root module missing `touch()` and the `touched_at` migration.
  Nothing imported it. The broader `migrations/` `tools/` `dev/` reorganisation in that
  issue is declined for now: `mmu_mcp_server.py`'s path sits in every user's MCP config,
  the `dev/` part is already moot since the PowerShell scripts were deleted in v0.1.1,
  and `Extra/` is five files.

### Fixed by publishing, not by patching

Both of these were real on GitHub and never real in the local repository. The GitHub repo
was created separately and had files *uploaded* into it, and the upload skipped dotfiles —
so `.env.example`, `.gitignore` and `documents/.gitkeep` existed locally, were correctly
tracked, and were simply absent from the published tree. Pushing the actual history fixed
all three at once.

- **`.env.example` is back** ([#4](https://github.com/kain1077/Nova_MMU_ai/issues/4)) —
  the install instructions referenced a file the published repo did not contain.
- **`.gitignore` is back, and `tests/__pycache__/` is gone** (#3). Without the
  `.gitignore`, a fresh clone was one `git add -A` away from committing its own `.env`.
- **`documents/.gitkeep` is back** — nobody filed this one, but without it the
  `${MMU_DOC_HOST:-./documents}:/docs:ro` mount had no directory to bind.

## [0.1.1] — 2026-09-07

Response to the project's first outside review. Most of this is the reviewer's list, acted
on rather than argued with; the two items that got an argument instead of a patch — the
choice of Neo4j over SQLite, and the absence of benchmarks — are now answered in the
README rather than left to be inferred.

### Fixed

- **CON numbers are no longer reused after a deletion.** `get_next_con()` derived the next
  address from `max(existing) + 1`, which is unique at write time but not stable over
  time: deleting the highest-numbered memory handed that same number to the next write, so
  anything outside MMU holding the old CON silently pointed at a different memory. The
  number now comes from a monotonic `:Counter` node that is never recomputed from the
  population. Existing graphs seed the counter from their current maximum on first run, so
  nothing is renumbered. Collisions that predate the fix remain: they can be found by
  grouping on the CON segment of the address, but not repaired, since nothing records
  which memory held the number first.

### Changed

- **Host scripts are cross-platform.** `mmu_recall_speed_test.ps1` is now
  `mmu_recall_speed_test.py` — standard library only, no PowerShell, and it runs the same
  on Windows, macOS and Linux. Its default probe set is generic rather than being the
  author's own memories, and it now refuses to exceed `MMU_ARCHIVE_THRESH` recall calls
  without `--force`, because a large burst can archive conversational memories it didn't
  return.
- **Hardcoded `C:\mmu` paths removed** from `neo4j_backfill.py`, `migrate_add_valence.py`
  and `Extra/graph_viz.py`. Defaults now resolve relative to the repo, overridable through
  the same environment variables as before.
- **`NEO4J_PASS` is required, not defaulted.** `neo4j_backfill.py` and
  `migrate_add_valence.py` fell back to `mmupassword` when the variable was unset, which
  meant they quietly tried a publicly-known password against whatever was listening.

### Added

- `CONTRIBUTING.md`, issue templates, and a PR template.
- **Why Neo4j and not SQLite** and **Benchmarks** sections in the README — the first
  answers a design question that deserves an answer rather than a shrug; the second states
  plainly that no retrieval-quality benchmark exists yet.
- Platform notes throughout the README: `python3` vs `python` for MCP config, PowerShell's
  lack of an inline `VAR=value` prefix, and `uv` install instructions for macOS and Linux
  alongside the Windows one.

### Removed

- `mmu_phase66_validation.ps1`, `mmu_phase8_deploy.ps1`, `mmu_rate_diagnostic.ps1` —
  single-machine deploy runbooks tied to specific past phases, hardcoded to `C:\mmu` and a
  specific Python build. They documented this project's history, not its use.

## [0.1.0] — 2026-09-06

First public release, and the state the first outside review was written against.
Everything below predates the changelog and is summarized rather than itemized.

### Added

- **Graph-backed memory** on Neo4j: `Memory`, `Keyword`, `Source`, `Session` nodes with
  `CO_RECALLED` weights that build up between memories retrieved together.
- **Two-path recall.** A fast keyword gate (`light_index_v2.py`) answers most queries;
  semantic vector search handles the rest, with the two blended by a configurable weight.
- **Aging and colour state.** Memories strengthen with use and fade without it; unused
  ones archive after a threshold.
- **MCP server** (`mmu_mcp_server.py`) exposing `get_session_context`, `recall_memory`,
  `save_memory` and `rate_memory` to any MCP-capable client.
- **Document ingestion** (`/ingest`) confined to a read-only mount, with a mandatory
  dry-run path that reports chunk counts before anything is written.
- **Optional web ingestion**, off by default, with `src_type=3` marking enforced
  server-side so a web-sourced memory cannot be stored as though you'd said it.
- **Skill crystallization** (Phases 12–13.2): dense memory clusters are nominated as
  proposals; confirming one is a human decision that demotes its member memories into a
  `Skill`. Reversible with `/skills/{id}/uncrystallize`.
- **Idle daemon** (`mmu_idle_daemon.py`), optional, host-side — the only component that
  talks to a chat model.
- **`POST /index_repair`** for repairing drift between the keyword index and the graph.
- Self-check on every server start, reporting Neo4j connectivity, vector index state, and
  configured-vs-actual embedding dimension.

[0.1.2]: https://github.com/kain1077/Nova_MMU_ai/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/kain1077/Nova_MMU_ai/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kain1077/Nova_MMU_ai/releases/tag/v0.1.0
