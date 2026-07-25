"""Offline gate for the explore loop on an element that cannot be actuated (M9-LIVE).

Run:  .venv/bin/python tests/test_explore_loop_offline.py

The defect this pins, seen live 2026-07-23: `act` marks an element exercised only on SUCCESS, so a
control that always raises stayed in the candidate set and the planner proposed it again on the very
next step. The live log showed the same click 34 times, ~5s apart, until `max_steps` — a whole run's
budget spent on one button, reported as exploration.

Every assertion here counts REAL executor calls rather than inspecting state, because the state
could look right while the driver was still being hammered. The load on the browser is the thing
that was wrong, so the load on the browser is the thing measured.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver          # noqa: E402

from brain import budget, graph as graph_mod                 # noqa: E402
from brain.graph import build_graph                          # noqa: E402
from brain.planner import HeuristicPlanner                   # noqa: E402
from brain.state import normalize_url, semantic_id           # noqa: E402

PAGE = "file:///s/index.html"
MAX_STEPS = 40


class ClickEx:
    """A one-page executor where named buttons refuse to be clicked, and some report themselves
    disabled. `clicks` records every locator the graph actually sent, which is the measurement."""

    def __init__(self, buttons, breaking=(), links=(), breaking_links=()):
        self.buttons = buttons          # [{"name":..., "disabled": bool}]
        self.breaking = set(breaking)   # names whose click raises
        self.links = list(links)
        self.breaking_links = set(breaking_links)
        self.url = ""
        self.clicks = []                # every browser.click locator, in order
        self.navigations = []           # every browser.navigate url, in order

    def call(self, m, **p):
        if m == "browser.navigate":
            self.navigations.append(p["url"])
            if p["url"] in self.breaking_links:
                raise RuntimeError(f"navigation refused: {p['url']}")
            self.url = p["url"]
            return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": f"- page {self.url}", "nodeCount": 1}
        if m == "browser.interactives":
            if self.url != PAGE:
                return {"elements": []}
            return {"elements": [{"tag": "button", "role": "button", "name": b["name"],
                                  "testid": None, "text": b["name"],
                                  "disabled": b.get("disabled", False)}
                                 for b in self.buttons]}
        if m == "browser.links":
            return {"links": [{"href": u, "text": u} for u in self.links]} if self.url == PAGE else {"links": []}
        if m == "browser.click":
            name = (p.get("locator") or {}).get("name", "")
            self.clicks.append(name)
            if name in self.breaking:
                raise RuntimeError(f"element not actionable: {name}")
            return {"ok": True}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.probe":
            return {"count": 1}
        return {}


def _init():
    return {"run_id": "t", "run_mode": "explore", "target_url": PAGE, "base_origin": "file:///s/",
            "coverage_target": 0.85, "max_steps": MAX_STEPS, "artifact_dir": tempfile.mkdtemp(),
            "goal": "", "describe": "", "site_map": {}, "phase": "explore",
            "scenario_steps": [], "scenario_unmatched": [], "current_url": PAGE, "page_model": {},
            "exploration_plan": [{"step_id": 1, "action_type": "navigate", "semantic_id": "nav1",
                                  "intent": "nav", "target": PAGE, "locator": None,
                                  "alternatives": None, "is_milestone": True}],
            "plan_hash": "", "current_step": 1, "interactive_seen": [], "interactive_exercised": [],
            "visited_paths": [], "nav_frontier": [], "coverage_achieved": 0.0,
            "exploration_complete": False, "executed_actions": [], "errors": []}


def _run(ex):
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    ex.call("browser.navigate", url=PAGE)
    ex.navigations.clear()
    app = build_graph(ex, HeuristicPlanner(), lambda r: None).compile(checkpointer=MemorySaver())
    return app.invoke(_init(), config={"recursion_limit": 400, "configurable": {"thread_id": "t"}})


def test_a_permanently_failing_element_is_tried_twice_and_then_dropped():
    ex = ClickEx(buttons=[{"name": "Broken"}, {"name": "Works"}], breaking=["Broken"])
    final = _run(ex)

    broken_clicks = ex.clicks.count("Broken")
    assert broken_clicks == graph_mod._EXPLORE_FAIL_LIMIT, (
        f"the broken element was clicked {broken_clicks} times, expected exactly "
        f"{graph_mod._EXPLORE_FAIL_LIMIT} — this is the ×34 loop")
    # Far below max_steps is the point: without the blacklist the run burns its whole budget here.
    assert broken_clicks < MAX_STEPS / 4, broken_clicks
    # And dropping it must not stop exploration — the button that works still gets its turn.
    assert "Works" in ex.clicks, ex.clicks
    assert final["exploration_complete"] is True

    sid = semantic_id(normalize_url(PAGE), "button", "Broken")
    assert final["interactive_failed"].get(sid) == graph_mod._EXPLORE_FAIL_LIMIT, final["interactive_failed"]
    assert sid not in final["interactive_exercised"], "a failing element must not count as exercised"
    assert sid in final["interactive_seen"], (
        "it must stay SEEN — coverage should report a page with an unreachable control, not pretend "
        "the control is not there")


def test_the_retry_budget_is_the_constant_not_a_hardcoded_two():
    # Guards against the limit being read once and then ignored: move it and the behaviour must move.
    original = graph_mod._EXPLORE_FAIL_LIMIT
    try:
        graph_mod._EXPLORE_FAIL_LIMIT = 1
        ex = ClickEx(buttons=[{"name": "Broken"}, {"name": "Works"}], breaking=["Broken"])
        _run(ex)
        assert ex.clicks.count("Broken") == 1, ex.clicks
        graph_mod._EXPLORE_FAIL_LIMIT = 3
        ex3 = ClickEx(buttons=[{"name": "Broken"}, {"name": "Works"}], breaking=["Broken"])
        _run(ex3)
        assert ex3.clicks.count("Broken") == 3, ex3.clicks
    finally:
        graph_mod._EXPLORE_FAIL_LIMIT = original


def test_a_disabled_element_is_never_clicked_but_still_counted_as_seen():
    ex = ClickEx(buttons=[{"name": "Submit", "disabled": True}, {"name": "Works"}])
    final = _run(ex)

    assert "Submit" not in ex.clicks, (
        "a control the page reports as disabled must not be clicked at all — Playwright waits for "
        "actionability and times out, which is where the ~5s per useless iteration came from")
    assert "Works" in ex.clicks, ex.clicks

    sid = semantic_id(normalize_url(PAGE), "button", "Submit")
    assert sid in final["interactive_seen"], (
        "a disabled control is part of the page; dropping it from perception would report coverage "
        "over a smaller page than the one under test, and it is usually enabled later in a filled form")
    assert final["coverage_achieved"] < 1.0, (
        f"coverage must stay honest about the control that was never exercised: {final['coverage_achieved']}")


def test_aria_disabled_counts_the_same_as_the_attribute():
    # `disabled` is only valid on form controls, so a div/span acting as a button can only say so
    # through aria-disabled. Both spellings arrive as the same boolean from pw-executor.
    ex = ClickEx(buttons=[{"name": "Fancy", "disabled": True}, {"name": "Works"}])
    _run(ex)
    assert "Fancy" not in ex.clicks, ex.clicks


def test_a_link_that_cannot_be_followed_is_dropped_too():
    # The same loop, one axis over: a navigate candidate comes from the frontier and was equally
    # immune to being marked, so a link that always refuses looped exactly like a button.
    dead = "file:///s/dead.html"
    ex = ClickEx(buttons=[{"name": "Works"}], links=[dead], breaking_links=[dead])
    _run(ex)
    attempts = ex.navigations.count(dead)
    assert attempts == graph_mod._EXPLORE_FAIL_LIMIT, (
        f"the unreachable link was attempted {attempts} times, expected {graph_mod._EXPLORE_FAIL_LIMIT}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}\n       {e}")
        except Exception as e:  # a raise from the graph is a failure, not a crash to hide
            failed += 1
            print(f"  FAIL {t.__name__}\n       {type(e).__name__}: {e}")
    print(f"\nexplore-loop: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
