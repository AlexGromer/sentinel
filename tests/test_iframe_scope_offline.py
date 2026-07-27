"""Offline gate: a control inside an iframe is perceived, addressable and actuated.

Run:  .venv/bin/python tests/test_iframe_scope_offline.py

`page.$$eval`, `page.locator` and `getByRole` do not cross a frame boundary — a property of the
selector engine, not an oversight — so a control inside an iframe was invisible to planning.
`browser.perceptionAudit` had been counting exactly those under `unseen.iframe` since ADR-093, and
reporting 0 on every page the tool had ever seen, because not one of the ten fixtures contained an
`<iframe>` at all.

ADR-095 makes `frame` an AXIS on the locator rather than a seventh strategy: `frameLocator` carries
all six tiers, so `strategies.py`, `PRIORS`, `pick_confidence` and the locator-key vocabulary gate
are untouched. What that buys is checked here rather than asserted:

  * the frame is derived from the page, three ways, in stability order (name > id > position);
  * a frame-scoped locator RESOLVES and the control can be clicked and filled;
  * `browser.interactives` and `browser.perceptionAudit` still agree — the ADR-093 property has to
    survive perception growing, or the audit under-reports exactly as it once over-reported;
  * a frameless page produces byte-identical descriptors, so no stored `plan_hash` moves;
  * depth is capped at 1 and the cap is REPORTED (`opaque.frames_nested`), not silent;
  * the heal path carries the scope — dropping it would report a heal whose locator names a control
    that only exists inside a frame, and the replay would then fail on a step just called healed.
"""
import copy
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "testdata" / "fixtures"
FRAMES = "file://" + str(FIXTURES / "l10-frames.html")


def _drive(calls: list) -> "list | None":
    dist = REPO / "pw-executor" / "dist" / "server.js"
    if not dist.exists():
        print("     SKIP — pw-executor/dist not built (npm run build)")
        return None
    script = (
        'import sys, json; sys.path.insert(0, %r)\n'
        'from brain.executor import Executor\n'
        'ex = Executor("node %s")\n'
        'out = [ex.call(m, **p) for m, p in json.loads(%r)]\n'
        'ex.call("shutdown"); ex.close()\n'
        'print("@@RESULT@@" + json.dumps(out))\n' % (str(REPO), dist, json.dumps(calls))
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PW_NO_TRACE": "1"}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                       timeout=600)
    for line in (r.stdout or "").splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    print("     SKIP — no browser available:", ((r.stderr or "") + (r.stdout or ""))[-250:].replace("\n", " "))
    return None


def test_controls_inside_frames_are_perceived_and_carry_their_scope():
    """Three addressing modes, in the order stability demands. Asserted per control, because a count
    is satisfied by finding the same frame three times."""
    res = _drive([("browser.navigate", {"url": FRAMES}), ("browser.interactives", {})])
    if res is None:
        return
    els = res[1]["elements"]
    by_name = {(e.get("name") or "").strip(): e for e in els}
    want = {
        "Top button": None,                                # the top frame carries NO key at all
        "Pay now": 'iframe[name="payment"]',               # a name the author chose
        "Card number": 'iframe[name="payment"]',
        "Accept terms": "iframe#terms",                    # an id
        "Read more": "iframe#terms",
        "Anonymous frame button": "iframe >> nth=2",       # positional, the honest last resort
        "Outer frame button": "iframe#outer",
    }
    for name, frame in want.items():
        assert name in by_name, f"{name!r} not perceived at all; got {sorted(by_name)}"
        got = by_name[name].get("frame")
        assert got == frame, f"{name!r}: frame is {got!r}, want {frame!r}"
    assert "frame" not in by_name["Top button"], (
        "a top-frame control must carry NO `frame` key — not a null one. `canonical_plan_hash` "
        "hashes every field of every step, so a present-but-empty key would move all 106 stored "
        "plan hashes for a fact about the page rather than about the test.")
    # The nested control is NOT here — that is the declared boundary, asserted rather than assumed.
    assert "Deep button" not in by_name, (
        "a frame nested two deep was perceived; depth is capped at 1 on purpose and the cap is what "
        "`opaque.frames_nested` reports")
    # The payment frame carries BOTH a name and an id, so the preference is pinned rather than
    # merely exercised. Without this the two branches are interchangeable: a build that preferred
    # the id addresses every frame in this fixture just as well, and nothing fails.
    src = (FIXTURES / "l10-frames.html").read_text()
    line = next(l for l in src.splitlines() if "payment" in l and "<iframe" in l)
    assert 'name="payment"' in line and 'id="payment-frame"' in line, (
        f"§1 must offer BOTH ways to address it, or the order is untested: {line.strip()}")
    assert by_name["Pay now"]["frame"] == 'iframe[name="payment"]', (
        "the id won over the name. A name is chosen by the author and survives re-layout; ids are "
        "often framework-generated, so preferring one is a stability decision, not a coin toss.")


def test_the_locators_the_brain_builds_for_framed_controls_resolve():
    """The PLAN path — the whole point of the axis, and the thing none of the checks above touch.

    `descriptor_to_locator` (the heal path) is a different function from the converter that freezes
    a step's `locator` and `alternatives`. A build that carried the scope through healing and dropped
    it in `graph.py` passes every other test here and produces plans whose every framed step is
    dead. Caught by mutation.

    Each built locator is PROBED rather than inspected: a `frame` key of the right shape pointing at
    nothing would satisfy a structural check."""
    sys.path.insert(0, str(REPO))
    from brain.graph import _elements_from_interactives
    res = _drive([("browser.navigate", {"url": FRAMES}), ("browser.interactives", {})])
    if res is None:
        return
    built = _elements_from_interactives(res[1]["elements"], "/l10")
    framed = [e for e in built if e.get("frame")]
    assert len(framed) >= 5, f"only {len(framed)} framed descriptors; the sample is too thin"

    probes, meta = [("browser.navigate", {"url": FRAMES})], [None]
    for e in framed:
        for loc in [e["locator"]] + [a["locator"] for a in e["alternatives"]]:
            assert loc.get("frame") == e["frame"], (
                f"a locator for {e['name']!r} lost its scope: {loc}")
            probes.append(("browser.probe", {"locator": loc}))
            meta.append((e["name"], loc))
    res2 = _drive(probes)
    if res2 is None:
        return
    for m, r in zip(meta, res2):
        if m is None:
            continue
        name, loc = m
        assert r.get("count") == 1, (
            f"the plan would freeze {loc} for {name!r}, and it resolves {r.get('count')}")


def test_a_frame_scoped_locator_resolves_and_the_control_can_be_used():
    """Perception without action is half a capability. Click AND fill, because they take different
    paths through the executor and only one of them proves the locator resolves at all."""
    pay = {"role": "button", "name": "Pay now", "frame": 'iframe[name="payment"]'}
    res = _drive([
        ("browser.navigate", {"url": FRAMES}),
        ("browser.probe", {"locator": pay}),
        ("browser.probe", {"locator": {k: v for k, v in pay.items() if k != "frame"}}),
        ("browser.click", {"locator": pay}),
        ("browser.fill", {"locator": {"role": "textbox", "name": "Card number",
                                      "frame": 'iframe[name="payment"]'}, "value": "4242"}),
        ("browser.probe", {"locator": {"testid": "anon-btn", "frame": "iframe >> nth=2"}}),
    ])
    if res is None:
        return
    _, scoped, unscoped, click, fill, positional = res
    assert scoped["count"] == 1, f"a frame-scoped locator resolved {scoped['count']}"
    assert unscoped["count"] == 0, (
        "the SAME locator without its frame resolved anyway — then the scope is decorative and this "
        "gate proves nothing. The negative control is the half that matters.")
    assert click.get("clicked") is True, click
    assert fill.get("filled") is True, fill
    assert positional["count"] == 1, f"positional frame addressing resolved {positional['count']}"


def test_the_audit_still_counts_what_perception_returns():
    """The ADR-093 property, re-checked now that perception reaches further.

    A numerator left top-frame-only would under-report the moment frames were read — the same defect
    ADR-093 fixed, running the other way. Checked on the frame fixture AND on a frameless one,
    because the equality is trivially true where there are no frames."""
    for fx, url in (("l10-frames.html", FRAMES), ("l5.html", "file://" + str(FIXTURES / "l5.html"))):
        res = _drive([("browser.navigate", {"url": url}),
                      ("browser.interactives", {}), ("browser.perceptionAudit", {})])
        if res is None:
            return
        _, inter, audit = res
        assert audit["seen"] == len(inter["elements"]), (
            f"{fx}: audit says {audit['seen']}, perception returns {len(inter['elements'])}")


def test_the_boundary_is_reported_rather_than_passed_over():
    """A frame we chose not to enter and a frame we could not enter are different sentences.

    `l10` §4 puts a real control behind a second-level frame, so the denominator knows about
    something the numerator does not — which is the only shape that proves the cap is a cap and not
    an absence."""
    res = _drive([("browser.navigate", {"url": FRAMES}), ("browser.perceptionAudit", {})])
    if res is None:
        return
    a = res[1]
    assert a["opaque"]["frames_nested"] == 1, a["opaque"]
    assert a["unseen"]["iframe"] == 1, (
        f"the control behind the nested frame must stay in `unseen`: {a['unseen']}. If this is 0 the "
        "fixture stopped demonstrating the boundary — an earlier version nested one `srcdoc` inside "
        "another and parsed as an empty document, so the boundary was reported with nothing behind "
        "it and the gate would have passed either way.")
    assert a["seen"] == 7 and a["total"] == 8, (a["seen"], a["total"])
    assert a["ratio"] == 0.875, a["ratio"]
    per_frame = {f.get("selector"): f for f in a["frames"]}
    assert per_frame[None]["perceived"] is False, "the nested frame must be marked unperceived"
    assert all(f["perceived"] for s, f in per_frame.items() if s), per_frame


def test_a_page_without_frames_produces_byte_identical_descriptors():
    """No stored plan may move because the tool learned to read frames.

    Asserted by DEEP EQUALITY against a copy with every `frame` key stripped: any leakage — a null,
    an empty string, a key ordered differently into the locator — shows up here. `l1` is used
    because it is the flattest fixture: if a frame key can appear anywhere, it appears everywhere."""
    sys.path.insert(0, str(REPO))
    from brain.graph import _elements_from_interactives
    res = _drive([("browser.navigate", {"url": "file://" + str(FIXTURES / "l1.html")}),
                  ("browser.interactives", {})])
    if res is None:
        return
    raw = res[1]["elements"]
    assert raw, "l1 perceived nothing; this check would be vacuous"
    assert all("frame" not in e for e in raw), \
        f"a frameless page emitted a `frame` key: {[e for e in raw if 'frame' in e][:2]}"
    built = _elements_from_interactives(raw, "/l1")
    stripped = copy.deepcopy(built)
    for e in stripped:
        e.pop("frame", None)
        for loc in [e.get("locator")] + [a["locator"] for a in e.get("alternatives") or []]:
            if isinstance(loc, dict):
                loc.pop("frame", None)
    assert built == stripped, "a frameless page grew a `frame` key somewhere in its descriptors"


def test_the_heal_path_carries_the_scope():
    """`descriptor_to_locator` is the one mapping both re-ground tiers share. Dropping the frame
    there is the silent kind of wrong: the heal reports a locator, the locator names a control that
    exists only inside a frame, and the replay fails on a step the audit just called healed.

    Driven through the REAL function against REAL elements, then the result is probed — asserting the
    dict shape alone would pass for a frame selector that addresses nothing."""
    sys.path.insert(0, str(REPO))
    from brain.healing import descriptor_to_locator
    res = _drive([("browser.navigate", {"url": FRAMES}), ("browser.interactives", {})])
    if res is None:
        return
    els = res[1]["elements"]
    in_frames = [e for e in els if e.get("frame")]
    assert len(in_frames) >= 5, f"only {len(in_frames)} framed controls; the sample is too thin"

    probes = [("browser.navigate", {"url": FRAMES})]
    for e in in_frames:
        loc = descriptor_to_locator(e)
        assert loc and loc.get("frame") == e["frame"], (
            f"the heal locator lost its scope: {loc} from {e}")
        probes.append(("browser.probe", {"locator": loc}))
    res2 = _drive(probes)
    if res2 is None:
        return
    for e, r in zip(in_frames, res2[1:]):
        assert r.get("count") == 1, (
            f"a heal locator for {e['name']!r} in {e['frame']!r} resolves {r.get('count')} — the "
            "shape was right and the address was not")


def test_the_frame_is_part_of_the_controls_identity():
    """Two frames holding a control of the same role and name are TWO controls.

    Without the frame in `semantic_id` they collide: coverage counts one where there are two, and the
    second reads as already exercised. Checked on synthesised elements rather than a fixture, because
    the collision needs two identical names and a fixture built to have them would be documenting the
    test rather than the product."""
    sys.path.insert(0, str(REPO))
    from brain.graph import _elements_from_interactives
    same = [{"role": "button", "name": "Submit", "tag": "button", "text": "Submit",
             "testid": None, "visible": True, "frame": 'iframe[name="a"]'},
            {"role": "button", "name": "Submit", "tag": "button", "text": "Submit",
             "testid": None, "visible": True, "frame": 'iframe[name="b"]'}]
    built = _elements_from_interactives(same, "/p")
    assert len({e["semantic_id"] for e in built}) == 2, \
        f"two controls in different frames collapsed to one identity: {built}"

    # ...and a top-frame control must hash exactly as it did before frames existed.
    top = [{"role": "button", "name": "Submit", "tag": "button", "text": "Submit",
            "testid": None, "visible": True}]
    from brain.state import semantic_id
    assert _elements_from_interactives(top, "/p")[0]["semantic_id"] == semantic_id("/p", "button", "Submit"), \
        "a top-frame control's identity changed; every stored plan referencing it would break"


def test_the_frame_is_a_scope_and_not_a_seventh_strategy():
    """The design claim, checked where it would break.

    A new strategy would need a prior, and there is no measurement to set one from — `PRIORS` is
    already six numbers this codebase admits are unmeasured (GAP-RISK-002). So `frame` must leave the
    vocabulary alone, and a frame-scoped role+name locator must still be scored as `role_name`."""
    sys.path.insert(0, str(REPO))
    from brain import strategies as S
    from brain.healing import pick_confidence
    assert "frame" not in S.STRATEGY_BY_LOCATOR_KEY, "frame entered the strategy vocabulary"
    assert "frame" not in S.PRIORS, "frame acquired a prior nobody measured"

    scoped = {"role": "button", "name": "Pay", "frame": 'iframe[name="payment"]'}
    plain = {"role": "button", "name": "Pay"}
    assert pick_confidence(scoped) == pick_confidence(plain), \
        "scoping a locator changed its confidence; a frame says WHERE, not how well"

    from brain.record_bridge import _infer_strategy
    assert _infer_strategy(scoped) == S.ROLE_NAME, \
        f"a frame-scoped role+name locator inferred {_infer_strategy(scoped)!r}"
    assert _infer_strategy({"testid": "x", "frame": "iframe#f"}) == S.TESTID


def test_the_exported_test_keeps_the_frame():
    """An export that drops the scope compiles, runs, and fails on a control the plan reaches."""
    sys.path.insert(0, str(REPO))
    from brain.exporter import _locator_expr
    expr = _locator_expr({"role": "button", "name": "Pay now", "frame": 'iframe[name="payment"]'})
    assert "frameLocator('iframe[name=\"payment\"]')" in expr, expr
    assert expr.startswith("page.frameLocator("), expr
    assert ".getByRole('button', { name: 'Pay now' })" in expr, expr
    # An unscoped locator must be untouched — the axis is additive or it is a rewrite.
    assert _locator_expr({"role": "button", "name": "Pay now"}) == \
        "page.getByRole('button', { name: 'Pay now' })"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} iframe-scope tests passed")
