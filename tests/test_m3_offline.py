"""Offline trust-layer + healing tests for Sentinel (no browser, no network, no LLM).

Run:  .venv/bin/python tests/test_m3_offline.py     (or: pytest tests/)

A FakeEx simulates pw-executor JSON-RPC responses so we can deterministically exercise:
- plan_hash HARD-ABORT (exit 3),
- dual golden baseline capture (first-landing) + diff,
- a11y regression -> exit 2; visual-only regression -> advisory (exit 0),
- L1-L6 self-healing via testid,
- AUT-SHA-gated flake quarantine.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.store import Store              # noqa: E402
from brain.healing import HealingEngine    # noqa: E402
from brain.replay import run_replay        # noqa: E402
from brain.state import canonical_plan_hash  # noqa: E402


class FakeEx:
    """Simulated pw-executor.

    `drift`       : index.html's button name AND screenshot change (real DOM drift).
    `visual_only` : index.html's screenshot changes but a11y/name stays (pure visual drift).
    """

    def __init__(self, drift=False, visual_only=False):
        self.drift = drift
        self.visual_only = visual_only
        self.url = ""

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p["url"]
            return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        base = self.url.rsplit("/", 1)[-1]
        a_drift = self.drift and base == "index.html"
        v_drift = (self.drift or self.visual_only) and base == "index.html"
        if m == "browser.snapshot":
            return {"ariaSnapshot": f'- button "{"Launch" if a_drift else "Old"}"'}
        if m == "browser.screenshotHash":
            return {"hash": "shot_" + base + ("_v2" if v_drift else "")}
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.probe":
            loc = p["locator"]
            if loc.get("testid") == "cta":
                return {"count": 1}
            if loc.get("role") == "button" and loc.get("name") == "Old":
                return {"count": 0 if self.drift else 1}   # role+name breaks only on real drift
            return {"count": 0}
        return {}


def _plan():
    steps = [
        {"step_id": 1, "action_type": "navigate", "semantic_id": "nav1", "intent": "nav",
         "target": "file:///s/index.html", "locator": None, "alternatives": None},
        {"step_id": 2, "action_type": "click", "semantic_id": "sidB", "intent": "click",
         "locator": {"role": "button", "name": "Old"},
         "alternatives": [{"strategy": "testid", "locator": {"testid": "cta"}, "prior": 0.95},
                          {"strategy": "role_name", "locator": {"role": "button", "name": "Old"}, "prior": 0.90}]},
    ]
    return {"plan_id": "p1", "target_url": "file:///s/index.html",
            "plan_hash": canonical_plan_hash(steps), "steps": steps}


def _store():
    return Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)


def _he(ex, st):
    return HealingEngine(ex, st, "r", use_llm=False)


def test_plan_hash_hard_abort():
    p = _plan()
    p["steps"][1]["intent"] = "TAMPERED"  # canonical hash no longer matches stored
    st, ex = _store(), FakeEx()
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 3 and r["steps"] == [], r


def test_clean_replay_exit0():
    p, st, ex = _plan(), _store(), FakeEx(drift=False)
    run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp(), baseline=True)
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 0 and r["healed"] == 0 and r["failed"] == 0 and not r["regressions"], r


def test_drift_heals_and_a11y_regresses_exit2():
    p, st = _plan(), _store()
    exc = FakeEx(drift=False)
    run_replay(exc, st, _he(exc, st), p, p["target_url"], tempfile.mkdtemp(), baseline=True)
    exd = FakeEx(drift=True)
    r = run_replay(exd, st, _he(exd, st), p, "file:///s2/index.html", tempfile.mkdtemp())
    assert r["exit_code"] == 2, r           # a11y regression gates exit 2
    assert r["healed"] == 1, r              # broken role+name healed via testid
    assert any(g["page"] == "index.html" and g["exit2"] for g in r["regressions"]), r


def test_visual_only_regression_is_advisory_exit0():
    p, st = _plan(), _store()
    exc = FakeEx(drift=False)
    run_replay(exc, st, _he(exc, st), p, p["target_url"], tempfile.mkdtemp(), baseline=True)
    exv = FakeEx(visual_only=True)
    r = run_replay(exv, st, _he(exv, st), p, "file:///s2/index.html", tempfile.mkdtemp())
    assert r["exit_code"] == 0, r           # visual-only does NOT gate exit 2
    assert any(g["page"] == "index.html" and not g["exit2"] for g in r["regressions"]), r


def test_quarantine_suppresses_and_resets_on_aut_change():
    st = _store()
    for _ in range(3):
        st.record_step("p1", "sX", passed=False, aut_sha="shaA")
    assert st.is_quarantined("p1", "sX")                                  # 3/5 fails -> quarantined
    assert st.record_step("p1", "sX", passed=False, aut_sha="shaA") is True
    assert st.record_step("p1", "sX", passed=False, aut_sha="shaB") is False  # app changed -> window reset


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
