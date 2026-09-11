#!/usr/bin/env python3
"""
mmu_recall_speed_test.py
========================
Measures /recall latency against a live MMU server and reports read_ms -- the
server's own internal timing -- alongside full round-trip time. The two are not
the same number and the gap between them is often the interesting part; see the
loopback diagnostic below.

Runs on Windows, macOS and Linux. Standard library only, no pip install needed.

PROBE SETS
  The default set is deliberately generic, because a latency probe is only
  meaningful against a graph that actually contains the thing you asked for.
  Yours will not contain the same memories as anyone else's, so replace these
  with prompts that hit your own graph:

      python mmu_recall_speed_test.py --prompts "my dog" "the tax deadline"
      python mmu_recall_speed_test.py --prompts-file my_probes.txt

  Keeping one fixed set and re-running it after each change is what makes the
  numbers comparable over time. The set matters less than the fact that it
  doesn't move.

SAFETY NOTE -- read before running:
  _age_memories() bumps a use-counter on every non-recalled, non-Document
  memory on EVERY recall call, and MMU_ARCHIVE_THRESH defaults to 20. A heavy
  burst of test recalls can therefore archive conversational memories that the
  burst didn't happen to touch -- this really happened, twice, during Phase 9
  and Phase 11 validation.

  So: the default probe set is small on purpose, the script refuses to fire
  more calls than the threshold without --force, and it prints the colour
  distribution before and after so you can see whether anything moved.

USAGE
  python mmu_recall_speed_test.py
  python mmu_recall_speed_test.py --repeat 2
  python mmu_recall_speed_test.py --prompts "who wrote this" "project deadline"
  python mmu_recall_speed_test.py --prompts-file probes.txt
  python mmu_recall_speed_test.py --skip-color-check
  python mmu_recall_speed_test.py --base-url http://127.0.0.1:8766
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Matches MMU_ARCHIVE_THRESH's default. The real value could differ if you've
# overridden it -- this is a heuristic guard, not an authoritative check.
ARCHIVE_THRESH = 20

# The port a normal MMU install listens on, and therefore the one this script
# must not aim at without being told to. This tool fires /recall, and every
# /recall ages every memory it does not return -- the same reasoning that moved
# tests/test_mmu.py off this port as a default. The two now agree.
PRODUCTION_PORT = "8765"


def guard_production(base_url, allowed):
    """Refuse a real graph's port unless explicitly permitted."""
    if urllib.parse.urlparse(base_url).port != int(PRODUCTION_PORT) or allowed:
        return
    sys.exit(
        f"\nRefusing to benchmark {base_url}.\n\n"
        f"Port {PRODUCTION_PORT} is the default MMU port, so it is usually where a\n"
        f"real graph lives. This script fires /recall, and every /recall ages every\n"
        f"memory it does not return.\n\n"
        f"Point --base-url at a disposable instance, or pass --allow-production if\n"
        f"this really is throwaway.\n"
    )

DEFAULT_PROMPTS = [
    "what am I working on",
    "what did I decide about the project",
    "who did I talk to recently",
    "what do I prefer",
    "what happened last week",
]


# ── terminal colour, disabled when piped or when NO_COLOR is set ──────────────

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def cyan(t):   return _c(t, "36")
def yellow(t): return _c(t, "33")
def red(t):    return _c(t, "31")
def green(t):  return _c(t, "32")
def dim(t):    return _c(t, "2")


# ── HTTP, stdlib only ────────────────────────────────────────────────────────

def http_json(url: str, payload: dict | None = None, timeout: float = 60.0):
    """GET when payload is None, POST otherwise. Returns parsed JSON."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── loopback latency diagnostic ──────────────────────────────────────────────

def loopback_diagnostic(base_url: str) -> None:
    """
    read_ms and round-trip time can differ by a lot for reasons that have
    nothing to do with MMU's code. Resolving the name "localhost" can burn a
    second or more trying IPv6 before falling back to IPv4 -- most visibly on
    Windows, where WPAD proxy auto-detection can add a similar delay per
    request. Both happen before the request reaches the server, so read_ms is
    blind to them.

    One comparison call against the literal 127.0.0.1 tells you immediately
    whether that's what you're looking at. If it is, the fix is to change every
    MMU_BASE / embedding base URL you have from "localhost" to "127.0.0.1" --
    that affects every real recall your model makes, not just this script.
    """
    print(cyan("--- Loopback latency diagnostic (one /health call each) ---"))
    literal_base = base_url.replace("localhost", "127.0.0.1")

    if literal_base == base_url:
        print(dim("  Base URL doesn't use the name 'localhost'; nothing to compare."))
        print()
        return

    try:
        t0 = time.perf_counter()
        http_json(f"{base_url}/health", timeout=15)
        ms_name = round((time.perf_counter() - t0) * 1000, 1)

        t0 = time.perf_counter()
        http_json(f"{literal_base}/health", timeout=15)
        ms_literal = round((time.perf_counter() - t0) * 1000, 1)

        print(f"  {base_url}  ->  {ms_name}ms")
        print(f"  {literal_base}  ->  {ms_literal}ms")

        if ms_name > ms_literal * 3 and ms_name > 500:
            print(yellow("  '127.0.0.1' is much faster than 'localhost' -- this is the"))
            print(yellow("  IPv6-then-fallback resolution delay. Switch MMU_BASE (and your"))
            print(yellow("  embedding base URL) to 127.0.0.1 everywhere. This affects every"))
            print(yellow("  real recall/remember/rate call, not just this test script."))
    except Exception as e:
        print(yellow(f"  Diagnostic call failed: {e}"))

    print()


# ── Neo4j helpers, via docker exec ───────────────────────────────────────────

def cypher(container: str, user: str, password: str, query: str) -> str | None:
    """
    Run one Cypher statement through the Neo4j container's cypher-shell.

    Returns stdout, or None if Docker isn't there, the container isn't running,
    or the query failed. Every caller treats None as "skip this check" -- none
    of this is essential to the timing numbers.
    """
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "cypher-shell",
             "-u", user, "-p", password, query],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def memory_count(base_url: str, container: str, user: str, password: str | None):
    """
    Graph size, for context on where these numbers should land.

    /insights nests the total under `totals.memories` rather than exposing it
    at the top level, so try that first, then the older flat field, then fall
    back to counting in Neo4j directly -- which stays correct no matter how
    /insights' shape changes.
    """
    try:
        insights = http_json(f"{base_url}/insights", timeout=30)
        totals = insights.get("totals") or {}
        if totals.get("memories") is not None:
            return totals["memories"]
        if insights.get("total_memories") is not None:
            return insights["total_memories"]
    except Exception:
        pass

    if password:
        out = cypher(container, user, password,
                     "MATCH (m:Memory) RETURN count(m) AS total")
        if out:
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if lines:
                return lines[-1]

    return None


def color_distribution(container, user, password, label):
    """Colour spread of conversational (non-Document) memories."""
    if not password:
        return False
    print(cyan(f"--- Conversational (non-Document) colour distribution {label} ---"))
    out = cypher(container, user, password,
                 "MATCH (m:Memory) WHERE m.src_type <> 2 "
                 "RETURN m.color AS color, count(*) AS n")
    if out is None:
        print(yellow("Could not query Neo4j directly. Skipping colour check."))
        print(dim("(Needs Docker on PATH, the Neo4j container running, and NEO4J_PASS set.)"))
        print()
        return False
    print(out.rstrip())
    print()
    return True


# ── probe run ────────────────────────────────────────────────────────────────

def run_probes(base_url, prompts, top_k, repeat):
    results = []
    for r in range(1, repeat + 1):
        for prompt in prompts:
            t0 = time.perf_counter()
            try:
                resp = http_json(f"{base_url}/recall",
                                 {"prompt": prompt, "top_k": top_k})
                round_trip = round((time.perf_counter() - t0) * 1000, 1)
                anticipated = resp.get("anticipated")
                results.append({
                    "pass": r,
                    "prompt": prompt,
                    "read_path": resp.get("read_path"),
                    "read_ms": resp.get("read_ms"),
                    "round_trip_ms": round_trip,
                    "count": resp.get("count"),
                    "anticipated": len(anticipated) if anticipated else None,
                })
            except Exception as e:
                print(red(f"FAILED on prompt '{prompt}': {e}"))
                results.append({
                    "pass": r, "prompt": prompt, "read_path": "ERROR",
                    "read_ms": None, "round_trip_ms": None,
                    "count": None, "anticipated": None,
                })
    return results


def print_table(results):
    headers = ["Pass", "Prompt", "ReadPath", "ReadMs", "RoundTripMs", "Count", "Antic"]
    keys = ["pass", "prompt", "read_path", "read_ms", "round_trip_ms", "count", "anticipated"]

    rows = [[("" if r[k] is None else str(r[k])) for k in keys] for r in results]
    widths = [max(len(h), *(len(row[i]) for row in rows)) if rows else len(h)
              for i, h in enumerate(headers)]

    print(cyan("--- Results ---"))
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))
    print()


def show_stats(results, label, field="read_ms"):
    vals = [r[field] for r in results if r.get(field) is not None]
    if not vals:
        print(red(f"{label} -- no successful calls to summarize."))
        return
    print(f"{label} -- {field}: min={min(vals):.1f}  "
          f"avg={statistics.mean(vals):.1f}  max={max(vals):.1f}  (n={len(vals)})")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Measure MMU /recall latency (read_ms and round-trip).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-url", default=os.environ.get("MMU_BASE", "http://127.0.0.1:8766"),
                   help="MMU server base URL (default: $MMU_BASE or http://127.0.0.1:8766, "
                        "the second-instance convention)")
    p.add_argument("--allow-production", action="store_true",
                   help=f"Permit running against port {PRODUCTION_PORT}, where a real graph lives")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--repeat", type=int, default=1,
                   help="Passes over the probe set (default: 1)")
    p.add_argument("--prompts", nargs="+", metavar="PROMPT",
                   help="Probe prompts, replacing the generic defaults")
    p.add_argument("--prompts-file", metavar="PATH",
                   help="File of probe prompts, one per line; # comments allowed")
    p.add_argument("--skip-color-check", action="store_true",
                   help="Skip the before/after Neo4j colour distribution")
    p.add_argument("--skip-latency-diagnostic", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="Run even when the call count reaches MMU_ARCHIVE_THRESH")
    p.add_argument("--neo4j-container", default=os.environ.get("MMU_NEO4J_NAME", "mmu-neo4j"))
    p.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    p.add_argument("--results-dir", default="scratchpad")
    args = p.parse_args()

    # Read from the environment, never baked in. This once defaulted to a real
    # password and was committed; a credential in a repo is a credential
    # published, whether or not the repo was public yet.
    neo4j_pass = os.environ.get("NEO4J_PASS")

    # Build the probe list.
    if args.prompts_file:
        text = Path(args.prompts_file).read_text(encoding="utf-8")
        prompts = [ln.strip() for ln in text.splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
        if not prompts:
            sys.exit(f"No usable prompts in {args.prompts_file}.")
    elif args.prompts:
        prompts = args.prompts
    else:
        prompts = DEFAULT_PROMPTS

    base_url = args.base_url.rstrip("/")
    guard_production(base_url, args.allow_production)

    print(cyan("=== MMU recall speed test ==="))
    print(f"Base URL: {base_url} | top_k={args.top_k} | "
          f"prompts={len(prompts)} | repeat={args.repeat}\n")

    total_recalls = len(prompts) * args.repeat
    print(yellow(f"This run will fire {total_recalls} /recall calls."))
    if total_recalls >= ARCHIVE_THRESH:
        print(red(f"WARNING: {total_recalls} >= the default MMU_ARCHIVE_THRESH ({ARCHIVE_THRESH})."))
        print(red("Conversational memories NOT recalled in this run could get archived (turned Blue)."))
        print(red("This happened twice before, during Phase 9 and Phase 11 validation."))
        if not args.force:
            sys.exit(red("Refusing to run. Use fewer prompts, a smaller --repeat, or --force."))
        print(red("--force given; continuing anyway."))
    elif total_recalls >= ARCHIVE_THRESH - 5:
        print(yellow(f"Note: close to the archive threshold ({ARCHIVE_THRESH}). "
                     "Fine once, watch it if you repeat."))
    print()

    if not args.skip_latency_diagnostic:
        loopback_diagnostic(base_url)

    total_memories = memory_count(base_url, args.neo4j_container,
                                  args.neo4j_user, neo4j_pass)
    print(cyan(f"Current graph size: "
               f"{total_memories if total_memories is not None else 'unknown'} memories"))
    print(dim("Reference points from this project's own history, for scale only --"))
    print(dim("your graph and probes differ, so these are not a target to hit:"))
    print(dim("  ~55 memories    3.8 ms hot path"))
    print(dim("  ~95 memories    23.9 - 61.2 ms (after semantic embeddings)"))
    print(dim("  954 memories    ~130 ms"))
    print()

    color_checked = False
    if not args.skip_color_check:
        color_checked = color_distribution(args.neo4j_container, args.neo4j_user,
                                           neo4j_pass, "BEFORE")

    results = run_probes(base_url, prompts, args.top_k, args.repeat)

    print_table(results)
    print(cyan("--- read_ms summary (server-internal timing) ---"))
    show_stats(results, "All probes")
    print()
    print("round-trip ms (includes network + client overhead, NOT comparable to read_ms):")
    show_stats(results, "All probes", field="round_trip_ms")
    print()

    if color_checked:
        color_distribution(args.neo4j_container, args.neo4j_user, neo4j_pass, "AFTER")
        print(yellow("Compare the two tables by eye. If Blue grew while Green/Yellow shrank,"))
        print(yellow("this run archived conversational memories."))
        print()

    # Save for later comparison.
    try:
        out_dir = Path(args.results_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = out_dir / f"recall_speed_{stamp}.json"
        out_file.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "base_url": base_url,
            "top_k": args.top_k,
            "total_memories": total_memories,
            "results": results,
        }, indent=2), encoding="utf-8")
        print(green(f"Results saved to {out_file}"))
    except OSError as e:
        print(yellow(f"Could not save results file: {e}"))

    # Non-zero exit if every probe failed, so CI and shell chains notice.
    if all(r["read_path"] == "ERROR" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
