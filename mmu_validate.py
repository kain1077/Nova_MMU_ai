#!/usr/bin/env python3
"""
MMU validation harness -- before/after snapshots for optimization work.

The point of this tool is to make "I optimized it and nothing broke" a
measurement instead of a claim. It captures one JSON snapshot of a running
MMU's health, index/graph agreement and recall latency; run it before a
change and after, then `compare` the two.

    python mmu_validate.py snapshot --label before
    #  ... make the change, rebuild, restart ...
    python mmu_validate.py snapshot --label after
    python mmu_validate.py compare before after

WHAT IT TOUCHES
  Read-only except for /recall, which ages every memory it does not return --
  that is MMU's normal read path, not this tool being destructive, but it is
  still a real write. Use --probes 0 for a pure read-only integrity check.

  /index_repair is called in DRY-RUN mode only. This tool never passes
  apply=true; reconciling an index is a decision, not a measurement.

SAFETY
  Refuses to touch the ports a real graph lives on -- 8765 (MMU) and 7474
  (the Neo4j browser, which is not an MMU API at all). Point it at a
  disposable instance. The override exists for people whose only instance is
  disposable, and it is deliberately awkward to type.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Ports that belong to a real graph. 8765 is MMU's default; 7474 is the Neo4j
# browser, which exposes full database access and speaks no MMU endpoint.
PROTECTED_PORTS = {"8765", "7474"}
OVERRIDE_ENV = "MMU_VALIDATE_ALLOW_PROTECTED"

# Generic probes. Deliberately few: every probe is a /recall, and every
# /recall ages the memories it did not return.
DEFAULT_PROBES = [
    "what am I working on",
    "what did I decide",
    "what do I prefer",
]


# Windows consoles default to cp1252. `compare` printed U+2192 and died with
# UnicodeEncodeError before emitting a single number, on the one platform this
# tool is most likely to be run from -- it was authored and tested on Linux,
# where the default encoding hid it. Output is ASCII now; this is the belt to
# that braces, so anything non-ASCII added later degrades to a replacement
# character rather than taking the whole run down.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass


def _c(code, s):
    return s if os.environ.get("NO_COLOR") else f"\033[{code}m{s}\033[0m"


def red(s):    return _c("31", s)
def green(s):  return _c("32", s)
def yellow(s): return _c("33", s)
def cyan(s):   return _c("36", s)
def bold(s):   return _c("1", s)


# ── safety ───────────────────────────────────────────────────────────────────

def guard_target(base_url):
    """
    Refuse a protected port unless explicitly overridden.

    Checked before any request is made, so a refused target costs the server
    nothing at all -- the same ordering tests/test_mmu.py uses.
    """
    port = urllib.parse.urlparse(base_url).port
    port = str(port) if port else ""
    if port not in PROTECTED_PORTS:
        return
    if os.environ.get(OVERRIDE_ENV, "").strip() not in ("", "0", "false", "False"):
        print(red(f"{OVERRIDE_ENV} is set -- running against protected port {port} anyway."))
        return
    sys.exit(red(
        f"\nRefusing to run against {base_url}.\n\n"
        f"Port {port} is where a real MMU graph lives. This tool fires /recall,\n"
        f"and every /recall ages every memory it does not return.\n\n"
        f"Point --base-url at a disposable instance, or set\n"
        f"{OVERRIDE_ENV}=1 if this really is throwaway.\n"
    ))


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _key_headers():
    key = os.environ.get("MMU_API_KEY", "").strip()
    return {"X-MMU-Key": key} if key else {}


def _req(base, path, method="GET", payload=None, timeout=30):
    url = f"{base}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", **_key_headers()}
    # Identify as a tool, not a conversation: the server's activity tracker
    # resets the idle timer on /recall, and a benchmark should not convince
    # the idle daemon that a human is present.
    headers["X-MMU-Source"] = "idle-daemon"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    return body, (time.perf_counter() - t0) * 1000.0


def _try(base, path, method="GET", payload=None, timeout=30):
    """Same as _req, but returns an error dict instead of raising."""
    try:
        body, ms = _req(base, path, method, payload, timeout)
        return body, ms, None
    except urllib.error.HTTPError as e:
        return None, None, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"


# ── snapshot ─────────────────────────────────────────────────────────────────

def take_snapshot(base, probes, top_k, repeat):
    snap = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base,
        "probes": probes,
        "top_k": top_k,
        "repeat": repeat,
    }

    print(cyan("health"), end=" ", flush=True)
    health, _, err = _try(base, "/health")
    if err:
        sys.exit(red(f"\n/health failed: {err}\nIs the server up at {base}?"))
    snap["health"] = health
    n4j = health.get("neo4j") or {}
    print(green(f"ok -- {health.get('total_memories')} cards, "
                f"{n4j.get('memories', '?')} in graph"))

    print(cyan("index agreement"), end=" ", flush=True)
    repair, _, err = _try(base, "/index_repair", method="POST")
    snap["index_repair"] = repair if not err else {"error": err}
    if err:
        print(yellow(f"unavailable ({err})"))
    else:
        m, p = repair.get("missing_count", 0), repair.get("phantom_count", 0)
        msg = f"missing={m} phantom={p}"
        print(green(msg) if (m == 0 and p == 0) else red(msg))

    print(cyan("embeddings"), end=" ", flush=True)
    emb, _, err = _try(base, "/embedding_status")
    snap["embedding_status"] = emb if not err else {"error": err}
    print(yellow(f"unavailable ({err})") if err
          else green(f"{emb.get('embedded', '?')}/{emb.get('total', '?')} embedded"))

    if not probes or repeat < 1:
        print(yellow("recall probes skipped (read-only mode)"))
        snap["recall"] = {"skipped": True}
        return snap

    total = len(probes) * repeat
    print(cyan(f"recall x{total}"), end=" ", flush=True)
    rows = []
    for _ in range(repeat):
        for prompt in probes:
            body, ms, err = _try(base, "/recall", method="POST",
                                 payload={"prompt": prompt, "top_k": top_k})
            if err:
                rows.append({"prompt": prompt, "error": err})
                continue
            rows.append({
                "prompt":     prompt,
                "round_ms":   round(ms, 1),
                "read_ms":    body.get("read_ms"),
                "read_path":  body.get("read_path"),
                "count":      body.get("count"),
                "skills":     len(body.get("skills") or []),
                "anticipated": len(body.get("anticipated") or []),
            })
    snap["recall"] = {"calls": rows}

    ok = [r for r in rows if "error" not in r]
    if ok:
        for field in ("round_ms", "read_ms"):
            vals = [r[field] for r in ok if isinstance(r.get(field), (int, float))]
            if vals:
                snap["recall"][field] = {
                    "min": round(min(vals), 1),
                    "avg": round(statistics.mean(vals), 1),
                    "max": round(max(vals), 1),
                    "n":   len(vals),
                }
        rt = snap["recall"].get("round_ms", {})
        print(green(f"avg {rt.get('avg')}ms round-trip, "
                    f"{snap['recall'].get('read_ms', {}).get('avg')}ms read"))
    else:
        print(red("all recall probes failed"))

    # Colour distribution AFTER the probes, so a reader can see what the run
    # itself moved by diffing this against health.color_summary above.
    post, _, err = _try(base, "/health")
    snap["health_after_probes"] = post if not err else {"error": err}
    if not err:
        before = health.get("color_summary") or {}
        after = post.get("color_summary") or {}
        moved = {k: after.get(k, 0) - before.get(k, 0)
                 for k in set(before) | set(after)
                 if after.get(k, 0) != before.get(k, 0)}
        snap["colors_moved_during_run"] = moved
        if moved:
            print(yellow(f"  colours moved during this run: {moved}"))

    return snap


# ── compare ──────────────────────────────────────────────────────────────────

def _delta(before, after, label, unit="ms", lower_is_better=True):
    if before is None or after is None:
        return f"  {label}: n/a"
    d = after - before
    if before:
        pct = (d / before) * 100.0
        pct_s = f" ({pct:+.1f}%)"
    else:
        pct_s = ""
    arrow = "->"
    line = f"  {label}: {before}{unit} {arrow} {after}{unit}{pct_s}"
    if abs(d) < 1e-9:
        return line
    good = (d < 0) if lower_is_better else (d > 0)
    return (green(line) if good else red(line))


def compare(a, b):
    print(bold(cyan("\n=== MMU validation: before vs after ===\n")))
    print(f"  before: {a['captured_at']}  ({a['base_url']})")
    print(f"  after : {b['captured_at']}  ({b['base_url']})\n")

    ha, hb = a.get("health", {}), b.get("health", {})
    print(bold("Integrity"))
    ca, cb = ha.get("total_memories"), hb.get("total_memories")
    note = "" if ca == cb else red("  <-- CARD COUNT CHANGED")
    print(f"  index cards: {ca} -> {cb}{note}")

    na = (ha.get("neo4j") or {}).get("memories")
    nb = (hb.get("neo4j") or {}).get("memories")
    note = "" if na == nb else red("  <-- GRAPH COUNT CHANGED")
    print(f"  graph nodes: {na} -> {nb}{note}")

    for snap, name in ((a, "before"), (b, "after")):
        r = snap.get("index_repair") or {}
        m, p = r.get("missing_count"), r.get("phantom_count")
        if m or p:
            print(red(f"  {name}: index drift -- missing={m} phantom={p}"))
        elif m == 0 and p == 0:
            print(green(f"  {name}: index and graph agree"))

    print(bold("\nRecall latency"))
    ra, rb = a.get("recall") or {}, b.get("recall") or {}
    if ra.get("skipped") or rb.get("skipped"):
        print("  (a snapshot ran read-only; no latency to compare)")
    else:
        for field in ("round_ms", "read_ms"):
            fa, fb = ra.get(field) or {}, rb.get(field) or {}
            print(f"  {field}:")
            for stat in ("min", "avg", "max"):
                print(_delta(fa.get(stat), fb.get(stat), f"    {stat}"))

    print(bold("\nColour movement caused by each run"))
    print(f"  before: {a.get('colors_moved_during_run') or 'none'}")
    print(f"  after : {b.get('colors_moved_during_run') or 'none'}")
    print()


# ── main ─────────────────────────────────────────────────────────────────────

def _snap_path(results_dir, label):
    return Path(results_dir) / f"mmu_snapshot_{label}.json"


def main():
    p = argparse.ArgumentParser(
        description="Capture and compare MMU health/latency snapshots.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="capture one snapshot")
    s.add_argument("--label", required=True, help="name for this snapshot, e.g. 'before'")
    # 8766 is this repo's second-instance convention, matching
    # tests/test_mmu.py and mmu_recall_speed_test.py. Override with
    # --base-url or MMU_VALIDATE_BASE; confirm which container is actually
    # listening before you do, since container name and port are configurable
    # and a disposable instance is only disposable if you picked the right one.
    s.add_argument("--base-url", default=os.environ.get("MMU_VALIDATE_BASE",
                                                        "http://127.0.0.1:8766"))
    s.add_argument("--top-k", type=int, default=10)
    s.add_argument("--repeat", type=int, default=2,
                   help="passes over the probe set (default: 2)")
    s.add_argument("--probes", nargs="*", metavar="PROMPT",
                   help="probe prompts; pass with no values for read-only mode")
    s.add_argument("--results-dir", default="scratchpad")

    c = sub.add_parser("compare", help="diff two snapshots")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--results-dir", default="scratchpad")

    args = p.parse_args()

    if args.cmd == "compare":
        pa, pb = _snap_path(args.results_dir, args.before), _snap_path(args.results_dir, args.after)
        for path in (pa, pb):
            if not path.exists():
                sys.exit(red(f"No snapshot at {path}"))
        compare(json.loads(pa.read_text()), json.loads(pb.read_text()))
        return

    base = args.base_url.rstrip("/")
    guard_target(base)

    probes = DEFAULT_PROBES if args.probes is None else args.probes
    print(bold(cyan("=== MMU validation snapshot ===")))
    print(f"target: {base} | label: {args.label} | "
          f"probes: {len(probes)} x{args.repeat}\n")

    snap = take_snapshot(base, probes, args.top_k, args.repeat)

    out = _snap_path(args.results_dir, args.label)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2))
    print(green(f"\nsaved {out}"))


if __name__ == "__main__":
    main()
