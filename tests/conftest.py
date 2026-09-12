"""
pytest hooks for the MMU suite.

Exists for one reason: to make "these tests did not run" as visible as "these
tests failed".

test_mmu.py prints its warnings to stderr at import time, which pytest captures
during collection -- so the banners never reached the terminal in a normal run.
Both conditions reported below mean the suite silently covered less than it
appears to, which is the failure mode that let five regression tests skip on
every machine they were ever run on, CI included.

Everything here is best-effort by construction. A reporting nicety must never
be able to break collection, and the first version of this file did exactly
that: it imported the suite as `tests.test_mmu`, which resolves locally only
because `python -m pytest` puts the working directory on sys.path, and died
with ModuleNotFoundError under CI's bare `pytest tests/`.
"""

import os
import sys

# pytest prepends this directory to sys.path before importing conftest, but say
# so explicitly rather than depending on the import mode staying that way.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import test_mmu as _t
except Exception:                       # pragma: no cover - never fail the run
    _t = None


def pytest_report_header(config):
    if _t is None:
        return []
    try:
        lines = []
        if getattr(_t, "_mmu_server", None) is None:
            lines.append("!! " + _t._SERVER_IMPORT_REASON)
        if getattr(_t, "_AIMED_AT_PRODUCTION", False):
            lines.append("!! " + _t._PRODUCTION_REASON)
        elif not getattr(_t, "_LIVE_OK", False):
            lines.append("!! no MMU server at %s -- every live test will skip"
                         % getattr(_t, "BASE", "?"))
        return lines
    except Exception:                   # pragma: no cover
        return []
