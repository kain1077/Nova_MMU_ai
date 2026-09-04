# Phase 11 — Knowledge Seeding (Document Ingestion) — DEPLOY REPORT

Status: **deployed and validated** on the development machine, 2026-08-30.
Design doc: `C:\MMU\phase_packages\phase11_knowledge_seeding.md`
Prior phase: `C:\MMU\phase_packages\phase9_deploy_report.md`
Report author: Claude Code (real terminal + Docker access; all output below is actual).

**Final state: 954 memories (93 conversational + 861 document chunks), 954 embedded,
0 missing.** The phase was validated on a single paper (127 chunks); the user then elected
to ingest the full five-paper corpus — see §9.1.

---

## 1. Scope, as agreed with the user

| Item | Decision |
|---|---|
| GRP assignment | Caller-specified. No auto-classifier in `mmu_server.py`. Confirmed: the AI model already selects GRP with code guidance and this has not been a problem. |
| Voice transcription | **Deferred.** Not built. The user: "would rather get the code completed before adding extras." |
| Image ingestion | **Deferred.** The user wants to plan image creation/storage separately so it does not slow down memory. |
| Voice + images long-term | Expected to live in a **separate repository**, not this one. |
| First document | `Entropic_Relativity_TheoryvFin.pdf` — chosen because it is one of the three papers NOT already represented in the graph, so a recall hit provably comes from ingestion. |
| Rollout strategy | Ingest one paper, measure, then decide. Measured (§5.7), then the remaining four were ingested (§9.1). |

`SOURCE_LABELS[5] = "Voice Note"` was added as a reserved label only, so the number
cannot be claimed by something else later. Nothing uses it.

---

## 2. Pre-flight findings (measured, not assumed)

### 2.1 The corpus
`C:\Users\<you>\Documents\Theory\Fin` — five papers, all text-based, no OCR needed:

| Paper | Pages | Words | Est. chunks @300w |
|---|---:|---:|---:|
| Dimensional Relativity | 67 | 24,180 | 90 |
| Entropic Relativity | 78 | 27,839 | 103 |
| Fractional Relativity | 114 | 41,995 | 156 |
| Geometric Relativity | 114 | 50,170 | 186 |
| Universal Relativity | 96 | 39,350 | 146 |
| **All five** | | | **681** |

**Scale warning raised before building:** the existing graph was 93 memories. One paper
produces 90–186 chunks; all five would be 681, outnumbering curated conversational
memories roughly 7:1. This is why only one paper was ingested for validation. The real
totals came in ~30% higher than these estimates (§9.1).

### 2.2 Extractor choice: pdfplumber, decided by test
The design doc said to test `pypdf` vs `pdfplumber` against a real paper rather than
assume. On the same page of Entropic Relativity:

```
PYPDF:       ...the fracturedwybridge... thewzbridge... One-WaywzParticipation...
PDFPLUMBER:  ...the fractured wy bridge... the wz bridge... One-Way wz Participation...
```

pypdf runs subscripts into adjacent words. `fracturedwybridge` is unrecoverable — the
keyword gate can never match it, and it cannot be reliably split back apart.
pdfplumber preserves word boundaries at ~7x the cost per page (0.15s vs 0.02s), which
is ~12s for a whole paper and irrelevant for a one-time ingest.

**pdfplumber, on evidence.** It does emit `(cid:N)` for unmapped glyphs; those are
stripped in `clean_text()`.

---

## 3. What was built

### New file: `ingest.py`
Separate module so chunking is unit-testable without FastAPI or Neo4j, and so chunk
quality can be inspected before anything is committed to the graph. It does **not**
write to the graph.

| Function | Purpose |
|---|---|
| `clean_text()` | strips `(cid:N)`, dot leaders, de-hyphenates across line breaks |
| `extract_pdf()` | pdfplumber, page-by-page, skips front matter |
| `extract_plain()` / `load_document()` | markdown/text, dispatch |
| `chunk_text()` / `chunk_document()` | paragraph-aware chunking with overlap and line tracking |
| `extract_keywords()` | frequency-ranked, reuses `light_index_v2.tokenize()` |

**Keyword extraction reuses the existing tokenizer** rather than inventing a second
one. The gate that later retrieves these memories applies the same stopword list and
stemming rules, so a private tokenizer would drift out of agreement with it.

**`INGEST_STOPWORDS` is applied only here**, never to `light_index_v2.STOPWORDS`.
Academic prose is dense in connectives (`thus`, `where`, `since`, `respectively`) that
the gate's general-purpose list never needed to cover. Editing the shared list would
have altered gate behaviour for every existing memory in the graph.

### `mmu_server.py`
| Addition | Purpose |
|---|---|
| `IngestIn` model + `POST /ingest` | document ingestion, with `dry_run` |
| `_resolve_doc_path()` | confines `source_path` under `MMU_DOC_ROOT` |
| `SOURCE_LABELS[5]` | reserved "Voice Note" |
| document aging exemption | §6.3 |
| semantic tiebreak + ranked output order | §6.2 |

`/ingest` calls `MMUCore.add_memory()` once per chunk. No part of the existing write
path is reimplemented, and because Phase 9 moved embedding into `add_memory()`,
chunks are embedded automatically with no extra work.

### `Dockerfile` / `docker-compose.yml`
- `pdfplumber` added to the pip install list; `COPY ingest.py .`
- `C:/Users/<you>/Documents/Theory:/docs:ro` — **read-only on purpose**: ingestion
  reads originals and must never be able to modify them.
- `MMU_DOC_ROOT=/docs`

---

## 4. Security: path confinement

`/ingest` takes a caller-supplied path, which without confinement is an
arbitrary-file-read primitive against the container filesystem. `_resolve_doc_path()`
resolves and confines under `DOC_ROOT`. Verified:

```
POST {"source_path":"../../etc/passwd"}     -> 400 "must resolve inside /docs (got /etc/passwd)"
POST {"source_path":"/app/mmu_server.py"}   -> 400 "must resolve inside /docs (got /app/mmu_server.py)"
POST {"source_path":"Fin/nope.pdf"}         -> 404 "file not found: /docs/Fin/nope.pdf"
```

---

## 5. Deploy and validation — actual output

### 5.1 Dependencies importable in the RUNNING container
```
$ docker compose build --no-cache mmu-server && docker compose up -d --force-recreate mmu-server
$ docker exec mmu-memory-server python -c "import pdfplumber, ingest; ..."
pdfplumber 0.11.10
ingest module OK
chunk_text True

$ docker exec mmu-memory-server grep -n "def ingest_document\|def chunk_text" /app/mmu_server.py /app/ingest.py
/app/mmu_server.py:898:def ingest_document(...)
/app/ingest.py:182:def chunk_text(...)

$ docker exec mmu-memory-server ls /docs/Fin/
Dimensional_RelativityvFin.pdf  Entropic_Relativity_TheoryvFin.pdf  ... (5 files)
```

### 5.2 Chunk quality was reviewed BEFORE writing
First dry run at 300 words exposed three problems, all fixed before any commit:

| Problem | Before | After |
|---|---|---|
| Table-of-contents sludge (dot leaders) | chunk 3 was TOC | front-matter pages skipped |
| Page furniture | 21 chunks <50 words, min 4 | 7 chunks, min 42 |
| Junk keywords | `any, not, one, two` | `curvature, frt, drt, push-back` |
| Total chunks | 153 | 127 |

Final dry run: **127 chunks, 26,797 of 27,839 words retained (96%)**, 2.9s. Samples
shown to the user for approval before ingesting.

### 5.3 The ingest
```
{ "status": "ingested", "source": "Entropic_Relativity_TheoryvFin.pdf",
  "grp_code": 602, "pages": 78, "chunks": 127,
  "written": 127, "embedded": 127, "failed": 0,
  "elapsed_ms": 27394.2, "ms_per_chunk": 215.7 }
```

**127/127 written, 127/127 embedded, 0 failed.** ~216ms per chunk. Only ~21ms of that
is the embedding call; the remaining ~195ms is `add_memory()` overhead — a
`get_memory_count()` round trip, `bake_similar()`, and a full `v2_index.save()` per
chunk. Acceptable here (27s) but it is O(n) work per write, so a 681-chunk full-corpus
ingest would be several minutes and grow super-linearly.

### 5.4 Provenance is traceable to the real source
```
$ MATCH (m:Memory) WHERE m.src_type=2 AND m.src_chunk IN [0,63,126] ...
chunk, page, addr
0,   5,  "094.005.602.000,000~000|2.000.005"   "Abstract The four preceding papers..."
63,  40, "157.005.602.000,000~000|2.063.040"   "Proof ? Part (i): Uniqueness from trihedral..."
126, 78, "220.005.602.000,000~000|2.126.078"   "servables from the five pre-locked..."
```

Spot-checked chunk 63 against the actual PDF — page 40 of
`Entropic_Relativity_TheoryvFin.pdf` begins:

> "Proof — Part (i): Uniqueness from trihedral geometry (bottom-up) / A stable
> three-packet baryon in the trihedral corner requires phase-locked junctions across
> exactly two orthogonal planes."

Exact match. `src_type.src_chunk.src_line` in the address reads as
`2.063.040` = Document, chunk 63, page 40.

**This required a fix (see §6.1).** As first written, `src_line` held a line offset
within the page, which meant 107 of 127 chunks recorded `src_line=1` and the page
number was stored nowhere — provenance that could not actually be traced.

### 5.5 `/insights` source breakdown
```
source_distribution: {'Conversation': 19, 'AI-Self': 68, 'Document': 127, 'Background Cognition': 6}
```

### 5.6 Recall tests
**Verbatim** (`"phase-locked junctions trihedral baryon"`) — page 40, the exact chunk,
ranked first among documents:
```
via=pinned  sem=0.715  (ambient)
via=pinned  sem=0.712  (ambient)
DOC via=direct sem=0.867 pg=040  "Proof - Part (i): Uniqueness from trihedral geometry..."
DOC via=direct sem=0.859 pg=056
DOC via=direct sem=0.857 pg=069
```

**Paraphrase** (`"why does a signal slow down when it travels close to a heavy star"` —
shares no keywords with "Shapiro delay") — returns the Shapiro derivation:
```
DOC via=direct sem=0.832 pg=037  "= b2 +x2, the excess coordinate travel time is 2GM dx 2GM..."
```

Ingested content is genuinely findable, by exact term and by paraphrase.

### 5.7 Dilution of conversational recall — measured, and mild
The pre-ingest fear was that document chunks would crowd out curated memories. Measured
across the standing probe set, it largely did not happen:

```
PROBE                                    pre-ingest    post-ingest
Star Wars                                3 conv        3 conv + 0 doc
Dimensional Relativity Theory paper      5 conv        3 conv + 0 doc  (2 slots to pins)
Castlevania Symphony of the Night        3 conv        3 conv + 0 doc
what is the user's game design focus         4 conv        4 conv + 0 doc
tell me about my pet                     3 conv        2 conv + 1 doc
who am I married to                      3 conv        3 conv + 0 doc
```

Everyday vocabulary does not stem-match physics prose, so conversational queries are
mostly unaffected. Only one probe lost a slot to a document chunk. With
`skip_pinned=true` (what the MCP bridge uses after `session_bundle`) the pinned slots
are freed and coverage is full.

---

## 6. Issues found and fixed

### 6.1 `src_line` recorded an unusable locator (FIXED)
Line numbers restart per page, so "line 1" described **107 of 127 chunks**, and the
page number was not stored anywhere — chunk 63 could not be traced to page 40. This
failed the acceptance criterion outright.

**Fix:** for PDFs `src_line` now carries the **page number**; markdown/text still use
the real document-wide `start_line`. The design doc anticipated this ("page number can
inform `src_line`"). The 127 already-written memories were deleted and re-ingested.

That deletion also validated the Phase 9 `delete_memory()` fix at volume: 127 deletes,
Neo4j and the v2 index both returned to exactly 93.

### 6.2 The keyword gate could not tell 50 tied chunks apart (FIXED)
This is Phase 9 §7.5 (all `direct` hits score exactly `W_DIRECT = 1.00`), which was
harmless at 93 curated memories and became actively wrong at 220. A physics query now
ties 50+ rows at 1.00, so the passage that actually answers the prompt was no more
likely to surface than any other. Observed directly: the verbatim query returned pages
56/68/74 and **not** page 40, the chunk that literally contains the queried phrase.

Three changes, in the order they were needed:

1. **Semantic tiebreak.** `semantic_score` breaks ties among equal keyword scores.
   It is a tiebreak, not a filter — primary ordering is still the keyword score, so a
   semantic signal can reorder equals but can never promote a weak keyword hit above a
   strong one. `address` is the final key, which also makes recall **deterministic**.
2. **Pinned protected.** The first version let a document chunk with a better embedding
   push Red pins out of the results entirely. Red pins are ambient context and are keyed
   ahead of everything else rather than competing on semantic score.
3. **Wide candidate window.** The tiebreak can only reorder what `rank()` returns, and
   `rank()` cuts ties arbitrarily — so with a narrow window page 40 never reached the
   tiebreak at all. The window is now `min(max(top_k*10, 50), 200)`.

Result: page 40 ranks first, and two consecutive identical runs of the full probe set
are now **byte-identical** where they previously churned.

**A blunt floor was tried first and rejected on evidence.** `MMU_SEMANTIC_FLOOR=0.78`
did surface the right chunks, but it also dropped memory `020` ("...married to Sam...
Major interests include Star Wars") from the query *"Star Wars"* — a memory that
literally names the thing asked about. Short conversational memories score lower against
short queries than long dense document chunks do, so a single global threshold cannot
serve both populations. The floor remains available and stays at **0.0**; the tiebreak
achieves the goal without discarding anything.

### 6.3 Recall output was never in ranked order (FIXED — pre-existing)
`fetch_payloads()` returns rows in Neo4j's order, and `_v2_recall()` iterated that
directly. The `top_k` cut was correct but the order handed to Nova was arbitrary — the
best match could appear last in `context_block`. Ranked order is now re-imposed after
hydration.

### 6.4 Documents would have archived themselves into dormancy (FIXED)
The most consequential finding of this phase. `_age_memories()` archives any memory not
recalled within `ARCHIVE_THRESH=20` recalls. Most chunks of a paper are never directly
recalled, so within ~20 recalls of ordinary use the **entire ingested document turns
Blue** — and Blue is excluded from CO_RECALLED expansion *and* from Phase 9 semantic
search. The paraphrase retrieval that justified ingesting the paper would have quietly
stopped working.

Not a bug introduced here; an interaction between Phase 11 and the pre-existing colour
matrix that could not be seen until documents existed.

**Fix (the user's decision): `src_type == 2` is skipped in `_age_memories()`,** the same way
Red already is. The colour matrix models episodic decay, which is the right model for
conversational memories and the wrong one for reference material — a paper does not
become less true because nobody asked about it. Verified by running 25 recalls, past the
threshold of 20:

```
kind            color    n
Conversational  Blue     91     <- aged normally, as designed
Conversational  Red       2
Document        Green   126     <- exempt, still reachable by semantic search
Document        Yellow    1
```

Side benefit: document addresses stop being rewritten, so the `CHUNK.LINE` provenance
encoded in the address is now a stable identifier.

### 6.5 Found but NOT fixed: Session nodes do not mean "conversation"

Surfaced on 2026-08-30 while drafting the retrieval-control design
(`maybe_retrieval_control.md`), not during Phase 11 itself. Recorded here because it is
a live defect in shipped behaviour and should not sit only inside a deferred document.

`POST /recall` (`mmu_server.py:927`) mints a fresh UUID on **every call**:

```python
mmu._current_session_id = str(uuid.uuid4())
```

So `write_recall_edges()` attaches every recall's `RECALLED_IN` edges to a brand-new
Session node. Verified against the live graph: 164 Session nodes exist, and the most
common memories-per-session counts are 5, 3, 8, 4 and 2 -- those are `top_k` values, not
conversations.

Consequences:
- A Session node is a *recall event*, not a conversation, despite Phase 8's
  `/session_resume` and `get_session_resume()` (`neo4j_layer.py:459`) reading as though
  it were a conversation.
- The real conversation identifier, the `X-MMU-Session` header minted per LM Studio
  conversation, is captured by `_touch_activity()` (`mmu_server.py:173`) and used by
  `write_happened_in()` for memories *saved* -- but never for memories *recalled*.
- Anything that wants "what did this conversation touch" cannot get it from the graph
  today.

Not fixed here because the fix changes what a Session node means and would leave 164
existing per-recall Session nodes as historical noise -- that is the user's call, not a
silent cleanup. The minimal change is to accept `X-MMU-Session` on `/recall` (it is
already accepted on `/remember` and `/rate`) and pass it to `write_recall_edges()`,
falling back to a generated id when the header is absent so direct curl calls still work.

### 6.6 Known limitation, not fixed: glued words on justified lines
pdfplumber occasionally joins words on justified/kerned lines
(`eventistheactofcollapsingthewave`). It affects payload readability, not retrieval —
each chunk still yields plenty of clean tokens, as the keyword lists show. Fixing it
would need a word-splitting dependency and carries its own false-positive risk. Flagged
rather than papered over.

### 6.7 Still open, from Phase 9: CON numbers are not unique
`add_memory()` derives `con` from `get_memory_count() + 1`, which collides after
deletions. Each restore run in §7 hit exactly one collision and resolved it by assigning
a distinct `use`. Bulk ingestion makes this more likely, not less.

---

## 7. Graph aging incidents during validation

Validation recalls archived the user's conversational memories **twice** — roughly 45
recalls in the Phase 9 pass and ~28 in this one, against `ARCHIVE_THRESH=20`. Both were
caused by this validation work, not by pre-existing state, and both were remediated by
resetting Blue to Green with `use=0, arc=0` in both stores (valence preserved,
embeddings intact through the address rewrite).

The restore script lives at
`scratchpad/restore_blue.py`; it dry-runs by default, detects address collisions against
the `memory_addr` UNIQUE constraint before writing, and backs up the index file.

**Root cause worth carrying forward:** aging is driven by *recall count*, not elapsed
time. Twenty recalls archive a memory whether they happen over three months or twenty
minutes, so any validation-heavy session ages the whole graph. `MMU_ARCHIVE_THRESH` was
deliberately left at 20 (the user declined raising it). Future phases doing heavy recall
testing should either raise it for the duration or budget the recall count explicitly.

Documents are now immune to this (§6.4); conversational memories are not.

---

## 8. Acceptance checklist

- [x] New dependencies added to Dockerfile and confirmed importable **in the running
      container** (`pdfplumber 0.11.10`), not just "pip install succeeded"
- [x] A real document ingested end to end; chunk count and samples reported for review
      **before** committing (127 chunks, samples approved)
- [x] `src_chunk`/`src_line` populated correctly and traceable to source — spot-checked
      chunk 63 against page 40 of the actual PDF (required the §6.1 fix)
- [x] GRP assignment caller-specified; no auto-classification added to `mmu_server.py`
- [x] Keyword extraction reuses `light_index_v2.tokenize()`, not a new tokenizer
- [x] Recall confirms ingested content is findable — verbatim **and** paraphrase (§5.6)
- [ ] Voice transcription — **deliberately deferred**, not built this pass
- [x] `--no-cache` rebuild + running-container verification performed, output shown

---

## 9. Where this leaves the roadmap

Roadmap order is **9 → 11 → 10 → 12+13**. Phase 9 and Phase 11 are done; **Phase 10
(proactive memory: pattern detection, temporal tags, anticipation) is next.**

### 9.1 Full corpus ingested (2026-08-30, after the initial report)

The user elected to ingest the remaining four papers. All succeeded, zero failures:

```
Dimensional  114 chunks   28.1s   246.8 ms/chunk
Fractional   208 chunks   59.1s   284.3 ms/chunk
Geometric    220 chunks   76.0s   345.6 ms/chunk
Universal    192 chunks   74.9s   390.0 ms/chunk
TOTAL -> 954 memories, 954 embedded, 0 missing, in 4.0 min
```

Final composition: **861 document chunks + 93 conversational memories** (~9.3:1).
Actual chunk counts ran ~30% above the word-count estimates in §2.1, because per-page
chunking yields more boundaries than a flat word division predicts.

Recall verified at the new scale: `"Star Wars"` returns purely conversational hits with
no document contamination, and `"how does entropy relate to the cosmological constant"`
correctly blends four document chunks (sem 0.887–0.842) with a relevant conversational
memory (0.839). The §6.4 aging exemption is holding: 860 of 861 document chunks Green.

**Two scaling trends now on record:**
- Write cost grew 247 → 390 ms/chunk across the run, since `bake_similar()` is O(n) in
  graph size — ~58% slower at 4.3× the memories. Another 1000 chunks lands near 500–600ms.
- Recall latency grew from ~30ms to ~130ms, from scoring a wider candidate pool against
  a much larger tied set.

Both are comfortable today. Neither is free at the next order of magnitude.

**Decisions the user still owns:**
1. **Whether `bake_similar()` needs a bulk path.** It is the dominant per-write cost and
   grows with graph size; an `embed=False`-style deferral is the equivalent optimization.
2. **Retrieval control** — scoped recall ("semantic zoom" over GRP domains / src_type),
   an emotion sieve over the Phase 6.5 valence fields, and a working-memory buffer.
   Requested by the model during testing, discussed 2026-08-30, and proposed as a small
   phase ahead of Phase 10. Note the sieve ships inert: only 3 of 284 memories carried a
   valence rating at the time of that discussion, so the constraint is rating volume,
   not code.

**Useful for Phase 10:** pattern detection now has a much denser graph to work against,
which was the roadmap's stated reason for sequencing 10 after 11. Document chunks carry
`src_type=2` and a stable address, so temporal/cluster analysis can distinguish
reference material from lived conversational memory without heuristics.

Backups: `mmu_server.py.pre11.bak`, `Dockerfile.pre11.bak`,
`docker-compose.yml.pre11.bak`, plus the Phase 9 `.pre9*.bak` set.
