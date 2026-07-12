"""Ties the MV3 recorder's ACTUAL output (#44) to the record->scenario bridge (#46) — offline, no browser.

test_record_bridge_offline.py grounds hand-authored events; this one uses a VERBATIM transcript captured
from the real content-script running in Chromium against extension/test/e2e/login-fixture.html (the live
e2e, extension/test/e2e/recorder.e2e.mjs). It proves the recorder and the bridge agree in practice: the
emitted event shape grounds into real selectors, the password redaction survives end-to-end, and the
scenario replays exit 0.

Run:  uv run pytest tests/test_record_bridge_recorder_e2e.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.healing import HealingEngine                       # noqa: E402
from brain.record_bridge import build_scenario               # noqa: E402
from brain.replay import run_replay                           # noqa: E402
from brain.store import Store                                 # noqa: E402

URL = "file:///s/login.html"

# Verbatim from extension/test/e2e/recorder.e2e.mjs against the login fixture: type email (input burst +
# change), type password (redacted -> secretRef, value NEVER captured), click Sign in (ranked candidates,
# testid first), submit the form. The literal password "hunter2-SECRET" the user typed is ABSENT by design.
RECORDED = [
    {"type": "input", "url": URL, "value": "us", "selectorCandidates": [
        {"strategy": "role_name", "locator": {"role": "textbox", "name": "Email"}},
        {"strategy": "label", "locator": {"label": "Email"}},
        {"strategy": "css", "locator": {"css": "#email"}},
        {"strategy": "xpath", "locator": {"xpath": "/html/body[1]/form[1]/input[1]"}}]},
    {"type": "change", "url": URL, "value": "user@example.test", "selectorCandidates": [
        {"strategy": "role_name", "locator": {"role": "textbox", "name": "Email"}},
        {"strategy": "label", "locator": {"label": "Email"}},
        {"strategy": "css", "locator": {"css": "#email"}},
        {"strategy": "xpath", "locator": {"xpath": "/html/body[1]/form[1]/input[1]"}}]},
    {"type": "change", "url": URL, "secretRef": "USER_PASSWORD", "selectorCandidates": [
        {"strategy": "role_name", "locator": {"role": "textbox", "name": "Password"}},
        {"strategy": "label", "locator": {"label": "Password"}},
        {"strategy": "css", "locator": {"css": "#password"}},
        {"strategy": "xpath", "locator": {"xpath": "/html/body[1]/form[1]/input[2]"}}]},
    {"type": "click", "url": URL, "selectorCandidates": [
        {"strategy": "testid", "locator": {"testid": "login-btn"}},
        {"strategy": "role_name", "locator": {"role": "button", "name": "Sign in"}},
        {"strategy": "text", "locator": {"text": "Sign in"}},
        {"strategy": "css", "locator": {"css": "#go"}},
        {"strategy": "xpath", "locator": {"xpath": "/html/body[1]/form[1]/button[1]"}}]},
    {"type": "submit", "url": URL, "selectorCandidates": [
        {"strategy": "css", "locator": {"css": "#f"}},
        {"strategy": "xpath", "locator": {"xpath": "/html/body[1]/form[1]"}}]},
]


class FakeEx:
    """Minimal pw-executor stand-in: every locator resolves, every verb succeeds."""

    def __init__(self):
        self.calls = []
        self.url = ""

    def call(self, m, **p):
        self.calls.append((m, p))
        if m == "browser.navigate":
            self.url = p.get("url", "")
            return {"url": self.url, "title": "", "status": 200}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": '- alert "ok"', "nodeCount": 1}
        if m == "browser.screenshotHash":
            return {"hash": "shot1"}
        if m == "browser.probe":
            return {"count": 1}
        return {"ok": True}


def _by(steps, kind):
    return [s for s in steps if s.get("action_type") == kind]


def test_recorder_output_grounds_to_real_selectors():
    scenario, unmatched = build_scenario(RECORDED, session="live")
    steps = scenario["steps"]
    assert unmatched == [], unmatched

    # leading navigate synthesized from the recorded page URL
    assert steps[0]["action_type"] == "navigate" and steps[0]["target"] == URL, steps[0]

    # input burst + change on Email collapse into ONE fill carrying the final value; primary = role_name
    fills = _by(steps, "fill")
    email = [f for f in fills if f.get("value") == "user@example.test"]
    assert len(email) == 1, fills
    assert email[0]["locator"] == {"role": "textbox", "name": "Email"}, email[0]
    # the lower-prior candidates become heal alternatives (no fabrication, real selectors only)
    alt_locs = [a["locator"] for a in email[0]["alternatives"]]
    assert {"css": "#email"} in alt_locs and {"label": "Email"} in alt_locs, email[0]

    # the button click's primary locator is the highest-prior candidate the recorder emitted: testid
    clicks = _by(steps, "click")
    assert len(clicks) == 1 and clicks[0]["locator"] == {"testid": "login-btn"}, clicks

    # submit is dropped (the button click already captures the intent) — verbs are navigate/fill/click only
    assert {s["action_type"] for s in steps} == {"navigate", "fill", "click"}, [s["action_type"] for s in steps]


def test_password_redaction_survives_the_bridge():
    scenario, _ = build_scenario(RECORDED)
    blob = json.dumps(scenario)
    # the literal password value never existed in the events and must never appear in the scenario
    assert "hunter2" not in blob

    pw = [s for s in scenario["steps"] if s.get("locator") == {"role": "textbox", "name": "Password"}]
    assert len(pw) == 1, scenario["steps"]
    assert pw[0]["action_type"] == "fill"
    assert pw[0].get("secretRef") == "USER_PASSWORD", pw[0]   # filled from an env ref, not a literal
    assert pw[0].get("value") in (None, ""), pw[0]            # no literal value carried


def test_select_and_enter_submit_ground_to_executable_verbs():
    # The recorder tags a <select> change with verb=select and an Enter-driven submit with verb=press
    # (key=Enter) — so they replay as selectOption / press, not a fill() Playwright would reject.
    events = [
        {"type": "change", "url": URL, "verb": "select", "value": "admin",
         "selectorCandidates": [{"strategy": "role_name", "locator": {"role": "combobox", "name": "Role"}}]},
        {"type": "submit", "url": URL, "verb": "press", "key": "Enter",
         "selectorCandidates": [{"strategy": "css", "locator": {"css": "#email"}}]},
    ]
    scenario, unmatched = build_scenario(events)
    assert unmatched == [], unmatched
    kinds = [s["action_type"] for s in scenario["steps"]]
    assert kinds == ["navigate", "select", "press"], kinds
    select_step = scenario["steps"][1]
    assert select_step["locator"] == {"role": "combobox", "name": "Role"} and select_step["value"] == "admin"
    press_step = scenario["steps"][2]
    assert press_step["locator"] == {"css": "#email"} and press_step["key"] == "Enter"


def test_recorded_scenario_replays_exit_zero():
    scenario, _ = build_scenario(RECORDED)
    ex = FakeEx()
    store = Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)
    heal = HealingEngine(ex, store, "r", use_llm=False)
    r = run_replay(ex, store, heal, scenario, scenario["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 0, r
    assert r["failed"] == 0, r


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
