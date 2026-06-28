"""Offline determinism tests for Sentinel (no browser, no network, no LLM).

Run:  .venv/bin/python tests/test_determinism_offline.py     (or: pytest tests/)

Covers GAP-RISK-009 — the opt-in visual-authoritative flip in brain/replay.py:
- DEFAULT (env unset): a screenshot-only golden regression is ADVISORY -> exit 0 (current M3 behavior).
- SENTINEL_VISUAL_AUTHORITATIVE=1: the same visual diff GATES exit 2, like an a11y regression.

The flip is gated behind an env flag (default off) so default behavior is unchanged; turning it on by
default awaits the real-browser byte-stability proof (M9-LIVE). A FakeEx simulates pw-executor JSON-RPC
so we can drive a deterministic visual-only drift without a browser.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.store import Store              # noqa: E402
from brain.healing import HealingEngine    # noqa: E402
from brain.replay import run_replay        # noqa: E402
from brain.state import canonical_plan_hash  # noqa: E402

_FLAG = "SENTINEL_VISUAL_AUTHORITATIVE"


class FakeEx:
    """Simulated pw-executor with a VISUAL-ONLY drift on index.html (screenshot changes, a11y/name stay)."""

    def __init__(self, visual_only=False):
        self.visual_only = visual_only
        self.url = ""

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p["url"]
            return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        base = self.url.rsplit("/", 1)[-1]
        v_drift = self.visual_only and base == "index.html"
        if m == "browser.snapshot":
            return {"ariaSnapshot": '- button "Old"'}          # a11y NEVER drifts here
        if m == "browser.screenshotHash":
            return {"hash": "shot_" + base + ("_v2" if v_drift else "")}
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.probe":
            loc = p["locator"]
            if loc.get("role") == "button" and loc.get("name") == "Old":
                return {"count": 1}                            # locator stays valid (no heal needed)
            return {"count": 0}
        return {}


def _plan():
    steps = [
        {"step_id": 1, "action_type": "navigate", "semantic_id": "nav1", "intent": "nav",
         "target": "file:///s/index.html", "locator": None, "alternatives": None},
        {"step_id": 2, "action_type": "click", "semantic_id": "sidB", "intent": "click",
         "locator": {"role": "button", "name": "Old"}, "alternatives": None},
    ]
    return {"plan_id": "p1", "target_url": "file:///s/index.html",
            "plan_hash": canonical_plan_hash(steps), "steps": steps}


def _store():
    return Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)


def _he(ex, st):
    return HealingEngine(ex, st, "r", use_llm=False)


def _baseline_then_replay():
    """Freeze goldens on the clean site, then replay against a visual-only drifted target."""
    p, st = _plan(), _store()
    exc = FakeEx(visual_only=False)
    run_replay(exc, st, _he(exc, st), p, p["target_url"], tempfile.mkdtemp(), baseline=True)
    exv = FakeEx(visual_only=True)
    return run_replay(exv, st, _he(exv, st), p, "file:///s2/index.html", tempfile.mkdtemp())


def test_visual_advisory_default_exit0():
    """Env unset -> screenshot-only regression is advisory; exit 0, page recorded with exit2=False."""
    os.environ.pop(_FLAG, None)
    r = _baseline_then_replay()
    assert r["exit_code"] == 0, r
    visual = [g for g in r["regressions"] if g["page"] == "index.html"]
    assert visual and not visual[0]["exit2"], r
    assert visual[0]["kinds"] == ["visual(advisory)"], r


def test_visual_authoritative_flip_exit2():
    """SENTINEL_VISUAL_AUTHORITATIVE=1 -> the same visual diff gates exit 2 (authoritative)."""
    os.environ[_FLAG] = "1"
    try:
        r = _baseline_then_replay()
    finally:
        os.environ.pop(_FLAG, None)
    assert r["exit_code"] == 2, r
    visual = [g for g in r["regressions"] if g["page"] == "index.html"]
    assert visual and visual[0]["exit2"], r
    assert visual[0]["kinds"] == ["visual"], r


def test_flag_parsing_is_truthy_only():
    """A non-truthy value (e.g. '0') must parse as OFF → advisory (distinguishes 'flag correctly off'
    from 'feature removed', so the assertion binds to the real advisory branch, not just exit 0)."""
    os.environ[_FLAG] = "0"
    try:
        r = _baseline_then_replay()
    finally:
        os.environ.pop(_FLAG, None)
    assert r["exit_code"] == 0, r
    visual = [g for g in r["regressions"] if g["page"] == "index.html"]
    assert visual and not visual[0]["exit2"] and visual[0]["kinds"] == ["visual(advisory)"], r


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
