"""Offline M9.2b tests — two-phase goal (§L) + describe-first (§B) + rich RunConfig (no browser/network/LLM).

Run:  .venv/bin/python tests/test_m9_2b_offline.py

Proves:
- brain/scenario.py grounding: scenario refs / describe drafts bind ONLY to real site-map elements
  (bogus/ambiguous → unmatched, never fabricated); cross-page `navigate` synthesized from the element's
  `page`; authored steps carry the grounded locator+alternatives and NO LLM `reason`/score field (so the
  frozen scenario replays deterministically);
- GoalPlanner.build_scenario / DescribePlanner.draft (FakeBackend) + graceful degradation;
- the full two-phase machine via the LangGraph: a multi-page heuristic walk accumulates a `site_map`
  generalized BEYOND buttons (inputs), then the scenario head grounds a cross-page scenario;
- the grounded scenario replays via FakeEx to exit 0;
- rich RunConfig: declarative auth → M9.1 env, named scenarios + selector, malformed → raise, precedence;
- __main__ exit codes: describe-unmatched → 1, zero grounded → 1, GOAL⊕DESCRIBE → 3.
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import budget                                                    # noqa: E402
from brain.llm import LLMResult                                             # noqa: E402
from brain.planner import GoalPlanner, DescribePlanner, HeuristicPlanner    # noqa: E402
from brain.scenario import ground_scenario, reconcile, flatten_site_map    # noqa: E402
from brain.runconfig import load_run_config, apply_run_config              # noqa: E402
from brain.replay import run_replay                                         # noqa: E402
from brain.store import Store                                               # noqa: E402
from brain.healing import HealingEngine                                     # noqa: E402
from brain.state import canonical_plan_hash, semantic_id, normalize_url     # noqa: E402


class FakeBackend:
    name, model, supports_vision = "fake", "fake-model", False

    def __init__(self, reply='{"steps": []}', pt=10, ct=10):
        self.reply, self._pt, self._ct, self.calls = reply, pt, ct, []

    def complete(self, prompt, *, max_tokens, temperature):
        self.calls.append(prompt)
        return LLMResult(self.reply, self._pt, self._ct)

    def complete_vision(self, *a, **k):
        raise NotImplementedError


_LOGIN, _BILLING = "file:///s/login.html", "file:///s/billing.html"


def _site_map():
    return {
        _LOGIN: [
            {"semantic_id": "u", "role": "textbox", "name": "Username", "testid": None,
             "locator": {"role": "textbox", "name": "Username"},
             "alternatives": [{"strategy": "role_name", "locator": {"role": "textbox", "name": "Username"}, "prior": 0.9}],
             "page": _LOGIN},
            {"semantic_id": "sub", "role": "button", "name": "Sign in", "testid": None,
             "locator": {"role": "button", "name": "Sign in"}, "alternatives": [], "page": _LOGIN},
        ],
        _BILLING: [
            {"semantic_id": "pay", "role": "button", "name": "Pay", "testid": "pay",
             "locator": {"role": "button", "name": "Pay"},
             "alternatives": [{"strategy": "testid", "locator": {"testid": "pay"}, "prior": 0.95}], "page": _BILLING},
        ],
    }


# --- scenario.py grounding ---------------------------------------------------
def test_ground_scenario_binds_crosspage_and_drops_bogus():
    steps, unmatched = ground_scenario(
        [{"ref": "u", "verb": "fill", "value": "alice"}, {"ref": "pay", "verb": "click"},
         {"ref": "NOPE", "verb": "click"}], _site_map())
    assert [s["action_type"] for s in steps] == ["navigate", "fill", "navigate", "click"], steps
    assert unmatched == [{"ref": "NOPE", "reason": "ref not in site map"}], unmatched
    fill = steps[1]
    assert fill["locator"] == {"role": "textbox", "name": "Username"} and fill["value"] == "alice"
    assert fill["alternatives"], "grounded alternatives copied from the map element"
    assert steps[2]["target"] == _BILLING, "cross-page navigate target is the element's real page URL"


def test_scenario_steps_carry_no_llm_field_and_hash_stable():
    refs = [{"ref": "u", "verb": "fill", "value": "x", "reason": "LLM said so", "score": 0.9},
            {"ref": "pay", "verb": "click"}]
    s1, _ = ground_scenario(refs, _site_map())
    s2, _ = ground_scenario(refs, _site_map())
    allowed = {"step_id", "action_type", "semantic_id", "intent", "target", "locator", "alternatives",
               "is_milestone", "phase", "value", "text", "clear", "key", "condition", "expected",
               "expect_ok", "secretRef"}
    assert all(set(s) <= allowed for s in s1), [set(s) - allowed for s in s1]   # no reason/score leak
    assert canonical_plan_hash(s1) == canonical_plan_hash(s2)                    # deterministic


def test_reconcile_binds_by_role_name_crosspage_and_unmatched():
    draft = [{"verb": "fill", "intent": "user", "hypothesized_target": {"role": "textbox", "name": "Username"}, "value": "a"},
             {"verb": "click", "intent": "pay", "hypothesized_target": {"role": "button", "name": "Pay"}},
             {"verb": "click", "intent": "ghost", "hypothesized_target": {"role": "button", "name": "Nonexistent"}}]
    steps, unmatched = reconcile(draft, _site_map())
    assert [s["action_type"] for s in steps] == ["navigate", "fill", "navigate", "click"], steps
    assert len(unmatched) == 1 and "Nonexistent" in json.dumps(unmatched), unmatched


def test_reconcile_ambiguous_is_unmatched_never_guess():
    sm = {"p": [{"semantic_id": "a", "role": "button", "name": "Save", "testid": None, "locator": {}, "alternatives": [], "page": "p"},
                {"semantic_id": "b", "role": "button", "name": "Save", "testid": None, "locator": {}, "alternatives": [], "page": "p"}]}
    steps, unmatched = reconcile([{"verb": "click", "hypothesized_target": {"role": "button", "name": "Save"}}], sm)
    assert steps == [] and len(unmatched) == 1, (steps, unmatched)   # two "Save" -> conservative -> unmatched


# --- planner heads (FakeBackend) --------------------------------------------
def test_build_scenario_returns_refs_and_degrades():
    budget.reset(plan_limit=10000, heal_limit=10000)
    fb = FakeBackend(json.dumps({"steps": [{"ref": "u", "verb": "fill", "value": "a"}, {"ref": "pay", "verb": "click"}]}))
    out = GoalPlanner(goal="login and pay", backend=fb).build_scenario([{"semantic_id": "u"}, {"semantic_id": "pay"}])
    assert [r["ref"] for r in out["refs"]] == ["u", "pay"], out
    assert GoalPlanner(goal="x", backend=False).build_scenario([])["refs"] == []   # no backend -> empty
    budget.reset(plan_limit=10, heal_limit=10)
    budget.tracker().add("plan", LLMResult("x", 20, 0))                            # over budget
    fb2 = FakeBackend('{"steps": []}')
    assert GoalPlanner(goal="x", backend=fb2).build_scenario([{"semantic_id": "u"}])["refs"] == [] and fb2.calls == []
    budget.reset()


def test_describe_draft_returns_steps_and_degrades():
    budget.reset(plan_limit=10000, heal_limit=10000)
    fb = FakeBackend(json.dumps({"steps": [{"verb": "click", "intent": "x", "hypothesized_target": {"role": "button", "name": "Pay"}}]}))
    out = DescribePlanner(description="pay the bill", backend=fb).draft()
    assert out["draft"] and out["draft"][0]["verb"] == "click", out
    assert DescribePlanner(description="x", backend=False).draft()["draft"] == []   # no backend -> empty
    budget.reset()


# --- full two-phase machine (LangGraph, multi-page FakeEx) -------------------
class WalkEx:
    """A 2-page fake site: the heuristic walk navigates login->billing, accumulating a site map that
    includes an INPUT (not just buttons)."""
    PAGES = {
        _LOGIN: {"interactives": [{"tag": "button", "role": "button", "name": "Sign in", "testid": "signin", "text": "Sign in"},
                                  {"tag": "input", "role": "textbox", "name": "Username", "testid": None, "text": ""}],
                 "links": [{"href": _BILLING, "text": "Billing"}]},
        _BILLING: {"interactives": [{"tag": "button", "role": "button", "name": "Pay", "testid": "pay", "text": "Pay"}],
                   "links": []},
    }

    def __init__(self):
        self.url = ""

    def call(self, m, **p):
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
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.probe":
            return {"count": 1}
        return {}


def _explore_init(goal="", describe=""):
    return {"run_id": "t", "run_mode": "explore", "target_url": _LOGIN, "base_origin": "file:///s/",
            "coverage_target": 0.85, "max_steps": 40, "artifact_dir": tempfile.mkdtemp(),
            "goal": goal, "describe": describe, "site_map": {}, "phase": "explore",
            "scenario_steps": [], "scenario_unmatched": [], "current_url": _LOGIN, "page_model": {},
            "exploration_plan": [{"step_id": 1, "action_type": "navigate", "semantic_id": "nav1",
                                  "intent": "nav", "target": _LOGIN, "locator": None, "alternatives": None,
                                  "is_milestone": True}],
            "plan_hash": "", "current_step": 1, "interactive_seen": [], "interactive_exercised": [],
            "visited_paths": [], "nav_frontier": [], "coverage_achieved": 0.0,
            "exploration_complete": False, "executed_actions": [], "errors": []}


def _invoke(ex, scenario_head, init):
    from langgraph.checkpoint.memory import MemorySaver
    from brain.graph import build_graph
    app = build_graph(ex, HeuristicPlanner(), lambda r: None, scenario_head=scenario_head).compile(checkpointer=MemorySaver())
    return app.invoke(init, config={"recursion_limit": 200, "configurable": {"thread_id": "t"}})


def test_graph_two_phase_sitemap_generalized_and_scenario_grounded():
    budget.reset(plan_limit=100000, heal_limit=100000)
    ex = WalkEx()
    user_sid = semantic_id(normalize_url(_LOGIN), "textbox", "Username")
    pay_sid = semantic_id(normalize_url(_BILLING), "button", "pay")    # button Pay anchors on testid "pay"
    fb = FakeBackend(json.dumps({"steps": [{"ref": user_sid, "verb": "fill", "value": "alice"},
                                           {"ref": pay_sid, "verb": "click"}, {"ref": "bogus", "verb": "click"}]}))
    ex.call("browser.navigate", url=_LOGIN)        # land on page 1 before the walk
    final = _invoke(ex, GoalPlanner(goal="log in and pay", backend=fb), _explore_init(goal="log in and pay"))
    sm = final["site_map"]
    assert normalize_url(_LOGIN) in sm and normalize_url(_BILLING) in sm, list(sm)
    assert "textbox" in {e["role"] for page in sm.values() for e in page}, "site map generalized beyond buttons"
    sc = final["scenario_steps"]
    kinds = [s["action_type"] for s in sc]
    assert "fill" in kinds and "click" in kinds and "navigate" in kinds, kinds   # grounded + cross-page navigate
    # §5 determinism @ integration: the grounded fill carries the cataloguer's locator+alternatives,
    # and the synthesized navigate targets the element's REAL discovered URL.
    fill = next(s for s in sc if s["action_type"] == "fill")
    assert fill["locator"] == {"role": "textbox", "name": "Username"} and fill["alternatives"], fill
    assert any(s["action_type"] == "navigate" and s["target"] == normalize_url(_BILLING) for s in sc), sc
    assert final["scenario_unmatched"] == [{"ref": "bogus", "reason": "ref not in site map"}], final["scenario_unmatched"]
    budget.reset()


def test_pure_explore_invariant_unchanged_by_generalization():
    # ADR-028 §2: generalizing the cataloguer beyond buttons must NOT perturb pure-explore
    # coverage/convergence/plan_hash. scenario_head=None -> the scenario node is a no-op.
    def _run():
        budget.reset(plan_limit=100000, heal_limit=100000)
        ex = WalkEx()
        ex.call("browser.navigate", url=_LOGIN)
        return _invoke(ex, None, _explore_init())
    a, b = _run(), _run()
    assert a["plan_hash"] == b["plan_hash"], "pure-explore plan_hash must be deterministic + unperturbed"
    assert a["coverage_achieved"] == b["coverage_achieved"] == 1.0, a["coverage_achieved"]
    assert {s["action_type"] for s in a["exploration_plan"]} <= {"navigate", "click"}, "no input leaked as an explore step"
    assert not a.get("scenario_steps"), "scenario node is a no-op in pure explore"
    budget.reset()


def test_graph_describe_branch_reconciles_through_node():
    budget.reset(plan_limit=100000, heal_limit=100000)
    ex = WalkEx()
    ex.call("browser.navigate", url=_LOGIN)
    draft = json.dumps({"steps": [
        {"verb": "fill", "intent": "user", "hypothesized_target": {"role": "textbox", "name": "Username"}, "value": "alice"},
        {"verb": "click", "intent": "pay", "hypothesized_target": {"role": "button", "name": "Pay"}},
        {"verb": "click", "intent": "ghost", "hypothesized_target": {"role": "button", "name": "Nonexistent"}}]})
    final = _invoke(ex, DescribePlanner(description="log in and pay", backend=FakeBackend(draft)),
                    _explore_init(describe="log in and pay"))
    kinds = [s["action_type"] for s in final["scenario_steps"]]
    assert "fill" in kinds and "click" in kinds and "navigate" in kinds, kinds   # describe branch grounds through the node
    assert len(final["scenario_unmatched"]) == 1 and "Nonexistent" in json.dumps(final["scenario_unmatched"]), final["scenario_unmatched"]
    budget.reset()


def test_elements_from_interactives_role_mapping_and_button_identical():
    from brain.graph import _elements_from_interactives
    raw = [{"tag": "button", "role": "button", "name": "Save", "testid": "save", "text": "Save"},
           {"tag": "input", "role": "textbox", "name": "Email", "testid": None, "text": ""},
           {"tag": "select", "role": "combobox", "name": "Country", "testid": None, "text": ""},
           {"tag": "a", "role": "link", "name": "Home", "testid": None, "text": "Home"},
           {"tag": "input", "role": "checkbox", "name": "Agree", "testid": None, "text": ""}]
    by_name = {e["name"]: e for e in _elements_from_interactives(raw, "p")}
    assert by_name["Save"]["role"] == "button" and by_name["Email"]["role"] == "textbox"
    assert by_name["Country"]["role"] == "combobox" and by_name["Home"]["role"] == "link"
    assert by_name["Agree"]["role"] == "checkbox", by_name["Agree"]   # not misclassified as textbox
    # a BUTTON stays byte-identical to the old button-only cataloguer: same semantic_id, no `label` alt.
    save = by_name["Save"]
    assert save["semantic_id"] == semantic_id("p", "button", "save")
    assert [a["strategy"] for a in save["alternatives"]] == ["testid", "role_name"], save["alternatives"]


def test_match_role_bearing_draft_never_cross_role_binds():
    # a draft {role: button, name: Delete} where the only "Delete" is a LINK must be unmatched, not bound.
    sm = {"p": [{"semantic_id": "lnk", "role": "link", "name": "Delete", "testid": None,
                 "locator": {"role": "link", "name": "Delete"}, "alternatives": [], "page": "p"}]}
    steps, unmatched = reconcile([{"verb": "click", "hypothesized_target": {"role": "button", "name": "Delete"}}], sm)
    assert steps == [] and len(unmatched) == 1, (steps, unmatched)
    # but a role-LESS draft {name: Delete} still binds by name
    steps2, _ = reconcile([{"verb": "click", "hypothesized_target": {"name": "Delete"}}], sm)
    assert len(steps2) == 2 and steps2[1]["semantic_id"] == "lnk", steps2   # navigate + click on the real link


def test_unsupported_verb_is_unmatched_not_a_bad_step():
    steps, unmatched = ground_scenario([{"ref": "pay", "verb": "navigate"}], _site_map())   # 'navigate' is not a verb
    assert steps == [] and unmatched and "unsupported verb" in json.dumps(unmatched), (steps, unmatched)


def test_runconfig_unknown_scenario_and_both_modes_raise():
    env = {"GOAL": "", "DESCRIBE": "", "SCENARIO": "typo_name"}
    try:                                                # unknown --scenario -> config error (exit 3), not silent first
        apply_run_config({"scenarios": [{"name": "login", "goal": "G"}]}, env)
        assert False, "unknown --scenario must raise"
    except ValueError:
        pass
    try:                                                # an entry with BOTH goal+describe -> XOR validation raises
        load_run_config(_yaml("scenarios:\n  - {name: x, goal: G, describe: D}\n"))
        assert False, "goal+describe entry must raise"
    except ValueError:
        pass


# --- grounded scenario replays deterministically -----------------------------
class ReplayEx:
    def __init__(self):
        self.url = ""

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", "")
            return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- x", "nodeCount": 1}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.probe":
            return {"count": 1}
        return {}


def test_grounded_scenario_replays_to_exit0_and_rehash_stable():
    steps, _ = ground_scenario([{"ref": "u", "verb": "fill", "value": "alice"}, {"ref": "pay", "verb": "click"}], _site_map())
    sc = [{**s, "step_id": i + 1} for i, s in enumerate(steps)]
    plan = {"plan_id": "sc", "target_url": _LOGIN, "plan_hash": canonical_plan_hash(sc), "steps": sc}
    assert canonical_plan_hash(plan["steps"]) == plan["plan_hash"]        # stored == re-hash (determinism)
    ex = ReplayEx()
    st = Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)
    he = HealingEngine(ex, st, "r", use_llm=False)
    r = run_replay(ex, st, he, plan, _LOGIN, tempfile.mkdtemp())
    assert r["exit_code"] == 0 and r["failed"] == 0, r                    # the authored scenario replays clean


# --- rich RunConfig ----------------------------------------------------------
def _yaml(text):
    p = os.path.join(tempfile.mkdtemp(), "rc.yaml")
    with open(p, "w") as f:
        f.write(text)
    return p


def test_runconfig_auth_maps_to_m9_1_env():
    env = {"STORAGE_STATE": "", "STORAGE_STATE_SAVE": "", "PLAN_FILE": "", "PW_NO_TRACE": ""}
    cfg = load_run_config(_yaml("auth:\n  storage_state: s.json\n  pw_no_trace: true\n  login_plan: l.json\n"))
    apply_run_config(cfg, env)
    assert env["STORAGE_STATE"] == "s.json" and env["PW_NO_TRACE"] == "1" and env["PLAN_FILE"] == "l.json", env


def test_runconfig_scenarios_selector():
    cfg = load_run_config(_yaml("scenarios:\n  - {name: login, goal: GOAL_A}\n  - {name: signup, describe: DESC_B}\n"))
    env = {"GOAL": "", "DESCRIBE": "", "SCENARIO": "signup"}
    apply_run_config(cfg, env)
    assert env["DESCRIBE"] == "DESC_B" and env["GOAL"] == "", env          # selector picks the describe entry
    env2 = {"GOAL": "", "DESCRIBE": "", "SCENARIO": ""}
    apply_run_config(cfg, env2)
    assert env2["GOAL"] == "GOAL_A", env2                                  # no selector -> first entry


def test_runconfig_auth_precedence_preset_env_wins():
    env = {"STORAGE_STATE": "preset.json"}                                 # user-exported -> file must NOT override
    apply_run_config(load_run_config(_yaml("auth: {storage_state: file.json}\n")), env)
    assert env["STORAGE_STATE"] == "preset.json", env


def test_runconfig_malformed_auth_and_scenarios_raise():
    for body in ("auth: not_a_mapping\n", "scenarios:\n  - {name: x}\n"):  # auth not a map; scenario lacks goal/describe
        try:
            load_run_config(_yaml(body))
            assert False, body
        except ValueError:
            pass


# --- __main__ exit codes -----------------------------------------------------
def test_write_scenario_exit_codes():
    import brain.__main__ as m
    step = [{"step_id": 1, "action_type": "navigate", "semantic_id": "n", "intent": "go", "target": "u",
             "locator": None, "alternatives": None, "is_milestone": True, "phase": "scenario"}]
    o1 = pathlib.Path(tempfile.mkdtemp())
    assert m._write_scenario(o1, "r", "file:///s", step, [], False) == 0 and (o1 / "scenario.json").exists()
    o2 = pathlib.Path(tempfile.mkdtemp())
    assert m._write_scenario(o2, "r", "file:///s", step, [{"ref": "x"}], True) == 1   # describe unmatched -> 1
    assert (o2 / "reconcile-report.json").exists()
    o3 = pathlib.Path(tempfile.mkdtemp())
    assert m._write_scenario(o3, "r", "file:///s", [], [], False) == 1               # zero grounded -> 1


def test_goal_and_describe_mutually_exclusive_exit3():
    import brain.__main__ as m

    class _Ex:
        def call(self, *a, **k):
            return {}

    saved = dict(os.environ)
    os.environ.update({"GOAL": "g", "DESCRIBE": "d"})
    try:
        rc = m._run_explore(_Ex(), "r", pathlib.Path(tempfile.mkdtemp()), "file:///s/index.html", 0.85, 40)
        assert rc == 3, rc                                                # mutually exclusive heads -> exit 3
    finally:
        os.environ.clear()
        os.environ.update(saved)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
