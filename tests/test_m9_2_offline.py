"""Offline M9.2a tests — GoalPlanner + make_planner + RunConfig (no browser, no network, no LLM).

Run:  .venv/bin/python tests/test_m9_2_offline.py

A FakeBackend (the test_m8/test_b1 pattern) returns canned JSON so we can deterministically prove:
- GoalPlanner is GROUNDED — it returns ONLY a real candidate (by index) or `done`; an OOB/garbage reply
  degrades to done/heuristic, it NEVER fabricates an action that isn't on the map (ADR-022);
- graceful degradation — no goal / no backend / exhausted plan budget -> HeuristicPlanner (no backend call);
- make_planner routes GOAL/PLANNER correctly (--goal auto-default; explicit flag wins);
- RunConfig YAML loads allowed keys (unknown ignored), and apply precedence is flag > file > default;
- goal-mode is best-effort (labeled) while frozen steps still hash deterministically.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import budget                                              # noqa: E402
from brain.llm import LLMResult                                       # noqa: E402
from brain.planner import GoalPlanner, HeuristicPlanner, make_planner  # noqa: E402
from brain.runconfig import load_run_config, apply_run_config        # noqa: E402
from brain.state import canonical_plan_hash                          # noqa: E402


class FakeBackend:
    name, model, supports_vision = "fake", "fake-model", False

    def __init__(self, reply='{"index": 0}', pt=10, ct=10):
        self.reply, self._pt, self._ct, self.calls = reply, pt, ct, []

    def complete(self, prompt, *, max_tokens, temperature):
        self.calls.append(prompt)
        return LLMResult(self.reply, self._pt, self._ct)

    def complete_vision(self, *a, **k):
        raise NotImplementedError


def _cands():
    return [
        {"kind": "click", "semantic_id": "a", "role": "button", "name": "Cancel", "target": None,
         "intent": "click button 'Cancel'", "locator": {"role": "button", "name": "Cancel"}, "alternatives": []},
        {"kind": "click", "semantic_id": "b", "role": "button", "name": "Sign in", "target": None,
         "intent": "click button 'Sign in'", "locator": {"role": "button", "name": "Sign in"}, "alternatives": []},
        {"kind": "navigate", "semantic_id": "c", "role": None, "name": None, "target": "file:///s/next.html",
         "intent": "navigate", "locator": None, "alternatives": []},
    ]


def _state():
    return {"current_url": "file:///s/login.html", "current_step": 1, "max_steps": 40,
            "coverage_achieved": 0.0, "coverage_target": 0.85}


# --- 1-4: GoalPlanner grounding ---------------------------------------------
def test_goalplanner_picks_grounded_candidate_by_index():
    budget.reset(plan_limit=10000, heal_limit=10000)
    fb = FakeBackend('{"index": 1}')
    d = GoalPlanner(goal="log in as admin", backend=fb).propose(_state(), _cands())
    assert d["action"]["semantic_id"] == "b" and not d["done"], d     # picked candidate #1 (Sign in)
    assert fb.calls and "log in as admin" in fb.calls[0], "the goal must be in the prompt"
    budget.reset()


def test_goalplanner_done_on_goal():
    budget.reset(plan_limit=10000, heal_limit=10000)
    d = GoalPlanner(goal="x", backend=FakeBackend('{"done": true, "reason": "goal met"}')).propose(_state(), _cands())
    assert d["done"] and d["action"] is None and "goal met" in d["reason"], d
    budget.reset()


def test_goalplanner_oob_index_is_done_not_fabricated():
    budget.reset(plan_limit=10000, heal_limit=10000)
    d = GoalPlanner(goal="x", backend=FakeBackend('{"index": 99}')).propose(_state(), _cands())
    assert d["done"] and d["action"] is None, d                       # OOB -> done, never a fabricated action
    budget.reset()


def test_goalplanner_garbage_reply_falls_back():
    budget.reset(plan_limit=10000, heal_limit=10000)
    d = GoalPlanner(goal="x", backend=FakeBackend('not json at all')).propose(_state(), _cands())
    assert d == HeuristicPlanner().propose(_state(), _cands()), d      # unparseable -> heuristic, no crash/fabrication
    budget.reset()


# --- 5-7: graceful degradation ----------------------------------------------
def test_goalplanner_no_backend_falls_back():
    d = GoalPlanner(goal="x", backend=False).propose(_state(), _cands())   # False = no backend (skip make_backend)
    assert d == HeuristicPlanner().propose(_state(), _cands()), d


def test_goalplanner_budget_exceeded_falls_back():
    budget.reset(plan_limit=50, heal_limit=20)
    budget.tracker().add("plan", LLMResult("x", 60, 0))               # already over the 50-token plan limit
    fb = FakeBackend('{"index": 0}')
    d = GoalPlanner(goal="x", backend=fb).propose(_state(), _cands())
    assert d == HeuristicPlanner().propose(_state(), _cands()), d
    assert fb.calls == [], "backend must NOT be called once the plan budget is exhausted"
    budget.reset()


def test_goalplanner_no_goal_is_explore():
    fb = FakeBackend('{"index": 1}')
    d = GoalPlanner(goal="", backend=fb).propose(_state(), _cands())
    assert d == HeuristicPlanner().propose(_state(), _cands()), d
    assert fb.calls == [], "no goal -> heuristic explore, backend not called"


# --- 8: make_planner routing (no propose -> no network) ---------------------
def test_make_planner_routing():
    assert make_planner({"GOAL": "do x"}).name == "goal"             # auto-default by --goal presence
    assert make_planner({"PLANNER": "goal"}).name == "goal"           # explicit
    assert make_planner({"PLANNER": "goal", "GOAL": "y"}).name == "goal"
    assert make_planner({"PLANNER": "llm"}).name == "llm"
    assert make_planner({}).name == "heuristic"
    assert make_planner({"PLANNER": "heuristic"}).name == "heuristic"  # no goal -> heuristic
    assert make_planner({"PLANNER": "llm", "GOAL": "z"}).name == "llm"  # explicit planner wins over goal presence


# --- 9-13: RunConfig YAML ----------------------------------------------------
def _yaml(text):
    p = os.path.join(tempfile.mkdtemp(), "rc.yaml")
    with open(p, "w") as f:
        f.write(text)
    return p


def test_load_run_config_parses_allowed_keys_ignores_unknown():
    cfg = load_run_config(_yaml(
        "mode: goal\ngoal: log in as admin\ncoverage_target: 0.9\nmax_steps: 10\n"
        "plan_budget: 1234\nauth: {provider: keycloak}\n"))   # `auth` is M9.2b -> ignored now
    assert cfg["mode"] == "goal" and cfg["goal"] == "log in as admin"
    assert cfg["coverage_target"] == 0.9 and cfg["max_steps"] == 10 and cfg["plan_budget"] == 1234
    assert "auth" not in cfg, cfg                                     # forward-compat: unknown key ignored


def test_load_run_config_missing_returns_empty():
    assert load_run_config("/nonexistent/rc.yaml") == {}


def test_load_run_config_non_mapping_raises():
    try:
        load_run_config(_yaml("- just\n- a\n- list\n"))
        assert False, "non-mapping YAML must raise"
    except ValueError:
        pass


def test_apply_run_config_flag_beats_file_file_beats_default():
    env = {"PLANNER": "heuristic", "COVERAGE_TARGET": "0.85", "MAX_STEPS": "40", "GOAL": "FLAG GOAL"}
    apply_run_config({"mode": "goal", "goal": "FILE GOAL", "coverage_target": 0.9}, env)
    assert env["GOAL"] == "FLAG GOAL", env          # explicit --goal beats the file
    assert env["PLANNER"] == "goal", env            # file overrode the still-default planner
    assert env["COVERAGE_TARGET"] == "0.9", env     # file overrode the still-default coverage


def test_apply_run_config_explicit_flag_not_clobbered():
    env = {"PLANNER": "llm", "COVERAGE_TARGET": "0.5", "MAX_STEPS": "40"}   # explicit non-defaults
    apply_run_config({"mode": "goal", "coverage_target": 0.9}, env)
    assert env["PLANNER"] == "llm", env             # explicit --planner llm wins over file mode:goal
    assert env["COVERAGE_TARGET"] == "0.5", env     # explicit flag wins over file


def test_apply_run_config_explicit_default_flag_not_clobbered():
    # the hard case: an explicit flag whose value EQUALS the agentctl default must still beat the file.
    # SENTINEL_EXPLICIT (emitted by agentctl via fs.Visit) carries the actually-set flag names.
    env = {"PLANNER": "heuristic", "COVERAGE_TARGET": "0.85", "MAX_STEPS": "40",
           "SENTINEL_EXPLICIT": "planner,coverage-target"}
    apply_run_config({"mode": "goal", "coverage_target": 0.5, "max_steps": 10}, env)
    assert env["PLANNER"] == "heuristic", env       # explicit --planner heuristic wins even when == default
    assert env["COVERAGE_TARGET"] == "0.85", env    # explicit flag wins even when == default
    assert env["MAX_STEPS"] == "10", env            # NOT in SENTINEL_EXPLICIT -> file overrides the default


def test_apply_run_config_mode_planner_conflict_raises():
    try:
        apply_run_config({"mode": "goal", "planner": "llm"}, {})
        assert False, "conflicting mode/planner must raise"
    except ValueError:
        pass


def test_apply_run_config_mode_planner_agree_ok():
    env = {}
    apply_run_config({"mode": "goal", "planner": "goal"}, env)      # agreeing alias -> no conflict
    assert env["PLANNER"] == "goal", env


def test_runconfig_file_drives_goalplanner_through_make_planner():
    # END-TO-END: load -> apply -> make_planner through an agentctl-shaped env (the __main__ path).
    path = _yaml("mode: goal\ngoal: win the billing flow\ncoverage_target: 0.9\n")
    env = {"PLANNER": "heuristic", "GOAL": "", "COVERAGE_TARGET": "0.85", "MAX_STEPS": "40"}
    apply_run_config(load_run_config(path), env)
    assert env["GOAL"] == "win the billing flow", env   # file goal fills the blank (default) GOAL
    assert env["PLANNER"] == "goal", env                # mode:goal flips the still-default planner
    assert env["COVERAGE_TARGET"] == "0.9", env         # file overrides the still-default coverage
    p = make_planner(env)                               # what _run_explore() then does on os.environ
    assert p.name == "goal" and p.goal == "win the billing flow", p.name


def test_load_run_config_bad_numeric_raises():
    try:
        load_run_config(_yaml("max_steps: lots\n"))    # non-int for a numeric key -> config error (exit 3)
        assert False, "bad numeric must raise"
    except ValueError:
        pass


def test_main_malformed_run_config_exits_3():
    import brain.__main__ as m
    bad = _yaml("- not\n- a\n- mapping\n")
    saved = dict(os.environ)
    os.environ.update({"RUN_CONFIG": bad, "RUN_MODE": "clear-quarantine",
                       "ARTIFACT_DIR": tempfile.mkdtemp(), "RUN_ID": "t"})
    try:
        assert m.main() == 3   # load_run_config raises -> caught in main() -> exit 3, before mode dispatch
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --- 14: determinism ---------------------------------------------------------
def test_goal_mode_labeled_and_frozen_steps_hash_stable():
    gp = GoalPlanner(goal="x", backend=FakeBackend('{"index": 0}'))
    assert gp.name == "goal" and gp.model, gp.name                   # best-effort but labeled (transcript)
    # goal-mode never touches hashing; two independently-built step dicts with DIFFERENT key insertion
    # order must hash equal (sort_keys stability), so a frozen plan replays deterministically.
    a = canonical_plan_hash([{"step_id": 1, "action_type": "navigate", "intent": "go", "target": "u"}])
    b = canonical_plan_hash([{"target": "u", "intent": "go", "action_type": "navigate", "step_id": 1}])
    assert a == b, "canonical_plan_hash must be key-order-independent (sort_keys)"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
