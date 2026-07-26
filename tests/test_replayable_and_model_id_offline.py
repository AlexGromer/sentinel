"""Offline gate for two defects a CLEAN END-TO-END RUN found that reading never did (2026-07-26).

Run:  .venv/bin/python tests/test_replayable_and_model_id_offline.py

1. A replay could never be replayed. `resolveFromRun` probes a run's OWN directory for a frozen plan,
   and a replay wrote only its report there — so `has_plan` was false forever and the re-run control
   stayed grey, even though the plan was perfectly well known (it had arrived via `from_run`).
   brain now freezes `executed-plan.json`: the plan this run accepted and ran.

2. The model id reached no artifact. A goal run's transcript showed `qwen3:14b` and 551 tokens while
   `plan.json` carried `models: {"plan": null}` and `scenario.json` carried neither models nor tokens.
   `graph.py` read `.model` off the EXPLORE planner — heuristic by default, and with no such attribute
   — while the head that actually called the LLM had one and it was recorded nowhere. Downstream,
   `persistResult` got an empty model, so every metric point's `model` label came out blank; that label
   is the seam a cross-project rollup groups on (ADR-056).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.replay import run_replay                         # noqa: E402
from brain.state import canonical_plan_hash                 # noqa: E402

PAGE = "file:///s/app.html"
GOOD = {"testid": "pay"}


class Ex:
    def __init__(self):
        self.url = PAGE

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", self.url); return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- page app", "nodeCount": 2}
        if m in ("browser.interactives", "browser.links"):
            return {"elements": [], "links": []}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.probe":
            return {"count": 1 if p.get("locator") == GOOD else 0}
        if m == "browser.click":
            if p.get("locator") == GOOD:
                return {"ok": True}
            raise RuntimeError("not found")
        return {}

    def close(self):
        pass


class Heal:
    def heal(self, ctx):
        return {"locator": None, "strategy": None, "confidence": 0.0, "outcome": "failed"}


class Store:
    def record_step(self, *a, **k): return False
    def get_golden(self, *a, **k): return None
    def save_golden(self, *a, **k): return None
    def audit(self, *a, **k): return None


def _plan(plan_hash=None):
    steps = [{"step_id": 1, "action_type": "click", "intent": "click button 'Pay'",
              "semantic_id": "sid-pay", "is_milestone": False, "locator": GOOD, "alternatives": []}]
    p = {"plan_id": "p1", "steps": steps, "plan_hash": plan_hash or canonical_plan_hash(steps),
         "target_url": PAGE}
    return p


# --- 1. a replay freezes what it ran ---------------------------------------------------------------
def test_replay_freezes_the_plan_it_executed():
    d = tempfile.mkdtemp()
    plan = _plan()
    run_replay(Ex(), Store(), Heal(), plan, PAGE, d, run_id="t")

    path = os.path.join(d, "executed-plan.json")
    assert os.path.exists(path), "a replay left no executed-plan.json — it can never be replayed"
    saved = json.load(open(path))
    assert saved.get("steps"), "the frozen copy carries no steps"
    # Byte-for-byte the plan that ran: a re-run has to execute the SAME thing, and a plan_hash that
    # differed would hard-abort on integrity (ADR-006) instead of replaying.
    assert saved["plan_hash"] == plan["plan_hash"], (saved["plan_hash"], plan["plan_hash"])
    assert saved["target_url"] == plan["target_url"], saved["target_url"]


def test_the_frozen_copy_is_not_named_plan_json():
    """`plan.json` in an explore run means "the plan this run PRODUCED", and persistResult reads
    coverage_achieved out of it. Reusing that name here would attribute the ORIGINAL explore's coverage
    to every replay of it — a silent corruption of the coverage metric in exchange for a shorter name."""
    d = tempfile.mkdtemp()
    run_replay(Ex(), Store(), Heal(), _plan(), PAGE, d, run_id="t")
    assert not os.path.exists(os.path.join(d, "plan.json")), \
        "a replay wrote plan.json — coverage of the original explore will now be read off this run"


def test_a_plan_hash_hard_abort_freezes_nothing():
    """ADR-006 refuses to execute a plan whose hash does not match. Such a run executed NOTHING, so
    putting a re-run of it one click away would hand the operator back the very plan the integrity gate
    just rejected."""
    d = tempfile.mkdtemp()
    rep = run_replay(Ex(), Store(), Heal(), _plan(plan_hash="deadbeefdeadbeef"), PAGE, d, run_id="t")
    assert rep["exit_code"] == 3, rep.get("exit_code")
    assert not os.path.exists(os.path.join(d, "executed-plan.json")), \
        "a run that hard-aborted on integrity offered its rejected plan for re-run"


# --- 2. the model id reaches the artifacts ---------------------------------------------------------
class FakeHead:
    """The scenario head as goal/describe mode builds it: it is the object that actually calls the LLM,
    and — unlike the heuristic explore planner — it knows which model it used.

    `build_scenario` is implemented rather than left off: a stub missing the method the graph calls
    sends the run down a different branch entirely, and the assertion then passes (or fails) for a reason
    that has nothing to do with the claim. Returning no refs is fine — this test is about which MODEL is
    recorded, not about grounding."""
    name = "goal"
    model = "qwen3:14b"

    def build_scenario(self, flat_map, goal, history=None):
        return {"refs": [], "tokens": {"prompt": 215, "completion": 336}}


def _run_graph_and_read_plan(scenario_head):
    """Drive the REAL graph to its report node and read the plan.json it froze.

    Asserting on graph.py's source text was the first version of this and it was worthless: it proved a
    string was present, not that the artefact came out right. The harness mirrors the M9.2b tests."""
    from langgraph.checkpoint.memory import MemorySaver
    from brain.graph import build_graph
    from brain.planner import HeuristicPlanner
    from brain import budget
    budget.reset(plan_limit=100000, heal_limit=100000)
    d = tempfile.mkdtemp()
    init = {"run_id": "t", "run_mode": "explore", "target_url": PAGE, "base_origin": "file:///s/",
            "coverage_target": 0.85, "max_steps": 6, "artifact_dir": d,
            "goal": "pay", "describe": "", "site_map": {}, "phase": "explore",
            "scenario_steps": [], "scenario_unmatched": [], "current_url": PAGE, "page_model": {},
            "exploration_plan": [{"step_id": 1, "action_type": "navigate", "semantic_id": "nav1",
                                  "intent": "nav", "target": PAGE, "locator": None,
                                  "alternatives": None, "is_milestone": True}],
            "plan_hash": "", "current_step": 1, "interactive_seen": [], "interactive_exercised": [],
            "visited_paths": [], "nav_frontier": [], "coverage_achieved": 0.0,
            "exploration_complete": False, "executed_actions": [], "errors": []}
    app = build_graph(Ex(), HeuristicPlanner(), lambda r: None,
                      scenario_head=scenario_head).compile(checkpointer=MemorySaver())
    app.invoke(init, config={"recursion_limit": 200, "configurable": {"thread_id": "t"}})
    return json.load(open(os.path.join(d, "plan.json")))


def test_plan_json_names_the_model_that_spent_the_tokens():
    """The explore planner is heuristic and has no `.model`; the scenario head is what called the LLM.
    Reading only the former left `models.plan` null on every goal run, so cost came out 0 and — the part
    that actually matters — the `model` label on every metric point came out blank."""
    plan = _run_graph_and_read_plan(FakeHead())
    models = plan.get("models") or {}
    assert models, "plan.json carries no models block at all"
    assert models.get("plan") == "qwen3:14b", models
    assert models.get("author") == "qwen3:14b", models
    assert models.get("explore") is None, models


def test_a_run_with_no_llm_head_still_reports_honestly():
    """A pure heuristic explore spent nothing on a model, and must say null rather than borrow a name."""
    plan = _run_graph_and_read_plan(None)
    models = plan.get("models") or {}
    assert models.get("plan") is None, models
    assert models.get("author") is None, models


def test_scenario_json_says_who_authored_it_and_at_what_cost():
    """A scenario is the artifact people HAND EACH OTHER — it is the deliverable of a goal run. One that
    cannot say which model authored it, at what token cost, is not reproducible by whoever receives it."""
    from brain.__main__ import _write_scenario
    import pathlib
    out = pathlib.Path(tempfile.mkdtemp())
    steps = [{"action_type": "click", "intent": "click 'Pay'", "locator": GOOD, "semantic_id": "s"}]
    rc = _write_scenario(out, "run7", PAGE, steps, [], False, author_model="qwen3:14b")
    assert rc == 0, rc
    sc = json.load(open(out / "scenario.json"))
    assert sc.get("models"), "scenario.json carries no models block"
    assert sc["models"]["author"] == "qwen3:14b", sc["models"]
    assert "tokens" in sc and isinstance(sc["tokens"], dict), sc.get("tokens")


def test_an_unauthored_scenario_says_so_rather_than_lying():
    """describe/goal without an LLM head must record null, not a plausible-looking default: a scenario
    that names a model it never used is worse than one that names none."""
    from brain.__main__ import _write_scenario
    import pathlib
    out = pathlib.Path(tempfile.mkdtemp())
    steps = [{"action_type": "click", "intent": "click 'Pay'", "locator": GOOD, "semantic_id": "s"}]
    _write_scenario(out, "run8", PAGE, steps, [], False, author_model=None)
    sc = json.load(open(out / "scenario.json"))
    assert sc["models"]["author"] is None, sc["models"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok   {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} checks passed")
