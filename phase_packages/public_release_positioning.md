# Positioning and Known Limitations

Status: **shipped.** All roadmap phases are complete, the shareable-release stage was
validated by a from-scratch second install, and the security work described below landed
before this repository was made public.

Written 2026-08-30; revised for the public release 2026-09-04.

This is the honest read on what MMU is, what is genuinely novel about it, and where it
falls short. If you are deciding whether MMU is worth your time, start here rather than
with the feature list — the limitations are as load-bearing as the features.

---

## What MMU is

> **MMU is a persistent memory system for language models that runs entirely on your own
> machine — it remembers across conversations, retrieves by meaning as well as by keyword,
> and lets memories fade or strengthen the way real ones do.**

Some things it deliberately does **not** claim:

- **Not "AI memory" in the general sense.** That framing is vague and collapses immediately
  into "so how is this different from RAG?" The specific claims below are the interesting
  ones.
- **Not "like a human brain."** There are real biological analogues in the design —
  consolidation, decay, procedural crystallization — and they are interesting. Leading with
  the metaphor oversells them and invites the assumption that there is nothing underneath.
- **Nothing about AGI, consciousness, or self-awareness.** The design lets a model maintain
  a self-model across sessions. That is a property of the memory architecture, not a claim
  about machine minds.

---

## What is actually differentiated

Worth being clear-eyed about, because the comparison question is a fair one.

### Genuinely distinctive

- **Memories decay on two axes.** Archiving requires sustained disuse **and** real elapsed
  time. Most memory systems either keep everything forever or evict by recency/LRU. The
  Green → Yellow → Blue gradient with a pre-hold state is a real design decision with real
  consequences — and there is a concrete reason count alone fails: a busy afternoon will
  archive the entire graph. That happened here, twice, during validation runs. The
  before/after is in `phase10_deploy_report.md`.
- **Two-tier retrieval.** An O(1) keyword gate answers most queries in milliseconds; the
  embedding layer is a *fallback and a tiebreaker*, not the primary path. Most comparable
  systems are vector-first. This one is measurably faster on the common case, and the
  numbers are in `phase9_deploy_report.md`.
- **Provenance in the address.** `CON.PRI.GRP.USE,ARC~VAL|SRC.CHUNK.LINE` encodes identity,
  domain, usage, valence and source location in the key itself. Unusual, and it makes
  "where did this come from" answerable: chunk 63 → page 40, verifiable against the source
  PDF.
- **Two-way memory.** The model writes its own memories unprompted, not only the user's.
- **Human confirmation as architecture.** Crystallization *cannot* be triggered by the idle
  daemon. The propose/confirm split is structural rather than conventional: the system
  restructures its own memory only with a human in the loop.

### Table stakes

These are table stakes, not selling points, and are listed so nobody has to go looking:

- Chunking and embedding documents. Every RAG system does this.
- Vector similarity search. Ditto.
- MCP integration. Increasingly expected.
- Running against local models.

---

## Known limitations

Every one of these is defensible as built. None of them survives being oversold, so they
are stated plainly.

| Area | Real state |
|---|---|
| **Skill crystallization** | Built and validated, but **has never crystallized anything** — no cluster in the development graph meets the bar yet. The mechanism is tested; it has not fired in anger. |
| **Temporal patterns** | Implemented, and needs weeks of real usage before it can detect anything. It is collecting data, not yet producing findings. |
| **Emotion / valence** | Schema is live; **3 of ~960 memories are rated.** Effectively inert. Treat it as a direction, not a feature. |
| **Scale** | Tested to roughly 1,000 memories, on one machine, by one person. It has not been load-tested. |
| **Multi-user** | Single-user by design: in-process state, no tenancy. This is a positioning choice rather than an omission — see Security below for what that implies. |
| **Platform** | Developed and validated on Windows with Docker Desktop. Nothing in it is Windows-specific, but the Linux and macOS paths have not been exercised. |

---

## Security posture

MMU began as a personal tool on a single machine, and its original defaults reflected
that. Three issues were found and fixed before this repository was published; they are
documented here rather than quietly patched, because the reasoning matters more than the
diff.

**The REST API had no authentication and CORS was wide open.** With
`allow_origins=["*"]` and no auth on `/recall`, `/remember`, `/export` or `/forget_all`,
any website loaded in a browser on the same machine could have read or destroyed the
entire memory graph. The wildcard origin made responses *readable*, so this was
exfiltration rather than blind writes. This is a well-known class of vulnerability in
"local-only, no auth" tools, and it was the right thing to find before shipping.

Now: CORS defaults to **no** permitted origins (`MMU_CORS_ORIGINS` is empty unless you set
it), both services bind to `127.0.0.1` by default via `MMU_BIND`, and an optional
shared-secret header is available through `MMU_API_KEY`. The compose file no longer
publishes on all interfaces.

**A development password was reachable from the repository.** The published history begins
at the public release for exactly this reason. Rotate `NEO4J_PASS` for your own instance
regardless — [Changing your database password](../README.md#changing-your-database-password)
covers it.

**There was no license.** Now AGPL-3.0; see [LICENSE](../LICENSE).

What remains true by design: MMU is single-user and local-first. There is no tenancy model
and no per-user isolation. Do not expose it to a network you do not control.

---

## How it compares

The reasonable question is *"how is this different from mem0 / Letta / Zep / LangChain
memory?"* Honestly:

> Those are mostly memory-as-a-service, or a memory layer inside a larger framework. MMU is
> a standalone local system with an opinion about *forgetting* — memories decay on both use
> count and elapsed time, and the keyword gate handles the common case so the vector layer
> acts as a fallback rather than the primary path. It is also single-user and local-first by
> design, which is a narrower target than most of those projects aim at.

That landscape moves quickly. If you are comparing seriously, check the current state of
those projects rather than trusting a snapshot written here.

---

## Where to look first

If you are evaluating MMU, these are the things most worth your attention, roughly in
order of how much they tell you:

1. **Paraphrase recall.** Asking *"who am I married to"* surfaces "married to Sam" even
   though "married" was never a keyword. It shows exactly what the embedding layer buys.
2. **The aging incident.** Validation runs archived 61 memories twice, which is why
   archiving now requires both a use count and elapsed time. A real bug, a real fix, and a
   real before/after table in `phase10_deploy_report.md`.
3. **Document provenance.** Ingest a paper, recall a passage, confirm that chunk 63 maps to
   page 40, and open the PDF to check.
4. **The tie-break fix.** Seventeen memories scoring exactly 1.0, with semantic similarity
   breaking the tie so the right one surfaces. `phase9_deploy_report.md`.
5. **`/skill_candidates` returning nothing.** Counterintuitive, but a system that declines
   to manufacture a result tells you something about how it was built.

---

## The honest framing

This is a carefully built personal system with an unusually complete development record,
tested by one person on one machine, with real known limitations. That is a genuinely
useful thing to publish — and it is a *different* thing from a production-ready product.

It is presented as what it is. The engineering stands on its own.
