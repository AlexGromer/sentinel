"""Offline test for the adaptive structured-output token budget (M9-LIVE, brain/llm.py).

A reasoning model can spend a tight max_tokens entirely on THINK tokens and stop before emitting any
answer (finish_reason="length", empty content). `complete_structured` must, rather than silently
degrade: retry the SAME call with a doubled ceiling until content appears or the hard cap is reached,
sum the token cost across attempts (ADR-021), persist the working ceiling per model, and — critically —
NOT retry a genuine (non-length) bad reply. No network: a FakeBackend drives every branch.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain.llm as L  # noqa: E402


class FakeBackend:
    name = "openai"
    supports_vision = False
    supports_structured = False

    def __init__(self, model="fake-reasoner", floor=1500, reply='{"steps":[{"ref":"a","verb":"click"}]}',
                 finish="stop"):
        self.model = model
        self.floor = floor      # emits content only once max_tokens >= floor
        self.reply = reply
        self.finish = finish
        self.calls = []

    def complete(self, prompt, *, max_tokens, temperature):
        self.calls.append(max_tokens)
        if max_tokens < self.floor:
            return L.LLMResult("", 10, max_tokens, model=self.model, finish_reason="length")
        return L.LLMResult(self.reply, 10, 40, model=self.model, finish_reason=self.finish)


def _isolate(tmp):
    # set the module attribute directly: _BUDGET_FILE is resolved from the env at import time, so
    # setting the env here (after import) would be ignored and writes would land in the repo's state/.
    L._BUDGET_FILE = os.path.join(tmp, "llm-budget.json")
    L._learned_cache = None
    L._TOKEN_HARD_MAX = 4000
    L._ADAPTIVE = True


def test_escalates_sums_and_persists():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        b = FakeBackend(floor=1500)
        r = L.complete_structured(b, "p", {"type": "object"}, max_tokens=800, temperature=0)
        assert r.data is not None, "should have escalated to a parseable reply"
        assert b.calls == [800, 1600], f"escalation ladder wrong: {b.calls}"
        assert r.completion_tokens == 800 + 40, f"tokens must be summed across attempts: {r.completion_tokens}"
        store = json.load(open(L._BUDGET_FILE))
        assert store == {"fake-reasoner": 1600}, f"working ceiling not persisted: {store}"


def test_second_run_starts_from_learned_ceiling():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        json.dump({"fake-reasoner": 1600}, open(L._BUDGET_FILE, "w"))
        L._learned_cache = None
        b = FakeBackend(floor=1500)
        r = L.complete_structured(b, "p", {"type": "object"}, max_tokens=800, temperature=0)
        assert r.data is not None
        assert b.calls == [1600], f"should start at the learned 1600, no escalation: {b.calls}"


def test_no_retry_on_genuine_bad_reply():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        # finish_reason='stop' but unparseable garbage: more room won't help -> exactly one call
        b = FakeBackend(floor=0, reply="not json at all", finish="stop")
        r = L.complete_structured(b, "p", {"type": "object"}, max_tokens=800, temperature=0)
        assert r.data is None
        assert len(b.calls) == 1, f"a non-length failure must not be retried: {b.calls}"


def test_hard_cap_is_respected():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        L._TOKEN_HARD_MAX = 3200
        b = FakeBackend(floor=99999)  # never satisfiable
        r = L.complete_structured(b, "p", {"type": "object"}, max_tokens=800, temperature=0)
        assert r.data is None
        assert b.calls == [800, 1600, 3200], f"must stop at the hard cap: {b.calls}"
        assert max(b.calls) <= 3200


def test_disabled_does_not_escalate():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        L._ADAPTIVE = False
        b = FakeBackend(floor=99999)
        r = L.complete_structured(b, "p", {"type": "object"}, max_tokens=800, temperature=0)
        assert r.data is None
        assert b.calls == [800], f"with adaptation off there is exactly one attempt: {b.calls}"


def test_budget_exhausted_stops_escalation():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        import brain.budget as B

        class _Exhausted:
            def exceeded(self, role):
                return True

        orig = B.tracker
        B.tracker = lambda: _Exhausted()
        try:
            b = FakeBackend(floor=99999)
            r = L.complete_structured(b, "p", {"type": "object"}, max_tokens=800, temperature=0, role="plan")
            assert r.data is None
            assert b.calls == [800], f"an exhausted role budget must block escalation: {b.calls}"
        finally:
            B.tracker = orig


if __name__ == "__main__":
    for fn in [test_escalates_sums_and_persists, test_second_run_starts_from_learned_ceiling,
               test_no_retry_on_genuine_bad_reply, test_hard_cap_is_respected,
               test_disabled_does_not_escalate, test_budget_exhausted_stops_escalation]:
        fn()
        print(f"  {fn.__name__}: PASS")
    print("adaptive-budget offline: ALL PASS")
