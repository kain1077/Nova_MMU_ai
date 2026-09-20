# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org),
with the caveat that MMU is pre-1.0 — the HTTP API and the graph schema can still change
between minor versions, and will say so here when they do.

## [Unreleased]

Packaged installers for every platform, and an audit pass: two concurrency
bugs with real data loss behind them, one write-amplification fix on the
hottest path, and the removal of code that could not run.

### Added

- **The session bundle stops growing with the artifact backlog.**
  `/creative_outputs/mark_seen` had no caller anywhere in the system. Every artifact
  an idle pass ever wrote stayed `presented_to_user=false` forever, so `/session_bundle`
  re-served the same growing backlog to every conversation, and the ones past its
  `limit=10` were never surfaced at all. On the graph this was found on: 19 artifacts
  queued, 0 ever marked, and an artifact block of 14,681 characters inside a 17,441
  character bundle -- 84% of everything the model read before the first user word, and
  the only part of it without a bound.

  Two halves, and both were needed. Someone has to ring the bell: both bridges now mark
  artifacts seen when `get_session_context` is *called*, not when the bundle is
  prefetched. An `initialize` handshake fires when LM Studio spawns the process, which
  is not the same event as a human reading anything, so a health probe or a window
  opened and closed must not consume the queue. `/session_bundle` returns `surfaced_ids`
  so the caller marks exactly what was rendered -- marking "all unseen" instead would
  burn the artifacts still waiting behind the cap, taking them from unread to
  never-shown-and-flagged-read.

  And the block has to be a digest: three artifacts, title and opening line, capped by
  `MMU_BUNDLE_ARTIFACT_MAX` and `MMU_BUNDLE_ARTIFACT_CHARS`. The crystallization block
  directly below it already caps itself at three on the reasoning that a wall trains the
  reader to scroll past the whole thing; that reasoning always applied here too.

  Measured after the change on a seeded instance: a 950-character bundle where the same
  content produced thousands, with the queue behind the cap intact and drained three at
  a time per conversation.

- **`GET /creative_outputs/{output_id}` and a `read_artifact` MCP tool.** The recovery
  path that makes the digest honest -- cutting the block to openers is only a compression
  if the rest is still reachable, otherwise it is data loss in a nicer shape. Resolves the
  8-character id prefix the bundle prints. An ambiguous prefix returns 404 rather than an
  arbitrary winner, because handing back the wrong artifact silently is worse than
  saying the id was no good.

- **Backend refusals carry the backend's own explanation.** `raise_for_status()`
  reports `400 Client Error: Bad Request for url: .../chat/completions` and discards
  the response body -- and the body is where the server says things like
  `request (8810 tokens) exceeds the available context size (8192 tokens)`. Session
  summaries were placeholders for days and whole cognition passes were dying, both
  with that one uninformative line.

  `LMStudioError` now keeps the status, the message and the token counts when the
  server gives them. Reading them is fiddlier than it should be: llama.cpp-backed
  servers nest a whole JSON document inside the message *string*, behind a prose
  prefix, so the parser has to go looking for the braces rather than testing whether
  the value starts with one.

- **The cognition prompt is sized to the model that is actually loaded.**
  `MMU_IDLE_MODEL` defaults to the placeholder `local-model`, which LM Studio resolves
  to whichever model loaded first. With a 176k model and an 8k model both loaded,
  identical config and an identical prompt would work one hour and fail instantly the
  next -- which is what made these 400s look intermittent and unrelated to size.
  `/idle_prompt`'s 24000-character default is roughly 6000 tokens: comfortable in 176k,
  impossible in 8k once the tool schemas and the reply budget are counted beside it.

  The daemon now asks LM Studio's `/api/v0/models` what is loaded and how much room it
  has, assumes the smallest loaded window when the configured name is the placeholder,
  reserves tokens for the tool schemas (`MMU_TOOL_SCHEMA_RESERVE`) and the reply, and
  passes the result to `/idle_prompt` as `max_chars`. A backend that will not report a
  context length keeps the old default, so nothing changes for one that never had the
  problem. Startup logs the window and names the placeholder, because all of this was
  invisible.

  Models the backend types as embeddings are excluded. The embedding model is loaded
  for most of the server's life -- every recall uses it -- and reports a 2048 window,
  so counting it as a chat candidate collapsed the budget to its floor at every depth:
  a deep pass assembling 2043 characters and calling it context.

- **Overlapping skill proposals are merged instead of offered side by side.**
  Two proposals over the same memory are not two pieces of work. Colour is
  single-valued, so the first to claim a member locks every other cluster containing
  it out permanently. The queue never said so: 26 pending proposals, 24 of which
  shared members with another, presented as a numbered list of things to do.

  Working through that list the way it reads is a loop, and the refusal message
  closed it -- see *Fixed* below.

  `find_skill_candidates` is `MATCH (a)-(b)-(c)`, so every candidate has exactly
  three members; clusters larger than that were not rejected by the design, they were
  unreachable by it. Overlapping triples are now folded back together before they are
  queued, which both gets past three and makes the output disjoint: a cluster is
  grown, emitted, and every remaining candidate still touching it is dropped rather
  than offered, because once that cluster exists those candidates are unconfirmable.

  Growth is bounded three ways because one bound does not hold. An absolute coherence
  floor gets outrun: each admission moves a mean over every pair, so a growing cluster
  ratchets through it without any single step crossing it -- a first attempt merged 35
  triples into one 18-member cluster spanning six domains, never once dropping below
  0.45. The bounds that work are relative to the seed (`MMU_MERGE_COHERENCE_DROP`) plus
  a hard size ceiling (`MMU_MERGE_MAX_MEMBERS`), alongside the floor
  (`MMU_MERGE_COHERENCE_FLOOR`).

  Measured on a seeded graph: 14 raw candidates with 51 overlapping pairs became 2
  disjoint proposals of 5 and 4 members, with zero overlap remaining and the two
  topics kept apart rather than merged into one blob.

- **Proposals report what they exclude.** `excludes` names the other pending proposals
  sharing a member. Only proposal-vs-skill blocking was ever reported, so after an
  uncrystallize both conflicting proposals showed as unblocked, both were attempted,
  and the loop closed.

- **A blocked proposal offers the tree instead of a dead end.** It now names the owning
  routine, lists the members no routine has claimed (`free_members`), and suggests
  crystallizing those with `extends` set to the owner (`suggested_parent`) -- which is
  the outcome the overlap was evidence for.

- **A skill inherits the associations of the cluster it replaced.** Crystallizing
  severed the pathway that identified the cluster. Those memories were grouped BECAUSE
  they kept being recalled alongside things around them; compression then demoted them
  to Blue and -- since crystallized members were excluded from co-recall pairing --
  stopped them accumulating at all, while the Skill got no graph position of its own.
  Its only edges were `HAS_KEYWORD`, `PROCEDURALIZED_FROM` and `EXTENDS_SKILL`. On the
  graph this was found on, 356 co-recall edges worth 855 in total ran from crystallized
  members out to 99 still-live memories, and not one of them could reach the skill
  standing in for those members. Compressing a cluster made it *harder* to reach
  associatively, which is the opposite of the point.

  Each outside memory's summed co-recall weight to any member now becomes one
  `ASSOCIATED_WITH` edge to the Skill -- summed rather than averaged, because a memory
  tied to three members of a cluster is more strongly about it than one tied to a
  single member, and averaging erases exactly that. The projection derives membership
  from `PROCEDURALIZED_FROM` rather than taking it from the caller, so it is also
  correct to re-run after `add_skill_members()` or `remove_skill_members()`: it SETS
  the seed and carries forward whatever has accumulated since, and therefore cannot
  inflate what it repairs.

  `POST /skills/project_associations` backfills skills crystallized before this
  existed. Dry-run by default, like `/index_repair`.

- **The edge is live, and it is the only growth path left.** It cannot run through the
  members -- they are Blue and excluded from pairing, deliberately, so a compressed
  cluster stops thickening its own edges. What does still happen is that a skill is
  DELIVERED in a recall, beside other memories, and that is the same evidence co-recall
  captures between two memories, one level up.

  `GET /skills/growth` reports memories accumulating association without being members,
  with `seeded` separated from `grown`, because only the second is new evidence -- a
  high weight that is entirely seed is just the cluster it already was. Those are
  candidates for `POST /skills/{id}/members`, so the signal now has a mechanism behind
  it rather than being an observation with nowhere to go.

- **Recall can reach a skill through the graph, not only through wording.**
  `match_skills()` takes the addresses the query already matched and asks which skills
  they point at, ranked BESIDE the embedding and keyword paths rather than as a
  tiebreak -- a skill reached because the conversation is demonstrably in its
  neighbourhood is not weaker evidence than one reached by wording, and treating it as
  a tiebreak would leave the co-recall graph decorative.

  Scoring saturates (`w/(w+MMU_ASSOC_REF)`), because association weight has no ceiling
  and dividing by a maximum would let one hot neighbourhood outrank everything reached
  by meaning. A first cut used a reference of 8 and effectively everything saturated:
  a query scored 0.947 by association against semantic matches at 0.86, which is not
  ranking beside but ranking above. Per-edge weights run median 3, p90 15, max 59 on a
  real graph, and a score sums every edge from the memories one query matched, so
  per-query totals land in the tens to low hundreds; at a reference of 40 that same
  query scores 0.78 and the semantic match leads it.

  `MMU_ASSOC_FLOOR` keeps weak associations out for the same reason `min_semantic` is
  high: a matched skill WITHHOLDS its members from the delivered context, so a loose
  match subtracts evidence rather than adding noise.

  Uncrystallizing drops the projection -- it describes a position in the graph that is
  about to stop existing.

### Fixed

- **A direct question is no longer buried under reflections.** `get_creative_outputs()`
  sorts `question_for_user` ahead of everything else precisely so that cannot happen, and
  the bundle's `reversed(unseen)` then put it back at the bottom. The two halves of that
  intent were written in different files and cancelled out.

- **The session summary's retry no longer makes an overflow worse.** It doubled
  `max_tokens` when reasoning ate the answer, which is the right remedy for that failure
  and exactly wrong for this one -- a window that was already too small does not improve
  by reserving more of it for output. Two retries pulling in opposite directions is why
  a session could fail, retry, and fail again inside 20ms.

  An overflow now shrinks the transcript instead (`MMU_SESSION_SUMMARY_SHRINK_TRIES`),
  keeping the head and the tail and dropping the middle the way `/idle_prompt` already
  trims, and the doubling retry is capped against the real window. The system message is
  never trimmed: it carries the JSON shape the reply has to match, and cutting it would
  trade a prompt that does not fit for a reply that cannot be parsed.

  Verified against a real backend: a 10,371-token prompt into an 8,192-token window now
  completes after shrinking, where it previously produced a placebo summary reading
  "A conversation took place."

- **The crystallization refusal no longer recommends the one move that cannot work.**
  It said "either uncrystallize the skill that owns them, or pick a different proposal"
  without naming which skill, so the reader had to guess an id -- and the advice itself
  was the trap. Freeing the members lets exactly one of the overlapping proposals
  succeed, so undoing and retrying only swaps which one refuses. A transcript of the
  resulting loop: crystallize, blocked, notice two proposals share members, conclude
  "uncrystallizing one should free them up for both", uncrystallize, still blocked,
  pick another routine to undo. That reasoning is correct given what it was told.

  The refusal now names the owning routine and its trigger, states plainly that
  overlapping proposals are alternatives, and points at `extends` instead.

- **Proposals a new skill makes impossible are retired.** Only the exactly-matching
  proposal was ever closed on crystallize, so a cluster that merely OVERLAPPED the new
  skill stayed pending forever while being unconfirmable. 22 of 26 pending proposals
  were blocked that way, and 14 had no route at all. That is the queue a reviewer works
  through, hitting refusal after refusal. Retirement now runs on crystallize and again
  on every sweep. A proposal with two or more unclaimed members is deliberately kept --
  it can still be crystallized under the owning routine.

- **The pending-proposal ceiling no longer deadlocks the merge.** It counts pending
  proposals, so a queue full of fragments had no room for the merged cluster that would
  supersede them: the sweep deferred every one and nothing could change
  (`created=0, deferred=4, pending=26` against a ceiling of 25). The ceiling was always
  documented as a bound on unreviewed work rather than on the graph, so a cluster that
  absorbs what is already queued is let through.

- **Rejecting a merged cluster returns the fragments it absorbed.** Otherwise saying no
  to an 8-member cluster silently says no to the 3-member clusters inside it, which
  nobody reviewed -- and nothing would undo it, since `superseded` is not `pending` and
  the sweep's `ON MATCH` leaves those rows alone forever.

- **Neo4j-backed tests no longer skip themselves while Neo4j is running.**
  `neo4j_layer` reads its `NEO4J_*` names into module-level constants at import, so
  whichever test imported it first froze in whatever the environment held then, and a
  helper loading `.env` afterwards could not undo it. Credentials now load at module
  scope, before anything can import it.


- **Skills can grow.** `POST /skills/{id}/members` adds memories to an existing
  skill; `POST /skills/{id}/members/remove` takes them back out and restores their
  pre-skill colour. Crystallization could create a skill and delete one and nothing
  in between: `crystallize_skill()` always CREATEs, and it refuses any memory an
  active skill already owns, so adding a fourth memory to a three-memory skill meant
  `/uncrystallize` followed by `/crystallize`.

  That round trip is a replacement, not a rebuild. It mints a new `skill_id`, resets
  `invocation_count` to zero, discards `created_at`, drops the embedding and keyword
  edges, and cannot run at all while an active child extends the skill -- so the tree
  had to be dismantled first. A skill with seven recorded invocations came back
  claiming none, which meant the honest record of which skills are actually used was
  the price of growing one.

  Measured on the graph this was built against: all twelve skills held exactly three
  members, the `min_cluster` floor. Not one had ever grown past its birth size,
  because nothing could make it.

  Both operations keep the skill's id, history and tree position. Adding takes the
  same `confirmed=true` gate as `/crystallize` and the same `MMU_ALLOW_MODEL_CRYSTALLIZE`
  rule, because it demotes real memories to Blue; removing is not gated the same way,
  since it restores a memory rather than burying one. Removing the last member is
  refused and points at `/uncrystallize` -- a Skill with no root system is still
  matchable and still delivered, with nothing left to trace it back to. A skill can
  rewrite its `trigger`/`procedure` in the same call and is re-indexed when it does,
  dropping the old wording's keywords so it stops matching prompts about text it no
  longer contains.

- **Packaged installers.** Two single-file binaries per platform, built in
  `packaging/`: `mmu-setup` (installer, launcher, doctor) and `mmu-mcp` (the MCP bridge,
  frozen). Neither needs Python installed. Built for Windows, Linux, Intel Mac and Apple
  silicon by `.github/workflows/release.yml`; PyInstaller cannot
  cross-compile, so that is four runners rather than one.

  Docker is still a genuine prerequisite — Neo4j is a JVM database and does not fold into
  an executable — so `mmu-setup` packages *everything around* MMU rather than MMU itself.
  What it removes is the error-prone part of the install:

  - **The embedding dimension is measured, not asked for.** The README's install asks you
    to `curl` your endpoint and count the numbers in the response. That number is baked
    into the Neo4j vector index at creation time, and getting it wrong produces a graph
    that accepts saves and silently never embeds them. `mmu-setup` probes LM Studio,
    Ollama, llama.cpp and vLLM, embeds a test string, counts the vector, and rewrites
    `127.0.0.1` to `host.docker.internal` so the container can actually reach it.
  - **The startup self-check is parsed, not printed.** A dimension mismatch fails the
    install instead of sitting in a log waiting to be grepped.
  - **MCP client config is merged, never replaced**, with a timestamped backup. The
    README warns that most clients replace their config and tells you to paste every
    server by hand; this adds one key and leaves the rest alone.
  - `.env` is generated *inside* `.env.example`, so all 121 lines of explanation survive.
    The generated Neo4j password is alphanumeric on purpose: docker compose expands
    `${...}` using values from `.env`, so a `$` in the password reaches the container
    mangled and presents as a wrong password against a correct-looking file.

  `mmu-setup doctor` checks Docker, project files, `.env`, the endpoint's actual
  dimension against the configured one, the server and every registered client — and
  reports all of them rather than stopping at the first failure.

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

### Changed

- **The workflow actions moved to their Node 24 releases.** `actions/checkout@v4` and
  `actions/setup-python@v5` run on Node 20, which GitHub deprecated in September
  2025. The runner has been force-executing them on Node 24 ever since and
  annotating every run to say so; when that fallback is withdrawn, the workflows
  stop working. Both are now on v7, and `upload-artifact` moves from v4 to v7 for
  the same reason. `download-artifact` deliberately stays on v4 until it can be
  exercised -- it runs only in the `release` job, which no pull request triggers,
  so bumping it here would be an untested change to the one path that has to work
  when a tag is cut.

- **The aging pass writes once per recall, not once per memory.** It runs on
  every `/recall` and walks the whole graph; each address rewrite was its own
  Cypher query and, since `neo4j_session()` opens a session per call, its own
  Bolt session — so a recall on a 500-memory graph could issue 500 sequential
  round trips before returning. `write_color_updates_batch()` applies the pass
  in one `UNWIND`. Three aging passes over a 60-memory fixture went from **104
  round trips to 3**. The collision rule is unchanged and still enforced in the
  database rather than only by the caller's in-memory pre-check.

  Measured against a live instance over real Bolt: ~60 address rewrites per
  recall collapse to one query, and median `/recall` goes from **404 ms to
  326 ms, −20%** on a 60-memory graph (40 measured calls after 10 warm-up,
  replicated on both builds). `read_ms` is unchanged, as it should be — the
  index read path is untouched and the whole saving is in the write phase.

- **Every tool that writes to a graph now refuses the port a real one lives
  on.** `tests/test_mmu.py` got this guard in 0.1.2, after a bare `pytest` aged
  four of someone's real memories. The other two tools that write were never
  given it: `mmu_recall_speed_test.py` defaulted to 8765, and
  `Extra/test_client.py` **hardcoded** 8765 with no override at all while
  pinning memories, saving memories, and firing a deliberate burst of unrelated
  recalls to demonstrate aging. Both now default to 8766 and refuse 8765
  without an explicit opt-in.

### Fixed

- **Crystallizing a cluster now actually relieves the recall bias it was built to
  relieve.** The roadmap's stated purpose is that a hot path *converts* into a Routine
  "rather than accumulating recall-weight without bound forever," and the README says the
  un-biasing is worth more than the compression. Nothing was wired to either claim.

  `write_recall_edges()` paired Blue like any other colour -- the comment read *"Include
  if Green/Yellow/Blue"* -- and it runs from `mmu.recall()` **before** the endpoint knows
  which skills matched, so the substitution was invisible to the graph no matter what
  recall delivered. `bump_corecall()`, the index's mirror of those edges, did the same.
  A crystallized cluster went on thickening its own CO_RECALLED edges on every recall,
  exactly as if it had never been compressed. On the graph this was found on, **two of
  the three highest-degree hubs were crystallized members**, at 36 and 35 connections.

  Members are now excluded from co-recall *pairing* in both tiers. They are not excluded
  from recall: a member is still a direct keyword hit and still surfaces. It just stops
  making its own cluster denser every time it does. `RECALLED_IN` is still written --
  "this surfaced during this session" stays true, and it is episodic history rather than
  the weight that biases retrieval. The Skill records the delivery through
  `invocation_count`, which is where that signal belongs.

- **A crystallized memory no longer climbs back out of compression on its first keyword
  hit.** `_age_memories()` promotes Blue to Yellow when a memory is recalled, which is
  right for an archived memory -- warm, just back from the archive -- and wrong for a
  Routine member, which is Blue for an entirely different reason. The tier-1 keyword gate
  has no colour filter, so members are recalled directly and were promoted straight back
  into shortcut expansion and co-recall pairing. The graph this was found on reported 36
  members against 34 Blue: two had already leaked out.

  Colour could not carry the distinction, so the v2 index card gained a `skill_member`
  flag, set by every crystallize path and cleared by every reverse. Members are now
  skipped by the aging pass entirely rather than having their colour pinned, which also
  stops their addresses being rewritten -- and that matters more than it looks, because a
  Routine's member list *is* a list of addresses, and rewriting them underneath it is what
  makes `/crystallize` report members that "matched no memory".

  `POST /index_repair` reconciles the flag as a third kind of drift, `MISFILED`, alongside
  `MISSING` and `PHANTOM`. Every member crystallized before the flag existed is in that
  state, so this is a repair rather than an assumption of a fresh graph. It refuses to
  unflag anything when the graph cannot be read, rather than treating "could not ask" as
  "no members".

  `write_recall_edges()` also stopped asking Neo4j for each recalled address separately --
  it was one round trip per address on the path that runs on every recall. Colour and
  membership now come back in a single query.

- **CI ran no tests on a pull request that changed only code or tests.** The
  suite was a job inside `release.yml`, whose `pull_request` trigger is scoped
  by a paths filter listing what goes into a binary -- `packaging/**`, the
  Dockerfile, `requirements.txt`. That list never mentioned `mmu_server.py`,
  `neo4j_layer.py`, `light_index_v2.py` or `tests/**`, so a server-only or
  test-only PR matched nothing, ran nothing, and displayed an empty check list
  while doing it -- which reads as "nothing to check" rather than "nothing was
  checked". The suite now lives in its own `tests.yml` with no paths filter at
  all, and `release.yml` calls it so a tag build is still gated on it. The
  four-runner binary matrix stays path-scoped; that scoping was right for an
  expensive job and only wrong as a gate on a fifteen-second one.

- **`/health` reported an empty graph as zero memories.** `get_neo4j_stats()`
  chained three `MATCH` clauses through `WITH`, and such a chain yields no rows
  at all if any single link matches nothing -- the `CO_RECALLED` link matches
  nothing until the first recall creates an edge. A graph holding 60 memories
  and 110 keywords reported `{"status": "connected", "memories": 0,
  "keywords": 0}`. Wrong on precisely the graphs whose state is hardest to
  confirm another way: a fresh instance, or one restored from a dump before
  any recall. It also silently defeated any tool that reads graph size from
  `/health`. Three independent `COUNT {}` subqueries now, so an empty pattern
  contributes 0 instead of erasing the other two.

- **One un-encodable character could eat a whole idle daemon log line.**
  Windows gives stdout the ANSI code page whenever it is not a real console --
  piped, redirected, or run under a service wrapper -- and memories routinely
  carry characters cp1252 cannot encode; the physics notes alone bring
  increment signs, square roots and minus signs. `logging` does not degrade on
  an encode failure, it prints a `UnicodeEncodeError` traceback *instead of*
  the line, so a single such character lost the entire message. The file
  handler already pinned utf-8; stdout now gets the same guarantee, applied to
  the stream rather than the handler so `--show-prompt`, which prints the
  assembled prompt directly, is covered by the same fix.

- **The MCP bridge works when frozen.** `mmu_mcp_server.py` located `.env` relative to
  `__file__`, which under PyInstaller points into a temporary extraction directory that
  is recreated at every launch — so a frozen bridge silently saw none of `.env`, exactly
  the failure the loader was written to fix. It now looks beside its own executable and
  then in the platform install directory. The staleness check had the same root cause and
  now watches the executable, which restores the detection rather than merely silencing
  it.

- **Uninstalling one MMU no longer unregisters another.** MCP client config is global
  while an install is not, so tearing down a second checkout removed the entry pointing at
  the first. Removal is now gated on the entry actually launching the project being
  removed; anything else is reported and left alone. Found by doing it.

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

- **`mmu_validate.py compare` crashed on every Windows console.** It printed
  U+2192 into cp1252 and died with `UnicodeEncodeError` before emitting a
  single number. `snapshot` never reaches that line, so the tool looked healthy
  right up to the point its output was needed. Authored and tested on Linux,
  where the default encoding hid it. Output is ASCII now.

- **Five of the six new regression tests never ran anywhere.**
  `pytest.importorskip("mmu_server")` skipped silently when `fastapi` was
  absent from the host — which is normal, since it is a container dependency.
  That covered both concurrency regressions and all three batched-aging
  regressions: the tests written for this very branch. Skips print beside
  passes, so the suite read as green. The skip now names the missing dependency
  and the command that fixes it, and `tests/conftest.py` repeats it in the
  pytest report header, which collection-time output capture cannot swallow.
  With the dependency present the suite goes from **64 to 70 passing**.

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
