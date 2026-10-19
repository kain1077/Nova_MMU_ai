"""
get_next_con()'s fallback chain never hands out count()+1.

count()+1 collides after any deletion -- delete 5 of 100 memories and it
returns 96, a CON that is already taken. The Counter node fixed the normal
path; this covers the last-resort fallback, which still used it.

Pure: neo4j_layer is imported with Neo4j disabled and get_driver() replaced
by a fake, so nothing here can reach a database. It does not import test_mmu.
"""

import os
import sys

os.environ["NEO4J_ENABLED"] = "false"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import neo4j_layer  # noqa: E402


class _Res:
    def __init__(self, rec):
        self._rec = rec

    def single(self):
        return self._rec


class _Sess:
    def __init__(self, script):
        self.script = script

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **kw):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _Res(step)


class _Drv:
    def __init__(self, script):
        self.script = script

    def session(self):
        return _Sess(self.script)


def _with(monkeypatch, script):
    monkeypatch.setattr(neo4j_layer, "get_driver", lambda: _Drv(script))
    monkeypatch.setattr(neo4j_layer, "get_memory_count",
                        lambda: (_ for _ in ()).throw(AssertionError("count()+1 used")))


def test_counter_path(monkeypatch):
    _with(monkeypatch, [{"con": 1121}])
    assert neo4j_layer.get_next_con() == 1121


def test_counter_failure_falls_back_to_max_plus_one(monkeypatch):
    # After deletions count() is below max(); max()+1 is the safe value.
    _with(monkeypatch, [RuntimeError("counter tx failed"), {"max_con": 1120}])
    assert neo4j_layer.get_next_con() == 1121


def test_neo4j_down_returns_one_and_never_counts(monkeypatch):
    _with(monkeypatch, [RuntimeError("counter tx failed"), RuntimeError("down")])
    assert neo4j_layer.get_next_con() == 1
