"""Offline test for M14 tail 2 — AG-UI emission + auto-HITL signal on the REPLAY path.

Run:  .venv/bin/python tests/test_m14_replay_agui_offline.py     (or: pytest tests/)

The graph modes emit AG-UI events (M14 W4); the run_replay path did not — it degraded a replay/baseline
run to a raw `log` timeline with no chips and no auto-HITL signal (docs/M14_CONTRACT.md §7, GAP-M9-21).
This exercises the wiring: run_replay now emits run.started · step.progress · heal (with the REAL L1–L6
strategy/confidence, not the graph stub's always-ok=False) · verdict, and emits `hitl_needed` when
consecutive heal MISSES reach SENTINEL_AUTO_HITL_THRESHOLD. No browser, no network, no LLM.

Composes test_m3_offline.py's FakeEx/HealingEngine fixtures (a deterministic, offline heal) with
test_m14_agui_offline.py's redirect_stdout + @@AGUI parse pattern.
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import agui                       # noqa: E402
from brain.store import Store                # noqa: E402
from brain.healing import HealingEngine      # noqa: E402
from brain.replay import run_replay          # noqa: E402
from brain.state import canonical_plan_hash  # noqa: E402


def _parse(text):
    return [__import__("json").loads(l[len(agui.PREFIX):]) for l in text.splitlines()
            if l.startswith(agui.PREFIX)]


def _types(events):
    return [e["type"] for e in events]


def _store():
    return Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)


# --- a FakeEx that heals the "Old" button via the testid alternative (real auto_healed) ----------
class HealEx:
    def __init__(self):
        self.url = ""

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p["url"]
            return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": '- button "Launch"'}
        if m == "browser.screenshotHash":
            return {"hash": "shot"}
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.probe":
            loc = p["locator"]
            if loc.get("testid") == "cta":
                return {"count": 1}          # the healed alternative resolves
            return {"count": 0}              # the primary (role/name) is broken -> forces heal
        return {}


# --- a FakeEx where NOTHING resolves: every heal MISSES (drives the auto-HITL counter) ------------
class MissEx:
    def __init__(self):
        self.url = ""

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p["url"]
            return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- text nothing"}
        if m == "browser.screenshotHash":
            return {"hash": "shot"}
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.probe":
            return {"count": 0}              # nothing ever resolves -> heal always misses
        return {}


def _heal_plan():
    steps = [
        {"step_id": 1, "action_type": "navigate", "semantic_id": "nav1", "intent": "nav",
         "target": "file:///s/index.html", "locator": None, "alternatives": None},
        {"step_id": 2, "action_type": "click", "semantic_id": "sidB", "intent": "click the CTA",
         "locator": {"role": "button", "name": "Old"},
         "alternatives": [{"strategy": "testid", "locator": {"testid": "cta"}, "prior": 0.95}]},
    ]
    return {"plan_id": "p1", "target_url": "file:///s/index.html",
            "plan_hash": canonical_plan_hash(steps), "steps": steps}


def _miss_plan(n_clicks):
    steps = [{"step_id": 1, "action_type": "navigate", "semantic_id": "nav1", "intent": "nav",
              "target": "file:///s/index.html", "locator": None, "alternatives": None}]
    for i in range(n_clicks):
        steps.append({"step_id": 2 + i, "action_type": "click", "semantic_id": f"sid{i}",
                      "intent": "click", "locator": {"role": "button", "name": f"Gone{i}"},
                      "alternatives": []})
    return {"plan_id": "pm", "target_url": "file:///s/index.html",
            "plan_hash": canonical_plan_hash(steps), "steps": steps}


def _run(ex, plan, **kw):
    st = _store()
    heal = HealingEngine(ex, st, "r", use_llm=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report = run_replay(ex, st, heal, plan, plan["target_url"], tempfile.mkdtemp(), run_id="rX", **kw)
    return report, _parse(buf.getvalue())


def test_replay_emits_full_timeline():
    report, ev = _run(HealEx(), _heal_plan())
    assert report["exit_code"] == 0 and report["healed"] == 1, report
    types = _types(ev)
    for want in ("run.started", "step.progress", "heal", "verdict"):
        assert want in types, f"missing {want} in {types}"
    # every event carries the run_id we passed
    assert all(e["run_id"] == "rX" for e in ev), ev
    # run.started names the mode/target/planner
    started = next(e for e in ev if e["type"] == "run.started")
    assert started["data"]["mode"] == "replay" and started["data"]["target"] == "file:///s/index.html"
    # step.progress counts 1..total
    progs = [e for e in ev if e["type"] == "step.progress"]
    assert progs[0]["data"]["n"] == 1 and progs[-1]["data"]["total"] == 2, progs
    # the heal event carries the REAL outcome (ok=True) + strategy/confidence, not the graph stub
    heal = next(e for e in ev if e["type"] == "heal")
    assert heal["data"]["ok"] is True and heal["data"]["strategy"], heal
    assert isinstance(heal["data"].get("confidence"), (int, float)), heal
    # verdict carries the REAL structured exit code
    verdict = next(e for e in ev if e["type"] == "verdict")
    assert verdict["data"]["exit_code"] == 0 and verdict["data"]["verdict"] == "pass", verdict


def test_baseline_mode_emits_started_and_verdict():
    report, ev = _run(HealEx(), _heal_plan(), baseline=True)
    assert report["exit_code"] == 0, report
    started = next(e for e in ev if e["type"] == "run.started")
    assert started["data"]["mode"] == "baseline", started
    assert any(e["type"] == "verdict" for e in ev), _types(ev)


def test_auto_hitl_signal_fires_at_threshold():
    # two consecutive heal misses; threshold 2 -> a hitl_needed once the streak reaches 2
    os.environ["SENTINEL_AUTO_HITL_THRESHOLD"] = "2"
    try:
        report, ev = _run(MissEx(), _miss_plan(2))
    finally:
        del os.environ["SENTINEL_AUTO_HITL_THRESHOLD"]
    assert report["exit_code"] == 1, report  # both clicks failed to heal
    hitls = [e for e in ev if e["type"] == "hitl_needed"]
    assert hitls, f"expected a hitl_needed at threshold 2, got {_types(ev)}"
    assert hitls[0]["data"]["count"] >= 2 and hitls[0]["data"]["reason"] == "consecutive_heal_failures", hitls
    # every miss emitted a heal ok=False
    heals = [e for e in ev if e["type"] == "heal"]
    assert heals and all(h["data"]["ok"] is False for h in heals), heals


def test_threshold_zero_emits_no_hitl():
    # threshold 0 (default/off): the timeline still emits, but NO hitl_needed signal — parity with
    # graph-mode where threshold=0 is inert.
    os.environ.pop("SENTINEL_AUTO_HITL_THRESHOLD", None)
    report, ev = _run(MissEx(), _miss_plan(3))
    assert not any(e["type"] == "hitl_needed" for e in ev), _types(ev)
    # but the rich timeline is still there
    assert "heal" in _types(ev) and "verdict" in _types(ev), _types(ev)


def test_healed_step_resets_the_streak():
    # LOAD-BEARING (the reset must matter): miss, miss, SUCCESS, miss, miss at threshold 3.
    #   working reset -> streak peaks at 2 (1,2,reset 0,1,2)          -> NO hitl_needed
    #   broken/no-op reset -> streak accumulates to 4 (1,2,3,4)       -> hitl_needed WOULD fire
    # So asserting "no hitl_needed" only passes because the reset works.
    os.environ["SENTINEL_AUTO_HITL_THRESHOLD"] = "3"
    try:
        healable = {"action_type": "click", "semantic_id": "sidH", "intent": "click",
                    "locator": {"role": "button", "name": "Old"},
                    "alternatives": [{"strategy": "testid", "locator": {"testid": "cta"}, "prior": 0.95}]}
        gone = lambda i: {"action_type": "click", "semantic_id": f"g{i}", "intent": "click",
                          "locator": {"role": "button", "name": f"Gone{i}"}, "alternatives": []}
        steps = [{"step_id": 1, "action_type": "navigate", "semantic_id": "nav1", "intent": "nav",
                  "target": "file:///s/index.html", "locator": None, "alternatives": None},
                 gone(0), gone(1), {**healable, "step_id": 4}, gone(2), gone(3)]
        for i, s in enumerate(steps):
            s.setdefault("step_id", i + 1)
        plan = {"plan_id": "pmix", "target_url": "file:///s/index.html",
                "plan_hash": canonical_plan_hash(steps), "steps": steps}

        class MixEx(MissEx):  # everything misses EXCEPT the testid alternative (so the healable step passes)
            def call(self, m, **p):
                if m == "browser.probe" and p["locator"].get("testid") == "cta":
                    return {"count": 1}
                return super().call(m, **p)

        report, ev = _run(MixEx(), plan)
    finally:
        del os.environ["SENTINEL_AUTO_HITL_THRESHOLD"]
    assert not any(e["type"] == "hitl_needed" for e in ev), _types(ev)
    # sanity: the healable step really did heal (so the reset had something to reset)
    assert report["healed"] == 1, report


def test_non_heal_failure_also_counts_toward_the_streak():
    # A navigate that THROWS is a real failure but not a heal miss. It must still extend the streak —
    # graph-mode counts every failure, and a run stuck on navigation should summon a human too.
    class NavFailEx(MissEx):
        def call(self, m, **p):
            if m == "browser.navigate":
                raise RuntimeError("network down")
            return super().call(m, **p)

    steps = [{"step_id": i + 1, "action_type": "navigate", "semantic_id": f"n{i}", "intent": "nav",
              "target": "file:///s/p.html", "locator": None, "alternatives": None} for i in range(2)]
    plan = {"plan_id": "pnav", "target_url": "file:///s/p.html",
            "plan_hash": canonical_plan_hash(steps), "steps": steps}
    os.environ["SENTINEL_AUTO_HITL_THRESHOLD"] = "2"
    try:
        report, ev = _run(NavFailEx(), plan)
    finally:
        del os.environ["SENTINEL_AUTO_HITL_THRESHOLD"]
    hitls = [e for e in ev if e["type"] == "hitl_needed"]
    assert hitls, f"navigate failures must count toward the auto-HITL streak, got {_types(ev)}"
    assert hitls[0]["data"]["count"] >= 2, hitls


def test_malformed_threshold_does_not_crash():
    # a garbage SENTINEL_AUTO_HITL_THRESHOLD must not crash the replay (parse guarded -> off)
    os.environ["SENTINEL_AUTO_HITL_THRESHOLD"] = "not-a-number"
    try:
        report, ev = _run(MissEx(), _miss_plan(2))
    finally:
        del os.environ["SENTINEL_AUTO_HITL_THRESHOLD"]
    assert report["exit_code"] == 1, report          # ran to completion, did not crash
    assert not any(e["type"] == "hitl_needed" for e in ev), _types(ev)


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"ALL PASS ({len(tests)})")


if __name__ == "__main__":
    _main()
