"""Offline M9.10 / R2a tests — stateful multi-turn authoring (ADR-048). No browser / network / real LLM.

Run:  .venv/bin/python tests/test_r2_multiturn_offline.py

Proves the checkpointer-resume design from the two risk spikes, now wired into the REAL brain:
- TWO-TURN at the graph level: turn-1 (cold) explores + authors over goal-1; turn-2 RESUMES the same
  thread (shared checkpointer) and re-authors over the PERSISTED site_map — WITHOUT re-driving the
  browser (conditional entry → `scenario`), `messages` accumulate via add_messages, and the refine
  prompt carries the prior conversation;
- ONE-SHOT regression: with NO messages the entry routes to `perceive` (full walk) and the authoring
  prompt has no conversation block — byte-identical to the M9.2b path;
- `_user_turns` extraction (dicts + duck-typed message objects);
- `_run_chat` dispatch WARM path: a pre-seeded SQLite thread is resumed and refined with NO browser
  (the _NoBrowser guard would raise on any browser call), scenario.json written; + the GOAL/DESCRIBE
  guard (exit 3).
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import budget                                                    # noqa: E402
from brain.llm import LLMResult                                             # noqa: E402
from brain.planner import GoalPlanner, HeuristicPlanner                     # noqa: E402
from brain.graph import build_graph, _user_turns                            # noqa: E402
from brain.state import semantic_id, normalize_url                          # noqa: E402

_LOGIN, _BILLING = "file:///s/login.html", "file:///s/billing.html"
_USER_SID = semantic_id(normalize_url(_LOGIN), "textbox", "Username")
_PAY_SID = semantic_id(normalize_url(_BILLING), "button", "pay")    # the Pay button anchors on testid "pay"


class QueuedFakeBackend:
    """Pops one canned reply per `complete` call (turn-1 reply, then turn-2 reply); records the prompts
    so a test can assert the multi-turn refine context was threaded in."""
    name, model, supports_vision = "fake", "fake-model", False

    def __init__(self, replies, pt=10, ct=10):
        self.replies, self.prompts, self._pt, self._ct = list(replies), [], pt, ct

    def complete(self, prompt, *, max_tokens, temperature):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else '{"steps": []}'
        return LLMResult(reply, self._pt, self._ct)

    def complete_vision(self, *a, **k):
        raise NotImplementedError


class WalkEx:
    """A 2-page fake site (login -> billing) that COUNTS calls, so a test can prove a warm turn drives
    NO browser at all (call count unchanged across the resume)."""
    PAGES = {
        _LOGIN: {"interactives": [{"tag": "button", "role": "button", "name": "Sign in", "testid": "signin", "text": "Sign in"},
                                  {"tag": "input", "role": "textbox", "name": "Username", "testid": None, "text": ""}],
                 "links": [{"href": _BILLING, "text": "Billing"}]},
        _BILLING: {"interactives": [{"tag": "button", "role": "button", "name": "Pay", "testid": "pay", "text": "Pay"}],
                   "links": []},
    }

    def __init__(self):
        self.url, self.n = "", 0

    def call(self, m, **p):
        self.n += 1
        if m == "browser.navigate":
            self.url = p["url"]
            return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": f"- page {self.url}", "nodeCount": 1}
        if m == "browser.interactives":
            return {"elements": self.PAGES.get(self.url, {}).get("interactives", [])}
        if m == "browser.links":
            return {"links": self.PAGES.get(self.url, {}).get("links", [])}
        return {}

    def close(self):
        pass


def _explore_init(goal="", messages=None):
    init = {"run_id": "t", "run_mode": "chat", "target_url": _LOGIN, "base_origin": "file:///s/",
            "coverage_target": 0.85, "max_steps": 40, "artifact_dir": tempfile.mkdtemp(),
            "goal": goal, "describe": "", "site_map": {}, "phase": "explore",
            "scenario_steps": [], "scenario_unmatched": [], "current_url": _LOGIN, "page_model": {},
            "exploration_plan": [{"step_id": 1, "action_type": "navigate", "semantic_id": "nav1",
                                  "intent": "nav", "target": _LOGIN, "locator": None, "alternatives": None,
                                  "is_milestone": True}],
            "plan_hash": "", "current_step": 1, "interactive_seen": [], "interactive_exercised": [],
            "visited_paths": [], "nav_frontier": [], "coverage_achieved": 0.0,
            "exploration_complete": False, "executed_actions": [], "errors": []}
    if messages is not None:
        init["messages"] = messages
    return init


def _reply(steps):
    return json.dumps({"steps": steps})


# --- the core: two-turn resume (graph level, shared checkpointer) -------------
def test_two_turn_resume_skips_explore_and_refines():
    from langgraph.checkpoint.memory import MemorySaver
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb = QueuedFakeBackend([
        _reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}, {"ref": _PAY_SID, "verb": "click"}]),
        _reply([{"ref": _USER_SID, "verb": "fill", "value": "bob"}]),
    ])
    ex = WalkEx()
    saver = MemorySaver()
    head = GoalPlanner(goal="log in", backend=fb)   # self.goal is a fallback; the per-turn goal flows via state
    app = build_graph(ex, HeuristicPlanner(), lambda r: None, scenario_head=head).compile(checkpointer=saver)
    cfg = {"recursion_limit": 200, "configurable": {"thread_id": "conv-1"}}

    # turn 1 (COLD): empty site_map -> route_entry -> perceive -> full walk -> author over goal-1.
    ex.call("browser.navigate", url=_LOGIN)
    t1 = app.invoke(_explore_init(goal="log in", messages=[{"role": "user", "content": "log in"}]), config=cfg)
    explore_calls = ex.n
    assert explore_calls > 2, "turn-1 must explore (perceive walk drove the browser)"
    sm1, sc1 = t1["site_map"], t1["scenario_steps"]
    assert normalize_url(_LOGIN) in sm1 and normalize_url(_BILLING) in sm1, list(sm1)
    assert sc1 and any(s["action_type"] == "fill" and s.get("value") == "alice" for s in sc1), sc1

    # turn 2 (WARM): persisted site_map + messages -> route_entry -> scenario (NO re-explore).
    budget.reset(plan_limit=10**6, heal_limit=10**6)   # fresh per-turn budget (real: a fresh process)
    t2 = app.invoke({"messages": [{"role": "user", "content": "set username to bob"}],
                     "goal": "set username to bob"}, config=cfg)

    assert ex.n == explore_calls, f"turn-2 must NOT drive the browser (re-explore): {ex.n} != {explore_calls}"
    assert t2["site_map"] == sm1, "site_map persisted unchanged across the resume (not re-discovered)"
    sc2 = t2["scenario_steps"]
    fills = [s for s in sc2 if s["action_type"] == "fill"]
    assert fills and fills[0]["value"] == "bob", sc2          # refine reflects the correction
    assert sc2 != sc1, "turn-2 re-authored a DIFFERENT scenario (refine, not a replay of turn-1)"
    assert all(s["action_type"] != "click" or s["semantic_id"] != _PAY_SID for s in sc2), "reply-2 dropped Pay"

    # messages accumulated: turn1 user + turn1 assistant-summary + turn2 user + turn2 assistant-summary.
    contents = [getattr(m, "content", None) for m in t2["messages"]]
    assert len(t2["messages"]) == 4, contents
    assert contents[0] == "log in" and contents[2] == "set username to bob", contents

    # the turn-2 authoring prompt carried the PRIOR conversation (turn-1 goal) as refine context.
    assert "prior conversation" in fb.prompts[-1] and "log in" in fb.prompts[-1], fb.prompts[-1]
    budget.reset()


# --- one-shot regression: no conversation -> unchanged behavior --------------
def test_one_shot_unchanged_without_conversation():
    from langgraph.checkpoint.memory import MemorySaver
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"},
                                    {"ref": _PAY_SID, "verb": "click"}])])
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    app = build_graph(ex, HeuristicPlanner(), lambda r: None,
                      scenario_head=GoalPlanner(goal="log in", backend=fb)).compile(checkpointer=MemorySaver())
    # NO messages key -> route_entry -> perceive (full walk), authoring prompt has no conversation block.
    final = app.invoke(_explore_init(goal="log in"), config={"recursion_limit": 200,
                                                             "configurable": {"thread_id": "one-shot"}})
    assert ex.n > 2, "one-shot must explore (perceive walk ran)"
    assert final["scenario_steps"], "one-shot authored a scenario"
    assert "prior conversation" not in fb.prompts[-1], "one-shot prompt must carry NO refine/history block"
    budget.reset()


# --- _user_turns helper ------------------------------------------------------
def test_user_turns_extracts_user_content():
    class _Msg:
        def __init__(self, t, c):
            self.type, self.content = t, c
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "x"},
            _Msg("human", "b"), _Msg("ai", "y"), {"role": "user", "content": ""}]
    assert _user_turns(msgs) == ["a", "b"], _user_turns(msgs)   # assistant/ai dropped; empty user dropped
    assert _user_turns(None) == [] and _user_turns([]) == []


# --- _run_chat dispatch: WARM refine path (real wrapper, no browser) ---------
def test_run_chat_warm_refine_no_browser():
    import brain.__main__ as m
    import brain.llm as llm
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = os.path.join(tempfile.mkdtemp(), "conversations.db")

    # 1) PRE-SEED the thread (a cold turn-1) directly into the SAME SQLite file, then close it — this
    #    simulates the prior turn's separate process. Risk-spike #2 proved this survives the boundary.
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb1 = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    with SqliteSaver.from_conn_string(db) as saver:
        app = build_graph(ex, HeuristicPlanner(), lambda r: None,
                          scenario_head=GoalPlanner(goal="log in", backend=fb1)).compile(checkpointer=saver)
        app.invoke(_explore_init(goal="log in", messages=[{"role": "user", "content": "log in"}]),
                   config={"recursion_limit": 200, "configurable": {"thread_id": "conv-warm"}})

    # 2) _run_chat turn-2 (WARM): must detect the persisted site_map, refine with NO browser, write
    #    scenario.json. make_backend is patched so the in-_run_chat GoalPlanner authors offline.
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    out = pathlib.Path(tempfile.mkdtemp())
    fb2 = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "bob"}])])
    saved, orig_mb = dict(os.environ), llm.make_backend
    os.environ.update({"GOAL": "set username to bob", "DESCRIBE": "", "CHECKPOINT_DSN": "",
                       "SENTINEL_CONVERSATIONS_DB": db, "SENTINEL_CONVERSATION_ID": "conv-warm"})
    llm.make_backend = lambda role: fb2
    try:
        rc = m._run_chat("turn2", out, "conv-warm", None, 0.85, 40)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        llm.make_backend = orig_mb

    assert rc == 0, f"warm refine should author >=1 grounded step -> exit 0, got {rc}"
    sc = json.loads((out / "scenario.json").read_text())
    fills = [s for s in sc["steps"] if s["action_type"] == "fill"]
    assert fills and fills[0]["value"] == "bob", sc          # refined over the PERSISTED map, no re-explore
    assert sc["mode"] == "goal", sc
    budget.reset()


def test_run_chat_needs_goal_or_describe_exit3():
    import brain.__main__ as m
    saved = dict(os.environ)
    os.environ.update({"GOAL": "", "DESCRIBE": ""})
    try:
        rc = m._run_chat("r", pathlib.Path(tempfile.mkdtemp()), "conv-x", _LOGIN, 0.85, 40)
        assert rc == 3, f"chat with neither GOAL nor DESCRIBE must exit 3, got {rc}"
        os.environ.update({"GOAL": "g", "DESCRIBE": "d"})                 # both set -> also exit 3
        assert m._run_chat("r", pathlib.Path(tempfile.mkdtemp()), "conv-x", _LOGIN, 0.85, 40) == 3
    finally:
        os.environ.clear()
        os.environ.update(saved)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
