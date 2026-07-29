"""Offline gate: keep the crawl measurement (docs/CRAWL_ANALYSIS.internal.md) honest (PROD-CRAWL).

Run:  .venv/bin/python tests/test_crawl_measured_offline.py

The analysis rests on measured facts about the coverage model. The load-bearing one is the set of
roles coverage is computed over — the whole "stale vs real" verdict on the buttons-only claim hinges
on it being exactly button+tab. This pins that real value (imported, not grepped), so widening the
coverage roles fails here and forces the measurement to be re-taken rather than silently drifting.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.graph import _CLICK_ROLES  # noqa: E402


def main() -> int:
    # If this changes, docs/CRAWL_ANALYSIS.internal.md is out of date: the coverage denominator moved,
    # and the "buttons-only is stale / links-excluded is by design" verdicts must be re-measured.
    assert _CLICK_ROLES == ("button", "tab"), (
        f"coverage roles are now {_CLICK_ROLES!r}, not (button, tab) — re-measure the crawl analysis")
    # and it is a tuple of role strings, not accidentally a wider container.
    assert all(isinstance(r, str) for r in _CLICK_ROLES), "coverage roles must be role strings"
    print(f"crawl-measured: OK (coverage roles = {_CLICK_ROLES})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
