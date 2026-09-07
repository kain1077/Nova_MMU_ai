"""
MMU as a retriever: ingest a haystack through /remember, query through /recall.

TWO THINGS THAT WOULD QUIETLY RUIN THE NUMBERS
----------------------------------------------

**Recall is not side-effect-free.** Every /recall ages every memory it does
*not* return, and MMU_ARCHIVE_THRESH defaults to 20. Over a 500-question run
that means the graph is mutating underneath the benchmark, and a memory can be
archived out of reach before the question that needed it is asked. The fix is
configuration, not code: set MMU_ARCHIVE_THRESH and MMU_AGING_MIN_MEMORIES
absurdly high on the benchmark instance. `check_aging_disabled()` below verifies
you did, because discovering this after a nine-hour run is worse than a startup
check that costs one request. bench/README.md has the compose settings.

**Each instance needs a clean graph.** Haystacks overlap in vocabulary; a
distractor from instance 3 is a plausible retrieval for instance 4. teardown()
erases, through the guards in safety.py.

ONE MEMORY PER TURN
-------------------
A turn is roughly the granularity MMU is built for -- one thing that was said,
one memory. Sessions are recoverable because the harness keeps its own
address -> turn map, built from what /remember returns rather than from
anything round-tripped through the graph.
"""

from __future__ import annotations

import requests

from ..dataset import Instance
from ..safety import Target, erase
from .base import Retrieved

# Ceiling on how many memories one instance may write. A LongMemEval_S haystack
# is roughly 40-50 sessions; blowing well past that means the loader is
# producing garbage and it is better to stop than to spend an hour ingesting it.
MAX_TURNS_PER_INSTANCE = 5000


class AgingStillOn(RuntimeError):
    pass


class MMUBackend:
    name = "mmu"

    def __init__(self, target: Target, *, keyword_fn=None, timeout: float = 120.0,
                 grp_code: int = 900):
        self.target = target
        self.timeout = timeout
        self.grp_code = grp_code
        # Keyword extraction is MMU's own, imported lazily so the harness can
        # be imported without the server package on the path.
        self._keyword_fn = keyword_fn
        self._by_address: dict[str, tuple[str, str]] = {}   # addr -> (session, turn)
        self._session = requests.Session()

    # ── keywords ──────────────────────────────────────────────────────────

    def _keywords(self, text: str) -> list[str]:
        if self._keyword_fn is None:
            try:
                import ingest
                self._keyword_fn = ingest.extract_keywords
            except ImportError:
                # Falling back would change what is being measured -- MMU's
                # keyword gate is half its retrieval -- so this is fatal rather
                # than silently degraded.
                raise RuntimeError(
                    "Could not import `ingest` for keyword extraction. Run the "
                    "harness from the repo root, or pass keyword_fn explicitly. "
                    "Substituting a different extractor would not be measuring "
                    "MMU."
                )
        return self._keyword_fn(text)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def setup(self, instance: Instance) -> None:
        turns = instance.turns
        if len(turns) > MAX_TURNS_PER_INSTANCE:
            raise RuntimeError(
                f"{instance.question_id} has {len(turns)} turns, past the "
                f"{MAX_TURNS_PER_INSTANCE} sanity limit. Check the loader with "
                f"`run.py inspect` before ingesting this."
            )

        self._by_address.clear()
        for turn in turns:
            payload = f"{turn.role}: {turn.text}" if turn.role else turn.text
            r = self._session.post(
                self.target.url("/remember"),
                json={
                    "keywords": self._keywords(turn.text),
                    "payload": payload,
                    "grp_code": self.grp_code,
                    # Provenance in the note as well as the local map. The map
                    # is what scoring reads; this is here so a human staring at
                    # a graph mid-debug can tell where a memory came from.
                    "note": f"lme:{turn.turn_id}",
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            address = r.json().get("address")
            if address:
                self._by_address[address] = (turn.session_id, turn.turn_id)

    def query(self, question: str, top_k: int) -> list[Retrieved]:
        r = self._session.post(
            self.target.url("/recall"),
            json={"prompt": question, "top_k": top_k},
            timeout=self.timeout,
        )
        r.raise_for_status()
        body = r.json()

        out: list[Retrieved] = []
        for rank, mem in enumerate(body.get("memories", [])):
            address = mem.get("address", "")
            session_id, turn_id = self._by_address.get(address, ("", ""))
            if not session_id:
                # A memory this harness didn't write. Either the graph wasn't
                # clean or the sentinel surfaced; either way it is not a hit
                # against any evidence session, and counting it as one would
                # flatter the result.
                continue
            out.append(Retrieved(
                session_id=session_id,
                turn_id=turn_id,
                text=mem.get("payload", ""),
                score=float(mem.get("score") or 0.0),
                rank=rank,
            ))
        return out

    def teardown(self) -> None:
        erase(self.target)
        self._by_address.clear()

    # ── preflight ─────────────────────────────────────────────────────────

    def check_aging_disabled(self, min_threshold: int = 100_000) -> None:
        """
        Confirm the benchmark instance won't archive memories mid-run.

        There is no endpoint that reports the thresholds directly, so this
        infers from /insights where it can and otherwise tells the operator
        what to verify. An advisory check that names the exact risk beats a
        silent assumption.
        """
        try:
            r = self._session.get(self.target.url("/insights"), timeout=30)
            r.raise_for_status()
            config = r.json().get("config") or {}
        except requests.RequestException:
            config = {}

        thresh = config.get("archive_thresh")
        if thresh is not None and int(thresh) < min_threshold:
            raise AgingStillOn(
                f"MMU_ARCHIVE_THRESH is {thresh} on {self.target.base_url}.\n"
                f"Recall ages every memory it doesn't return, so over a long run "
                f"the graph\nchanges underneath the benchmark and results stop "
                f"being reproducible.\n\n"
                f"Set MMU_ARCHIVE_THRESH and MMU_AGING_MIN_MEMORIES to something "
                f"enormous\n(bench/README.md has the compose block) and restart "
                f"the benchmark instance."
            )
        if thresh is None:
            print("  note: could not read archive_thresh from /insights -- "
                  "verify MMU_ARCHIVE_THRESH is very high on this instance "
                  "before trusting a long run.")
