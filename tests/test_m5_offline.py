"""Offline M5-2 / M6 tests — set-of-marks visual heal Tier-7 via a provider-agnostic backend.

Run:  .venv/bin/python tests/test_m5_offline.py
Verifies: Tier-7 wiring is gated by BOTH `use_visual` AND `backend.supports_vision`; the chosen
mark maps to a REAL locator (not coordinates); the FLAGGED band; and text re-grounding flowing
through `backend.complete`. No real provider call — a FakeBackend returns canned JSON.
The real Sonnet-vision PoC (>=70% accuracy) is gated and run by the user with a key.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.store import LocalStore        # noqa: E402
from brain.healing import HealingEngine   # noqa: E402
from brain.llm import LLMResult           # noqa: E402


class FakeBackend:
    """Provider-agnostic stand-in (replaces the old Anthropic-shaped FakeLLM). Records calls."""

    def __init__(self, reply, *, supports_vision=False, model="fake-model", name="fake"):
        self.reply, self.supports_vision = reply, supports_vision
        self.model, self.name = model, name
        self.calls = []

    def complete(self, prompt, *, max_tokens, temperature):
        self.calls.append(("complete", max_tokens, temperature))
        return LLMResult(self.reply, 3, 5)

    def complete_vision(self, prompt, image_b64, *, max_tokens, temperature):
        self.calls.append(("complete_vision", max_tokens, temperature))
        return LLMResult(self.reply, 3, 5)


class FakeEx:
    def __init__(self, extra_ok=None):
        self.extra_ok = extra_ok or {}   # an extra locator dict that should resolve to 1
        self.marks = [
            {"mark": 0, "role": "button", "name": "Launch", "testid": "cta", "bbox": {}},
            {"mark": 1, "role": "button", "name": "Other", "testid": None, "bbox": {}},
        ]

    def call(self, m, **p):
        if m == "browser.probe":
            loc = p["locator"]
            ok = loc.get("testid") == "cta" or (loc.get("role") == "button" and loc.get("name") == "Launch")
            if not ok and loc == self.extra_ok:
                ok = True
            return {"count": 1 if ok else 0}
        if m == "browser.setOfMarks":
            with open(p["path"], "wb") as f:
                f.write(b"\x89PNG fake")
            return {"marks": self.marks, "path": p["path"]}
        return {}


def _store():
    return LocalStore(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)


def _ctx():
    return {"step": 1, "semantic_id": "sid", "page_path": "p", "dom_hash": "h",
            "intent": "click 'Get started'",
            "attempted_locator": {"role": "button", "name": "Get started"},
            "alternatives": [], "interactives": []}  # L1-L6 all miss -> escalate


def test_tier7_visual_heal_picks_mark_to_real_locator():
    st, ex = _store(), FakeEx()
    he = HealingEngine(ex, st, "r", use_llm=False, use_visual=True)
    he._backend = FakeBackend('{"mark": 0}', supports_vision=True)   # vision-capable provider
    r = he.heal(_ctx())
    assert r["strategy"] == "visual", r
    assert r["locator"] == {"testid": "cta"}, r          # mark -> REAL locator, not coordinates
    assert r["outcome"] == "flagged" and r["confidence"] == 0.80, r  # visual lands in FLAGGED band
    assert ("complete_vision", 100, 0) in he._backend.calls, he._backend.calls


def test_tier7_skipped_when_backend_has_no_vision():
    st, ex = _store(), FakeEx()
    he = HealingEngine(ex, st, "r", use_llm=False, use_visual=True)
    he._backend = FakeBackend('{"mark": 0}', supports_vision=False)  # text-only provider
    r = he.heal(_ctx())
    assert r["outcome"] == "failed", r                   # vision gated off by capability
    assert all(c[0] != "complete_vision" for c in he._backend.calls), he._backend.calls


def test_tier7_gated_off_by_use_visual_false():
    st, ex = _store(), FakeEx()
    he = HealingEngine(ex, st, "r", use_llm=False, use_visual=False)
    he._backend = FakeBackend('{"mark": 0}', supports_vision=True)
    r = he.heal(_ctx())
    assert r["outcome"] == "failed", r                   # visual disabled regardless of capability
    assert all(c[0] != "complete_vision" for c in he._backend.calls), he._backend.calls


def test_text_reground_flows_through_backend():
    css_loc = {"css": "#login"}
    st, ex = _store(), FakeEx(extra_ok=css_loc)          # make the css selector resolve to 1
    he = HealingEngine(ex, st, "r", use_llm=False, use_visual=False)
    he._backend = FakeBackend('{"css": "#login"}', supports_vision=False)
    r = he.heal(_ctx())
    assert ("complete", 200, 0) in he._backend.calls, he._backend.calls
    # css prior 0.65 * 0.90 = 0.585 < FLAG 0.60 -> needs_review (locator omitted by the gate)
    assert r["outcome"] == "needs_review", r
    assert round(r["confidence"], 3) == 0.585, r


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
