"""Offline M14 W4 tests — AG-UI event emission + full auto-escalate-to-HITL (ADR-055).

Run:  .venv/bin/python tests/test_m14_agui_offline.py

No browser / network / real LLM / orchestrator. Proves:
- `brain/agui.py`: `emit()` prints exactly one `@@AGUI <json>` line per call — compact JSON, the
  frozen envelope shape ({type,run_id,seq,ts,data}), a monotonic per-process `seq`;
- `heal` node: every entry (a failed act+verify that the stub can't recover) increments
  `consecutive_heal_failures`; a successful `verify` resets it to 0; `verify` failures accumulate
  into `failed_steps`;
- `checkpoint` node: past `SENTINEL_AUTO_HITL_THRESHOLD` consecutive heal failures, it arms the
  SAME `_takeover_armed` latch an operator takeover would (docs/M14_CONTRACT.md §4) and emits
  `hitl_needed` — the existing `route_checkpoint` then routes to the `takeover` node exactly as it
  does for an operator-initiated pause (ADR-054), so `app.invoke()` returns with `__interrupt__`;
- REGRESSION: `SENTINEL_AUTO_HITL_THRESHOLD` unset/0 (the default) NEVER arms, even across many
  consecutive heal misses — byte-identical to the pre-M14 operator-only takeover behavior;
- REGRESSION: the AG-UI emission is fully additive over the existing R2/R3 offline harness
  (WalkEx/QueuedFakeBackend/GoalPlanner/MemorySaver) — explore+author still completes, still
  produces the same scenario_steps, and the heal-miss counters stay at 0 (WalkEx never fails).
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import agui, budget                                              # noqa: E402
from brain.graph import build_graph                                         # noqa: E402
from brain.planner import GoalPlanner, HeuristicPlanner                     # noqa: E402

# Reuse the r2_multiturn offline harness (2-page fake site + queued fake LLM) for the "AG-UI is
# additive over an existing authored run" regression test.
from tests.test_r2_multiturn_offline import (                              # noqa: E402
    WalkEx, QueuedFakeBackend, _explore_init, _reply, _USER_SID, _LOGIN,
)


def _parse_agui_lines(text: str) -> list:
    """Pull every `@@AGUI` envelope out of captured stdout, in order."""
    return [json.loads(line[len(agui.PREFIX):]) for line in text.splitlines()
            if line.startswith(agui.PREFIX)]


class FailNTimesEx:
    """A minimal fake pw-executor: one page, one clickable 'Boom' button. The first `fails` click
    attempts raise (act() fails -> verify() routes to heal()); every click after that succeeds. No
    links, so the run converges (no_candidates) the moment the button is finally exercised."""
    URL = "file:///s/fail.html"

    def __init__(self, fails: int):
        self.fails, self.clicks, self.n = fails, 0, 0

    def call(self, m: str, **p) -> dict:
        self.n += 1
        if m == "browser.navigate":
            return {"url": self.URL}
        if m == "browser.currentUrl":
            return {"url": self.URL, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "page", "nodeCount": 1}
        if m == "browser.interactives":
            return {"elements": [{"tag": "button", "role": "button", "name": "Boom",
                                  "testid": "boom", "text": "Boom"}]}
        if m == "browser.links":
            return {"links": []}
        if m == "browser.click":
            self.clicks += 1
            if self.clicks <= self.fails:
                raise RuntimeError(f"boom #{self.clicks}")
            return {}
        return {}

    def close(self) -> None:
        pass


def _init(target: str, max_steps: int = 40) -> dict:
    """A cold pure-explore init (current_step=0, empty page_model) — no scenario_head, so the run
    is explore-only (no LLM anywhere in this file)."""
    return {"run_id": "m14-test", "run_mode": "explore", "target_url": target,
            "base_origin": "file:///s/", "coverage_target": 0.85, "max_steps": max_steps,
            "artifact_dir": tempfile.mkdtemp(), "goal": "", "describe": "", "site_map": {},
            "phase": "explore", "scenario_steps": [], "scenario_unmatched": [],
            "current_url": target, "page_model": {}, "exploration_plan": [], "plan_hash": "",
            "current_step": 0, "interactive_seen": [], "interactive_exercised": [],
            "visited_paths": [], "nav_frontier": [], "coverage_achieved": 0.0,
            "exploration_complete": False, "executed_actions": [], "errors": []}


def _build(ex, max_steps: int = 40):
    from langgraph.checkpoint.memory import MemorySaver
    os.environ.pop("ORCH_ADDR", None)   # no orchestrator wired -> runcontrol.make_client() -> _Noop
    app = build_graph(ex, HeuristicPlanner(), lambda r: None).compile(checkpointer=MemorySaver())
    cfg = {"recursion_limit": 200, "configurable": {"thread_id": "m14-thread"}}
    return app, cfg


# --- agui.emit: envelope shape, compact JSON, monotonic seq ------------------
def test_agui_emit_envelope_shape_and_seq():
    buf = io.StringIO()
    seq_before = agui._seq
    with redirect_stdout(buf):
        agui.emit("test_event", "run-abc", foo="bar", n=3)
        agui.emit("test_event2", "run-abc", n=4)
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    assert len(lines) == 2, lines
    envs = []
    for line in lines:
        assert line.startswith(agui.PREFIX), line
        raw = line[len(agui.PREFIX):]
        assert raw.startswith("{"), raw
        assert ", " not in raw and ": " not in raw, f"not compact json: {raw!r}"
        env = json.loads(raw)
        assert set(env.keys()) == {"type", "run_id", "seq", "ts", "data"}, env
        assert env["run_id"] == "run-abc"
        assert isinstance(env["seq"], int)
        datetime.fromisoformat(env["ts"])   # raises if not valid ISO8601
        envs.append(env)
    assert envs[0]["type"] == "test_event" and envs[0]["data"] == {"foo": "bar", "n": 3}
    assert envs[1]["type"] == "test_event2" and envs[1]["data"] == {"n": 4}
    assert envs[1]["seq"] == envs[0]["seq"] + 1, "seq is monotonic per process"
    assert envs[0]["seq"] > seq_before


# --- heal-miss counter increments; a successful verify resets it -------------
def test_heal_failure_counter_increments_and_resets_on_success():
    os.environ.pop("SENTINEL_AUTO_HITL_THRESHOLD", None)   # OFF (default) for this test
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    ex = FailNTimesEx(fails=2)
    app, cfg = _build(ex)
    buf = io.StringIO()
    with redirect_stdout(buf):
        final = app.invoke(_init(FailNTimesEx.URL), config=cfg)

    assert not final.get("__interrupt__"), "threshold OFF must never interrupt"
    assert final.get("exploration_complete"), final
    assert final.get("failed_steps") == 2, final.get("failed_steps")
    assert final.get("consecutive_heal_failures", 0) == 0, "the 3rd (successful) click must reset it"
    assert not final.get("_takeover_armed")

    events = _parse_agui_lines(buf.getvalue())
    heals = [e for e in events if e["type"] == "heal"]
    assert len(heals) == 2, heals
    assert all(h["data"]["ok"] is False for h in heals)
    assert any(e["type"] == "run.started" for e in events)
    assert not any(e["type"] == "hitl_needed" for e in events), "must not fire below threshold"
    budget.reset()


# --- past threshold: arms the SAME takeover latch + emits hitl_needed --------
def test_auto_hitl_threshold_arms_takeover_and_emits_hitl_needed():
    os.environ["SENTINEL_AUTO_HITL_THRESHOLD"] = "2"
    try:
        budget.reset(plan_limit=10**6, heal_limit=10**6)
        ex = FailNTimesEx(fails=5)   # more failures available than the threshold needs
        app, cfg = _build(ex)
        buf = io.StringIO()
        with redirect_stdout(buf):
            t1 = app.invoke(_init(FailNTimesEx.URL), config=cfg)

        assert t1.get("__interrupt__"), "the threshold breach must pause the graph (like an operator takeover)"
        assert app.get_state(cfg).next, "paused mid-graph with a pending next node"
        assert t1.get("consecutive_heal_failures") == 2, t1.get("consecutive_heal_failures")
        assert not t1.get("exploration_complete"), "must not have converged — it's paused, not done"

        events = _parse_agui_lines(buf.getvalue())
        hitl = [e for e in events if e["type"] == "hitl_needed"]
        assert len(hitl) == 1, hitl
        assert hitl[0]["data"] == {"reason": "consecutive_heal_failures", "count": 2}, hitl[0]
        assert set(hitl[0].keys()) == {"type", "run_id", "seq", "ts", "data"}
        heals = [e for e in events if e["type"] == "heal"]
        assert len(heals) == 2, "must pause exactly at the threshold, not run further misses first"
        budget.reset()
    finally:
        os.environ.pop("SENTINEL_AUTO_HITL_THRESHOLD", None)


# --- regression: threshold=0 (default/unset) never arms, however many misses ---
def test_threshold_zero_never_arms_regression():
    os.environ.pop("SENTINEL_AUTO_HITL_THRESHOLD", None)
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    ex = FailNTimesEx(fails=10)          # never succeeds within the run
    app, cfg = _build(ex, max_steps=5)   # converges via max_steps, still 5 consecutive misses
    buf = io.StringIO()
    with redirect_stdout(buf):
        final = app.invoke(_init(FailNTimesEx.URL, max_steps=5), config=cfg)

    assert not final.get("__interrupt__"), "threshold=0 must NEVER interrupt, even with 5 consecutive misses"
    assert not final.get("_takeover_armed")
    assert not final.get("takeover_returns")
    # NB: route_checkpoint routes max_steps-exhaustion straight to "scenario" without ever setting
    # exploration_complete (that flag is only set by plan()'s own convergence check) — plan_hash
    # being written is the proof report() ran the walk to completion via the OTHER convergence path.
    assert final.get("consecutive_heal_failures") == 5, final.get("consecutive_heal_failures")
    assert final.get("plan_hash"), "report() ran to completion (byte-identical convergence path)"

    events = _parse_agui_lines(buf.getvalue())
    assert not any(e["type"] == "hitl_needed" for e in events)
    heals = [e for e in events if e["type"] == "heal"]
    assert len(heals) == 5, heals
    verdict = next(e for e in events if e["type"] == "verdict")
    assert verdict["data"]["failed"] == 5, verdict   # errors were real (FailNTimesEx genuinely raised)
    budget.reset()


# --- regression: AG-UI emission is additive over the R2/R3 explore+author harness ---
def test_walkex_regression_agui_emits_without_breaking_authoring():
    from langgraph.checkpoint.memory import MemorySaver
    os.environ.pop("SENTINEL_AUTO_HITL_THRESHOLD", None)
    os.environ.pop("ORCH_ADDR", None)
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    app = build_graph(ex, HeuristicPlanner(), lambda r: None,
                      scenario_head=GoalPlanner(goal="log in", backend=fb)).compile(checkpointer=MemorySaver())
    buf = io.StringIO()
    with redirect_stdout(buf):
        final = app.invoke(_explore_init(goal="log in"),
                           config={"recursion_limit": 200, "configurable": {"thread_id": "m14-regress"}})

    assert not final.get("__interrupt__")
    assert final.get("scenario_steps"), "regression: existing explore+author flow still completes"
    assert final.get("consecutive_heal_failures", 0) == 0, "WalkEx never fails a click -> no heal misses"
    assert final.get("failed_steps", 0) == 0

    events = _parse_agui_lines(buf.getvalue())
    types = [e["type"] for e in events]
    assert "run.started" in types and "verdict" in types
    assert "heal" not in types, "the heal node is never entered on an all-succeeding walk"
    verdict = next(e for e in events if e["type"] == "verdict")
    assert verdict["data"]["verdict"] == "ok" and verdict["data"]["exit_code"] == 0, verdict
    budget.reset()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
