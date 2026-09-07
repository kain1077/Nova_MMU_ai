"""
Guards that stand between this harness and somebody's real graph.

The harness erases the graph between instances -- it has to, since each
LongMemEval instance is its own haystack and leftover memories from the last
one would poison retrieval for the next. That makes a wrong --base-url a
data-loss event, and the wrong URL is one character away from the right one
(8765 is the default MMU port; 8766 is the convention for a second instance).

So a destructive call is allowed only against a graph that has explicitly been
marked as disposable. Marking is a separate, deliberate step (`run.py init`),
and the mark lives *in the graph itself* -- not in a config file that could
still be pointing at last week's target.

Three independent checks, all of which must pass:

  1. The base URL is not the default production port, unless --allow-default-port.
  2. The graph carries the sentinel memory written by `init`.
  3. The graph is smaller than MAX_SAFE_MEMORIES, or the sentinel is present.

Check 2 is the real one. Checks 1 and 3 exist because a sentinel can be written
to the wrong graph too, and a 1,000-memory graph that someone marked by accident
should still give them pause.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import requests

# The port a normal MMU install listens on. Refused by default -- not because
# anything about it is special, but because it's the one your real graph is
# most likely behind.
PRODUCTION_PORT = 8765

# A graph larger than this is assumed to be real until proven otherwise. The
# LongMemEval haystacks are large, so this is checked before ingest, not after.
MAX_SAFE_MEMORIES = 200

# Written by `init`, checked before every destructive call. The keyword is
# deliberately unlovely; nothing types this by accident.
SENTINEL_KEYWORD = "__longmemeval_harness_disposable_graph__"
SENTINEL_PAYLOAD = (
    "This graph belongs to the LongMemEval benchmark harness and is erased "
    "between instances. If you are reading this in a graph you care about, "
    "the harness was pointed at the wrong server -- see bench/README.md."
)

FORGET_TOKEN = "DELETE ALL MY MEMORIES"


class UnsafeTarget(RuntimeError):
    """Raised instead of touching a graph that hasn't been marked disposable."""


@dataclass
class Target:
    """A checked MMU instance the harness is allowed to write to and erase."""
    base_url: str

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"


def _health(base_url: str, timeout: float = 10.0) -> dict:
    try:
        r = requests.get(f"{base_url.rstrip('/')}/health", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise UnsafeTarget(
            f"Could not reach an MMU server at {base_url}: {e}\n"
            f"Start one first -- see bench/README.md for standing up a second "
            f"instance that isn't your real graph."
        ) from e


def has_sentinel(base_url: str) -> bool:
    """
    True when this graph carries the disposable-graph marker.

    Looked up through /recall rather than /memories so it works the same on a
    graph with thousands of memories in it. A recall for the sentinel keyword
    is an exact keyword-gate hit, so it lands in the top few results or the
    sentinel isn't there.
    """
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/recall",
            json={"prompt": SENTINEL_KEYWORD, "top_k": 5},
            timeout=30,
        )
        r.raise_for_status()
        for mem in r.json().get("memories", []):
            if SENTINEL_KEYWORD in (mem.get("keywords") or ""):
                return True
            if SENTINEL_KEYWORD in (mem.get("payload") or ""):
                return True
    except requests.RequestException:
        return False
    return False


def write_sentinel(base_url: str) -> None:
    r = requests.post(
        f"{base_url.rstrip('/')}/remember",
        json={
            "keywords": [SENTINEL_KEYWORD, "benchmark", "harness"],
            "payload": SENTINEL_PAYLOAD,
            # Red/pinned so routine aging can't archive the one memory that
            # authorizes everything else this harness does.
            "color": "Red",
            "priority": 1,
            "note": "LongMemEval harness sentinel",
        },
        timeout=60,
    )
    r.raise_for_status()


def check_target(base_url: str, *, allow_default_port: bool = False,
                 require_sentinel: bool = True) -> Target:
    """
    Verify a base URL is safe to write to and erase. Raises UnsafeTarget if not.

    Call this once at startup and pass the returned Target around; the point is
    that a function holding a Target has evidence the check happened, rather
    than a string that may or may not have been checked somewhere.
    """
    base_url = base_url.rstrip("/")

    if f":{PRODUCTION_PORT}" in base_url and not allow_default_port:
        raise UnsafeTarget(
            f"Refusing to use {base_url}: port {PRODUCTION_PORT} is the default MMU\n"
            f"port, which is where a real graph usually lives. This harness erases\n"
            f"the graph between instances.\n\n"
            f"Stand up a separate instance on another port (bench/README.md shows\n"
            f"how), or pass --allow-default-port if you are certain."
        )

    health = _health(base_url)
    total = health.get("total_memories")
    if total is None:
        total = (health.get("neo4j") or {}).get("memories", 0)

    sentinel = has_sentinel(base_url)

    if require_sentinel and not sentinel:
        raise UnsafeTarget(
            f"Refusing to use {base_url}: this graph has no harness sentinel.\n\n"
            f"It currently holds {total} memories. If this is a throwaway instance,\n"
            f"mark it once with:\n\n"
            f"    python -m bench.longmemeval.run init --base-url {base_url}\n\n"
            f"If that number looks like a graph you care about, it is, and the\n"
            f"harness just stopped you from erasing it."
        )

    if not sentinel and isinstance(total, int) and total > MAX_SAFE_MEMORIES:
        raise UnsafeTarget(
            f"Refusing to use {base_url}: {total} memories and no sentinel.\n"
            f"That is well past the {MAX_SAFE_MEMORIES}-memory line this harness treats\n"
            f"as 'probably somebody's real graph'."
        )

    return Target(base_url=base_url)


def confirm_init(base_url: str, total_memories: int) -> bool:
    """
    Interactive confirmation before marking a graph disposable.

    `init` is the one place a human decides this graph is expendable, so it is
    the one place that asks out loud. Non-interactive callers must pass --yes,
    which is handled by the caller rather than answered for them here.
    """
    print(f"\nAbout to mark {base_url} as a DISPOSABLE benchmark graph.")
    print(f"It currently holds {total_memories} memories.")
    print("The harness will erase this graph, repeatedly, without asking again.\n")
    if total_memories > MAX_SAFE_MEMORIES:
        print(f"  !! {total_memories} memories is a lot for a fresh benchmark instance.")
        print(f"  !! Check the port before answering.\n")
    reply = input('Type "disposable" to confirm: ').strip()
    return reply == "disposable"


def erase(target: Target) -> int:
    """
    Erase every memory in a checked target. Returns how many were deleted.

    Takes a Target, not a string, so this cannot be called on a URL that never
    went through check_target().
    """
    if not has_sentinel(target.base_url):
        raise UnsafeTarget(
            f"Sentinel missing from {target.base_url} at erase time. Refusing.\n"
            f"Either the graph was replaced mid-run or the harness is pointed "
            f"somewhere new."
        )
    r = requests.post(
        target.url("/forget_all"),
        params={"confirm": FORGET_TOKEN},
        timeout=300,
    )
    r.raise_for_status()
    deleted = r.json().get("deleted", 0)

    # forget_all removes the sentinel too, since it removes everything. Put it
    # straight back, or the next instance's erase refuses.
    write_sentinel(target.base_url)
    return deleted


def die(message: str) -> None:
    print(f"\n{message}\n", file=sys.stderr)
    raise SystemExit(2)
