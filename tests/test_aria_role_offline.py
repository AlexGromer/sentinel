"""Offline gate: the role the tool reports is the role the page has.

Run:  .venv/bin/python tests/test_aria_role_offline.py

`browser.interactives` used to emit `role` as `getAttribute('role') || tagName`. That field was named
`role` and was not one: for a plain `<a>` it read "a", and there is no ARIA role called `a`, so every
locator built from it resolved to nothing. Two consumers then patched around it differently —
`graph.py` re-derived a role from the tag (and inverted the ARIA rule, so `<button role="tab">` froze
as a button), while `healing.py` used the raw field as-is (so a re-ground onto any link produced a
dead locator). One mis-named field, two independent defects, in two modules.

MEASURED before the fix, across every fixture: 48 unusable locators — 42 from the raw-field
conflation, all of them on the self-healing path, plus 6 tabs on the plan path. Every explore run
against `l5.html` in `runs/` had `coverage 0.0, exercised 0`; the tool could see the tabs and could
not press one. After: 0.

The load-bearing check here is a PROPERTY, not a table:

    every role the executor claims must resolve against Playwright's own engine

A table of expected roles would have to be written by hand, i.e. by the same reasoning that produced
the bug, and would agree with a wrong implementation that shared its assumptions. Asking the page is
the only check that cannot.
"""
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "testdata" / "fixtures"


def _drive(calls: list) -> "list | None":
    """Run calls through the REAL executor. SKIP (not fail) without a build or a browser."""
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


def _url(name: str) -> str:
    return "file://" + str(FIXTURES / name)


def _fixtures() -> list:
    return sorted(p.name for p in FIXTURES.glob("*.html"))


# --- the load-bearing check -------------------------------------------------------------------------
def test_every_role_the_executor_claims_resolves_on_the_page():
    """Ask the page, never a table. Run over EVERY fixture, not a chosen one.

    Two exclusions, both stated rather than silently filtered:
      * a control that is off screen is not in the accessibility tree, so `getByRole` correctly finds
        nothing — `visible` (ADR-093) is what distinguishes that from a wrong role;
      * a name shared by several controls resolves to >1, which is an ambiguity problem (named in
        ADR-082), not a role problem.
    Anything else resolving to 0 means we claimed a role the page does not have."""
    calls, index = [], []
    for fx in _fixtures():
        calls += [("browser.navigate", {"url": _url(fx)}), ("browser.interactives", {})]
        index.append(fx)
    res = _drive(calls)
    if res is None:
        return
    per_fixture = {fx: res[2 * i + 1]["elements"] for i, fx in enumerate(index)}

    probes, meta = [], []
    for fx, els in per_fixture.items():
        probes.append(("browser.navigate", {"url": _url(fx)}))
        meta.append(None)
        for e in els:
            role, name = e.get("role"), (e.get("name") or "").strip()
            if not role or not name or not e.get("visible"):
                continue
            probes.append(("browser.probe", {"locator": {"role": role, "name": name}}))
            meta.append((fx, role, name, e.get("tag")))
    res2 = _drive(probes)
    if res2 is None:
        return

    checked, bad = 0, []
    for m, r in zip(meta, res2):
        if m is None:
            continue
        fx, role, name, tag = m
        n = r.get("count", 0)
        if n == 0:
            bad.append(f"{fx}: <{tag}> claims role {role!r} named {name!r} — the page resolves 0")
        else:
            checked += 1
    assert checked >= 40, (
        f"only {checked} role claims were checked; this gate is meant to sweep the whole corpus and "
        "a shrunken sample would pass over a defect it is supposed to see")
    assert not bad, "roles claimed but not present on the page:\n  " + "\n  ".join(bad)


def test_an_explicit_role_attribute_beats_the_tag():
    """The ARIA rule that was inverted, and the symptom it caused.

    Asserted in BOTH directions: the tab is found as a tab AND is NOT found as a button. Only the
    second half fails on the old code — a positive-only check passes while the product is broken,
    because `<button role="tab">` is still a real element with that name."""
    res = _drive([("browser.navigate", {"url": _url("l9-roles.html")}),
                  ("browser.interactives", {}),
                  ("browser.probe", {"locator": {"role": "tab", "name": "TabByRole"}}),
                  ("browser.probe", {"locator": {"role": "button", "name": "TabByRole"}})])
    if res is None:
        return
    _, inter, as_tab, as_button = res
    by_name = {(e.get("name") or "").strip(): e for e in inter["elements"]}
    assert by_name["TabByRole"]["role"] == "tab", (
        f"a <button role=tab> was reported as {by_name['TabByRole']['role']!r}; the explicit "
        "attribute has to win or the locator names a control the page does not have")
    assert by_name["TabByRole"]["tag"] == "button", "the tag is still reported, separately and truthfully"
    assert as_tab["count"] == 1
    assert as_button["count"] == 0, (
        "the page resolves this control as a button — then the old behaviour was not a defect and "
        "this whole ADR is wrong. It resolves 0 in every browser we measured.")
    # ... and the same rule applied to an anchor, which the old ladder mapped to `link` by tag.
    assert by_name["LinkAsTab"]["role"] == "tab", by_name["LinkAsTab"]
    assert by_name["LinkAsButton"]["role"] == "button", by_name["LinkAsButton"]
    assert by_name["DivAsButton"]["role"] == "button", by_name["DivAsButton"]


def test_input_is_eight_roles_chosen_by_type_not_one():
    """`<input>` was flattened to `textbox` regardless of `type`.

    The corpus could not catch this: its only checkbox lives in a `display:none` panel on `l5`, so
    every gate saw it as "off screen" rather than as "wrong role". `l9` exists so the four broken
    types are on a page something looks at."""
    res = _drive([("browser.navigate", {"url": _url("l9-roles.html")}),
                  ("browser.interactives", {})])
    if res is None:
        return
    by_name = {(e.get("name") or "").strip(): e for e in res[1]["elements"]}
    want = {"RoleText": "textbox", "RoleSearch": "searchbox", "RoleCheckbox": "checkbox",
            "RoleRadio": "radio", "RoleNumber": "spinbutton", "RoleRange": "slider"}
    for name, role in want.items():
        assert name in by_name, f"{name} not perceived at all; got {sorted(by_name)}"
        assert by_name[name]["role"] == role, (
            f"{name}: reported {by_name[name]['role']!r}, page has {role!r}")
    # `submit` takes its name from `value`, so it is matched on the role alone.
    assert any(e["role"] == "button" and e["tag"] == "input" for e in res[1]["elements"]), \
        "an <input type=submit> is a BUTTON; it used to be reported as a textbox"


def test_a_tag_name_is_never_reported_as_a_role():
    """The specific string that made 42 locators dead. `a`, `input`, `select`, `textarea` are tags;
    none of them is an ARIA role, and `getByRole` cannot match any of them."""
    res = _drive([(c, p) for fx in _fixtures()
                  for c, p in (("browser.navigate", {"url": _url(fx)}), ("browser.interactives", {}))])
    if res is None:
        return
    offenders = []
    for r in res[1::2]:
        for e in r["elements"]:
            if e.get("role") in ("a", "input", "select", "textarea", "div", "span"):
                offenders.append(e)
    assert not offenders, (
        f"{len(offenders)} elements report a TAG NAME where a role belongs, e.g. {offenders[:3]}")


def test_the_heal_path_builds_a_locator_that_resolves():
    """The 42. `healing.descriptor_to_locator` maps a live element to a locator, and it reads the
    executor's `role` field directly — so a tag name there became `getByRole('a')`, which resolves to
    nothing in principle. Asserted through the REAL function, not a copy of it."""
    sys.path.insert(0, str(REPO))
    from brain.healing import descriptor_to_locator
    res = _drive([(c, p) for fx in _fixtures()
                  for c, p in (("browser.navigate", {"url": _url(fx)}), ("browser.interactives", {}))])
    if res is None:
        return
    built, checked = [], 0
    for r in res[1::2]:
        for e in r["elements"]:
            loc = descriptor_to_locator(e)
            if loc and loc.get("role"):
                built.append(loc["role"])
                checked += 1
    assert checked >= 40, f"only {checked} heal locators built — too small a sample to mean anything"
    bad = sorted({r for r in built if r in ("a", "input", "select", "textarea")})
    assert not bad, f"the heal path still builds locators from tag names: {bad}"


def test_the_brain_reads_the_role_instead_of_re_deriving_it():
    """One place decides a role. Two places deciding it differently IS the defect.

    Asserted on the converter's source, anchored on the loop body rather than on the file, because
    the docstring above it necessarily quotes the ladder it removed — a document-wide search would be
    satisfied by the explanation instead of the code (this exact trap has fired three times here)."""
    src = (REPO / "brain" / "graph.py").read_text()
    i = src.index("def _elements_from_interactives")
    body = src[src.index("out = []", i):src.index("return out", i)]
    assert 'tag == "button"' not in body, "the tag->role ladder is back in the converter body"
    assert 'e.get("role")' in body, "the converter must READ the role"
    for tag_test in ('tag == "a"', 'tag == "select"', 'tag in ("input", "textarea")'):
        assert tag_test not in body, f"the converter still derives a role from the tag: {tag_test}"


def test_the_coverage_denominator_did_not_shrink_when_tabs_got_their_real_role():
    """A coverage number that improves because the page got smaller is the failure mode this
    codebase keeps finding.

    Before ADR-094 a `<button role=tab>` was mis-typed as a button and so was ALREADY in the
    candidate set — unreachable, but counted. Narrowing to `role == "button"` would have removed four
    controls per tabbed page from the denominator and made coverage rise for the wrong reason.
    Measured on `l5.html`: `interactive_seen` is 15 before and 15 after; `exercised` went 0 -> 9."""
    sys.path.insert(0, str(REPO))
    from brain.graph import _CLICK_ROLES, _elements_from_interactives
    assert "tab" in _CLICK_ROLES and "button" in _CLICK_ROLES, _CLICK_ROLES
    assert "checkbox" not in _CLICK_ROLES, (
        "widening beyond {button, tab} is a behaviour change wearing a bug fix's clothes — it grows "
        "the denominator on every form page for reasons that have nothing to do with roles")

    res = _drive([("browser.navigate", {"url": _url("l5.html")}), ("browser.interactives", {})])
    if res is None:
        return
    els = _elements_from_interactives(res[1]["elements"], "/l5")
    candidates = [e for e in els if e["role"] in _CLICK_ROLES]
    assert len(candidates) == 15, (
        f"l5 offers {len(candidates)} clickable controls; it offered 15 before the role fix and the "
        "set was supposed to be relabelled, not resized")
    assert sum(1 for e in candidates if e["role"] == "tab") == 4, \
        "the four tabs must still be in the set — as tabs"


def test_an_element_with_no_role_is_dropped_rather_than_named_after_its_tag():
    """`input[type=hidden]` matches the perception selector and has no ARIA role. Reporting the tag
    for it is what produced `getByRole('input')`; the honest answer is an empty role, and the brain
    must then drop it instead of planning a step nothing can address."""
    sys.path.insert(0, str(REPO))
    from brain.graph import _elements_from_interactives
    res = _drive([("browser.navigate", {"url": _url("l9-roles.html")}),
                  ("browser.interactives", {})])
    if res is None:
        return
    raw = res[1]["elements"]
    hidden = [e for e in raw if e.get("tag") == "input" and not e.get("role")]
    assert hidden, ("l9 §4 carries an <input type=hidden>; nothing reported a roleless element, so "
                    "this check is vacuous")
    assert all(e["role"] == "" for e in hidden), hidden
    # It must be droppable ONLY for lacking a role. The brain also drops anything with no
    # testid/name/text several lines earlier, so a nameless control would exit before the code under
    # test ever ran — the check would pass while proving nothing. Caught by mutation: a brain that
    # substituted the tag for a missing role kept every test green until this element was given an
    # anchor.
    assert all(e.get("testid") for e in hidden), (
        "the roleless element must carry a testid, or it is dropped by the no-anchor guard and this "
        "check never reaches the role logic at all")

    built = _elements_from_interactives(raw, "/l9")
    # Asserted by IDENTITY, not by falsiness. `not e["role"]` passes the moment the brain substitutes
    # a tag for the missing role: the element is then in the model under the name "input", which IS
    # the defect, and the check would call it clean.
    hidden_ids = {e["testid"] for e in hidden}
    leaked = [e for e in built if e.get("testid") in hidden_ids]
    assert not leaked, (
        f"a control with NO ARIA role reached the page model: {leaked}. Substituting the tag name "
        "for a missing role is how `getByRole('input')` came to exist.")
    assert all(e["role"] not in ("input", "a", "select", "textarea") for e in built), \
        f"a tag name is being used as a role in the page model: {built}"


def test_the_step_the_reader_sees_names_the_role_the_page_has():
    """The candidate the planner receives, and the sentence a human reads next to it.

    This was a THIRD place that decided a role instead of reading one: the candidate hardcoded
    `"role": "button"` and formatted `click button '{name}'`, so a live run against `l5.html` printed
    "click button 'Overview'" for a control the page calls a tab. The locator beside it was already
    correct — which is precisely why nobody noticed: the failure showed up as a click that timed out,
    not as a label that lied.

    Anchored on the `candidates.append(` call itself rather than on the file: the comment above it
    necessarily quotes the literal it removed, and a document-wide search would be satisfied by the
    explanation instead of the code. That trap has fired three times in this repository."""
    src = (REPO / "brain" / "graph.py").read_text()
    i = src.index('candidates.append({"kind": "click"')
    call = src[i:src.index("})", i)]
    assert '"role": b["role"]' in call, (
        f'the click candidate must carry the element\'s own role. Found:\n{call}')
    assert '"role": "button"' not in call, "the candidate is inventing a role again"
    assert "click button" not in call, (
        "the human-readable intent hardcodes 'button'; on a tabbed page it describes a control that "
        "is not there")
    assert "b['role']" in call or 'b["role"]' in call, call


def test_both_perception_surfaces_report_the_same_role():
    """`browser.setOfMarks` feeds the visual heal tier through the SAME `descriptor_to_locator`, so a
    mark carrying a tag name breaks the visual tier exactly as it broke the text tier. Two producers,
    one definition — asserted per control, not by count."""
    res = _drive([("browser.navigate", {"url": _url("l9-roles.html")}),
                  ("browser.interactives", {}), ("browser.setOfMarks", {})])
    if res is None:
        return
    _, inter, som = res
    on_screen = {(e.get("name") or "").strip(): e["role"] for e in inter["elements"] if e["visible"]}
    marks = {(m.get("name") or "").strip(): m["role"] for m in som["marks"]}
    shared = set(on_screen) & set(marks)
    assert len(shared) >= 8, f"too few controls compared ({len(shared)}) for this to mean anything"
    mismatched = {k: (on_screen[k], marks[k]) for k in shared if on_screen[k] != marks[k]}
    assert not mismatched, f"the two perception surfaces disagree about roles: {mismatched}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} ARIA-role tests passed")
