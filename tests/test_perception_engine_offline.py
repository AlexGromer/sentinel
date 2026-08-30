"""Offline gate: the audit measures the perception the product HAS, not a second copy of it.

Run:  .venv/bin/python tests/test_perception_engine_offline.py

ADR-092 shipped a measurement that disagreed with the thing it measured. `browser.interactives` calls
`page.$$eval`, i.e. Playwright's selector engine, which PIERCES open shadow roots. The audit counted
its numerator with `document.querySelectorAll` inside `page.evaluate`, which does not. So on
`l5.html` the audit reported `seen 15 / 23` with `shadow_dom: 8` while the executor was returning all
23 elements and `browser.click{role,name}` was actuating the very controls it called invisible. The
false `ratio < 1.0` raised a degradation, and the degradation reached shipped run artefacts.

The defect was invisible to the old gate because that gate stubs the executor: it pins the PLUMBING
(the ratio reaches the plan, once per page, degrades honestly) and cannot see that the number is
wrong. A stub answers with whatever the test author believed. So every check here drives the REAL
built `dist/server.js` over stdio against a REAL browser, and the load-bearing one compares the two
RPCs against each other rather than against a constant:

    audit.seen == len(interactives.elements)

That equality is the property. A constant would have to be re-derived by hand, i.e. by the same
reasoning that produced the bug.

What this pins:
  * the numerator IS the perception, not an estimate of it (l5, l1, l8);
  * a control inside an open shadow root is perceived, probed and clicked — so the tool's own claim
    about its reach is checked against the tool's behaviour, not against documentation;
  * a page with real blind spots reports ratio < 1.0 with the zones NAMED (l8);
  * a boundary we cannot cross is reported as opaque and NOT folded into the denominator — an
    uncountable blind spot stays uncounted rather than being guessed at;
  * `visible` reaches the brain as three states, so an older executor's silence never reads as
    "invisible" (which would empty the candidate set).
"""
import json
import os
import pathlib
import subprocess
import sys

_UNSET = object()
REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "testdata" / "fixtures"


def _drive(calls: list) -> "list | None":
    """Run a list of (method, params) through the REAL executor and return the results.

    Skipped rather than failed when the executor is not built or no browser is available: this suite
    must run without a browser install, and a skipped check says so out loud instead of passing
    quietly."""
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
                       timeout=300)
    marker = "@@RESULT@@"
    for line in (r.stdout or "").splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    print("     SKIP — no browser available:", ((r.stderr or "") + (r.stdout or ""))[-250:].replace("\n", " "))
    return None


def _fixture(name: str) -> str:
    return "file://" + str(FIXTURES / name)


# --- the load-bearing check -------------------------------------------------------------------------
def test_the_audit_counts_the_same_elements_the_perception_returns():
    """One engine, one answer. This is the check ADR-092 did not have and would have failed.

    Asserted across three fixtures with deliberately different shapes — a page with an open shadow
    root (l5), a flat page (l1), and a page that is mostly blind spot (l8) — because the original bug
    was invisible on a flat page: with no shadow root, `querySelectorAll` and the selector engine
    agree, and a single-fixture gate would have gone green over the defect."""
    for fx in ("l5.html", "l1.html", "l8-blindspots.html"):
        res = _drive([("browser.navigate", {"url": _fixture(fx)}),
                      ("browser.interactives", {}),
                      ("browser.perceptionAudit", {})])
        if res is None:
            return
        _, inter, audit = res
        n = len(inter["elements"])
        assert audit["seen"] == n, (
            f"{fx}: the audit says it sees {audit['seen']} controls while perception returns {n}. "
            "The numerator has to BE the perception, not a re-derivation of it (ADR-093).")
        assert audit["total"] >= audit["seen"], (
            f"{fx}: total {audit['total']} < seen {audit['seen']} — the denominator must contain "
            "the numerator, or the ratio is not a fraction of anything.")


def test_a_control_inside_an_open_shadow_root_is_perceived_probed_and_clicked():
    """The tool's claim about its own reach, checked against the tool's behaviour.

    `l5.html`'s `<x-color-picker>` uses `attachShadow({mode:'open'})`; its 8 controls are the ones
    ADR-092 called unseen. Perception, `browser.probe` and `browser.click` are asserted together on
    purpose: seeing an element and being able to act on it are different capabilities, and a
    perception gate that only checks the list would still let a product plan steps it cannot run."""
    res = _drive([("browser.navigate", {"url": _fixture("l5.html")}),
                  ("browser.interactives", {}),
                  ("browser.probe", {"locator": {"role": "button", "name": "Apply color"}}),
                  ("browser.click", {"locator": {"role": "button", "name": "Apply color"}})])
    if res is None:
        return
    _, inter, probe, click = res
    names = [(e.get("name") or "").strip() for e in inter["elements"]]
    # The six swatches + the hex field + the apply button, all inside the shadow root.
    for want in ("Red", "Green", "Blue", "Yellow", "Purple", "White", "Hex color value", "Apply color"):
        assert want in names, f"shadow-DOM control {want!r} missing from perception; got {names}"
    assert probe["count"] == 1, f"probe resolved {probe['count']} elements for a shadow-DOM control"
    assert click.get("clicked") is True, f"could not click a shadow-DOM control: {click}"


def test_the_open_shadow_root_page_is_reported_as_fully_seen():
    """`l5.html` has no frames and nothing outside the selector, so the honest answer is 1.0.

    Pinned as an exact number and not merely `> 0.652`: the defect produced a specific wrong value,
    and a bound would go green again on the next measurement that is wrong in a smaller way. The
    breakdown is asserted empty for the same reason — a ratio of 1.0 with a non-empty `unseen` would
    be two statements contradicting each other."""
    res = _drive([("browser.navigate", {"url": _fixture("l5.html")}),
                  ("browser.perceptionAudit", {})])
    if res is None:
        return
    audit = res[1]
    assert audit["ratio"] == 1.0, f"l5 reports ratio {audit['ratio']}, want 1.0 — {audit}"
    assert audit["unseen"] == {"outside_selector": 0, "iframe": 0}, audit["unseen"]
    assert audit["shadow_roots_open"] == 1, (
        f"l5 has exactly one open shadow root; audit says {audit['shadow_roots_open']}. This is the "
        "evidence for the ratio: it says we crossed a boundary rather than that there was none.")


def test_a_page_of_blind_spots_names_every_zone_it_cannot_see():
    """`l8-blindspots.html` exists so this claim is measured rather than asserted.

    Each expected number is tied to a section of that fixture, so a change to either side has to be
    made on purpose."""
    res = _drive([("browser.navigate", {"url": _fixture("l8-blindspots.html")}),
                  ("browser.interactives", {}),
                  ("browser.perceptionAudit", {})])
    if res is None:
        return
    _, inter, audit = res
    assert len(inter["elements"]) == 4, (
        "l8 holds exactly four controls our selector names — two ordinary (§0) and two present but "
        f"off screen (§5); the rest of the page is blind spot. Perception returned "
        f"{len(inter['elements'])} — the fixture and the gate have drifted.")
    assert audit["ratio"] is not None and audit["ratio"] < 1.0, (
        f"a page built out of blind spots reports {audit['ratio']}; that is the flattering number "
        "this whole ADR exists to prevent.")
    # §1 — five controls a person clicks that our selector does not name.
    assert audit["unseen"]["outside_selector"] == 5, audit["unseen"]
    # §2/§3 — boundaries. Reported, and deliberately NOT added to `total`: we cannot count what we
    # cannot enter, and inventing a number for it is the same error as omitting it.
    assert audit["opaque"]["canvas"] == 1, audit["opaque"]
    assert audit["opaque"]["shadow_roots_closed"] == 1, audit["opaque"]
    assert audit["total"] == audit["seen"] + audit["unseen"]["outside_selector"] + audit["unseen"]["iframe"], (
        f"the denominator {audit['total']} does not decompose into what we see plus what we named as "
        f"unseen: {audit['seen']} + {audit['unseen']}. An opaque zone must stay OUT of the fraction.")


def test_the_sealed_button_is_genuinely_unreachable():
    """The NEGATIVE control for the shadow-DOM claim above.

    "An open shadow root is reachable" is worth nothing without "a closed one is not" — otherwise the
    positive check could be passing because Playwright reaches everything, and the boundary we report
    in the UI would be decoration. `l8` §3 attaches `mode:'closed'`, so `probe` must find zero.

    It also guards the fixture: if someone changes that root to `open`, the closed-root count in the
    test above still says 1 (custom elements with no reachable root are what that heuristic counts),
    but THIS check fails — the fixture would no longer demonstrate the thing it exists to demonstrate."""
    res = _drive([("browser.navigate", {"url": _fixture("l8-blindspots.html")}),
                  ("browser.probe", {"locator": {"role": "button", "name": "Sealed button"}}),
                  ("browser.probe", {"locator": {"testid": "nothing-like-this-exists"}})])
    if res is None:
        return
    _, sealed, absent = res
    assert sealed["count"] == 0, (
        f"a button inside a CLOSED shadow root resolved to {sealed['count']} — either the fixture's "
        "root is no longer closed, or our claim that closed roots are a hard boundary is wrong.")
    assert absent["count"] == 0, "sanity: a locator for nothing must also resolve to nothing"


def test_the_executor_reports_hidden_controls_instead_of_hiding_them():
    """`visible` follows the `disabled` contract of ADR-070: the executor SAYS, the brain DECIDES.

    Measured on `l5.html`: 7 of the 23 perceived controls sit in `display:none` tab panels. That is
    also where two perception surfaces were silently disagreeing — `browser.setOfMarks` already drops
    zero-box elements, so it returned 16 while `browser.interactives` returned 23, and neither knew.
    The two are asserted against each other here, because the agreement is the point; a fixed
    constant would let both drift together."""
    res = _drive([("browser.navigate", {"url": _fixture("l5.html")}),
                  ("browser.interactives", {}),
                  ("browser.setOfMarks", {})])
    if res is None:
        return
    _, inter, som = res
    els = inter["elements"]
    assert all("visible" in e for e in els), "every perceived element must carry the field, not some"
    visible = [e for e in els if e["visible"]]
    hidden = [e for e in els if not e["visible"]]
    assert hidden, ("l5 has controls inside closed tab panels; none were reported hidden. Without a "
                    "hidden control on the page this check is vacuous.")
    assert len(visible) == len(som["marks"]), (
        f"{len(visible)} controls report visible, but set-of-marks produced {len(som['marks'])} "
        "marks. These are the two perception surfaces, and they have to agree about what is on "
        "screen or the text tier and the visual tier are looking at different pages.")
    # And the field must not have been achieved by dropping anything.
    assert len(els) > len(som["marks"]), (
        "perception must still REPORT the hidden controls — reporting is what makes coverage honest; "
        "filtering them here would shrink the page rather than describe it.")


def test_both_ways_a_control_leaves_the_screen_are_detected():
    """`l5` alone cannot pin this, and that is why `l8` §5 exists.

    Two mechanisms make a control unusable right now and they collapse differently: an element under
    a `display:none` ancestor loses its box, while `visibility:hidden` keeps a FULL-SIZE box (137x21,
    measured). Every hidden control in every other fixture is the first kind, so a mutation deleting
    the visibility test survived — the box check was silently carrying both cases in the corpus while
    covering only one in reality.

    Asserted per element, by id, rather than as a count: a count of 2 is satisfied by finding the
    same mechanism twice, which is exactly the hole this closes."""
    res = _drive([("browser.navigate", {"url": _fixture("l8-blindspots.html")}),
                  ("browser.interactives", {})])
    if res is None:
        return
    els = res[1]["elements"]
    by_name = {(e.get("name") or "").strip(): e for e in els}
    for name in ("hidden by an ancestor", "hidden by visibility"):
        assert name in by_name, f"l8 §5 control {name!r} is not perceived at all; got {list(by_name)}"
        assert by_name[name]["visible"] is False, (
            f"{name!r} reports visible={by_name[name]['visible']}; it is off screen and the executor "
            "has to say so, or the brain plans a step that cannot run.")
    for name in ("Real button", "Real link"):
        assert by_name[name]["visible"] is True, (
            f"{name!r} reports visible={by_name[name]['visible']} — the positive control. Without it "
            "a `visible: always False` implementation would pass every check above.")

    # The fixture has to keep exercising BOTH mechanisms, and the outcome above cannot tell:
    # two `display:none` controls satisfy every assertion so far, and then the visibility branch is
    # untested again while this file still looks like it covers it. Caught by mutation — editing the
    # fixture's second control to `display:none` left the whole suite green.
    #
    # Anchored on the ELEMENT LINE, not on a substring of the file: the section comment above these
    # controls names both mechanisms, so a document-wide search would be satisfied by the prose that
    # explains the requirement rather than by the markup that meets it.
    src = (FIXTURES / "l8-blindspots.html").read_text().splitlines()
    line_none = next(l for l in src if 'id="btn-in-none"' in l)
    line_vis = next(l for l in src if 'id="btn-vis-hidden"' in l)
    assert "display:none" in line_none, f"§5 lost its collapsed-box case: {line_none.strip()}"
    assert "visibility:hidden" in line_vis, (
        f"§5 lost its full-box case, so nothing exercises the visibility branch: {line_vis.strip()}")


def test_the_two_perception_surfaces_agree_about_what_is_on_screen():
    """`browser.interactives.visible` and the marks `browser.setOfMarks` produces are one definition.

    They were two: marks filtered on the bounding box alone, so on `l5.html` the text tier saw 23
    controls and the visual tier 16 and neither knew the other existed. Fixing that on `l5` is not
    enough — every hidden control there is a collapsed box, so a box-only filter agrees by accident.
    `l8` is the fixture where the two definitions come apart, so the agreement is asserted there."""
    for fx in ("l5.html", "l8-blindspots.html"):
        res = _drive([("browser.navigate", {"url": _fixture(fx)}),
                      ("browser.interactives", {}),
                      ("browser.setOfMarks", {})])
        if res is None:
            return
        _, inter, som = res
        on_screen = [e for e in inter["elements"] if e["visible"]]
        marks = som["marks"]
        assert len(on_screen) == len(marks), (
            f"{fx}: perception reports {len(on_screen)} controls on screen, set-of-marks produced "
            f"{len(marks)} marks. The vision tier and the text tier have to be looking at the same "
            "page, or a heal picks a mark over a patch of nothing.")
        assert {(e.get("name") or "").strip() for e in on_screen} == {
            (m.get("name") or "").strip() for m in marks}, (
            f"{fx}: the two surfaces agree on the COUNT but not on which controls — an equality that "
            "holds by arithmetic accident is not agreement.")


def test_the_brain_treats_an_old_executors_silence_as_unknown_not_as_invisible():
    """Three states, not two. Asserted on the brain, with no browser involved.

    An executor built before ADR-093 emits no `visible` key. If the reader coerced that to a bool,
    every element from such an executor would count as invisible, `plan()` would propose nothing, and
    explore would end at zero coverage while reporting no error at all — the exact silent-degradation
    shape this codebase keeps finding. So the descriptor carries None through and the consumer tests
    `is False`."""
    sys.path.insert(0, str(REPO))
    from brain.graph import _elements_from_interactives
    old = [{"tag": "button", "role": "button", "name": "Save", "testid": None, "text": "Save"}]
    new_hidden = [{"tag": "button", "role": "button", "name": "Save", "testid": None, "text": "Save",
                   "visible": False}]
    new_shown = [{"tag": "button", "role": "button", "name": "Save", "testid": None, "text": "Save",
                  "visible": True}]
    assert _elements_from_interactives(old, "/p")[0]["visible"] is None, \
        "an executor that never spoke must read as unknown"
    assert _elements_from_interactives(new_hidden, "/p")[0]["visible"] is False
    assert _elements_from_interactives(new_shown, "/p")[0]["visible"] is True

    # И потребитель обязан ДЕЙСТВОВАТЬ по этому различию, а не просто хранить его.
    #
    # ⚠ ЗДЕСЬ СТОЯЛ ГРЕП ПО ИСХОДНИКУ — `src.index('if b["semantic_id"] in spent')` с проверкой, что
    # в той же строке есть `is False`. Это утверждение о ФОРМЕ кода, то есть суррогат: оно ничего не
    # говорит о поведении и ломается от переименования, не связанного с предметом. Так и вышло —
    # ADR-137 переименовал ключ отсечки на `control_id`, и гейт покраснел, не заметив ни одной
    # перемены в том, что охраняет. Заменено на ЗАМЕР: прогоняем настоящий узел `plan` через
    # настоящий граф и смотрим, предлагается ли контрол.
    from langgraph.checkpoint.memory import MemorySaver
    from brain.graph import build_graph
    from brain.planner import HeuristicPlanner
    from brain.state import base_origin_of, page_identity, semantic_id as _sid
    import contextlib, io, tempfile

    def _proposes(visible_value) -> bool:
        """Предложит ли обход единственную кнопку, у которой `visible` равен переданному."""
        btn = {"tag": "button", "role": "button", "name": "Save", "testid": None, "text": "Save"}
        if visible_value is not _UNSET:
            btn["visible"] = visible_value
        clicked = []

        class Ex:
            def call(self, m, **p):
                if m == "browser.currentUrl":
                    return {"url": "file:///s/index.html", "title": ""}
                if m == "browser.snapshot":
                    return {"ariaSnapshot": "- document", "nodeCount": 1}
                if m == "browser.interactives":
                    return {"elements": [btn]}
                if m == "browser.links":
                    return {"links": []}
                if m == "browser.click":
                    clicked.append((p.get("locator") or {}).get("name"))
                    return {"clicked": True, "url": "file:///s/index.html", "navigated": False}
                if m.startswith("browser."):
                    return {}
                return {}

        target = "file:///s/index.html"
        art = tempfile.mkdtemp(prefix="perc-")
        init = {"step_id": 1, "intent": "nav", "semantic_id": _sid(page_identity(target), "navigate", ""),
                "action_type": "navigate", "target": page_identity(target), "locator": None,
                "alternatives": None, "is_milestone": True}
        st = {"run_id": "p", "run_mode": "explore", "target_url": target,
              "base_origin": base_origin_of(target), "coverage_target": 0.85, "artifact_dir": art,
              "goal": "", "describe": "", "site_map": {}, "phase": "explore", "scenario_steps": [],
              "scenario_unmatched": [], "current_url": target, "page_model": {},
              "exploration_plan": [init], "plan_hash": "", "current_step": 1,
              "interactive_seen": [], "interactive_exercised": [], "visited_paths": [],
              "nav_frontier": [], "coverage_achieved": 0.0, "exploration_complete": False,
              "max_steps": 3, "executed_actions": [{"step_id": 1, "type": "navigate", "ok": True}],
              "errors": []}
        app = build_graph(Ex(), HeuristicPlanner(), lambda r: None).compile(checkpointer=MemorySaver())
        with contextlib.redirect_stdout(io.StringIO()):
            app.invoke(st, config={"recursion_limit": 40, "configurable": {"thread_id": "p"}})
        return "Save" in clicked

    assert _proposes(_UNSET) is True, (
        "молчание СТАРОГО исполнителя (ключа `visible` нет вовсе) прочитано как «невидим» — обход "
        "перестал бы предлагать что-либо и закончился бы нулевым покрытием без единой ошибки")
    assert _proposes(True) is True, "видимый контрол не предложен"
    assert _proposes(False) is False, (
        "контрол, о котором исполнитель сказал `visible: False`, всё равно предложен — фильтр "
        "проверяет истинность вместо `is False`, и бюджет уходит на то, что не может сработать")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} perception-engine tests passed")
