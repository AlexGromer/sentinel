"""Offline M9.8 F4 / R3 tests — operator co-pilot takeover (interrupt) / return (resume), ADR-054.

Run:  .venv/bin/python tests/test_r3_takeover_offline.py

No browser / network / real LLM / orchestrator. Proves the brain-side takeover design (the live half —
extension + CDP — is @0xCoDSnet #47, gated to M9-LIVE):
- a pending takeover (signalled via a FAKE RunControl client) makes the explore graph interrupt() at the
  checkpoint boundary — app.invoke() returns with __interrupt__ and a pending next node, NOTHING authored;
- on Return (the fake clears the flag) the graph RESUMES the SAME checkpointer thread with
  Command(resume=...) — it continues from the pause (no restart), records the resume payload in
  `takeover_returns`, and finishes explore+author;
- the resume survives a FRESH SqliteSaver (the takeover window may outlive the brain process — risk-spike #4);
- _GrpcRunControl._verb precedence (abort > takeover > continue);
- _resume_through_takeovers (brain/__main__) loops invoke→await-return→Command(resume) until complete;
- REGRESSION: with no orchestrator wired (rc left default → _Noop) the graph never interrupts (byte-identical
  to the M9.2b one-shot path).
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.types import Command                                         # noqa: E402

from brain import budget                                                    # noqa: E402
from brain.graph import build_graph                                         # noqa: E402
from brain.planner import GoalPlanner, HeuristicPlanner                     # noqa: E402
from brain.runcontrol import CONTINUE, ABORT, TAKEOVER, _GrpcRunControl, _Noop  # noqa: E402

# Reuse the r2_multiturn offline harness (2-page fake site + queued fake LLM + explore-init builder).
from tests.test_r2_multiturn_offline import (                              # noqa: E402
    WalkEx, QueuedFakeBackend, _explore_init, _reply, _USER_SID, _LOGIN,
)


class FakeRC:
    """A fake RunControl client. poll() reports a pending takeover until release() is called (mirrors the
    orchestrator's Takeover→Return flip); report() never aborts (budget is unlimited here)."""

    def __init__(self, takeover_pending=True):
        self.released = not takeover_pending
        self.polls = self.reports = 0

    def report(self, run_id, node, prompt_tokens, completion_tokens, status="running"):
        self.reports += 1
        return CONTINUE

    def poll(self, run_id, node="checkpoint"):
        self.polls += 1
        return CONTINUE if self.released else TAKEOVER

    def release(self):
        self.released = True

    def close(self):
        pass


def _build(rc, backend, saver):
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    app = build_graph(ex, HeuristicPlanner(), lambda r: None,
                      scenario_head=GoalPlanner(goal="log in", backend=backend), rc=rc).compile(checkpointer=saver)
    return app, ex


# --- core: takeover interrupts, return resumes (in-process MemorySaver) ------
def test_takeover_interrupts_then_returns_resumes():
    from langgraph.checkpoint.memory import MemorySaver
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    rc = FakeRC(takeover_pending=True)
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    app, _ex = _build(rc, fb, MemorySaver())
    cfg = {"recursion_limit": 200, "configurable": {"thread_id": "to-1"}}

    # PAUSE: the first checkpoint poll sees a takeover -> interrupt() at the boundary.
    t1 = app.invoke(_explore_init(goal="log in"), config=cfg)
    assert t1.get("__interrupt__"), "a pending takeover must interrupt the graph"
    assert app.get_state(cfg).next, "the graph paused with a pending next node (state persisted mid-graph)"
    assert not t1.get("scenario_steps"), "nothing may be authored while paused for takeover"
    assert rc.polls >= 1

    # RETURN: orchestrator clears the flag -> resume the SAME thread, carrying the operator's delta.
    rc.release()
    payload = {"returned": True, "delta": "human_paid"}
    final = app.invoke(Command(resume=payload), config=cfg)
    assert not final.get("__interrupt__"), "resume completed the graph"
    assert final.get("scenario_steps"), "explore+author finished after the return"
    assert final.get("takeover_returns") == [payload], final.get("takeover_returns")
    budget.reset()


# --- the takeover window outlives the brain process (fresh SqliteSaver) ------
def test_takeover_resume_survives_fresh_saver_process():
    from langgraph.checkpoint.sqlite import SqliteSaver
    db = os.path.join(tempfile.mkdtemp(), "checkpoint.db")
    cfg = {"recursion_limit": 200, "configurable": {"thread_id": "to-fresh"}}
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])

    # PROCESS 1 — takeover pending: invoke pauses at the checkpoint interrupt, then the saver closes.
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    with SqliteSaver.from_conn_string(db) as s1:
        app1, _ = _build(FakeRC(takeover_pending=True), fb, s1)
        t1 = app1.invoke(_explore_init(goal="log in"), config=cfg)
    assert t1.get("__interrupt__"), "process-1 paused for takeover"
    assert not t1.get("scenario_steps")

    # PROCESS 2 — a FRESH saver (new brain process), takeover already returned: resume from the pause.
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    with SqliteSaver.from_conn_string(db) as s2:
        app2, _ = _build(FakeRC(takeover_pending=False), fb, s2)
        final = app2.invoke(Command(resume={"returned": True}), config=cfg)
    assert not final.get("__interrupt__"), "resumed across the process boundary"
    assert final.get("scenario_steps"), "explore+author finished in the fresh process"
    assert final.get("takeover_returns") == [{"returned": True}], final.get("takeover_returns")
    budget.reset()


# --- abort during a takeover converges immediately (abort > takeover) --------
def test_abort_during_takeover_converges():
    """If the orchestrator ABORTS while the graph is paused for a takeover, the resume must converge the
    run immediately (no further browser-driving cycle) — abort > takeover in the wait window too."""
    from langgraph.checkpoint.memory import MemorySaver
    budget.reset(plan_limit=10**6, heal_limit=10**6)

    class AbortAfterTakeoverRC:
        """poll: takeover once (arm+interrupt), then ABORT forever (operator/orchestrator kills the paused run)."""
        def __init__(self):
            self.polls = 0

        def report(self, run_id, node, p, c, status="running"):
            return CONTINUE

        def poll(self, run_id, node="checkpoint"):
            self.polls += 1
            return TAKEOVER if self.polls == 1 else ABORT

        def close(self):
            pass

    rc = AbortAfterTakeoverRC()
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    app, ex = _build(rc, fb, MemorySaver())
    cfg = {"recursion_limit": 200, "configurable": {"thread_id": "abort-to"}}

    t1 = app.invoke(_explore_init(goal="log in"), config=cfg)
    assert t1.get("__interrupt__"), "first poll = takeover -> interrupt"
    calls_at_pause = ex.n

    # resume: the checkpoint after the takeover node now polls ABORT -> converge straight to scenario.
    final = app.invoke(Command(resume={"returned": True}), config=cfg)
    assert not final.get("__interrupt__")
    assert final.get("exploration_complete"), "abort during the takeover converged the run"
    assert ex.n == calls_at_pause, "abort must NOT drive one more browser cycle (perceive/ground/act)"
    budget.reset()


# --- verb mapping precedence (abort > takeover > continue) -------------------
def test_grpc_verb_precedence():
    class _C:
        def __init__(self, abort=False, takeover=False):
            self.abort, self.takeover, self.reason = abort, takeover, ""
    v = _GrpcRunControl._verb
    assert v(_C()) == CONTINUE
    assert v(_C(takeover=True)) == TAKEOVER
    assert v(_C(abort=True)) == ABORT
    assert v(_C(abort=True, takeover=True)) == ABORT, "abort (hard stop) beats takeover (pause)"
    # the no-op client never aborts and never pauses (standalone CLI path).
    n = _Noop()
    assert n.report("r", "plan", 0, 0) == CONTINUE and n.poll("r") == CONTINUE


# --- _resume_through_takeovers drive loop (no browser; fake app + fake rc) ---
def test_resume_through_takeovers_loop():
    import brain.__main__ as m

    class FakeApp:
        """Returns __interrupt__ for `interrupts` resume calls, then completes. Records each invoke arg."""
        def __init__(self, interrupts):
            self.left, self.invokes = interrupts, []

        def invoke(self, arg, config=None):
            self.invokes.append(arg)
            if self.left > 0:
                self.left -= 1
                return {"__interrupt__": [object()]}
            return {"done": True}

    class RC:
        def __init__(self):
            self.polls = 0

        def poll(self, run_id, node="checkpoint"):
            self.polls += 1
            return TAKEOVER if self.polls < 2 else CONTINUE   # one wait-cycle, then released

        def report(self, *a, **k):
            return CONTINUE

        def close(self):
            pass

    app, rc = FakeApp(interrupts=0), RC()           # the passed-in `final` already carries __interrupt__
    final = m._resume_through_takeovers(app, {"__interrupt__": [object()]},
                                        {"configurable": {"thread_id": "x"}}, rc, "run-x")
    assert final == {"done": True}, final
    assert len(app.invokes) == 1 and isinstance(app.invokes[0], Command), app.invokes
    assert rc.polls >= 2, "awaited the return (polled until takeover cleared)"


# --- _run_chat cold turn: a takeover pauses + resumes (finding-3 regression) --
def test_run_chat_cold_turn_takeover_resumes():
    """A takeover during the chat COLD turn must pause + resume (mirror _run_explore), NOT tear down the
    browser and fail the turn. Without the _resume_through_takeovers wrap the cold invoke returns
    interrupted with no scenario_steps → exit 1; with it, the run resumes, authors, and exits 0."""
    import brain.__main__ as m
    import brain.llm as llm
    from brain import runcontrol

    class FakeExec:
        """WalkEx-like cold-turn executor: drives the 2-page site; raises after shutdown so a
        browser-torn-down-mid-pause bug would ALSO surface (belt and suspenders)."""
        def __init__(self):
            self.inner, self.dead = WalkEx(), False

        def call(self, method, **p):
            if self.dead:
                raise RuntimeError(f"executor used after shutdown ({method})")
            if method == "shutdown":
                self.dead = True
                return {}
            if method in ("initialize", "browser.traceStop"):
                return {}
            return self.inner.call(method, **p)

        def close(self):
            pass

    rc = FakeRC(takeover_pending=True)   # poll #1 = takeover (arm+interrupt); released before the resume
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    out = pathlib.Path(tempfile.mkdtemp())
    db = os.path.join(tempfile.mkdtemp(), "conversations.db")

    budget.reset(plan_limit=10**6, heal_limit=10**6)
    saved = dict(os.environ)
    orig_mx, orig_mb, orig_mc = m.make_executor, llm.make_backend, runcontrol.make_client
    # release the takeover as soon as _resume_through_takeovers begins to await it (mirrors the operator
    # Return arriving): the first wait-loop poll returns "continue".
    _orig_poll = rc.poll

    def poll_then_release(run_id, node="checkpoint"):
        v = _orig_poll(run_id, node)
        rc.release()
        return v
    rc.poll = poll_then_release

    os.environ.update({"GOAL": "log in", "DESCRIBE": "", "PW_EXECUTOR_CMD": "x",
                       "CHECKPOINT_DSN": "", "SENTINEL_CONVERSATIONS_DB": db,
                       "SENTINEL_CONVERSATION_ID": "cold-to"})
    m.make_executor = lambda cmd: FakeExec()
    llm.make_backend = lambda role: fb
    runcontrol.make_client = lambda: rc
    try:
        code = m._run_chat("cold1", out, "cold-to", _LOGIN, 0.85, 40)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        m.make_executor, llm.make_backend, runcontrol.make_client = orig_mx, orig_mb, orig_mc

    assert code == 0, f"cold-turn takeover must resume + author -> exit 0 (buggy path returns 1); got {code}"
    assert rc.polls >= 2, "the graph paused (arm) then resumed after the takeover cleared"
    sc = json.loads((out / "scenario.json").read_text())
    assert sc["steps"], "authored a scenario after resuming from the takeover"
    budget.reset()


# --- regression: no orchestrator wired -> never interrupts -------------------
def test_no_orchestrator_never_interrupts():
    from langgraph.checkpoint.memory import MemorySaver
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    # rc left DEFAULT (None -> make_client() -> _Noop because ORCH_ADDR is unset) — the production path.
    os.environ.pop("ORCH_ADDR", None)
    app = build_graph(ex, HeuristicPlanner(), lambda r: None,
                      scenario_head=GoalPlanner(goal="log in", backend=fb)).compile(checkpointer=MemorySaver())
    final = app.invoke(_explore_init(goal="log in"),
                       config={"recursion_limit": 200, "configurable": {"thread_id": "noop"}})
    assert not final.get("__interrupt__"), "no orchestrator -> no takeover -> no interrupt"
    assert final.get("scenario_steps"), "byte-identical to the one-shot explore+author path"
    assert not final.get("takeover_returns"), "no takeover cycles recorded"
    budget.reset()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
