"""Offline M5-2 tests — set-of-marks visual heal Tier-7 (mocked vision LLM; no real Anthropic call).

Run:  .venv/bin/python tests/test_m5_offline.py
Verifies the Tier-7 wiring (gated by use_visual), mark->real-locator mapping, and the FLAGGED band.
The actual Sonnet-vision PoC (>=70% accuracy) is gated and run by the user with a key.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.store import LocalStore     # noqa: E402
from brain.healing import HealingEngine  # noqa: E402


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]


class FakeLLM:
    """Minimal stand-in for anthropic.Anthropic(); returns a fixed reply."""
    def __init__(self, reply):
        self._reply = reply
        self.messages = self

    def create(self, **_kw):
        return _Msg(self._reply)


class FakeEx:
    def __init__(self):
        self.marks = [
            {"mark": 0, "role": "button", "name": "Launch", "testid": "cta", "bbox": {}},
            {"mark": 1, "role": "button", "name": "Other", "testid": None, "bbox": {}},
        ]

    def call(self, m, **p):
        if m == "browser.probe":
            loc = p["locator"]
            ok = loc.get("testid") == "cta" or (loc.get("role") == "button" and loc.get("name") == "Launch")
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
    he._llm = FakeLLM('{"mark": 0}')          # inject a fake vision LLM
    r = he.heal(_ctx())
    assert r["strategy"] == "visual", r
    assert r["locator"] == {"testid": "cta"}, r   # mark -> REAL locator, not coordinates
    assert r["outcome"] == "flagged" and r["confidence"] == 0.80, r  # visual lands in FLAGGED band


def test_tier7_gated_off_by_default():
    st, ex = _store(), FakeEx()
    he = HealingEngine(ex, st, "r", use_llm=False, use_visual=False)
    he._llm = FakeLLM('{"mark": 0}')
    r = he.heal(_ctx())
    assert r["outcome"] == "failed", r            # visual disabled -> no heal


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
