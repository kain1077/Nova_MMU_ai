"""
The contract every retriever in this harness implements.

The point of the shared interface is that MMU and its baselines run through
exactly the same loop -- same instances, same k, same scoring -- so a
difference in the results is a difference in retrieval and not a difference in
how the two were driven.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..dataset import Instance


@dataclass
class Retrieved:
    """
    One retrieved item, traced back to where it came from.

    `session_id` is the field scoring actually uses: LongMemEval marks evidence
    at session granularity, so a hit means "returned something from a session
    that contained the answer". `turn_id` is kept for error analysis, since
    "found the right session, wrong turn" and "found the wrong session" are
    different failures and only one of them is a retrieval problem.
    """
    session_id: str
    turn_id: str
    text: str
    score: float
    rank: int


class Backend(Protocol):
    """
    Lifecycle: setup() once per instance, query() once per question, teardown()
    before the next instance. Instances do not share state -- each has its own
    haystack, and leftovers from the previous one are contamination.
    """

    name: str

    def setup(self, instance: Instance) -> None:
        """Load this instance's haystack. Called before any query()."""
        ...

    def query(self, question: str, top_k: int) -> list[Retrieved]:
        """Retrieve for one question, best first."""
        ...

    def teardown(self) -> None:
        """Discard the haystack. Must leave nothing that could affect the next
        instance."""
        ...
