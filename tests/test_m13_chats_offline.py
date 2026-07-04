"""Offline M13 wave-5 tests — chats projection + GAP-M9-20 (refine-history cap + rolling summary).

Run:  .venv/bin/python tests/test_m13_chats_offline.py

No browser / network / gateway. Proves:
- `_capped_history`/`_rolling_summary` bound a growing conversation (short = byte-identical);
- the cap reaches the refine prompt (a long conversation gets an "[earlier: …]" summary prefix);
- `_project_chat` emits the chats projection (conversation_id/turn_count/last_goal/summary) and is a
  no-op without a store-gateway (STORE_ADDR unset).
"""
import os
import sys

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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
