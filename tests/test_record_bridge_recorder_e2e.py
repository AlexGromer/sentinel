"""Ties the MV3 recorder's ACTUAL output (#44) to the record->scenario bridge (#46) — offline, no browser.

test_record_bridge_offline.py grounds hand-authored events; this one uses the transcript the REAL
content-script produced in Chromium against extension/test/e2e/login-fixture.html. It proves the
recorder and the bridge agree in practice: the emitted event shape grounds into real selectors, the
password redaction survives end-to-end, and the scenario replays exit 0.

⚠ THE TRANSCRIPT USED TO BE A LITERAL IN THIS FILE, under the comment "Verbatim from
extension/test/e2e/recorder.e2e.mjs". It was not verbatim and never had been: both files have exactly
ONE commit (a415a80) and no diff since, so the list was hand-authored at birth and only claimed
provenance. Measured 2026-08-30 and again 2026-09-01: it held 5 events including an `input` with the
partial value "us", while the live recorder emits NO `input` at all. The test was green throughout —
it verified that the bridge understands what the recorder ONCE emitted, and could not notice that the
recorder emits something else. A hand-written transcript cannot go red for any recorder change, which
is the entire defect (ADR-143).

It is now an ARTEFACT: `extension/test/e2e/recorder.e2e.mjs` writes `recorded-transcript.json` on
every run, CI runs that e2e and fails if the committed file moved (`git diff --exit-code`), and this
file reads it. So the transcript can only ever say what the recorder actually produced.

⚠ AND THE HARNESS STOPPED FAKING `change`. It used to `page.dispatchEvent(…,'change')` after each
fill, so the document saw TWO change events per field — ours (isTrusted:false) and then Chromium's own
— and the frozen literal inherited that doubling, presenting a property of the TEST as a property of
the product. Measured after removing it: 6 events -> 4, with every e2e assertion still holding.

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

_TRANSCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "extension", "test", "e2e", "recorded-transcript.json")

with open(_TRANSCRIPT, encoding="utf-8") as _fh:
    RECORDED = json.load(_fh)

# Пол на число событий. Обязательный спутник чтения из файла: пустой (или усечённый) артефакт
# прошёл бы все утверждения ниже вакуумно — `build_scenario([])` не падает, а `_by(...)` вернул бы
# пустые списки, и половина проверок стала бы утверждениями о пустом множестве.
assert len(RECORDED) >= 4, (
    f"{_TRANSCRIPT}: {len(RECORDED)} events, floor is 4 (measured 2026-09-01) — "
    "the transcript is truncated and every assertion below would pass over nothing")
assert not any(e.get("type") == "input" for e in RECORDED), (
    "the transcript carries an `input` event; the recorder does not emit those, so this artefact "
    "was not produced by a real run")
assert "hunter2" not in json.dumps(RECORDED), "the transcript carries the typed password"


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

    # the Email `change` becomes ONE fill carrying the committed value; primary locator = role_name.
    # ⚠ This used to read "input burst + change … collapse into ONE fill", describing a collapse the
    # recorder performs but this transcript never exercised: the live recorder emits no `input` at
    # all. The collapse itself is covered by test_record_bridge_offline.py on authored events.
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
