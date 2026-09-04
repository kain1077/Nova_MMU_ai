# Phase 13.1 -- Crystallization Repair

Status: SHIPPED. Not a new capability. Phase 12 and 13 built the whole
crystallization machine correctly and then could not reach it -- three separate
scoring decisions, each defensible in isolation, combined to make skill formation
structurally impossible.

Found by asking a plain question of a running instance: 999 memories, 724 co-recall
edges, and zero `Skill` nodes ever formed.

---

## Symptom

An operator noticed the model kept returning to one subject and suspected a skill was
failing to form. The instance reported:

```
/health          999 memories, 724 CO_RECALLED edges
/skills          {"skills": [], "count": 0}
/skill_candidates 2 candidates, both from the same GRP domain
```

Zero skills is a legitimate answer on a young graph. It is not a legitimate answer on
a graph with 724 co-recall edges and months of history. The candidate finder was
returning almost nothing, and what it did return came from one corner of the graph.

---

## Root causes

Three bugs, all the same shape: **a selection decision made on raw or absolute
numbers that belong to only one population of the graph.**

### 1. Documents were structurally excluded

`find_skill_candidates()` filtered `AND a.src_type <> 2 AND b.src_type <> 2 AND
c.src_type <> 2`.

That filter is correct in `get_anticipated_context()`, where it is documented: *"a
paragraph of a physics paper is not a thing to be reminded of."* Proactive surfacing
should not interrupt anyone with reference material.

Crystallization is the opposite operation. It compresses. A cluster of reference
chunks that are always recalled together is the single best case for compression
there is. On the instance where this was found, documents were 861 of 999 memories --
so 86% of the graph, including all of the largest domain, was permanently ineligible.

The consequence is not just "no skills." Because that corpus could never compress, it
stayed as hundreds of individual memories competing in every recall, which is a
retrieval bias with a structural cause. The operator felt the bias before anyone
found the filter.

### 2. Weights were normalized against the graph maximum

```python
rec = s.run("MATCH ()-[r:CO_RECALLED]-() RETURN max(r.weight) AS mx").single()
floor = float(min_pairwise_norm) * max_w
```

The maximum is one edge. On the instance measured:

| Source pair | Edges | Avg weight | Max weight |
|---|---|---|---|
| Conversation <-> Conversation | 67 | 7.36 | **34** |
| Document <-> Document | 222 | 2.26 | **9** |
| AI-Self <-> AI-Self | 151 | 2.95 | 14 |

The floor was `0.6 x 34 = 20.4`. The document population's strongest internal edge was
9 -- less than half the bar. Every population was being measured with a yardstick
borrowed from the hottest one, so even removing bug (1) would have changed nothing.

Worse, it was **anti-scaling**. Every recall of the hottest pair raised the floor for
every other cluster in the graph. The system became less able to crystallize the more
it was used, which is precisely backwards.

### 3. Ranking truncated on raw weight before scoring

```cypher
ORDER BY avg_weight DESC
LIMIT $lim
```

Even with (1) and (2) fixed, the query took the top `limit * 3` triangles by *raw*
weight and only then scored them in Python. Raw weight is dominated by whichever
source class is recalled most, so the hottest domain filled every slot and no document
cluster ever reached the scoring step. Bug (2) one layer down.

### And the previews disagreed with all of it

Three call sites had drifted into three different queries with three different scoring
formulas:

| Path | Shape | Weight floor | Source filter | Colour filter |
|---|---|---|---|---|
| `find_skill_candidates()` | mutual-density triangles | `0.6 x max` | excluded documents | excluded Blue |
| `get_insights()` | single hub + neighbours | `> 0.35` | none | none |
| `get_idle_context()` | single hub + neighbours | none at all | none | none |

So `/insights` reported a top candidate at `skill_score 0.7957` -- above the
documented "propose at >= 0.70" bar -- that `/skill_candidates` was structurally
incapable of ever returning. The system advertised a skill it could not build.

---

## The fix

**Documents are eligible.** The `src_type <> 2` filter is removed from
`find_skill_candidates()` and stays exactly where it was in
`get_anticipated_context()`. Every candidate now reports `src_mix` so a reviewer can
see what a cluster is made of.

**Normalize against a percentile, not the maximum.** `skill_weight_floor()` uses
`percentileCont(r.weight, 0.90)`, which describes the top of the real distribution,
barely moves when one pair gets hammered, and stays stable as the graph grows. An
absolute floor (`SKILL_MIN_ABS_WEIGHT = 3.0`) sits underneath it so a young or sparse
graph still cannot manufacture a candidate out of noise. `skill_score` is clamped to
0-1, which finally makes the documented 0.70 threshold mean something.

**Rank on the score, in the query.** Coherence and the normalized weight are computed
in Cypher and `ORDER BY skill_score DESC` runs before the `LIMIT`.

**Cap per-domain contribution.** `max_per_domain=2` stops one domain owning the whole
queue -- otherwise a graph that is 87% one subject proposes only that subject forever,
which is the retrieval bias reproducing itself in the review queue. The cap never
costs coverage: if it would return fewer than `limit`, the leftovers are filled back
in by score order.

**One implementation.** `get_insights()` and `get_idle_context()` now call
`find_skill_candidates()`. A preview of a decision previews the actual decision.

**Report the bar.** `/skill_candidates` returns `weight_floor`, `reference_weight`,
and `floor_set_by`, so an empty list can be read as evidence instead of a shrug. A
bare count is how a structural exclusion stayed invisible for an entire phase.

---

## The proposal queue

The human gate on `crystallize_skill()` was never the problem and does not move.
What it lacked was a doorbell: `find_skill_candidates()` was read-only and safe to
poll, but a poll nobody runs proposes nothing. Nothing in the daemon referenced skills
at all.

A `SkillProposal` is a durable, deduplicated note that a cluster looked ready.

```
GET  /skill_proposals                      the review queue
POST /skill_proposals/sweep                queue what currently qualifies
POST /skill_proposals/{id}/reject          decline, permanently
POST /crystallize                          unchanged -- still the only write path
```

`mmu_idle_daemon.py` calls the sweep at the end of each pass. It writes
`SkillProposal` nodes and nothing else: no Memory is modified, no Skill is created, no
memory is demoted. That is what makes it safe to run unattended, and it is the only
part of Phase 12/13 that is.

**`/crystallize` is still absent from `IDLE_TOOLS` and still requires
`confirmed=true`.** The queue changes who does the noticing, not who does the
deciding.

### An MMU address cannot identify anything durable

Found while validating the queue: the first build keyed proposals on member
addresses, and a sweep after two recalls queued the same clusters again under
new keys.

An MMU address encodes the `use` and `arc` counters, and `mmu_server` rewrites
it **in place** (`SET m.address = $new`) every time a memory is recalled or
ages. The same memory is `012.005.202.015` today and `012.005.202.017` after two
recalls -- one node, new name.

So an address is a *current description*, not an identity. Anything that
outlives the moment must key on `created_at`, which is written once at save and
never updated (verified unique across all 1000 nodes on the instance measured).

This has a sharper edge than duplicate proposals. `crystallize_skill()` matches
members with `WHERE m.address IN $addrs`. A proposal that stored addresses would
have quietly become uncrystallizable as soon as any member was recalled --
failing at the confirm step, on the one path in the system that is hardest to
debug because it is only ever exercised by a human. `get_skill_proposals()` now
resolves members live from `created_at` and reports `members_missing`, so a
reviewer is always handed addresses that will actually match.

Properties worth keeping if this is ever rewritten:

- **Idempotent.** A cluster that is still dense refreshes its existing proposal
  rather than queuing a second copy. Member identity is an order-independent hash, so
  the same three memories in any order are the same proposal.
- **Rejection is permanent.** A sweep that could quietly undo a rejection would make
  the judgement pointless. A rejected proposal is skipped, not re-offered.
- **Crystallizing closes the loop.** `/crystallize` marks a matching proposal
  `crystallized` so the sweep stops proposing a cluster that is now a skill.

---

## Result on the instance where this was found

Before -- 2 candidates, both from one domain, no skill reachable:

```
0.72 | grps [202, 202, 404]
0.68 | grps [202, 404, 404]
```

After -- 10 candidates across six domains, including the large document corpus that
had been excluded outright, in 0.19s:

```
1.000 | dom 2 | grps [202, 202, 202] | 1x AI-Self, 2x Conversation
1.000 | dom 6 | grps [601, 601, 602] | 3x AI-Self
1.000 | dom 6 | grps [601, 604, 602] | 2x AI-Self, 1x Document
0.861 | dom 5 | grps [503, 502, 504] | 1x Conversation, 2x AI-Self
0.833 | dom 8 | grps [801, 805, 502] | 3x AI-Self
0.833 | dom 1 | grps [101, 101, 202] | 3x Conversation
...
```

Thresholds now reported alongside:

```json
{"min_pairwise_norm": 0.6, "weight_floor": 3.6, "reference_weight": 6.0,
 "reference": "p90 of CO_RECALLED weight", "floor_set_by": "percentile"}
```

---

## The queue nothing could see

Shipped, populated, and invisible. `mmu_mcp_server.py` exposed four tools --
`get_session_context`, `recall_memory`, `save_memory`, `rate_memory` -- and
contained zero references to skills or crystallization. It never exposed
`/skills` or `/skill_candidates` either, in any earlier phase. The session
bundle surfaced unseen `CreativeOutput` artifacts; proposals are not artifacts,
so they did not appear. The one place candidates *did* render was the idle
prompt, consumed only by a daemon that was not running.

So the model could not see a proposal, could not be asked about one, and had no
tool that would return one. Only `curl` could read the queue.

Two channels now close that:

**`review_skills` (MCP, read-only).** Lists pending proposals with their score
breakdown, members, and previews. It cannot crystallize, reject, or sweep. The
tool description states that crystallizing demotes memories and needs human
confirmation obtained elsewhere.

**Session bundle injection.** Up to three pending proposals ride in front of
every conversation, next to where artifacts already appear.

The wording of that block matters more than the plumbing. It is written as a
prompt to *look*, not a recommendation to accept:

> Crystallizing one of these compresses its members into a Skill and DEMOTES
> those memories to Blue. [...] If one looks right to you, say which memories
> would be demoted and why the compression is worth it -- do not ask for
> approval as though it were a formality.

If the model picks the cluster, drafts the procedure, asks "shall I?", and gets
a yes, the human gate has become a rubber stamp on the model's own reasoning.
The gate is only worth having if the reviewer sees what is about to be demoted,
so the members are named in the block and the model is told to argue rather than
to prompt. The real review case makes this concrete: the highest-scoring
non-physics cluster was three near-duplicate identity facts, and crystallizing
it would have demoted core biographical memory to the colour the recall gate
treats as inactive.

---

## Ranking quality, after a real review

Two weaknesses only visible once a human read an actual queue.

### GRP coherence is filing, not meaning

A candidate scored `grp_coherence` 1.0 on nothing but arithmetic: game design,
the assistant's gender identity, and the user's self-description are all filed
under 5xx. The GRP code says where something was filed. It was being asked to
stand in for what something is about.

Every memory already carries an embedding, and Neo4j exposes
`vector.similarity.cosine`. Mean pairwise cosine across a cluster answers the
question directly. Neo4j normalizes cosine to `[0,1]` with 0.5 = orthogonal, so
it is rescaled to a real 0-1.

Three signals now, because the first two can both be satisfied by a cluster that
means nothing:

```
avg_weight_norm     they are recalled together      (structural evidence)
grp_coherence       they share a GRP domain         (filing evidence)
semantic_coherence  they are about the same thing   (meaning)

skill_score = 0.4 * avg_weight_norm + 0.3 * grp_coherence + 0.3 * semantic
```

When any member lacks an embedding, `semantic_coherence` is `None`, the score
falls back to the two structural signals, and `scored_without_embeddings` is set
so the fallback is visible instead of silent.

### Overlapping triangles crowd the queue

Four of ten slots in a real queue were the same four or five hot memories
recombined -- one finding wearing four faces. `max_per_domain` did not catch it,
because the triangles straddled two GRP domains and were counted as diverse.

`max_member_reuse=2` caps how many proposals any single memory may appear in.
Both caps fill from overflow if they would return fewer than `limit`, so
breadth is never bought with coverage, and neither ever excludes outright --
this phase exists because a silent structural exclusion went unnoticed for two
phases, and adding a new one to fix it would be a poor trade.

Effect on the same graph: the top of the queue went from four bio recombinations
to two pure `Document` clusters out of the physics corpus, at score 0.95 with
semantic coherence 0.84.

---

## The gate was hard for everyone

Once proposals were visible, the model correctly reported that it could not act
on them -- which is the design. What that exposed is that the reviewer could
barely act on them either.

Confirming meant hand-building JSON with member addresses copied out of a
listing. Those addresses are rewritten whenever a memory is recalled, so the
likeliest outcome of a careful review was a failed write. And the failure said:

```
HTTP 500  {"detail": "Crystallization failed; nothing was applied.
                      Check the server log."}
```

while the log held the answer the whole time: `expected 2 members, matched 1`.

A gate should be impossible for the model and easy for the reviewer. This one
was hard for both, which is a slower way of being closed.

**`POST /skill_proposals/{id}/crystallize`** confirms by proposal id.
`member_addresses` in the body is ignored; addresses are resolved from the
proposal's immutable `created_at` stamps at write time, so drift cannot happen
between reading and confirming. `confirmed=true` is still required, `trigger`
and `procedure` are still written by a human, members are still demoted to Blue,
and nothing exposed to the model can reach it.

**Failures name the cause.** A stale address now returns 409 with the addresses
that matched nothing and what to do about it -- a conflict with the current
graph, not a server fault.

**`mmu_review.py`** is the terminal for the decision: list, inspect, sweep,
reject, confirm. Confirming prints exactly which memories will be demoted,
requires the trigger and procedure to be typed, and then requires the word
CRYSTALLIZE. Deliberately a local script with no MCP surface -- the ergonomics
belong to the human side of the gate and nowhere else.

---

## Making it reversible, and then handing it over

The first real crystallization exposed the next gap: there was no undo.
`deprecate_skill()` marks a Skill dead and leaves every member stranded in
Blue, and `crystallize_skill()` overwrote `m.color` without recording what it
had been. So the one operation that restructures memory was the one operation
with no way back, and a skill judged wrong afterwards left its evidence buried.

The members of the very first proposal were Green, Yellow, Green -- an undo that
assumed a single prior colour would have quietly corrupted the aging state of
the rest. `crystallize_skill()` now records `pre_skill_color` before demoting,
and `POST /skills/{id}/uncrystallize?confirm=UNCRYSTALLIZE` deletes the Skill,
restores each member to the colour it actually had, and returns the proposal to
the queue -- undoing a crystallization says the skill was wrong, not that the
pattern was imaginary. It refuses while an active child extends the skill, for
the same reason `deprecate_skill()` does.

### Letting the model confirm

`MMU_ALLOW_MODEL_CRYSTALLIZE`, default **false**, and the default is the
recommendation.

With it on, the model gets a `crystallize_skill` tool and can confirm a proposal
it may itself have drafted, which means the review stops being a review. That is
a real cost and it is the reason this is a flag rather than a behaviour. It
exists because testing the full propose-and-confirm loop with a model requires
it.

Enforced **server-side**, via an `X-MMU-Source: model` tag, not only by omitting
the tool. A tool list is a client-side promise, and a write that demotes memory
should not depend on a client keeping one. Three cases, all covered by tests:

| Caller | Flag | Result |
|---|---|---|
| model-tagged | on | proceeds on its merits |
| model-tagged | off | 403, naming the flag |
| untagged (human) | either | unaffected |

The pairing matters more than either half. Handing a model a write that
restructures memory is defensible when the write is one command away from being
undone, and much less so when it is not. Reversibility landed first on purpose.

---

## Phase 13.2 -- delivery

Crystallization was write-only, and nothing noticed for two phases because
every part of it worked except the part that returns a skill to anyone.

A `Skill` node carried `trigger`, `procedure`, `confidence`, `status`, and
`invocation_count`, plus `PROCEDURALIZED_FROM` edges pointing *in* from its
members. No keywords. No embedding. Every retrieval path in the system reaches
a memory through the `Keyword` graph or the vector index, so a Skill was
unreachable by all of them. No `/recall`, session-bundle, or idle-context query
mentioned `:Skill` at all, and `invocation_count` -- written as `0` at creation
-- was never incremented, because nothing ever invoked anything.

Measured before the fix: crystallizing three memories changed recall by **zero
bytes**. Identical queries returned identical results, 7642 characters either
way. The 636-character skill was 11% of its 5,377 characters of source and was
never delivered in place of it.

The model found this before the code did. Asked about skills, it saved an
ordinary `Memory` titled `SKILL NODE: Dimensional Relativity Theory (DRT)
Framework`, with a trigger phrase and a `(Crystallized from memories: ...)`
footer -- reimplementing the mechanism at the only layer that was actually
retrievable. That memory came back `via=direct` in every test. The real Skill
never came back at all.

### What delivery required

**Skills join the retrieval graph.** A `skill_embedding` vector index, and
`HAS_KEYWORD` edges into the same `Keyword` nodes memories use -- not a
parallel vocabulary, or the keyword gate would need to learn about skills as a
special case. `_skill_embedding_text()` is the canonical "what text represents
a skill", mirroring `_embedding_text()`, because vectors only compare if the
text that produced them was assembled the same way.

**A matched skill substitutes for its members.** This is the whole point: a
skill delivered alongside everything it compressed has added text rather than
saved it. Members are never deleted and a direct query still finds them; they
are withheld from that one context block, and `replaced_members` reports which.

**`invocation_count` increments.** Which finally makes `confidence` and
`last_invoked` mean something, and gives an answer to "is this skill earning
its place" that is evidence rather than intuition.

`POST /skills/reindex` backfills anything crystallized before this existed, and
is idempotent.

### What it is honestly worth

Measured on a controlled pair -- two memories that genuinely surface for their
query:

```
before crystallizing   2607 chars, both members present
after                  2532 chars, 0 members present, skill delivered (0.91)
```

Substitution works. But on the real DRT skill, the same before/after showed the
skill *adding* 717 characters and withholding nothing, because its three
members do not surface for any query tried -- including their own near-verbatim
text. Among 869 chunks of one corpus they are never in the top 8.

That is worth stating plainly rather than rounding up: **a skill only saves
context when it displaces members that would otherwise have been retrieved.**
Co-recall density and retrieval competitiveness are different properties, and
`find_skill_candidates()` selects for the first. A cluster can be densely
co-recalled and still never win a query on its own.

So the compression case is real but conditional, and the unconditional gain is
different: the skill delivers procedural guidance -- *how* to answer -- that no
individual memory contained. Whether that trade is worth the demotion is a
judgement per skill, which is why `invocation_count` now exists to inform it.

---

## Regression guards

`tests/test_mmu.py` gains pure tests that fail if any of this is reintroduced:

- `test_documents_are_eligible_for_crystallization` -- asserts `src_type <> 2` is
  absent from `find_skill_candidates` and still present in `get_anticipated_context`.
- `test_skill_floor_never_normalizes_against_the_max` -- asserts `percentileCont` is
  used and `max(r.weight)` is not.
- `test_previews_share_one_candidate_implementation` -- asserts both preview paths
  call `find_skill_candidates()`.
- `test_member_key_is_order_independent` -- proposal dedup depends on it.

And live tests that the sweep creates no skills, is idempotent, and that `/insights`
and `/skill_candidates` return identical clusters.

---

## The general lesson

Every one of these bugs was a **correct decision copied to a context where its
reasoning did not hold**, or **a global statistic applied to a graph with several
distinct populations in it**. Neither shows up as an error. Both show up as a system
that quietly does nothing, and reports doing nothing as a normal result.

When a threshold produces an empty answer, report the threshold and what set it. An
empty list and an unreachable bar look identical from the outside, and they stayed
identical here for two phases.
