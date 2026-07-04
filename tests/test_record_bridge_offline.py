"""Offline tests for the record->scenario bridge (#46, M9.8 §3) — no browser, no network, no LLM.

Proves brain.record_bridge turns a recorded events.ndjson stream into an M9.2b scenario that:
- binds every step to a REAL recorded selector (no fabrication), reusing scenario.ground_scenario;
- collapses live-typing input bursts into one fill, synthesizes cross-page navigates, drops submit;
- replays to exit 0 against a FakeExecutor (the unchanged-fixture acceptance, offline equivalent).

Run:  uv run pytest tests/test_record_bridge_offline.py   (or .venv/bin/python tests/test_record_bridge_offline.py)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.healing import HealingEngine                                  # noqa: E402
from brain.record_bridge import build_scenario, load_events, write_scenario  # noqa: E402
from brain.replay import run_replay                                      # noqa: E402
from brain.store import Store                                            # noqa: E402

LOGIN = "file:///s/login.html"
HOME = "file:///s/home.html"

# A recorded flow: type into User (two input bursts → collapse), pick a Role (select), click Sign in
# (ranked candidates), a form submit (dropped — the click already captures it), then a cross-page click.
# The trailing click has no usable selector → must be skipped, never fabricated.
EVENTS = [
    {"type": "input", "url": LOGIN, "selectorCandidates": [{"strategy": "label", "locator": {"label": "User"}}], "value": "ali"},
    {"type": "input", "url": LOGIN, "selectorCandidates": [{"strategy": "label", "locator": {"label": "User"}}], "value": "alice"},
    {"type": "change", "url": LOGIN, "verb": "select",
     "selectorCandidates": [{"strategy": "role_name", "locator": {"role": "combobox", "name": "Role"}}], "value": "admin"},
    {"type": "click", "url": LOGIN, "selectorCandidates": [
        {"strategy": "role_name", "locator": {"role": "button", "name": "Sign in"}},
        {"strategy": "css", "locator": {"css": "button.submit"}}]},
    {"type": "submit", "url": LOGIN, "selectorCandidates": [{"strategy": "css", "locator": {"css": "form#login"}}]},
    {"type": "click", "url": HOME, "selectorCandidates": [{"strategy": "testid", "locator": {"testid": "profile"}}]},
    {"type": "click", "url": HOME, "selectorCandidates": []},
]


class FakeEx:
    """Minimal pw-executor stand-in: every locator resolves (probe=1), every verb succeeds."""

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
        if m == "browser.select":
            return {"selected": [p.get("value")]}
        if m == "browser.expect":
            return {"ok": True}
        return {"ok": True}


def _verbs(steps, kind):
    return [s for s in steps if s.get("action_type") == kind]


def test_events_ground_to_real_selectors_no_fabrication():
    scenario, unmatched = build_scenario(EVENTS, session="abc")
    steps = scenario["steps"]
    assert unmatched == [], unmatched
    assert scenario["plan_id"] == "record-abc"
    assert scenario["target_url"] == LOGIN

    # leading navigate synthesized from the first event's real URL
    assert steps[0]["action_type"] == "navigate" and steps[0]["target"] == LOGIN, steps[0]

    # typing burst collapsed to a single fill carrying the FINAL value
    fills = _verbs(steps, "fill")
    assert len(fills) == 1 and fills[0]["locator"] == {"label": "User"} and fills[0]["value"] == "alice", fills

    # explicit select verb honoured
    selects = _verbs(steps, "select")
    assert len(selects) == 1 and selects[0]["value"] == "admin", selects

    # ranked candidates: primary is the click locator, the css candidate becomes a heal alternative
    clicks = _verbs(steps, "click")
    signin = [c for c in clicks if c["locator"] == {"role": "button", "name": "Sign in"}]
    assert signin, clicks
    assert {"css": "button.submit"} in [a["locator"] for a in signin[0]["alternatives"]], signin[0]

    # cross-page navigate synthesized; the home-page click with NO selector is dropped (not fabricated)
    navs = _verbs(steps, "navigate")
    assert [n["target"] for n in navs] == [LOGIN, HOME], navs
    home_clicks = [c for c in clicks if c["locator"] == {"testid": "profile"}]
    assert len(home_clicks) == 1, clicks
    assert len(clicks) == 2, "submit dropped + empty-selector click skipped -> only 2 clicks"

    # NO fabricated selectors: every verb step's locator is one the recorder actually emitted.
    recorded = [{"label": "User"}, {"role": "combobox", "name": "Role"},
                {"role": "button", "name": "Sign in"}, {"css": "button.submit"},
                {"css": "form#login"}, {"testid": "profile"}]
    for s in steps:
        if s["action_type"] != "navigate":
            assert s["locator"] in recorded, f"fabricated locator in step: {s}"


def test_scenario_replays_exit_zero():
    scenario, _ = build_scenario(EVENTS)
    ex = FakeEx()
    store = Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)
    heal = HealingEngine(ex, store, "r", use_llm=False)
    r = run_replay(ex, store, heal, scenario, scenario["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 0, r
    assert r["failed"] == 0, r


def test_load_events_tolerates_blank_and_garbage_lines():
    p = os.path.join(tempfile.mkdtemp(), "events.ndjson")
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(EVENTS[0]) + "\n\n")
        f.write("{not json\n")                     # a partial/garbage line is skipped, not fatal
        f.write(json.dumps(EVENTS[3]) + "\n")
    events = load_events(p)
    assert len(events) == 2, events


def test_write_scenario_roundtrips_to_replayable_file():
    d = tempfile.mkdtemp()
    src = os.path.join(d, "events.ndjson")
    with open(src, "w", encoding="utf-8") as f:
        for ev in EVENTS:
            f.write(json.dumps(ev) + "\n")
    out = os.path.join(d, "scenario.json")
    scenario, unmatched = write_scenario(src, out, session="zz")
    on_disk = json.load(open(out, encoding="utf-8"))
    assert on_disk == scenario and unmatched == []
    assert on_disk["plan_hash"] and on_disk["steps"][0]["action_type"] == "navigate"


def test_press_without_key_is_dropped_with_key_kept():
    # A press whose key never resolved must be DROPPED — emitting key=None would bake into the step
    # and replay's browser.press key=None fails. A press WITH a key is kept.
    events = [
        {"type": "click", "url": LOGIN, "verb": "press",
         "selectorCandidates": [{"strategy": "css", "locator": {"css": "#a"}}]},          # no key -> dropped
        {"type": "click", "url": LOGIN, "verb": "press", "key": "Enter",
         "selectorCandidates": [{"strategy": "css", "locator": {"css": "#b"}}]},          # key -> kept
    ]
    scenario, _ = build_scenario(events)
    presses = _verbs(scenario["steps"], "press")
    assert len(presses) == 1, scenario["steps"]
    assert presses[0]["key"] == "Enter", presses[0]
    assert not any("key" in s and s["key"] is None for s in scenario["steps"]), "no key=None step may be emitted"


def test_assert_verb_passes_through_condition():
    events = [{"type": "click", "url": LOGIN, "verb": "assert", "condition": "visible", "expect_ok": False,
               "selectorCandidates": [{"strategy": "role_name", "locator": {"role": "alert", "name": "Err"}}]}]
    scenario, _ = build_scenario(events)
    asserts = _verbs(scenario["steps"], "assert")
    assert len(asserts) == 1, scenario["steps"]
    assert asserts[0]["condition"] == "visible" and asserts[0]["expect_ok"] is False, asserts[0]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
