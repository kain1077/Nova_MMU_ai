"""
The driver. Three subcommands:

    inspect   what a dataset file actually contains, for when loading fails
    init      mark a graph as disposable, once, out loud
    run       the benchmark

Every backend sees the same instances in the same order at the same k, so a
difference in the output is a difference in retrieval.

    python -m bench.longmemeval.run inspect --dataset data/longmemeval_s.json
    python -m bench.longmemeval.run init    --base-url http://127.0.0.1:8766
    python -m bench.longmemeval.run run     --dataset data/longmemeval_s.json \\
        --base-url http://127.0.0.1:8766 --limit 50
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import requests

from . import dataset as ds
from . import report as rp
from . import safety
from .backends.base import Backend
from .scoring import (ChatClient, InstanceResult, aggregate, judge_answer,
                      read_answer, score_retrieval)

KS = (1, 3, 5, 10)


# ── subcommand: inspect ──────────────────────────────────────────────────────

def cmd_inspect(args) -> int:
    ds.inspect(args.dataset, limit=args.limit or 2)
    return 0


# ── subcommand: init ─────────────────────────────────────────────────────────

def cmd_init(args) -> int:
    base_url = args.base_url.rstrip("/")

    if f":{safety.PRODUCTION_PORT}" in base_url and not args.allow_default_port:
        safety.die(
            f"Refusing to mark {base_url} disposable: port "
            f"{safety.PRODUCTION_PORT} is the default MMU port.\n"
            f"Pass --allow-default-port only if you are certain this is not "
            f"your real graph."
        )

    try:
        health = requests.get(f"{base_url}/health", timeout=10).json()
    except requests.RequestException as e:
        safety.die(f"No MMU server at {base_url}: {e}")
        return 2

    total = health.get("total_memories")
    if total is None:
        total = (health.get("neo4j") or {}).get("memories", 0)

    if safety.has_sentinel(base_url):
        print(f"{base_url} is already marked disposable ({total} memories). Nothing to do.")
        return 0

    if not args.yes and not safety.confirm_init(base_url, total):
        print("Not confirmed. Nothing was changed.")
        return 1

    safety.write_sentinel(base_url)
    print(f"\nMarked {base_url} as a disposable benchmark graph.")
    print("The harness will now erase it between instances without asking.")
    return 0


# ── backend construction ─────────────────────────────────────────────────────

def build_backends(args, target) -> dict[str, Backend]:
    requested = [n.strip() for n in args.backends.split(",") if n.strip()]
    backends: dict[str, Backend] = {}

    for name in requested:
        if name == "mmu":
            from .backends.mmu_backend import MMUBackend
            backend = MMUBackend(target)
            if not args.skip_aging_check:
                backend.check_aging_disabled()
            backends["mmu"] = backend

        elif name == "flat_vector":
            from .backends.flat_vector import FlatVectorBackend
            from .embeddings import EmbeddingClient
            client = EmbeddingClient()
            dim = client.probe()
            print(f"  flat_vector: embedding endpoint OK, dim={dim}")
            backends["flat_vector"] = FlatVectorBackend(client)

        elif name == "bm25":
            from .backends.bm25 import BM25Backend
            backends["bm25"] = BM25Backend()

        else:
            safety.die(f"Unknown backend '{name}'. "
                       f"Choose from: mmu, flat_vector, bm25")

    return backends


# ── subcommand: run ──────────────────────────────────────────────────────────

def cmd_run(args) -> int:
    instances = ds.load(args.dataset)
    print(f"Loaded {len(instances)} instances from {args.dataset}")

    if args.limit and args.limit < len(instances):
        instances = ds.stratified_subset(instances, args.limit, seed=args.seed)
        print(f"Using a stratified subset of {len(instances)} "
              f"(seed={args.seed}) across "
              f"{len({i.question_type for i in instances})} question types")

    # Only check the MMU target when an MMU backend is actually requested --
    # the baselines never touch a graph and shouldn't require one to exist.
    target = None
    if "mmu" in args.backends:
        try:
            target = safety.check_target(
                args.base_url, allow_default_port=args.allow_default_port)
        except safety.UnsafeTarget as e:
            safety.die(str(e))
        print(f"Target {target.base_url} checked: sentinel present.")

    backends = build_backends(args, target)
    if not backends:
        safety.die("No backends selected.")

    reader = judge = None
    if args.qa:
        reader = ChatClient(model=args.reader_model)
        judge = ChatClient(model=args.judge_model or args.reader_model)
        print(f"  QA enabled: reader={reader.model or '(server default)'} "
              f"judge={judge.model or '(server default)'}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "checkpoint.jsonl"

    # Resume support. A 500-instance run against a local embedding model takes
    # hours; losing it to one network blip would be its own reason not to run
    # the benchmark.
    done: set[tuple[str, str]] = set()
    if args.resume and checkpoint_path.exists():
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done.add((row["backend"], row["question_id"]))
        print(f"Resuming: {len(done)} instance-backend pairs already recorded")

    results: dict[str, list[InstanceResult]] = {name: [] for name in backends}

    # Replay checkpointed rows so a resumed run reports on everything, not just
    # what this process happened to compute.
    if args.resume and checkpoint_path.exists():
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["backend"] not in results:
                continue
            row["hit_at_k"] = {int(k): v for k, v in row["hit_at_k"].items()}
            results[row["backend"]].append(InstanceResult(**row))

    checkpoint = checkpoint_path.open("a", encoding="utf-8")
    failures = 0

    try:
        for n, instance in enumerate(instances, 1):
            print(f"\n[{n}/{len(instances)}] {instance.question_id} "
                  f"({instance.question_type}, {len(instance.sessions)} sessions, "
                  f"{len(instance.turns)} turns)")

            for name, backend in backends.items():
                if (name, instance.question_id) in done:
                    print(f"  {name:<12} skipped (checkpointed)")
                    continue

                result = InstanceResult(
                    question_id=instance.question_id,
                    question_type=instance.question_type,
                    backend=name,
                    evidence_sessions=sorted(instance.evidence_session_ids),
                    scorable=bool(instance.evidence_session_ids),
                    gold=instance.answer,
                )

                try:
                    t0 = time.perf_counter()
                    backend.setup(instance)
                    result.ingest_seconds = time.perf_counter() - t0

                    t0 = time.perf_counter()
                    retrieved = backend.query(instance.question, args.top_k)
                    result.query_seconds = time.perf_counter() - t0

                    result.retrieved_sessions = [r.session_id for r in retrieved]
                    result.hit_at_k = score_retrieval(instance, retrieved, KS)

                    if args.qa and reader and judge:
                        result.answer = read_answer(reader, instance.question, retrieved)
                        result.judged_correct = judge_answer(
                            judge, instance.question, instance.answer, result.answer)

                    hits = " ".join(f"@{k}={'Y' if result.hit_at_k.get(k) else 'n'}"
                                    for k in KS)
                    qa = "" if result.judged_correct is None else (
                        f"  qa={'Y' if result.judged_correct else 'n'}")
                    print(f"  {name:<12} {hits}{qa}  "
                          f"ingest={result.ingest_seconds:.1f}s "
                          f"query={result.query_seconds:.2f}s")

                except Exception as e:
                    failures += 1
                    print(f"  {name:<12} FAILED: {e}")
                    if args.verbose:
                        traceback.print_exc()
                    if failures > args.max_failures:
                        print(f"\nAborting: more than {args.max_failures} failures.")
                        raise
                finally:
                    try:
                        backend.teardown()
                    except Exception as e:
                        # A failed teardown leaves the next instance ingesting
                        # on top of this one's haystack, which silently
                        # contaminates everything after it. Not survivable.
                        print(f"  {name:<12} TEARDOWN FAILED: {e}")
                        raise

                results[name].append(result)
                checkpoint.write(json.dumps(result.to_json()) + "\n")
                checkpoint.flush()
    finally:
        checkpoint.close()

    summaries = {name: aggregate(rows, KS) for name, rows in results.items() if rows}
    if not summaries:
        safety.die("No results produced.")

    from .embeddings import EmbeddingClient
    meta = {
        "dataset": str(args.dataset),
        "n_instances": len(instances),
        "top_k": args.top_k,
        # Recorded for the report header only -- a bm25-only run has no
        # embedding model in play and must not require numpy to say so.
        "embedding_model": (EmbeddingClient(need_numpy=False).model
                            if {"mmu", "flat_vector"} & set(backends) else None),
        "reader_model": reader.model if reader else None,
        "judge_model": judge.model if judge else None,
        "failures": failures,
    }

    md_path = rp.write_report(out_dir, summaries, meta)

    print("\n" + rp.comparison_table(summaries))
    print()
    print(rp.per_type_table(summaries))
    print(f"\nReport written to {md_path}")
    if failures:
        print(f"{failures} instance-backend runs failed; see output above.")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="bench.longmemeval.run",
        description="Run LongMemEval against MMU and baselines.")
    sub = p.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="show a dataset file's real structure")
    p_inspect.add_argument("--dataset", required=True)
    p_inspect.add_argument("--limit", type=int, default=2)
    p_inspect.set_defaults(func=cmd_inspect)

    p_init = sub.add_parser("init", help="mark a graph as disposable")
    p_init.add_argument("--base-url", required=True)
    p_init.add_argument("--yes", action="store_true",
                        help="skip the interactive confirmation")
    p_init.add_argument("--allow-default-port", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="run the benchmark")
    p_run.add_argument("--dataset", required=True)
    p_run.add_argument("--base-url", default="http://127.0.0.1:8766")
    p_run.add_argument("--backends", default="mmu,flat_vector,bm25")
    p_run.add_argument("--top-k", type=int, default=10)
    p_run.add_argument("--limit", type=int, default=0,
                       help="run a stratified subset of this many instances")
    p_run.add_argument("--seed", type=int, default=0)
    p_run.add_argument("--out-dir", default="bench/results")
    p_run.add_argument("--resume", action="store_true",
                       help="skip instance-backend pairs already in checkpoint.jsonl")
    p_run.add_argument("--qa", action="store_true",
                       help="also run the reader and judge (needs a chat endpoint)")
    p_run.add_argument("--reader-model", default=None)
    p_run.add_argument("--judge-model", default=None)
    p_run.add_argument("--allow-default-port", action="store_true")
    p_run.add_argument("--skip-aging-check", action="store_true")
    p_run.add_argument("--max-failures", type=int, default=10)
    p_run.add_argument("--verbose", action="store_true")
    p_run.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
