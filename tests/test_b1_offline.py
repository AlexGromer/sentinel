"""Offline M6 tests — provider-agnostic LLM backend (no network, no real provider).

Run:  .venv/bin/python tests/test_b1_offline.py
Proves: the default (no-env) path stays the deterministic heuristic; a generic backend drives the
planner; fenced JSON survives; backend errors fall back; make_backend() yields None when
unconfigured (so the offline fallback is intact); set_llm_tokens reads normalized counts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.llm import LLMResult, make_backend       # noqa: E402
from brain.planner import LLMPlanner, HeuristicPlanner  # noqa: E402
from brain.otel import set_llm_tokens                # noqa: E402


class FakeBackend:
    """Provider-agnostic stand-in. Records calls; returns a canned reply."""

    def __init__(self, reply, *, supports_vision=False, model="fake-model", name="fake",
                 pt=3, ct=5, raises=False):
        self.reply, self.supports_vision = reply, supports_vision
        self.model, self.name = model, name
        self._pt, self._ct, self._raises = pt, ct, raises
        self.calls = []

    def complete(self, prompt, *, max_tokens, temperature):
        self.calls.append(("complete", max_tokens, temperature))
        if self._raises:
            raise RuntimeError("boom")
        return LLMResult(self.reply, self._pt, self._ct)

    def complete_vision(self, prompt, image_b64, *, max_tokens, temperature):
        self.calls.append(("complete_vision", max_tokens, temperature))
        if self._raises:
            raise RuntimeError("boom")
        return LLMResult(self.reply, self._pt, self._ct)


def _clear_env():
    for k in list(os.environ):
        if k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") or k.startswith("LLM_"):
            os.environ.pop(k, None)


def _state():
    return {"current_url": "u", "coverage_achieved": 0.0, "coverage_target": 0.85}


def test_planner_default_path_is_heuristic():
    _clear_env()
    p = LLMPlanner()
    assert p._backend is None, "no env/key -> no backend"
    assert p.name == "llm"
    assert p.model == "claude-opus-4-8"           # historical label preserved
    cands = [{"kind": "click", "role": "button", "name": "A", "target": None},
             {"kind": "navigate", "role": None, "name": None, "target": "/x"}]
    assert p.propose(_state(), cands) == HeuristicPlanner().propose(_state(), cands)


def test_planner_generic_backend_picks_index():
    _clear_env()
    fb = FakeBackend('{"index": 0}')
    p = LLMPlanner(backend=fb)
    assert p.model == "fake-model"                # model reflects the configured backend
    cands = [{"kind": "click", "role": "button", "name": "A", "target": None},
             {"kind": "click", "role": "button", "name": "B", "target": None}]
    d = p.propose(_state(), cands)
    assert d["action"] is cands[0], d
    assert d["tokens"] == {"prompt": 3, "completion": 5}, d
    assert fb.calls[0] == ("complete", 200, 0), fb.calls   # max_tokens/temperature preserved


def test_planner_done_reply_stops():
    _clear_env()
    p = LLMPlanner(backend=FakeBackend('{"done": true}'))
    cands = [{"kind": "click", "role": "b", "name": "n", "target": None}]
    d = p.propose(_state(), cands)
    assert d["done"] is True and d["action"] is None, d


def test_planner_fenced_json_survives():
    _clear_env()
    fb = FakeBackend('```json\n{"index": 1}\n```')   # markdown-fenced — common with OpenAI-compat
    p = LLMPlanner(backend=fb)
    cands = [{"kind": "click", "role": "b", "name": "A", "target": None},
             {"kind": "click", "role": "b", "name": "B", "target": None}]
    assert p.propose(_state(), cands)["action"] is cands[1]


def test_planner_backend_error_falls_back_to_heuristic():
    _clear_env()
    p = LLMPlanner(backend=FakeBackend("x", raises=True))
    cands = [{"kind": "click", "role": "b", "name": "A", "target": None}]
    d = p.propose(_state(), cands)
    assert d["action"] is cands[0] and d["tokens"] is None, d   # heuristic result


def test_make_backend_unconfigured_returns_none():
    _clear_env()
    assert make_backend("planner") is None
    assert make_backend("heal") is None


def test_make_backend_openai_without_key_or_base_url_is_none():
    _clear_env()
    os.environ["LLM_BACKEND"] = "openai"
    os.environ["LLM_MODEL"] = "gpt-4o-mini"
    assert make_backend("planner") is None        # needs a key or base_url -> fallback
    _clear_env()


def test_set_llm_tokens_normalized_and_none_safe():
    class _Span:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, k, v):
            self.attrs[k] = v

    sp = _Span()
    set_llm_tokens(sp, LLMResult("t", 7, 11))
    assert sp.attrs == {"llm.prompt_tokens": 7, "llm.completion_tokens": 11}, sp.attrs
    set_llm_tokens(None, object())                # tolerant of a None span (M4b contract)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
