"""Offline tests for prompt sanitization of AUT-derived strings (#37, THREAT_MODEL §6 rec #6).

No network / no real provider. Proves brain.sanitize strips control + format chars and length-caps
per field, AND that the planner/healing prompts actually constructed from a hostile candidate menu
carry neither raw control chars (raw-interpolated fields) nor their json-escaped form (menu fields),
and that an over-long element name is truncated rather than passed whole.

Run:  uv run pytest tests/test_sanitize_offline.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.healing import HealingEngine          # noqa: E402
from brain.llm import LLMResult                   # noqa: E402
from brain.planner import GoalPlanner, LLMPlanner  # noqa: E402
from brain.sanitize import MAX_FIELD, safe_json, safe_text  # noqa: E402


class CapturingBackend:
    """Records every prompt it is handed; returns a canned no-op reply."""

    def __init__(self, reply='{"done": true}', *, supports_vision=False):
        self.reply, self.supports_vision = reply, supports_vision
        self.model, self.name = "cap", "cap"
        self.prompts = []

    def complete(self, prompt, *, max_tokens, temperature):
        self.prompts.append(prompt)
        return LLMResult(self.reply, 1, 1)

    def complete_vision(self, prompt, image_b64, *, max_tokens, temperature):
        self.prompts.append(prompt)
        return LLMResult(self.reply, 1, 1)


# --- unit: safe_text / safe_json -------------------------------------------

def test_safe_text_strips_control_format_and_caps():
    payload = "ab\x00\x07cd‎‮ ef\n\t" + "X" * 5000
    out = safe_text(payload)
    for bad in ("\x00", "\x07", "‎", "‮"):
        assert bad not in out
    assert out.startswith("abcd ef X")        # printable kept, \n\t folded to one space
    assert len(out) <= MAX_FIELD + 1          # capped (+1 for the … marker)
    assert out.endswith("…")
    assert out.count("X") < 5000              # the tail was dropped


def test_safe_text_none_and_nonstr():
    assert safe_text(None) == ""
    assert safe_text(42) == "42"


def test_safe_json_recurses_and_preserves_nonstr():
    menu = [{"i": 0, "name": "ok\x00", "role": None, "nested": {"t": "a‮b"}}, "x\x07y", 7]
    out = safe_json(menu)
    assert out[0] == {"i": 0, "name": "ok", "role": None, "nested": {"t": "ab"}}
    assert out[1] == "xy"
    assert out[2] == 7


# --- integration: the constructed prompt is clean --------------------------

_HOSTILE_NAME = "Hi\x00there " + "Z" * 4000      # control char + over-long
_STATE = {"current_url": "http://h\x07ost/", "coverage_achieved": 0.0,
          "coverage_target": 0.85, "current_step": 0, "max_steps": 5}


def _assert_clean(prompt: str):
    # raw-interpolated field (current_url) — the literal control byte must be gone
    assert "\x07" not in prompt
    # menu field went through json.dumps(safe_json(...)) — neither the literal nor its \uXXXX escape
    assert "\x00" not in prompt and "\\u0000" not in prompt
    assert "‮" not in prompt and "\\u202e" not in prompt
    # over-long name truncated, but its capped prefix still reaches the model
    assert "Z" * 4000 not in prompt
    assert "Z" * 200 in prompt


def test_llm_planner_prompt_sanitized():
    cap = CapturingBackend()
    cands = [{"kind": "click", "role": "button‮", "name": _HOSTILE_NAME, "target": None}]
    LLMPlanner(backend=cap).propose(_STATE, cands)
    assert cap.prompts, "backend was not called"
    _assert_clean(cap.prompts[0])


def test_goal_planner_prompt_sanitized():
    cap = CapturingBackend()
    cands = [{"kind": "click", "role": "button‮", "name": _HOSTILE_NAME,
              "target": None, "intent": "do\x00 it"}]
    GoalPlanner(goal="pay", backend=cap).propose(_STATE, cands)
    assert cap.prompts, "backend was not called"
    _assert_clean(cap.prompts[0])
    assert "‮" not in cap.prompts[0]


def test_heal_reground_prompt_sanitized():
    cap = CapturingBackend(reply='{"none": true}')
    eng = HealingEngine(ex=None, store=None, run_id="t", use_llm=True, backend=cap)
    ctx = {"intent": "log in\x00 " + "Q" * 4000,
           "attempted_locator": {"css": "#a\x00"},
           "interactives": [{"role": "button", "name": "Go\x1bnow"}]}
    assert eng._llm_reground(ctx) is None        # canned {"none": true}
    prompt = cap.prompts[0]
    assert "\x00" not in prompt and "\x1b" not in prompt and "\\u0000" not in prompt
    assert "Q" * 4000 not in prompt              # over-long intent truncated
    assert "Q" * 200 in prompt


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
