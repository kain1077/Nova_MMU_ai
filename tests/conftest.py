"""
pytest hooks for the MMU suite.

Exists for one reason: to make "these tests did not run" as visible as "these
tests failed".

test_mmu.py printed its warnings to stderr at import time, which pytest
captures during collection -- so the banners never reached the terminal in a
normal run. Both conditions below mean the suite silently covered less than it
appears to, which is the failure mode that let five regression tests skip on
every machine they were ever run on. pytest_report_header() prints above the
progress line and is never captured.
"""

import tests.test_mmu as t


def pytest_report_header(config):
    lines = []
    if getattr(t, "_mmu_server", None) is None:
        lines.append("!! " + t._SERVER_IMPORT_REASON)
    if getattr(t, "_AIMED_AT_PRODUCTION", False):
        lines.append("!! " + t._PRODUCTION_REASON)
    elif not getattr(t, "_LIVE_OK", False):
        lines.append("!! no MMU server at %s -- every live test will skip"
                     % t.BASE)
    return lines
