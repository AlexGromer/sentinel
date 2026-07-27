#!/usr/bin/env python3
"""M-STRUCTURED-OUT (ADR-057) offline tests — strict LLM structured outputs + robust JSON fallback.

Self-executing (no pytest/conftest): the CI loop runs `python tests/test_m_structured_out_offline.py`.
Covers:
  * `extract_json` robustness (fences, trailing prose, nested braces, brace-in-string, no-object → raise);
  * `complete_structured` routing (native tool_use/json_schema vs text fallback vs data=None salvage);
  * the migrated consumers (planners ×4 + heal css/visual) under BOTH a structured and a text-only fake
    backend — asserting grounding (index/refs, OOB → done), budget accounting + over-budget skip, and
    byte-identical fallback behaviour are all preserved.

Invariant: `StructuredBackend.complete()` raises — so if any structured path accidentally text-parses
instead of using native `.data`, the test fails loudly.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import budget
from brain.healing import HealingEngine
from brain.llm import LLMResult, complete_structured, extract_json
from brain.planner import DescribePlanner, GoalPlanner, HeuristicPlanner, LLMPlanner


# ---- fakes -----------------------------------------------------------------
class StructuredBackend:
    """Native structured backend: `complete_json` returns a canned parsed object. `complete()` must
    never run (asserts the native path is taken, not the text fallback)."""
    name = "fake-structured"
    model = "fake"
    supports_vision = True
    supports_structured = True

    def __init__(self, data, *, pt=3, ct=5):
        self.data, self._pt, self._ct, self.calls = data, pt, ct, []

    def complete_json(self, prompt, *, schema, max_tokens, temperature):
        self.calls.append(("json", prompt))
        return LLMResult("<<native>>", self._pt, self._ct, data=self.data)

    def complete(self, prompt, *, max_tokens, temperature):
        raise AssertionError("structured backend must not fall back to complete()")

    def complete_vision(self, prompt, image_b64, *, max_tokens, temperature):
        self.calls.append(("vision", prompt))
        return LLMResult("<<native>>", self._pt, self._ct, data=self.data)


class TextBackend:
    """Text-only backend (supports_structured=False): `complete` returns a canned reply that
    `complete_structured` must parse via `extract_json`. Mirrors the historical FakeBackend."""
    name = "fake-text"
    model = "fake"
    supports_vision = True
    supports_structured = False

    def __init__(self, reply, *, pt=2, ct=4):
        self.reply, self._pt, self._ct, self.calls = reply, pt, ct, []

    def complete(self, prompt, *, max_tokens, temperature):
        self.calls.append(prompt)
        return LLMResult(self.reply, self._pt, self._ct)

    def complete_vision(self, prompt, image_b64, *, max_tokens, temperature):
        self.calls.append(prompt)
        return LLMResult(self.reply, self._pt, self._ct)


class FakeEx:
    """Minimal pw-executor stand-in for the visual-heal path."""
    def __init__(self, marks):
        self._marks = marks

    def call(self, method, **kw):
        return {"marks": self._marks} if method == "browser.setOfMarks" else {}


def _fakes(data):
    """Return (structured, text) fakes that both yield `data` — one natively, one as JSON text."""
    return StructuredBackend(data), TextBackend(json.dumps(data))


CANDS = [{"kind": "click", "role": "button", "name": "Login", "target": None,
          "intent": "log in", "semantic_id": "s1"},
         {"kind": "navigate", "role": None, "name": None, "target": "/next",
          "intent": None, "semantic_id": "s2"}]
STATE = {"current_url": "http://x", "coverage_achieved": 0.1, "coverage_target": 0.9,
         "current_step": 1, "max_steps": 10}


# ---- extract_json ----------------------------------------------------------
def test_extract_json_robust():
    assert extract_json('{"index":2}') == {"index": 2}
    assert extract_json('```json\n{"done": true}\n```') == {"done": True}          # fenced (b1 contract)
    assert extract_json('Sure, do this: {"index": 1} — hope that helps!') == {"index": 1}  # trailing prose
    assert extract_json('{"steps":[{"ref":"a","verb":"click"}]}') == {"steps": [{"ref": "a", "verb": "click"}]}
    assert extract_json('{"css":"a.x}y"}') == {"css": "a.x}y"}                      # brace inside a string
    for bad in ("no json here", "", "plain text"):
        raised = False
        try:
            extract_json(bad)
        except Exception:
            raised = True
        assert raised, f"extract_json should raise on {bad!r}"


# ---- complete_structured routing ------------------------------------------
def test_complete_structured_routing():
    r = complete_structured(StructuredBackend({"index": 7}), "p", {"type": "object"},
                            max_tokens=10, temperature=0)
    assert r.data == {"index": 7} and (r.prompt_tokens, r.completion_tokens) == (3, 5)

    tb = TextBackend('reply: {"done":true}')
    r = complete_structured(tb, "p", {"type": "object"}, max_tokens=10, temperature=0)
    assert r.data == {"done": True} and (r.prompt_tokens, r.completion_tokens) == (2, 4)
    assert tb.calls == ["p"]

    class NullStruct(StructuredBackend):  # native path returns data=None -> salvage from .text
        def complete_json(self, prompt, *, schema, max_tokens, temperature):
            return LLMResult('{"index":9}', 1, 1, data=None)

    r = complete_structured(NullStruct({}), "p", {"type": "object"}, max_tokens=10, temperature=0)
    assert r.data == {"index": 9}


# ---- planners (both backends) ---------------------------------------------
def test_llmplanner_picks_index_and_done():
    budget.reset(plan_limit=10_000)
    for be in _fakes({"index": 0}):
        d = LLMPlanner(backend=be).propose(STATE, CANDS)
        assert d["action"] == CANDS[0] and d["done"] is False, (be.name, d)
    for be in _fakes({"done": True}):
        d = LLMPlanner(backend=be).propose(STATE, CANDS)
        assert d["action"] is None and d["done"] is True, (be.name, d)
    budget.reset()


def test_goalplanner_grounding_and_garbage_fallback():
    budget.reset(plan_limit=10_000)
    for be in _fakes({"index": 99}):  # out-of-bounds index must degrade to done, never fabricate
        d = GoalPlanner(goal="log in", backend=be).propose(STATE, CANDS)
        assert d["action"] is None and d["done"] is True, ("OOB", be.name, d)
    for be in _fakes({"index": 0}):
        d = GoalPlanner(goal="log in", backend=be).propose(STATE, CANDS)
        assert d["action"] == CANDS[0], (be.name, d)
    # non-JSON text reply -> extract_json raises -> heuristic fallback (no crash, no fabrication)
    d = GoalPlanner(goal="log in", backend=TextBackend("not json at all")).propose(STATE, CANDS)
    assert d == HeuristicPlanner().propose(STATE, CANDS)
    budget.reset()


def test_build_scenario_refs():
    budget.reset(plan_limit=10_000)
    flat = [{"semantic_id": "s1", "page": "/", "role": "button", "name": "Login"}]
    data = {"steps": [{"ref": "s1", "verb": "click"}]}
    for be in _fakes(data):
        out = GoalPlanner(goal="log in", backend=be).build_scenario(flat, goal="log in")
        assert out["refs"] == [{"ref": "s1", "verb": "click"}], (be.name, out)
    budget.reset()


def test_describe_draft():
    budget.reset(plan_limit=10_000)
    data = {"steps": [{"verb": "click", "intent": "login",
                       "hypothesized_target": {"role": "button", "name": "Login"}}]}
    for be in _fakes(data):
        out = DescribePlanner(description="log in", backend=be).draft()
        assert out["draft"] == data["steps"], (be.name, out)
    budget.reset()


# ---- healing --------------------------------------------------------------
def test_heal_llm_reground_pick():
    """The structured contract for the text tier is an INDEX into the live element list, not a
    model-authored selector (ADR-082) — so the schema-driven and fallback backends alike are asserted
    to yield the picked element's own real locator."""
    budget.reset(heal_limit=10_000)
    ctx = {"intent": "submit", "attempted_locator": {"role": "button", "name": "Submit"},
           "interactives": [{"role": "button", "name": "Send", "testid": "send"}]}
    for be in _fakes({"index": 0}):
        out = HealingEngine(None, None, "run1", use_llm=True, backend=be)._llm_reground(ctx)
        assert out is not None and out[0] == "llm_pick" and out[1] == {"testid": "send"}, (be.name, out)
        assert out[3] == ctx["interactives"][0], (be.name, out)   # the live descriptor rides along
    for be in _fakes({"none": True}):
        assert HealingEngine(None, None, "run1", use_llm=True, backend=be)._llm_reground(ctx) is None
    budget.reset()


def test_heal_visual_reground_extract_json():
    budget.reset(heal_limit=10_000)
    marks = [{"mark": 1, "role": "button", "name": "X", "testid": "t1"}]
    ctx = {"intent": "click X", "interactives": []}
    he = HealingEngine(FakeEx(marks), None, "run1", use_llm=True, use_visual=True,
                       backend=TextBackend('{"mark":1}'))
    out = he._visual_reground(ctx)
    assert out is not None and out[0] == "visual" and out[1] == {"testid": "t1"}, out
    he2 = HealingEngine(FakeEx(marks), None, "run1", use_llm=True, use_visual=True,
                        backend=TextBackend('{"none":true}'))
    assert he2._visual_reground(ctx) is None
    budget.reset()


# ---- budget accounting -----------------------------------------------------
def test_budget_exhaustion_skips_backend():
    budget.reset(plan_limit=1)                              # tiny plan budget
    budget.tracker().add("plan", LLMResult("", 10, 10))     # pre-spend 20 -> over budget
    be = StructuredBackend({"index": 0})
    d = LLMPlanner(backend=be).propose(STATE, CANDS)
    assert d == HeuristicPlanner().propose(STATE, CANDS), d
    assert be.calls == [], "backend must NOT be called when over budget"
    budget.reset()


def test_token_accounting_propagates():
    budget.reset(plan_limit=10_000)
    be = StructuredBackend({"index": 0}, pt=7, ct=11)
    d = LLMPlanner(backend=be).propose(STATE, CANDS)
    assert d["tokens"] == {"prompt": 7, "completion": 11}, d["tokens"]
    assert budget.tracker().spent["plan"] == 18, budget.tracker().spent
    budget.reset()


def test_budget_charged_on_garbage_fallback():
    # Regression (adversarial-verify, 3 lenses): a non-JSON reply on the fallback path must STILL
    # debit the token budget — the tokens were spent at the provider — even though the decision
    # degrades to the heuristic. complete_structured must return data=None (not raise) so the
    # caller's budget.tracker().add() runs before the degrade.
    budget.reset(plan_limit=10_000)
    be = TextBackend("I cannot determine the next action.", pt=350, ct=12)
    d = GoalPlanner(goal="log in", backend=be).propose(STATE, CANDS)
    assert d == HeuristicPlanner().propose(STATE, CANDS), d               # degraded to heuristic
    assert budget.tracker().spent["plan"] == 362, budget.tracker().spent  # ...but tokens still booked
    budget.reset()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print(f"ok  {n}")
    print(f"OK — {len(tests)} M-STRUCTURED-OUT tests passed")
