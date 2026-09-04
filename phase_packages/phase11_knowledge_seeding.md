# Phase 11 -- Knowledge Seeding + Multi-Modal Input

Status: ready to build, and simpler than it first looks because the address schema
was already designed with this in mind (`SRC_TYPE.CHUNK.LINE` exists specifically for
tracing a memory back to where in a source document it came from).

Sequenced after Phase 9 in the recommended order because ingested documents are only
as useful as recall's ability to surface them later, and Phase 9's semantic fallback
is what makes a long paper's content findable by paraphrase rather than exact keyword
match. Phase 11 does not require Phase 9 to be done first technically, but validating
Phase 11 is much more convincing once semantic recall exists to prove the ingested
content is actually reachable.

---

## Copy this to Claude Code to start

```
I'm ready to build Phase 11 of the MMU project (knowledge seeding: document ingestion,
chunking, and GRP assignment; voice note transcription; structured data ingestion).
Full design doc is at C:\MMU\phase_packages\phase11_knowledge_seeding.md -- read it in
full first. It documents exact file/line anchors in the current codebase, a scope
decision I've already made and explained (search for "SCOPE DECISION"), the schema and
code changes to make, new dependencies to verify are installable in the Docker image,
and a deploy + validation sequence to run yourself with real terminal access. If Phase
9 (semantic embeddings) is already deployed, use a real semantic recall check as part
of your validation that ingested content is actually findable, not just stored.
```

---

## Goal

`POST /ingest` for documents (PDF, Markdown, plain text) with chunking and GRP
assignment. Voice note transcription. Structured data ingestion. The roadmap also
lists "image description memory," see SCOPE DECISION below for why that's being
descoped from this phase's new-code surface.

## Current state (read this before changing anything)

- `MMUCore.add_memory()` in `mmu_server.py` (~line 236) is the exact primitive this
  phase needs: it already takes `keywords, payload, priority, color, src_type,
  src_chunk, src_line, note, grp` and does the full write (Neo4j + v2 index +
  `bake_similar`). Ingestion should call this once per chunk. Do not reimplement any
  part of what `add_memory()` already does.
- `SOURCE_LABELS` in `mmu_server.py` (~line 78) has `2: "Document"` already reserved
  and unused elsewhere -- this phase is what it was reserved for. Add `5: "Voice
  Note"` for transcribed audio. Do not renumber the existing 0-4, other code depends
  on those exact values.
- The address schema's `SRC_TYPE.CHUNK.LINE` suffix (see `_gen_addr`/`_parse_addr`,
  ~lines 208-232) is designed exactly for provenance: which document, which chunk of
  it, which line it started at. Use `src_chunk` as the chunk's position within the
  document (0-indexed) and `src_line` as its starting line number in the source file.
  This is what lets the user or Nova later ask "where did this come from" and get a real
  answer.
- The Dockerfile installs a minimal dependency set (`fastapi uvicorn networkx pydantic
  neo4j`). No PDF/document parsing library is present yet -- you'll need to add one.
- GRP taxonomy is the 3-digit namespace documented in `MMU_Handoff_v13.md` (1xx
  Project through 9xx Misc). Ingested research papers most likely land in 602
  (Academic Papers) under 6xx Research; this is exactly the workflow the "Paper 1 /
  Paper 2" reading sessions from earlier in the project were already doing manually.

## SCOPE DECISION: GRP assignment is caller-specified, not auto-classified

Auto-classifying a chunk's GRP domain from its content is a judgment call (which of
nine domains does this paragraph belong to), not a mechanical operation. That's real
cognition, and the project has held a deliberate line so far that `mmu_server.py`
never calls a model, only `mmu_idle_daemon.py` does, specifically to keep the REST
server debuggable and model-agnostic. Unlike Phase 9's embedding call (which is a
narrow, defensible exception because vector math isn't reasoning), GRP classification
really is reasoning, so it should NOT become a third exception to that boundary.

Resolution: `/ingest` takes a single `grp_code` parameter for the whole document (the
caller already knows a physics paper is 602, a work document is 7xx, and so on --
this mirrors exactly how the user already classifies memories she saves conversationally
today). For structured data ingestion where rows may span multiple domains, accept an
optional per-row `grp_code` override so the caller can be as granular as the source
data warrants, but the mechanism stays "caller tells us," never "the server guesses."

If a genuinely automatic classifier is wanted later, that belongs in the idle daemon
(a new idle-pass task: "here are N chunks with no confirmed GRP, suggest one each"),
consistent with how the rest of the system's cognition already lives in that process.
Don't build that now unless the user asks for it -- it's speculative scope, not part of
what the roadmap actually describes.

## SCOPE DECISION: image description memory is descoped from new server code

Turning an image into a text memory requires interpreting what's in the image, which
is cognition, not mechanics, same reasoning as the GRP point above but sharper --
there's no "caller already knows the answer" shortcut for an image the way there is
for a document's GRP domain. The practical path that already exists end to end: when
Nova is shown an image during a live conversation (if the loaded chat model is
vision-capable) or the user describes one to her, she already has `save_memory` available
and can just use it, same as any other conversational memory. That requires zero new
server code.

Build a dedicated `/ingest/image` endpoint only if the user specifically wants a workflow
where he can batch-drop image files somewhere and have them described and filed
without an active conversation -- and if he does, that endpoint's job is only to run a
local vision model and hand the output to the same `/ingest` text pipeline below, not
to invent a separate ingestion path. Treat this as an explicit follow-up to ask about,
not silent scope creep into this phase.

## Implementation plan

### 1. New dependencies (Docker image)

Add to the Dockerfile's pip install list:
- `pypdf` (or `pdfplumber` if you find its text extraction meaningfully better on a
  real sample PDF -- test against one of the user's actual papers, don't assume)
- Nothing extra needed for Markdown/plain text, stdlib handles those
- For voice transcription: a local Whisper implementation (`faster-whisper` is the
  practical choice, CPU-friendly, no separate server process needed). This is a
  meaningfully heavier dependency than anything else in this Dockerfile, confirm it
  actually installs and loads a model within the container's resource limits before
  committing to it -- if it doesn't fit comfortably, running it as a script on the
  Windows host (same pattern as `mmu_idle_daemon.py`) rather than inside the
  `mmu-server` container is a reasonable fallback, note in your report which you used
  and why.

### 2. Chunking

Add a `chunk_text(text, target_words=300, overlap_words=30)` helper (new module,
`ingest.py`, or a section in `mmu_server.py`, your call, but keep it separable and
unit-testable). Split on paragraph boundaries first, then merge/split to hit the
target size, keeping a small overlap between consecutive chunks so a fact split
across a chunk boundary isn't lost. 300 words is a starting point, not gospel --
existing memories in the graph tend to be short (a sentence to a short paragraph);
if a real ingested paper produces chunks that feel too dense to be useful individual
memories once you see them in the graph, say so and propose a smaller target rather
than shipping a number that looked reasonable on paper.

Track line numbers as you chunk (for `src_line`) by counting newlines consumed, not
by re-scanning the whole document per chunk.

### 3. Keyword extraction per chunk

Reuse the same tokenizer already in the codebase (`tokenize()` in `light_index_v2.py`,
or `MMUCore._tokenize()` in `mmu_server.py`) to pull candidate keywords out of each
chunk rather than inventing a new extraction method -- consistency here matters
because the keyword gate that later retrieves these memories uses the exact same
stemming rules. A simple approach: take the most frequent non-stopword stems/words in
the chunk, cap at 6-8 per memory (matching the existing `save_memory` tool schema's
own guidance of 3-8 keywords).

### 4. `POST /ingest` endpoint (`mmu_server.py`)

```
IngestIn:
  source_path: str          # path to the file, or raw text if source_type == "text"
  source_type: str           # "pdf" | "markdown" | "text"
  grp_code: int               # whole-document default, per SCOPE DECISION above
  priority: int = 5
  title_note: Optional[str]  # goes into each chunk's `note` field for context
```

Flow: load the file by type, extract plain text (page-by-page for PDF so page number
can inform `src_line` if a page-level line count isn't available), run it through
`chunk_text()`, extract keywords per chunk, then call `add_memory()` once per chunk
with `src_type=2, src_chunk=<index>, src_line=<starting line>, grp=grp_code`. Return
a summary: how many chunks written, their addresses, total processing time. Do not
silently truncate a huge document -- if it produces more chunks than seems reasonable
to write in one request (say, over a few hundred), log the true count and consider
whether the endpoint needs to become async/background rather than a single blocking
request; report this back rather than quietly capping it.

### 5. Structured data ingestion

Same endpoint, `source_type: "structured"`, expecting the source to already be
tabular (CSV or JSON records). Each row becomes one memory (no paragraph chunking
needed, the row already IS the atomic unit), `src_chunk` is the row index, `src_line`
can mirror `src_chunk` since there's no meaningful line concept for structured data.
Support the optional per-row `grp_code` override described in the SCOPE DECISION
above.

### 6. Voice note transcription

A small standalone script (`mmu_ingest_voice.py`, host-side like the idle daemon, or
containerized depending on what your dependency check in step 1 concludes) that
transcribes an audio file with Whisper, then POSTs the resulting text to `/ingest`
with `source_type: "text"` and `src_type=5`. Keep transcription and ingestion as two
separable steps (transcribe, then feed the existing pipeline) rather than merging
them into one path -- if the transcript needs a manual cleanup pass before filing,
this separation lets that happen without touching the ingestion code at all.

## Deploy sequence (run these yourself, you have real terminal access)

1. Add the new pip dependencies to the Dockerfile, rebuild with `--no-cache`.
2. Verify the running container actually has the new libraries importable:
   `docker exec mmu-memory-server python -c "import pypdf; print('ok')"` (and
   whichever others you added).
3. `docker compose up -d --force-recreate mmu-server`, then the same
   grep-the-running-container verification used in every phase so far:
   `docker exec mmu-memory-server grep -n "def ingest\|chunk_text" /app/mmu_server.py`
4. Pick a real document the user cares about (one of the physics papers referenced
   elsewhere in this project is a good candidate, ask him which) and ingest it for
   real. Report the chunk count, a sample of 2-3 actual chunk payloads so the user can
   sanity-check chunk quality, and the addresses written.
5. Recall test: query for a phrase that appears verbatim in the ingested document
   (should hit via the keyword gate) and, if Phase 9 is deployed, a paraphrased query
   that doesn't share keywords (should hit via semantic fallback). Report both.
6. Confirm the new memories show up correctly in `/insights`' source-type breakdown
   with the right label ("Document" or "Voice Note").

## Acceptance checklist

- [ ] New dependencies added to Dockerfile and confirmed importable in the running
      container (not just "pip install succeeded during build")
- [ ] A real document from the user ingested end to end, chunk count and sample chunks
      reported for his review
- [ ] `src_chunk`/`src_line` are populated correctly and traceable back to the source
      (spot-check at least one chunk against the actual source file)
- [ ] GRP assignment follows the caller-specified model, no auto-classification added
      to `mmu_server.py`
- [ ] Keyword extraction reuses the existing tokenizer, not a new independent one
- [ ] Recall test confirms ingested content is actually findable, not just stored
- [ ] Voice transcription tested against at least one real short audio clip if that
      piece was built this pass
- [ ] `--no-cache` rebuild + running-container verification actually performed and
      shown in the report
