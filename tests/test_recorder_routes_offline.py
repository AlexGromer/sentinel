#!/usr/bin/env python3
"""ADR-138 — the recorder writes down a route change, and the bridge stops guessing at one.

WHY THIS FILE EXISTS. Until now nothing in the repository could go red for the recorder's route
behaviour, because the recorder had none: `RecorderEventType` was `click|input|change|submit` and
there was no line shape for "the address changed". The registry entry blamed a re-injection filter
in the service worker; measured on Chrome 151 and Chromium 150, that diagnosis was wrong twice over
(pushState DOES reach `status: 'complete'`, and the content script survives pushState anyway). What
was actually missing is asserted here.

THREE DEFECTS ARE UNDER TEST, all measured before a line was written:

  A-1  A route change with no action after it was represented by NOTHING. The transition was only
       ever inferred from the NEXT event's url, and a last action has no next event. Conversely a
       transition a click had already performed was synthesized AGAIN, and that second performance
       silently repaired broken routing: a click landing on `#/error` instead of `#/b` replayed
       green because the following navigate put the address back.
  A-2  That synthesized navigate is a hard `goto`. On a path-routed SPA it replaces the document and
       wipes the application's in-memory state; with no server history fallback it answers 404, which
       `replay.py` never looked at — the step was recorded `ok` and the failure surfaced a step later,
       on a locator, blamed on the application.
  A-3  A route living in the QUERY string was invisible end to end: `page_identity` drops the query,
       so two query routes produced no navigate at all and the scenario replayed one screen. This
       file covers that half of A-3 which needs no key migration; the other half (two same-named
       controls on different query routes collapsing to one `semantic_id`) is registered as
       [PAGE-IDENTITY-DROPS-QUERY], because moving that key moves every saved plan_hash.

HOW. The REAL journal (`extension/src/content/route-journal.ts`) and the REAL recorder are loaded
into a jsdom page over `extension/test/e2e/route-fixture.html` by the REAL driver
(`extension/test/record-in-jsdom.mjs`), and the events they actually emit are ground through the REAL
bridge, `brain.record_bridge.build_scenario`. Nothing here asserts the SHAPE of a source file: a
regex over route-journal.ts would agree with code that never runs, which is the failure mode this
repository keeps meeting in its own tests.

⚠ WHAT THIS FILE CANNOT PROVE, said out loud rather than implied. jsdom has ONE world. The whole
reason the journal is injected with `world: 'MAIN'` is that a `history.pushState` patched from the
ISOLATED world never sees the page's own call — measured in Chromium, and unobservable here. The
guard against that is a runtime one (`main: false` raises a recorder-warning the panel shows), not a
gate. Also compensated in the driver, and only this: jsdom delivers `postMessage` with
`event.source === null` where Chromium delivers `=== window`, so the driver restores that one field.

Offline: no network, no browser, no server. It needs the extension's dev dependencies (jsdom, tsx) —
and says so loudly rather than skipping, because a skip is green.

Run:  PYTHONPATH="$PWD" .venv/bin/python tests/test_recorder_routes_offline.py
"""
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain.record_bridge import build_scenario                          # noqa: E402
from brain.scenario import ground_scenario                              # noqa: E402
from brain.exporter import export_spec                                  # noqa: E402

EXT = os.path.join(ROOT, "extension")
FIXTURE_REL = os.path.join("test", "e2e", "route-fixture.html")
FIXTURE_ABS = os.path.join(EXT, FIXTURE_REL)
PAGE_URL = "https://spa.test/app"
SECRET = "SHOULD-NOT-APPEAR-IN-ANY-ARTIFACT"

# Floors, not current counts. A driver that stopped finding the fixture, or a journal that stopped
# reporting, would otherwise leave every assertion below passing over an empty list.
MIN_CONTROLS = 8      # [data-record] elements the driver must find
MIN_ROUTES = 6        # declared [data-route] values
MIN_EVENTS = 12       # RecorderLines emitted (clicks + routes)

_recorded = None


def recorded():
    """Run the real journal + recorder over the fixture once; cache for the whole file."""
    global _recorded
    if _recorded is not None:
        return _recorded
    node = shutil.which("node")
    if not node:
        raise AssertionError("node is not on PATH — the recorder is TypeScript and cannot be run without it")
    if not os.path.isdir(os.path.join(EXT, "node_modules")):
        raise AssertionError(
            "extension/node_modules is missing, so the recorder cannot be loaded. This FAILS rather "
            "than skips on purpose: a skipped gate is indistinguishable from a passing one. "
            "Run: cd extension && npm ci")
    proc = subprocess.run(
        [node, "--import", "tsx", os.path.join("test", "record-in-jsdom.mjs"), FIXTURE_REL, PAGE_URL,
         "--floor=%d" % MIN_CONTROLS],
        cwd=EXT, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError("the jsdom driver exited %d\nstderr:\n%s\nstdout:\n%s"
                             % (proc.returncode, proc.stderr[-4000:], proc.stdout[-2000:]))
    try:
        _recorded = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError("the driver printed no parsable JSON (%s):\n%s" % (exc, proc.stdout[-2000:]))
    return _recorded


def events():
    return recorded()["events"]


def routes():
    return [e for e in events() if e.get("type") == "route"]


def declared():
    """The routes the FIXTURE declares it will produce — derived from the markup, never maintained
    here. A control added to the fixture is expected because it is in the fixture."""
    src = open(FIXTURE_ABS, encoding="utf-8").read()
    return re.findall(r'data-route="([^"]+)"', src)


def scenario():
    sc, unmatched = build_scenario(events(), session="routes", target_url=PAGE_URL)
    assert unmatched == [], "the bridge could not ground: %r" % (unmatched,)
    return sc


def steps_by(kind):
    return [s for s in scenario()["steps"] if s.get("action_type") == kind]


# --- the floors -------------------------------------------------------------------------------

def test_the_driver_and_the_journal_both_actually_ran():
    rec = recorded()
    assert len(rec["actions"]) >= MIN_CONTROLS, rec["actions"]
    assert len(rec["events"]) >= MIN_EVENTS, rec["events"]
    assert len(declared()) >= MIN_ROUTES, declared()
    driven = {a["id"] for a in rec["actions"]}
    assert {"plain", "to-b", "to-orders", "secret", "redirect", "deferred", "forge", "last"} <= driven, driven


# --- what the journal saw ----------------------------------------------------------------------

def test_every_declared_route_arrived_and_no_others_were_invented():
    """EXACT equality, not `>=`. A journal that reported a route per click would satisfy every
    ordering assertion below; `#plain` declares no route, so only exactness catches that."""
    got, want = routes(), declared()
    assert len(got) == len(want), (
        "declared %d route(s) in the fixture but the journal reported %d:\n  declared=%r\n  got=%r"
        % (len(want), len(got), want, [r["url"] for r in got]))


def test_the_routes_arrived_in_the_order_the_fixture_declares_them():
    for i, (want, got) in enumerate(zip(declared(), routes())):
        assert want in got["url"], "route #%d: expected %r inside %r" % (i + 1, want, got["url"])


def test_replace_state_is_told_apart_from_push_state():
    """`how` is not decoration: an unattributed `pop` is the one case replay has to reproduce by
    itself, and telling it apart from a self-redirect is what keeps a navigate out of the others."""
    hows = {r["url"]: r["how"] for r in routes()}
    redirected = [u for u in hows if "#/redirected" in u]
    assert redirected and hows[redirected[0]] == "replace", hows
    pushed = [u for u in hows if "#/b" in u]
    assert pushed and hows[pushed[0]] == "push", hows


def test_a_route_that_lands_in_a_promise_is_still_seen_and_still_in_order():
    """The discriminator between this mechanism and the cheap alternative. Reading `location.href` a
    tick after the handler does not see a route the router resolves in a promise — measured in
    pw-executor, which needed `waitForURL` with a budget for exactly this."""
    late = [r for r in routes() if "#/late" in r["url"]]
    assert len(late) == 1, [r["url"] for r in routes()]
    click_before = [e for e in events() if e["seq"] < late[0]["seq"] and e.get("type") == "click"]
    assert click_before, events()
    after = [r for r in routes() if r["seq"] > late[0]["seq"]]
    assert all("#/done" in r["url"] for r in after), [r["url"] for r in after]


def test_the_wire_order_is_checkable_rather_than_assumed():
    seqs = [e.get("seq") for e in events()]
    assert all(isinstance(s, int) for s in seqs), seqs
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
    # every route is stamped AFTER the click that caused it — the attribution the bridge relies on
    for r in routes():
        prior = [e for e in events() if e["seq"] < r["seq"] and e.get("type") == "click"]
        assert prior, "route %r has no click before it: %r" % (r["url"], seqs)


def test_a_secret_in_the_address_never_reaches_an_artifact():
    """`scenario.json` is a file people commit into their own repository. Until routes existed the
    query was dropped by `page_identity` and this could not happen; now it can, so the same mandatory
    redaction that guards field values guards the address."""
    assert SECRET not in json.dumps(events()), "the secret reached the events"
    assert SECRET not in json.dumps(scenario()), "the secret reached the scenario"
    assert SECRET not in export_spec(scenario()), "the secret reached the exported spec"
    # the parameter NAME survives, so two routes differing by it stay distinguishable
    assert any("apiKey=REDACTED" in r["url"] for r in routes()), [r["url"] for r in routes()]


# --- what the bridge did with it ---------------------------------------------------------------

def test_a_transition_a_click_performed_is_not_performed_a_second_time():
    """A-1, second half. Every route in this fixture follows a click that caused it, so the ONLY
    navigate the scenario may contain is the opening one. A synthesized navigate here is the silent
    corrector that made a routing regression invisible on replay."""
    navs = steps_by("navigate")
    assert len(navs) == 1, ["%s -> %s" % (s["step_id"], s.get("target")) for s in navs]
    assert navs[0]["step_id"] == 1, navs[0]


def test_every_route_becomes_a_checkable_step():
    """A-1, first half. Including the LAST one, which previously vanished: it was inferred from the
    next event's url, and there is no next event."""
    asserts = steps_by("assert")
    assert len(asserts) == len(routes()), [s.get("expected") for s in asserts]
    assert all(s.get("condition") == "url_contains" for s in asserts), asserts
    assert all(s.get("expect_ok") is True for s in asserts), asserts
    assert "#/done" in (asserts[-1].get("expected") or ""), asserts[-1]


def test_a_query_route_is_visible_in_the_scenario():
    """A-3, the half that needs no key migration. `page_identity` still drops the query — this works
    because the route is keyed off the OBSERVED address instead."""
    expected = [s.get("expected") or "" for s in steps_by("assert")]
    assert any("?tab=orders" in e for e in expected), expected


def test_an_assertion_never_carries_the_host_it_was_recorded_on():
    """`retarget` rewrites only a navigate's `target`, so a full address in `expected` would fail the
    moment the plan is replayed against another stand — a red saying 'your application changed' about
    a stand that merely moved."""
    for s in steps_by("assert"):
        exp = s.get("expected") or ""
        assert "spa.test" not in exp and not exp.startswith("http"), s


def test_the_new_facts_stay_out_of_the_frozen_step():
    """`canonical_plan_hash` hashes every field of every step, so one extra key would move the hash of
    every plan ever saved. The parallel assertion in test_control_identity_offline.py exists for the
    same reason and was written after the same measurement."""
    for s in scenario()["steps"]:
        assert "route_arrived" not in s, s
        assert "observed_url" not in s, s


def test_the_exported_spec_still_parses_with_routes_in_it():
    """A route is full of regex metacharacters and `/` ends a JS regex literal, so `#/done` alone used
    to emit a `.spec.ts` that does not parse. Asserted on the artifact, not on the escaping function."""
    src = export_spec(scenario())
    for s in steps_by("assert"):
        assert "toHaveURL(/" in src, src
    assert "toHaveURL(/\\/" in src or "toHaveURL(/\\?" in src, src
    assert re.search(r"toHaveURL\(/[^/\n]*(?<!\\)/\s*\)", src) is None, (
        "an unescaped '/' closed the regex literal early:\n%s" % src)


def test_a_model_cannot_delete_a_navigate_from_its_own_plan():
    """`ground_scenario` is the last validator on the LLM authoring path. `trust_observed` is opt-in
    and only `record_bridge` sets it — a model returning `route_arrived: true` must change nothing."""
    site_map = {"http://x/a": [{"semantic_id": "aaa", "role": "button", "name": "A",
                                "locator": {"css": "#a"}, "alternatives": [], "page": "http://x/a"}],
                "http://x/b": [{"semantic_id": "bbb", "role": "button", "name": "B",
                                "locator": {"css": "#b"}, "alternatives": [], "page": "http://x/b"}]}
    refs = [{"ref": "aaa", "verb": "click"},
            {"ref": "bbb", "verb": "click", "route_arrived": True, "observed_url": "http://x/b"}]
    steps, unmatched = ground_scenario(refs, site_map)                      # default: NOT trusted
    assert unmatched == [], unmatched
    navs = [s for s in steps if s["action_type"] == "navigate"]
    assert len(navs) == 2, [s["action_type"] for s in steps]
    trusted, _ = ground_scenario(refs, site_map, trust_observed=True)       # the recording's own call
    assert len([s for s in trusted if s["action_type"] == "navigate"]) == 1, \
        [s["action_type"] for s in trusted]


class _NavEx:
    """pw-executor stand-in whose navigate answers with a chosen HTTP status; everything else works.

    Modelled on the FakeEx in test_record_bridge_offline.py — the point of difference is the one field
    replay never read.
    """

    def __init__(self, status):
        self.status, self.url = status, ""

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", "")
            return {"url": self.url, "title": "", "status": self.status}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": '- alert "ok"', "nodeCount": 1}
        if m == "browser.screenshotHash":
            return {"hash": "shot1"}
        if m == "browser.probe":
            return {"count": 1}
        if m == "browser.expect":
            return {"ok": True}
        return {"ok": True}


def _replay_one_navigate(status):
    """Replay a one-step plan whose only step is a navigate, against an executor answering `status`."""
    import tempfile
    from brain.healing import HealingEngine
    from brain.replay import run_replay
    from brain.store import Store
    plan = {"plan_id": "nav", "target_url": "http://x/app", "steps": [
        {"step_id": 1, "action_type": "navigate", "semantic_id": "n1", "intent": "go",
         "target": "http://x/app/orders", "locator": None, "alternatives": None,
         "is_milestone": True, "phase": "scenario"}]}
    ex = _NavEx(status)
    store = Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)
    heal = HealingEngine(ex, store, "r", use_llm=False)
    return run_replay(ex, store, heal, plan, plan["target_url"], tempfile.mkdtemp())


def test_a_navigate_that_answers_4xx_is_a_failure_not_a_pass():
    """A-2. `browser.navigate` has ALWAYS returned the response status and replay never looked at it.
    Measured: a synthesized navigate to a client-side route on a server with no history fallback
    answers 404, the step was recorded `ok`, the run exited 0, and the failure surfaced a step later
    on a locator — blamed on the application. The fault stays `app`: the executor arrived, the answer
    is the site's."""
    r = _replay_one_navigate(404)
    assert r["exit_code"] != 0, r
    assert r["failed"] >= 1, r
    step = r["steps"][0]
    assert step["outcome"] == "failed", step
    assert "404" in (step.get("error") or ""), step
    assert step.get("fault") == "app", step


def test_a_navigate_that_answers_2xx_or_makes_no_request_still_passes():
    """The other half, and the reason the condition is `status is not None`. Measured with a real
    Chromium: `file://` answers 200 for a file that exists (and THROWS for one that does not), while a
    fragment-only hop makes no request at all and reports null. Both must stay green, or the entire
    fixture corpus turns red."""
    for status in (200, 204, 304, None):
        r = _replay_one_navigate(status)
        assert r["exit_code"] == 0, (status, r)
        assert r["steps"][0]["outcome"] == "ok", (status, r["steps"][0])


def test_a_recording_with_no_routes_produces_exactly_what_it_used_to():
    """The regression guard. Everything above is additive: a stream with no `route` line must ground
    byte for byte as it did before ADR-138, or every existing recorded plan changes meaning."""
    evs = [{"type": "click", "url": "http://x/app#/a", "selectorCandidates": [{"strategy": "css", "locator": {"css": "a.n"}}]},
           {"type": "click", "url": "http://x/app#/b", "selectorCandidates": [{"strategy": "css", "locator": {"css": "b.p"}}]}]
    sc, unmatched = build_scenario(evs, session="plain")
    assert unmatched == [], unmatched
    kinds = [s["action_type"] for s in sc["steps"]]
    assert kinds == ["navigate", "click", "navigate", "click"], kinds


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
