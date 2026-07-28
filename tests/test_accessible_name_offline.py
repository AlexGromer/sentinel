"""Offline gate: a control the tool can see is a control the tool can NAME.

Run:  .venv/bin/python tests/test_accessible_name_offline.py

`browser.interactives` computed `name` as `aria-label || textContent`. An `<input>` has no text
content — its accessible name comes from the associated `<label>`, which was never read. So eleven
labelled form fields across this repository's fixtures arrived with an empty name, failed the brain's
"no anchor" guard, and were DROPPED from the page model without a word. On `l3.html`, the
validation-form fixture, five of nine controls reached the model while `browser.perceptionAudit`
reported a ratio of 1.00. We were not blind to them; we threw them away after seeing them, and every
number downstream described the smaller page.

`<select>` was worse than empty: `textContent` on a select is its OPTION LIST, so `l5.html`'s theme
picker was named "System default Light Dark" where the accessibility tree says "Theme:". A wrong name
is not a smaller version of a missing one — it makes a locator that looks specific and matches
nothing.

⚠ Why the ADR-094 gate did not catch any of this: it probes `{role, name}` and SKIPS elements whose
name is empty. It skipped exactly the broken ones. A gate that steps over its subject is not a weaker
gate, it is a gate about something else.

What this pins:
  * every visible perceived control has SOME way to be addressed — a name or a testid;
  * every claimed name RESOLVES against Playwright's engine (the ADR-094 property, one field along);
  * the page model loses nothing except controls that genuinely have no ARIA role;
  * a label's text names the control WITHOUT the control's own subtree — the case that separates
    "Theme:" from "Theme: one two".
"""
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "testdata" / "fixtures"


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


def _url(name: str) -> str:
    return "file://" + str(FIXTURES / name)


def _fixtures() -> list:
    return sorted(p.name for p in FIXTURES.glob("*.html"))


def _all_elements() -> "dict | None":
    calls, names = [], []
    for fx in _fixtures():
        calls += [("browser.navigate", {"url": _url(fx)}), ("browser.interactives", {})]
        names.append(fx)
    res = _drive(calls)
    if res is None:
        return None
    return {fx: res[2 * i + 1]["elements"] for i, fx in enumerate(names)}


# --- the load-bearing check -------------------------------------------------------------------------
def test_every_visible_control_can_be_addressed_somehow():
    """A control we perceive and cannot name is a control we silently discard.

    Deliberately NOT skipping the nameless ones — skipping them is exactly what let this live. The
    one legitimate exception is a control with no ARIA role at all, which the brain drops on purpose
    and which is never planned against."""
    per_fixture = _all_elements()
    if per_fixture is None:
        return
    nameless, checked = [], 0
    for fx, els in per_fixture.items():
        for e in els:
            if not e.get("role"):
                continue          # no role -> not a control; dropped deliberately (ADR-094)
            if not e.get("visible"):
                continue          # off screen -> not in the accessibility tree; a visibility fact
            checked += 1
            if not (e.get("name") or "").strip() and not e.get("testid"):
                nameless.append(f"{fx}: <{e.get('tag')}> role={e.get('role')!r} has no name and no testid")
    assert checked >= 80, f"only {checked} controls examined; the corpus sweep has shrunk"
    assert not nameless, (
        "controls the tool perceives but cannot address:\n  " + "\n  ".join(nameless) +
        "\nEach of these is dropped by the brain's no-anchor guard, silently, after being seen.")


def test_the_page_model_keeps_everything_that_has_a_role():
    """The count that made this visible: `len(model)` was smaller than `audit.seen` on five of
    thirteen fixtures and nothing said so.

    Asserted against the audit rather than a constant — the two are produced by different code paths,
    so an equality between them cannot be satisfied by adjusting one number."""
    sys.path.insert(0, str(REPO))
    from brain.graph import _elements_from_interactives
    for fx in _fixtures():
        res = _drive([("browser.navigate", {"url": _url(fx)}),
                      ("browser.interactives", {}), ("browser.perceptionAudit", {})])
        if res is None:
            return
        _, inter, audit = res
        raw = inter["elements"]
        model = _elements_from_interactives(raw, "/" + fx)
        roleless = [e for e in raw if not e.get("role")]
        assert len(model) == audit["seen"] - len(roleless), (
            f"{fx}: the executor saw {audit['seen']}, {len(roleless)} have no role, and "
            f"{len(model)} reached the page model. The difference is controls we perceived and then "
            "discarded for want of a name.")


def test_every_claimed_name_resolves_on_the_page():
    """The ADR-094 property, one field along: we claim a name, the engine has to agree.

    A table of expected names would be written by the same reasoning that produced the bug."""
    per_fixture = _all_elements()
    if per_fixture is None:
        return
    probes, meta = [], []
    for fx, els in per_fixture.items():
        probes.append(("browser.navigate", {"url": _url(fx)}))
        meta.append(None)
        for e in els:
            if not e.get("role") or not e.get("visible") or not (e.get("name") or "").strip():
                continue
            loc = {"role": e["role"], "name": e["name"]}
            if e.get("frame"):
                loc["frame"] = e["frame"]
            probes.append(("browser.probe", {"locator": loc}))
            meta.append((fx, e["role"], e["name"]))
    res = _drive(probes)
    if res is None:
        return
    bad, checked = [], 0
    for m, r in zip(meta, res):
        if m is None:
            continue
        checked += 1
        if r.get("count", 0) < 1:
            bad.append(f"{m[0]}: role={m[1]!r} name={m[2]!r} resolves 0")
    assert checked >= 80, f"only {checked} names checked; too small a sweep to mean anything"
    assert not bad, "names claimed but not present on the page:\n  " + "\n  ".join(bad)


def test_a_label_names_the_control_without_the_controls_own_subtree():
    """The case that separates a working name from a plausible one.

    A `<select>` wrapped in its `<label>` contributes its OPTIONS to that label's text, so the naive
    read gives "WrappedSelect: one two" while the accessibility tree says "WrappedSelect:". The naive
    name resolves to ZERO — `getByRole` looks for the given string INSIDE the real one, and a superset
    is not a substring.

    `l9` carries a VISIBLE one on purpose: the only other wrapped select in this repository sits in
    l5's hidden Settings panel, where nothing is in the accessibility tree and no probe can tell the
    two computations apart."""
    res = _drive([("browser.navigate", {"url": _url("l9-roles.html")}),
                  ("browser.interactives", {})])
    if res is None:
        return
    els = res[1]["elements"]
    wrapped = [e for e in els if (e.get("name") or "").startswith("WrappedSelect")]
    assert wrapped, f"l9 §3 lost its wrapped select; got {[e.get('name') for e in els]}"
    assert len(wrapped) == 1, wrapped
    name = wrapped[0]["name"]
    assert name == "WrappedSelect:", (
        f"the wrapped select is named {name!r}. Its own options are not its label — the accessibility "
        "tree says 'WrappedSelect:', and a name that carries the option text resolves to nothing.")
    assert "one" not in name and "two" not in name, name

    res2 = _drive([("browser.navigate", {"url": _url("l9-roles.html")}),
                   ("browser.probe", {"locator": {"role": "combobox", "name": name}}),
                   ("browser.probe", {"locator": {"role": "combobox",
                                                  "name": "WrappedSelect: one two"}})])
    if res2 is None:
        return
    assert res2[1]["count"] == 1, f"the corrected name resolves {res2[1]['count']}"
    assert res2[2]["count"] == 0, (
        "the naive name resolves too — then this distinction costs nothing and the check proves "
        "nothing. It resolved 0 when measured.")


def test_the_fallback_name_sources_are_the_ones_the_browser_uses():
    """`l9` §3b exists because both fallbacks had SURVIVING mutations: every other control in the
    corpus carries a label or an aria-label, so neither branch was ever reached and deleting either
    changed nothing anywhere.

    Both were then measured against `ariaSnapshot` rather than assumed:
      * a placeholder-only input IS named by its placeholder — `textbox "SearchPlaceholder"`;
      * an unlabelled `<select>` has NO name — a bare `- combobox:` — and naming it after its options
        yields a locator that looks specific and resolves to zero.
    """
    res = _drive([("browser.navigate", {"url": _url("l9-roles.html")}),
                  ("browser.interactives", {}),
                  ("browser.probe", {"locator": {"role": "textbox", "name": "SearchPlaceholder"}}),
                  ("browser.probe", {"locator": {"role": "combobox", "name": "alpha beta"}})])
    if res is None:
        return
    _, inter, by_ph, by_options = res
    els = inter["elements"]

    ph = [e for e in els if e.get("name") == "SearchPlaceholder"]
    assert ph, ("the placeholder-only input is nameless; the placeholder fallback is gone. Got "
                f"{[e.get('name') for e in els if e.get('tag') == 'input']}")
    assert by_ph["count"] == 1, f"the placeholder name resolves {by_ph['count']}"

    unlabelled = [e for e in els if e.get("testid") == "unlabelled-select"]
    assert unlabelled, "l9 §3b lost its unlabelled select; this check is vacuous"
    assert unlabelled[0]["name"] == "", (
        f"the unlabelled select is named {unlabelled[0]['name']!r}. Its options are choices, not a "
        "label — the accessibility tree gives it no name at all.")
    assert by_options["count"] == 0, (
        "naming a select after its options resolves anyway — then the guard costs nothing and this "
        "check proves nothing. It resolved 0 when measured against the engine.")


def test_a_form_field_is_named_by_its_label_in_both_spellings():
    """`<label for=id>` and a wrapping `<label>` are both how forms are written, and only one of them
    is reachable with `querySelector('label[for=…]')`. The DOM's own `labels` covers both, which is
    why it is used instead of a hand-rolled lookup.

    Anchored on named controls from two fixtures, so a regression in either spelling shows up."""
    res = _drive([("browser.navigate", {"url": _url("l2.html")}), ("browser.interactives", {}),
                  ("browser.navigate", {"url": _url("l5.html")}), ("browser.interactives", {})])
    if res is None:
        return
    l2 = {(e.get("name") or ""): e for e in res[1]["elements"]}
    l5 = {(e.get("name") or ""): e for e in res[3]["elements"]}
    # l2 uses <label for=…>
    assert "Username" in l2, f"l2's labelled username field is nameless; got {sorted(l2)}"
    assert "Password" in l2, sorted(l2)
    assert l2["Username"]["role"] == "textbox", l2["Username"]
    # l5 uses a WRAPPING label
    assert any(n.startswith("Enable notifications") for n in l5), (
        f"l5's wrapping-label checkbox is nameless; got {sorted(l5)}")
    assert any(n.startswith("Dark mode") for n in l5), sorted(l5)


def test_both_perception_surfaces_report_the_same_name():
    """`browser.setOfMarks` feeds the visual heal tier through the same `descriptor_to_locator`. Two
    name computations would put the visual tier and the text tier on different pages — the asymmetry
    ADR-093 removed for visibility and ADR-094 for roles, one field further along."""
    for fx in ("l2.html", "l3.html", "l9-roles.html"):
        res = _drive([("browser.navigate", {"url": _url(fx)}),
                      ("browser.interactives", {}), ("browser.setOfMarks", {})])
        if res is None:
            return
        _, inter, som = res
        on_screen = {(e.get("name") or "").strip() for e in inter["elements"] if e.get("visible")}
        marks = {(m.get("name") or "").strip() for m in som["marks"]}
        assert on_screen == marks, (
            f"{fx}: the two perception surfaces name controls differently.\n"
            f"  only in interactives: {sorted(on_screen - marks)}\n"
            f"  only in set-of-marks: {sorted(marks - on_screen)}")


def test_a_select_is_not_named_by_its_options():
    """The wrong-name case, checked where the corpus actually has it.

    `l5`'s theme picker read "System default Light Dark" — a name that looks specific and matches
    nothing. It is hidden, so it cannot be probed; the assertion is therefore on the NAME, which is
    what was wrong, and the resolvability half is carried by `l9`'s visible one above."""
    res = _drive([("browser.navigate", {"url": _url("l5.html")}), ("browser.interactives", {})])
    if res is None:
        return
    sel = [e for e in res[1]["elements"] if e.get("tag") == "select"]
    assert sel, "l5 lost its <select>; this check is vacuous"
    for e in sel:
        assert "System default" not in e["name"], (
            f"the select is named by its options: {e['name']!r}. Those are choices, not a label.")
        assert e["name"] == "Theme:", e["name"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} accessible-name tests passed")
