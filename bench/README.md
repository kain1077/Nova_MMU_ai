# LongMemEval benchmark harness

MMU has latency numbers and no retrieval-quality numbers. This is the harness for
fixing that. It runs MMU and two baselines over the same haystacks with the same
scoring, so the comparison is between retrieval systems rather than between
setups.

**Nothing here has been run against the real dataset yet.** The harness is tested
end-to-end on a synthetic fixture; the numbers it produces on LongMemEval will be
the first ones that exist. Treat the first run as a debugging run.

---

## The one thing to read before running anything

**This harness erases the graph between instances.** It has to — each LongMemEval
instance is its own haystack, and leftovers from the previous one are retrievable
distractors for the next.

That makes a wrong `--base-url` a data-loss event, and the wrong URL is one
character from the right one. Three guards stand in the way:

1. **Port 8765 is refused** before any network call. It's MMU's default port and
   therefore where a real graph usually lives.
2. **A graph without a sentinel is refused.** `init` writes a marker memory; every
   destructive call checks for it first. Your real graph doesn't have one.
3. **A graph over 200 memories with no sentinel is refused** regardless.

Guard 2 is the real one. Guards 1 and 3 exist because a sentinel can be written to
the wrong graph too.

If you'd rather have physical separation than software guards, run the benchmark
instance on a different machine. Nothing in the harness assumes co-location — point
`--base-url` at it.

---

## Setup

### 1. A benchmark instance that is not your real graph

Clone to a separate directory and override ports **and container names** in its
`.env` (container names are global, so a different project name isn't enough):

```
NEO4J_PASS=something-else
MMU_PORT=8766
NEO4J_HTTP_PORT=7475
NEO4J_BOLT_PORT=7688
MMU_NEO4J_NAME=mmu-bench-neo4j
MMU_SERVER_NAME=mmu-bench-server

# Aging OFF for the duration. See "Why aging must be off" below -- this is not
# optional, and a run with it on produces numbers that aren't reproducible.
MMU_ARCHIVE_THRESH=100000000
MMU_AGING_MIN_MEMORIES=100000000
MMU_HOLD_THRESH=100000000

# Proactive suggestions off: they add memories to a recall that the scoring would
# count as retrievals, which measures a different thing than the one under test.
MMU_ANTICIPATE_MAX=0
```

```bash
docker compose -p mmu-bench up -d
```

### 2. Harness dependencies

```bash
pip install -r bench/requirements.txt
```

### 3. Mark the benchmark graph disposable

Once, interactively. This is the step where a human decides the graph is
expendable:

```bash
python -m bench.longmemeval.run init --base-url http://127.0.0.1:8766
```

It prints the graph's current memory count and asks you to type `disposable`.
**Read that number before answering.** If it looks like your real graph, it is.

### 4. The dataset

Get LongMemEval from its own repository — it isn't redistributed here. Then check
the harness agrees with the file's actual structure:

```bash
python -m bench.longmemeval.run inspect --dataset path/to/longmemeval_s.json
```

Every field should say `ok`. If any says `MISS`, add the real field name to
`FIELD_CANDIDATES` in `dataset.py` — that tuple is the only place the raw schema
is read, so it's a one-line fix.

---

## Running

Start small. A stratified subset keeps the question-type mix of the full set, which
matters because the file is usually grouped by type and the first 50 instances are
all one category:

```bash
python -m bench.longmemeval.run run \
  --dataset path/to/longmemeval_s.json \
  --base-url http://127.0.0.1:8766 \
  --limit 50
```

Then the full set, with resume, because it takes hours:

```bash
python -m bench.longmemeval.run run \
  --dataset path/to/longmemeval_s.json \
  --base-url http://127.0.0.1:8766 \
  --resume
```

`--resume` skips instance-backend pairs already in `bench/results/checkpoint.jsonl`,
so an interrupted run continues rather than restarting.

Add `--qa` to run the reader and judge as well. Set `BENCH_CHAT_BASE` and
`BENCH_CHAT_MODEL`, or pass `--reader-model` / `--judge-model`.

---

## What the numbers mean

**Recall@k is the headline.** Did any of the top *k* retrieved items come from a
session that actually contained the answer? No model in the loop, so it's exactly
reproducible by anyone with the same dataset.

**QA accuracy is not comparable across runs.** It depends on the reader and judge
models, which have nothing to do with the memory system. A better reader lifts every
backend at once. The report names both models; quote it only with those attached.

### The baselines are the point

| Backend | What it is | What it tells you |
|---|---|---|
| `bm25` | Lexical BM25 over the turns | The floor. Failing to clear it means semantic retrieval isn't working. |
| `flat_vector` | Cosine over the **same embeddings MMU uses**, no graph | **The one that matters.** |
| `mmu` | The full system | |

`flat_vector` is deliberately in-process: no HTTP, no Neo4j, no containers. That
removes infrastructure overhead from the comparison, so if MMU wins it wins on
retrieval quality rather than on the baseline being handicapped.

**The gap between `mmu` and `flat_vector` is what MMU's graph layer is worth.** MMU's
whole claim to be more than a vector store rests on that layer — `CO_RECALLED`
weights, the keyword gate, skills. If there's no gap, there's no gap, and publishing
that is what makes every other number in the table believable.

### Read the per-type table before the average

Knowledge-update and abstention questions are where a memory system's handling of
superseded facts shows up. An average hides it completely, and those two categories
are the ones MMU's aging and colour model should either win or lose visibly.

---

## Why aging must be off

`/recall` is not side-effect-free. It ages every memory it does **not** return, and
`MMU_ARCHIVE_THRESH` defaults to 20. Over a 500-question run that means:

- the graph mutates underneath the benchmark, so results aren't reproducible; and
- a memory can be archived out of reach *before the question that needed it is asked*,
  which scores as a retrieval miss that isn't one.

The harness checks this at startup and refuses to proceed if it can tell aging is
live. `--skip-aging-check` exists for when `/insights` doesn't report the threshold,
but skipping the check doesn't disable the aging — set the env vars.

---

## Testing the harness

```bash
pytest bench/tests/ -v
```

22 tests, none of which need a server, an embedding endpoint, or the dataset. They
cover the loader against schema variants, BM25 ranking, scoring arithmetic, and —
most importantly — that the safety guards actually refuse.

There's also a synthetic fixture for end-to-end smoke tests:

```bash
python -m bench.longmemeval.run run \
  --dataset bench/fixtures/synthetic_smoke.json --backends bm25,flat_vector
```

`bm25` should score around 40% on it and `flat_vector` near 100% — the fixture is
built so lexical overlap fails on most questions and semantic matching doesn't. If
those two numbers come out similar, something is wired wrong.

---

## Files

```
bench/
  longmemeval/
    safety.py         the guards; read this first
    dataset.py        loading and normalizing LongMemEval
    embeddings.py     embedding client, shared with MMU's config
    scoring.py        recall@k, and the optional reader/judge
    report.py         the comparison table
    run.py            CLI: inspect / init / run
    backends/
      base.py         the interface all three implement
      mmu_backend.py  MMU over HTTP
      flat_vector.py  same embeddings, no graph
      bm25.py         lexical floor
  tests/              harness tests, no services needed
  fixtures/           synthetic dataset for smoke tests
```
