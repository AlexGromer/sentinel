"""Offline M8 tests — token budget ceiling + W3C context helpers (no collector, no network).

Run:  .venv/bin/python tests/test_m8_offline.py
Proves: BudgetTracker accumulates per role and flips exceeded() at the limit; an exhausted plan
budget degrades LLMPlanner to the heuristic (no backend call); spend is recorded under budget;
otel inject/extract are no-ops when tracing isn't configured.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import budget, otel                          # noqa: E402
from brain.budget import BudgetTracker                  # noqa: E402
from brain.llm import LLMResult                          # noqa: E402
from brain.planner import HeuristicPlanner, LLMPlanner   # noqa: E402


class FakeBackend:
    name, model, supports_vision = "fake", "fake-model", False

    def __init__(self, reply='{"index": 0}', pt=100, ct=100):
        self.reply, self._pt, self._ct, self.calls = reply, pt, ct, []

    def complete(self, prompt, *, max_tokens, temperature):
        self.calls.append((max_tokens, temperature))
        return LLMResult(self.reply, self._pt, self._ct)

    def complete_vision(self, *a, **k):
        raise NotImplementedError


def test_tracker_accumulates_and_flips_per_role():
    t = BudgetTracker(plan_limit=250, heal_limit=100, total_limit=0)
    assert not t.exceeded("plan")
    t.add("plan", LLMResult("x", 100, 100))     # 200 < 250
    assert not t.exceeded("plan")
    t.add("plan", LLMResult("x", 30, 30))       # 260 >= 250
    assert t.exceeded("plan")
    assert not t.exceeded("heal")               # heal limit independent
    assert t.total() == 260


def test_total_limit_gate():
    t = BudgetTracker(plan_limit=0, heal_limit=0, total_limit=150)  # per-role off, total on
    t.add("plan", LLMResult("x", 100, 0))
    assert not t.exceeded("heal")
    t.add("heal", LLMResult("x", 60, 0))        # total 160 >= 150
    assert t.exceeded("plan") and t.exceeded("heal")


def test_exceeded_plan_budget_degrades_planner_to_heuristic():
    budget.reset(plan_limit=50, heal_limit=20)
    budget.tracker().add("plan", LLMResult("x", 60, 0))   # already over the 50 limit
    fb = FakeBackend('{"index": 0}')
    p = LLMPlanner(backend=fb)
    cands = [{"kind": "click", "role": "b", "name": "A", "target": None}]
    state = {"current_url": "u", "coverage_achieved": 0.0, "coverage_target": 0.85}
    d = p.propose(state, cands)
    assert d == HeuristicPlanner().propose(state, cands)   # degraded to heuristic
    assert fb.calls == [], "backend must NOT be called once the plan budget is exhausted"
    budget.reset()                                          # restore env defaults for other tests


def test_planner_records_spend_when_under_budget():
    budget.reset(plan_limit=10000, heal_limit=10000)
    fb = FakeBackend('{"index": 0}', pt=7, ct=11)
    p = LLMPlanner(backend=fb)
    p.propose({"current_url": "u", "coverage_achieved": 0.0, "coverage_target": 0.85},
              [{"kind": "click", "role": "b", "name": "A", "target": None}])
    assert budget.tracker().spent["plan"] == 18, budget.tracker().spent
    assert fb.calls == [(200, 0)]
    budget.reset()


def test_otel_context_helpers_noop_without_endpoint():
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    otel.setup_tracing()                        # no endpoint -> tracer stays None
    carrier = {}
    assert otel.inject_context(carrier) is carrier and carrier == {}   # nothing injected
    assert otel.extract_context({"traceparent": "x"}) is None          # no-op extract


def test_runcontrol_noop_without_orchestrator():
    os.environ.pop("ORCH_ADDR", None)
    from brain import runcontrol
    rc = runcontrol.make_client()
    assert rc.report("run", "plan", 100, 50) is False   # no orchestrator -> never aborts
    rc.close()


def test_runcontrol_stubs_import():
    from brain.pb import runcontrol_pb2 as pb, runcontrol_pb2_grpc as pbg
    assert hasattr(pbg, "RunControlStub")
    ev = pb.RunEvent(run_id="r", node="plan", prompt_tokens=7, completion_tokens=11, status="running")
    assert ev.prompt_tokens == 7 and ev.node == "plan"


def test_grpc_trace_interceptor_noop_without_tracing():
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    from brain import otel, store
    otel.setup_tracing()                        # tracing off -> no traceparent injected
    interceptor = store._trace_interceptor()
    seen = {}

    class _Details:
        method = "/svc/M"
        timeout = None
        metadata = None
        credentials = None
        wait_for_ready = None
        compression = None

    def cont(details, request):
        seen["metadata"] = details.metadata
        return "resp"

    out = interceptor.intercept_unary_unary(cont, _Details(), "req")
    assert out == "resp"
    assert seen["metadata"] is None             # nothing injected when tracing is off


def test_graph_builds_with_per_node_spans_and_runcontrol():
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    os.environ.pop("ORCH_ADDR", None)
    from brain.graph import build_graph
    from brain.planner import HeuristicPlanner

    class FakeEx:
        def call(self, *a, **k):
            return {}

    b = build_graph(FakeEx(), HeuristicPlanner(), lambda r: None)   # span-wrap + no-op runcontrol
    assert b is not None and hasattr(b, "add_node")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
