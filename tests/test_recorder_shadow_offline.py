#!/usr/bin/env python3
"""PERCEPT-RECORDER-SHADOW — the MV3 recorder inside shadow DOM, end to end and offline.

WHY THIS FILE EXISTS AT ALL. Before it, a change to `extension/src/content/recorder.ts` could not
turn anything red:

  · the extension is named by no workflow, and `scripts/check_bilingual.py` excludes the directory
    outright, so nothing in CI compiled it, let alone ran it;
  · the one CI-visible recorder check, `tests/test_record_bridge_recorder_e2e.py`, replays a VERBATIM
    transcript frozen INSIDE the test file. It proves the bridge understands what the recorder once
    emitted; it cannot notice that the recorder now emits something else. A fix and a regression pass
    it identically;
  · the live Chromium proof, `extension/test/e2e/recorder.e2e.mjs`, does drive the real code, but it
    needs a full Chromium and a running control-api and is dev-only by design.

WHAT IS BEING PROVEN. The DOM RETARGETS `event.target` to the shadow HOST as an event leaves an open
shadow root, so a document-level listener that trusts `e.target` records `<x-color-picker>` where the
user clicked a button inside it. The executor's CSS and role engines pierce open roots and can drive
that button perfectly well — we simply had no way to write it down. `composedPath()` closes that.

HOW. The REAL recorder module is loaded into a jsdom page over `extension/test/e2e/shadow-fixture.html`
(driver: `extension/test/record-in-jsdom.mjs`, actions derived from the fixture's own `data-record`
attributes), and the events it actually emits are then ground through the REAL bridge,
`brain.record_bridge.build_scenario`. Nothing here asserts the SHAPE of any source file: a regex over
recorder.ts would agree with code that never runs, which is the failure mode this repository keeps
meeting in its own tests.

Offline: no network, no browser, no server. It does need the extension's dev dependencies (jsdom,
tsx) — and says so loudly rather than skipping, because a skip is green.

Run:  PYTHONPATH="$PWD" .venv/bin/python tests/test_recorder_shadow_offline.py
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain.record_bridge import build_scenario                # noqa: E402

EXT = os.path.join(ROOT, "extension")
FIXTURE = os.path.join("test", "e2e", "shadow-fixture.html")

# Floors, not current counts. A driver that stopped finding the fixture, or a recorder that stopped
# emitting, would otherwise leave every assertion below passing over an empty list.
MIN_CONTROLS = 9      # [data-record] elements the driver must find in the fixture
MIN_ACTIONS = 10      # actions performed (an element may declare several)
MIN_EVENTS = 10       # RecorderEvents the recorder emitted

_recorded = None


def recorded():
    """Run the real recorder over the fixture once; cache the result for the whole file."""
    global _recorded
    if _recorded is not None:
        return _recorded

    node = shutil.which("node")
    if not node:
        raise AssertionError("node is not on PATH — the recorder is TypeScript and cannot be run without it")
    if not os.path.isdir(os.path.join(EXT, "node_modules")):
        raise AssertionError(
            "extension/node_modules is missing, so the recorder cannot be loaded. This FAILS rather "
            "than skips on purpose: a skipped gate is indistinguishable from a passing one, and that "
            "is exactly how recorder.ts went ungated in the first place. Run: cd extension && npm ci")

    proc = subprocess.run(
        [node, "--import", "tsx", os.path.join("test", "record-in-jsdom.mjs"), FIXTURE,
         "--floor=%d" % MIN_CONTROLS],
        cwd=EXT, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError("the jsdom recorder driver exited %d\nstderr:\n%s\nstdout:\n%s"
                             % (proc.returncode, proc.stderr[-4000:], proc.stdout[-2000:]))
    try:
        _recorded = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError("the driver printed no parsable JSON (%s):\n%s" % (exc, proc.stdout[-2000:]))
    return _recorded


def events():
    return recorded()["events"]


def candidates(ev):
    return [(c["strategy"], c["locator"]) for c in ev["selectorCandidates"]]


def by_locator_value(evs, key, value):
    """Events carrying a candidate whose `key` equals `value` — the recorder's own answer, unranked."""
    return [e for e in evs if any(loc.get(key) == value for _, loc in candidates(e))]


def scenario():
    sc, unmatched = build_scenario(events(), session="shadow")
    assert unmatched == [], "the bridge could not ground: %r" % (unmatched,)
    return sc


def steps_by(kind):
    return [s for s in scenario()["steps"] if s.get("action_type") == kind]


# --- the floors -------------------------------------------------------------------------------

def test_the_driver_actually_drove_something():
    rec = recorded()
    assert len(rec["actions"]) >= MIN_ACTIONS, rec["actions"]
    assert len(rec["events"]) >= MIN_EVENTS, rec["events"]
    # The fixture's controls are spread over several trees. If the walk stopped descending into
    # shadow roots, only the light-DOM button would be driven and everything below would pass over
    # a single event — so the identities are asserted, not just the count.
    driven_ids = {a["id"] for a in rec["actions"]}
    assert {"plain", "swatch", "glyph", "alias", "mode", "token", "tgl", "lang-sel",
            "sealed-btn"} <= driven_ids, driven_ids


# --- the defect itself ------------------------------------------------------------------------

def test_a_click_inside_an_open_root_names_the_control_not_the_host():
    """THE regression. e.target for this click is <x-color-picker>; the user pressed the button."""
    clicks = [e for e in events() if e["type"] == "click"]
    swatch = by_locator_value(clicks, "testid", "swatch-red")
    assert len(swatch) == 1, "the button inside <x-color-picker> was not recorded: %r" % (clicks,)
    # and the host is NOT what got recorded for it
    assert not any(loc.get("css") == "#picker" for _, loc in candidates(swatch[0])), swatch[0]

    step = [s for s in steps_by("click") if s.get("locator") == {"testid": "swatch-red"}]
    assert len(step) == 1, "the grounded plan does not name the swatch: %r" % (steps_by("click"),)


def test_shadow_candidates_are_host_prefixed_css_and_carry_no_xpath():
    """A pierced css path must be unambiguous, and a document XPath for a shadow node must not exist.

    Both are measured against playwright-core rather than assumed (see selectors.ts): its CSS
    evaluator crosses a shadow host for `>` and for the descendant combinator, while its XPath engine
    is a bare document.evaluate with no shadow expansion — an xpath candidate here would be a locator
    guaranteed not to resolve, or to resolve to the wrong element.
    """
    swatch = by_locator_value(events(), "testid", "swatch-red")[0]
    css = [loc["css"] for s, loc in candidates(swatch) if s == "css"]
    assert css == ["#picker > #swatch"], css
    assert not [s for s, _ in candidates(swatch) if s == "xpath"], candidates(swatch)

    # the light-DOM control keeps BOTH — the shadow work changed nothing for an ordinary page
    plain = by_locator_value(events(), "testid", "plain-btn")[0]
    assert [loc["css"] for s, loc in candidates(plain) if s == "css"] == ["#plain"], candidates(plain)
    assert [loc["xpath"] for s, loc in candidates(plain) if s == "xpath"] == \
        ["/html/body[1]/button[1]"], candidates(plain)


def test_inert_internals_bind_to_the_host_that_carries_the_role():
    """<x-icon-button role=button aria-label> whose shadow content is a bare <span>.

    The naive fix — take composedPath()[0] and stop — records the span, which has no role and no name.
    Climbing out of the tree only when it offers nothing interactive keeps both cases right.
    """
    glyph = [e for e in events() if any(loc.get("css") == "#glyph" for _, loc in candidates(e))]
    assert glyph == [], "the inert <span> was recorded instead of its host: %r" % (glyph,)
    host = [e for e in events() if any(loc == {"role": "button", "name": "Delete row"}
                                       for _, loc in candidates(e))]
    assert len(host) == 1, "the icon button was not recorded by role+name: %r" % (events(),)


# --- events that never reach the document at all ----------------------------------------------

def test_non_composed_change_inside_a_component_is_captured():
    """`change` carries composed:false — it does not cross a shadow boundary in any browser.

    A document-level listener therefore never sees a <select> committed inside a component: the action
    is not mis-recorded, it is missing and silent. The recorder listens on the roots it has learned
    about from the composed events it did see.
    """
    select = [s for s in steps_by("select") if s.get("locator") == {"role": "combobox", "name": "Mode"}]
    assert len(select) == 1, "the shadow-hosted <select> produced no select step: %r" % (scenario()["steps"],)
    assert select[0].get("value") == "admin", select[0]


def test_a_component_entered_only_by_keyboard_is_still_recorded():
    """<x-lang> is never clicked and never typed into — a Tab and the arrow keys, which is how a
    keyboard user drives a <select>. `focus` is not composed, `focusin` is; without discovering the
    root from focusin, this component's `change` never reaches anything and the action is lost with
    no error anywhere."""
    lang = [s for s in steps_by("select") if s.get("locator") == {"role": "combobox", "name": "Language"}]
    assert len(lang) == 1, "the keyboard-only component produced no step: %r" % (scenario()["steps"],)
    assert lang[0].get("value") == "ru", lang[0]


def test_enter_submit_inside_a_component_presses_the_field_not_the_component():
    """document.activeElement is the HOST for anything focused inside a root — same retargeting."""
    press = steps_by("press")
    assert len(press) == 1, press
    assert press[0].get("key") == "Enter", press[0]
    assert press[0]["locator"] == {"role": "textbox", "name": "Alias"}, press[0]


def test_a_label_inside_the_root_is_found_there_and_not_in_the_document():
    """<label for=alias> lives in the component. Ids are scoped per tree: a document-scoped lookup
    finds nothing here — and, on a page that reuses the id, finds the WRONG element and names the
    candidate after it."""
    alias = [e for e in events() if any(loc.get("css") == "#settings > #alias"
                                        for _, loc in candidates(e))]
    assert alias, events()
    for ev in alias:
        assert ("label", {"label": "Alias"}) in candidates(ev), candidates(ev)
        assert ("role_name", {"role": "textbox", "name": "Alias"}) in candidates(ev), candidates(ev)


def test_a_component_republishing_a_composed_change_is_recorded_once():
    """A web component that re-dispatches a composed `change` from an inner node is seen by BOTH the
    root's listener and the document's. Asserted on the raw events, because the bridge collapses
    consecutive same-element fills and would hide the duplicate at the scenario level."""
    dup = [e for e in events()
           if e["type"] == "change" and any(loc.get("css") == "#toggle > #tgl" for _, loc in candidates(e))]
    assert len(dup) == 1, "the republished change was recorded %d times: %r" % (len(dup), dup)


# --- boundaries and security ------------------------------------------------------------------

def test_a_closed_root_degrades_to_its_host_without_fabricating():
    """A closed root is a boundary, not a debt: its nodes are absent from composedPath and
    host.shadowRoot is null. Recording the host is the only honest answer — and it must still be a
    recorded action, not a crash and not an invented selector for something we never saw."""
    sealed = [e for e in events() if any(loc.get("css") == "#sealed" for _, loc in candidates(e))]
    assert len(sealed) == 1, "the click into the closed root produced no event: %r" % (events(),)
    assert sealed[0]["type"] == "click", sealed[0]
    # nothing from inside it may appear anywhere
    assert "sealed-btn" not in json.dumps(events()), sealed


def test_redaction_still_holds_inside_a_component():
    """The password field is in a shadow root, i.e. on the path this PR changed. Redaction is the
    hardest acceptance criterion the recorder has (#44) and must not have been routed around."""
    blob = json.dumps(scenario())
    assert "NEVER-RECORD-THIS-VALUE" not in blob, "the typed secret reached the scenario"
    assert "NEVER-RECORD-THIS-VALUE" not in json.dumps(events()), "the typed secret reached the events"
    token = [s for s in steps_by("fill") if s.get("locator") == {"role": "textbox", "name": "Token"}]
    assert len(token) == 1, scenario()["steps"]
    assert token[0].get("secretRef") == "API_KEY", token[0]
    assert token[0].get("value") in (None, ""), token[0]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
