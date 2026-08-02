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


# --- ADR-108a: the objective belongs to the CONVERSATION -----------------------
def _seed_pinned_thread(db, conv, objective):
    """Run a real cold turn through _run_chat's own pinning path, so the thread carries chat_intent the
    way a first turn actually writes it — not a hand-built dict that could agree with a wrong reader."""
    import brain.__main__ as m
    import brain.llm as llm
    from langgraph.checkpoint.sqlite import SqliteSaver

    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    with SqliteSaver.from_conn_string(db) as saver:
        app = build_graph(ex, HeuristicPlanner(), lambda r: None,
                          scenario_head=GoalPlanner(goal=objective, backend=fb)).compile(checkpointer=saver)
        init = _explore_init(goal=objective, messages=[{"role": "user", "content": objective}])
        init["chat_intent"] = {"kind": "goal", "text": objective}
        app.invoke(init, config={"recursion_limit": 200, "configurable": {"thread_id": conv}})
    budget.reset()


def _chat_turn(db, conv, env, run="t2"):
    """Drive one warm _run_chat turn with `env` overlaid, returning (exit_code, out_dir)."""
    import brain.__main__ as m
    import brain.llm as llm

    budget.reset(plan_limit=10**6, heal_limit=10**6)
    out = pathlib.Path(tempfile.mkdtemp())
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "bob"}])])
    saved, orig_mb = dict(os.environ), llm.make_backend
    base = {"GOAL": "", "DESCRIBE": "", "MESSAGE": "", "CHECKPOINT_DSN": "",
            "SENTINEL_CONVERSATIONS_DB": db, "SENTINEL_CONVERSATION_ID": conv}
    base.update(env)
    os.environ.update(base)
    llm.make_backend = lambda role: fb
    try:
        return m._run_chat(run, out, conv, None, 0.85, 40), out
    finally:
        os.environ.clear()
        os.environ.update(saved)
        llm.make_backend = orig_mb
        budget.reset()


def test_pinned_goal_cannot_be_replaced():
    """A second turn declaring a DIFFERENT goal is refused — 'one conversation, one goal'.

    Refused BEFORE any browser or model work: _run_chat returns 3 from the peek, so the cost of the
    refusal is nothing and the reason is in the log rather than in a half-authored scenario.
    """
    db = os.path.join(tempfile.mkdtemp(), "conversations.db")
    _seed_pinned_thread(db, "conv-pin", "log in")
    rc, out = _chat_turn(db, "conv-pin", {"GOAL": "delete the account"})
    assert rc == 3, f"a changed goal must exit 3, got {rc}"
    assert not (out / "scenario.json").exists(), "a refused turn must not author anything"


def test_pinned_goal_may_be_restated():
    """Sending the SAME goal again is idempotent, not a violation — a client that always includes the
    objective (and every client did before `message` existed) must keep working."""
    db = os.path.join(tempfile.mkdtemp(), "conversations.db")
    _seed_pinned_thread(db, "conv-same", "log in")
    rc, out = _chat_turn(db, "conv-same", {"GOAL": "log in"})
    assert rc == 0, f"restating the pinned goal must be accepted, got {rc}"
    assert (out / "scenario.json").exists(), "an accepted turn should author"


def test_switching_kind_is_also_a_change():
    """goal -> describe is a different objective even when the text is identical: the two are authored
    by different heads, so accepting it would silently change what the conversation means."""
    db = os.path.join(tempfile.mkdtemp(), "conversations.db")
    _seed_pinned_thread(db, "conv-kind", "log in")
    rc, _ = _chat_turn(db, "conv-kind", {"DESCRIBE": "log in"})
    assert rc == 3, f"switching goal->describe must exit 3, got {rc}"


def test_message_carries_the_turn_and_leaves_the_objective_alone():
    """A follow-up sends MESSAGE only. It authors from the message, and the pinned objective survives —
    which is what makes a correction distinguishable from a new goal at all."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = os.path.join(tempfile.mkdtemp(), "conversations.db")
    _seed_pinned_thread(db, "conv-msg", "log in")
    rc, out = _chat_turn(db, "conv-msg", {"MESSAGE": "use bob instead of alice"})
    assert rc == 0, f"a message-only turn must be accepted, got {rc}"
    sc = json.loads((out / "scenario.json").read_text())
    fills = [s for s in sc["steps"] if s["action_type"] == "fill"]
    assert fills and fills[0]["value"] == "bob", sc      # authored from the MESSAGE, over the persisted map

    # The objective is still the one pinned on turn 1 — asserted by reading the thread, not by trusting
    # that nothing wrote to it.
    import brain.__main__ as m
    with SqliteSaver.from_conn_string(db) as saver:
        snap = build_graph(_NoBrowserProbe(), HeuristicPlanner(), lambda r: None,
                           scenario_head=None).compile(checkpointer=saver).get_state(
            {"configurable": {"thread_id": "conv-msg"}})
    assert snap.values.get("chat_intent") == {"kind": "goal", "text": "log in"}, snap.values.get("chat_intent")


class _NoBrowserProbe:
    """Raises on any browser call, so a probe that accidentally drives the executor fails loudly."""

    def call(self, name, **kw):
        raise AssertionError(f"probe must not touch the browser, got {name}")

    def close(self):
        pass


def test_cold_turn_pins_the_objective_into_the_thread():
    """The COLD path must write chat_intent, and this drives the real _run_chat to prove it.

    Added because a mutation SURVIVED: deleting `chat_intent` from the cold-turn init broke nothing,
    since every other test seeds a thread it built itself and so agreed with a reader that never had
    to read anything real. The pin is the foundation the whole rule stands on, and it was the one part
    with no test on it.
    """
    import brain.__main__ as m
    import brain.llm as llm
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = os.path.join(tempfile.mkdtemp(), "conversations.db")
    out = pathlib.Path(tempfile.mkdtemp())
    budget.reset(plan_limit=10**6, heal_limit=10**6)
    fb = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "alice"}])])
    ex = WalkEx()
    saved, orig_mb, orig_me = dict(os.environ), llm.make_backend, m.make_executor
    os.environ.update({"GOAL": "log in", "DESCRIBE": "", "MESSAGE": "", "CHECKPOINT_DSN": "",
                       "SENTINEL_CONVERSATIONS_DB": db, "SENTINEL_CONVERSATION_ID": "conv-cold",
                       "PW_EXECUTOR_CMD": "unused-because-make_executor-is-patched"})
    llm.make_backend = lambda role: fb
    m.make_executor = lambda cmd: ex          # a cold turn spawns a browser; this is the seam
    try:
        rc = m._run_chat("cold1", out, "conv-cold", _LOGIN, 0.85, 40)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        llm.make_backend, m.make_executor = orig_mb, orig_me
        budget.reset()

    assert rc == 0, f"a cold chat turn should author and exit 0, got {rc}"
    with SqliteSaver.from_conn_string(db) as saver:
        snap = build_graph(_NoBrowserProbe(), HeuristicPlanner(), lambda r: None,
                           scenario_head=None).compile(checkpointer=saver).get_state(
            {"configurable": {"thread_id": "conv-cold"}})
    assert snap.values.get("chat_intent") == {"kind": "goal", "text": "log in"}, \
        f"the cold turn did not pin the objective: {snap.values.get('chat_intent')!r}"

    # And the pin is load-bearing end to end: a SECOND turn with a different goal is now refused
    # against a thread this test never hand-seeded.
    rc2, _ = _chat_turn(db, "conv-cold", {"GOAL": "delete the account"})
    assert rc2 == 3, f"the pin written by the cold turn must be enforced on the next one, got {rc2}"


def test_first_turn_with_only_a_message_talks_and_pins_nothing():
    """CHANGED BY ADR-108b, deliberately — this used to assert exit 3.

    ADR-108a was right that a message must not be pinned AS the objective: a follow-up is not a goal,
    and pinning one would make a passing remark the thing the conversation is forever about. It drew
    the wrong conclusion from that, though — it refused the turn. So the only thing a person could do
    on a fresh conversation was state an objective, and the state where someone is still deciding did
    not exist.

    The turn is now answered (ADR-108b) and STILL pins nothing, which is the part ADR-108a got right
    and this keeps enforcing: `chat_intent` must be absent afterwards, so a goal stated later is
    accepted rather than refused as a change.
    """
    db = os.path.join(tempfile.mkdtemp(), "conversations.db")
    rc, out = _chat_turn(db, "conv-empty", {"MESSAGE": "and also check the footer"})
    assert rc == 0, f"a turn with only a message is a conversation turn and must succeed, got {rc}"
    assert (pathlib.Path(out) / "reply.json").exists(), "the turn produced no reply"

    from langgraph.checkpoint.sqlite import SqliteSaver
    with SqliteSaver.from_conn_string(db) as saver:
        snap = build_graph(_NoBrowserProbe(), HeuristicPlanner(), lambda r: None,
                           scenario_head=None).compile(checkpointer=saver).get_state(
            {"configurable": {"thread_id": "conv-empty"}})
    assert not (snap.values or {}).get("chat_intent"), \
        f"talking pinned an objective: {(snap.values or {}).get('chat_intent')!r}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
