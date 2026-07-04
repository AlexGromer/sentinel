"""Offline M13 wave-5 tests — chats projection + GAP-M9-20 (refine-history cap + rolling summary).

Run:  .venv/bin/python tests/test_m13_chats_offline.py

No browser / network / gateway. Proves:
- `_capped_history`/`_rolling_summary` bound a growing conversation (short = byte-identical);
- the cap reaches the refine prompt (a long conversation gets an "[earlier: …]" summary prefix);
- `_project_chat` emits the chats projection (conversation_id/turn_count/last_goal/summary) and is a
  no-op without a store-gateway (STORE_ADDR unset).
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import budget                                                    # noqa: E402
from brain.graph import build_graph, _capped_history, _rolling_summary      # noqa: E402
from brain.planner import GoalPlanner, HeuristicPlanner                     # noqa: E402

from tests.test_r2_multiturn_offline import (                              # noqa: E402
    WalkEx, QueuedFakeBackend, _explore_init, _reply, _USER_SID, _LOGIN,
)


# --- GAP-M9-20: cap + rolling summary (pure) ---------------------------------
def test_capped_history_and_summary():
    assert _capped_history([]) == []
    assert _capped_history(["a", "b", "c"]) == ["a", "b", "c"]           # <= keep -> unchanged
    assert _capped_history(["a", "b", "c"], keep=6) == ["a", "b", "c"]
    long = [f"t{i}" for i in range(10)]
    capped = _capped_history(long, keep=6)
    assert len(capped) == 7, capped                                       # 1 summary line + last 6
    assert capped[0].startswith("[earlier:") and capped[1:] == ["t4", "t5", "t6", "t7", "t8", "t9"], capped
    assert "4 turn(s)" in capped[0], capped[0]                            # 10 - 6 = 4 older turns summarised
    assert _rolling_summary([]) == ""
    s = _rolling_summary(["log in", "pay"])
    assert "2 turn(s)" in s and "log in" in s, s


# --- GAP-M9-20: the cap reaches the refine prompt ----------------------------
def test_refine_history_capped_in_prompt():
    from langgraph.checkpoint.memory import MemorySaver
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "v"}])])
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    app = build_graph(ex, HeuristicPlanner(), lambda r: None,
                      scenario_head=GoalPlanner(goal="s", backend=fb)).compile(checkpointer=MemorySaver())
    cfg = {"recursion_limit": 200, "configurable": {"thread_id": "cap"}}
    # cold turn-1 (explore + author), then 7 warm refine turns -> 8 total; turn-8's prior history (7
    # turns) exceeds the keep=6 cap, so the oldest collapses into a summary prefix.
    app.invoke(_explore_init(goal="turn-1 open the app",
                             messages=[{"role": "user", "content": "turn-1 open the app"}]), config=cfg)
    for i in range(2, 9):
        budget.reset(plan_limit=10**6, heal_limit=10**6)
        app.invoke({"messages": [{"role": "user", "content": f"turn-{i} refine step {i}"}],
                    "goal": f"turn-{i} refine step {i}"}, config=cfg)
    p = fb.prompts[-1]
    assert "[earlier:" in p, "a long conversation must cap older turns into a summary prefix"
    assert "1 turn(s)" in p, "7 prior turns - keep 6 = 1 older turn summarised"
    budget.reset()


# --- chats projection emission (patched projector; no gateway) ---------------
def test_project_chat_emits_projection():
    import brain.__main__ as m
    import brain.store as store

    captured = {}

    class FakeProjector:
        def upsert_chat(self, **kw):
            captured.update(kw)

        def close(self):
            pass

    orig = store.make_chat_projector
    store.make_chat_projector = lambda: FakeProjector()
    try:
        final = {"messages": [{"role": "user", "content": "log in"},
                              {"role": "assistant", "content": "authored 1 step"},
                              {"role": "user", "content": "set username to bob"}]}
        m._project_chat("conv-1", "http://app.example", final)
    finally:
        store.make_chat_projector = orig
    assert captured.get("conversation_id") == "conv-1", captured
    assert captured.get("last_target") == "http://app.example", captured
    assert captured.get("turn_count") == 2, captured               # two USER turns (assistant excluded)
    assert captured.get("last_goal") == "set username to bob", captured
    assert "2 turn(s)" in captured.get("summary", ""), captured


def test_project_chat_noop_without_gateway():
    import brain.__main__ as m
    os.environ.pop("STORE_ADDR", None)  # make_chat_projector -> None
    m._project_chat("c", "t", {"messages": [{"role": "user", "content": "x"}]})  # must not raise


# --- GAP-M9-19: SENTINEL_REFINE_REVERIFY forces a warm thread to re-explore --
def test_reverify_forces_reexplore_on_warm_thread():
    import brain.__main__ as m
    import brain.llm as llm
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = os.path.join(tempfile.mkdtemp(), "conversations.db")
    # 1) seed a WARM thread (a cold turn-1 that explores + persists a site_map) into the SQLite file.
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb1 = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    with SqliteSaver.from_conn_string(db) as saver:
        app = build_graph(ex, HeuristicPlanner(), lambda r: None,
                          scenario_head=GoalPlanner(goal="log in", backend=fb1)).compile(checkpointer=saver)
        app.invoke(_explore_init(goal="log in", messages=[{"role": "user", "content": "log in"}]),
                   config={"recursion_limit": 200, "configurable": {"thread_id": "conv-rv"}})

    # 2) _run_chat turn-2 with REVERIFY=1: despite the warm thread it must take the COLD path and spawn a
    #    browser executor (re-explore), NOT the warm no-browser refine.
    made = {"n": 0}

    def fake_make_executor(cmd):
        made["n"] += 1
        e = WalkEx()
        e.call("browser.navigate", url=_LOGIN)
        return e

    budget.reset(plan_limit=10**6, heal_limit=10**6)
    out = pathlib.Path(tempfile.mkdtemp())
    fb2 = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "bob"}])])
    saved, orig_mx, orig_mb = dict(os.environ), m.make_executor, llm.make_backend
    os.environ.update({"GOAL": "set user bob", "DESCRIBE": "", "CHECKPOINT_DSN": "", "PW_EXECUTOR_CMD": "x",
                       "SENTINEL_CONVERSATIONS_DB": db, "SENTINEL_CONVERSATION_ID": "conv-rv",
                       "SENTINEL_REFINE_REVERIFY": "1"})
    m.make_executor = fake_make_executor
    llm.make_backend = lambda role: fb2
    try:
        rc = m._run_chat("t2", out, "conv-rv", _LOGIN, 0.85, 40)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        m.make_executor, llm.make_backend = orig_mx, orig_mb
    assert made["n"] == 1, "REVERIFY=1 must force the cold path (spawn a browser executor to re-explore)"
    assert rc == 0, f"re-explore + author should exit 0, got {rc}"
    budget.reset()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
