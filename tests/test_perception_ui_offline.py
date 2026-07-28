"""Offline gate: the visibility measurement REACHES a person.

Run:  .venv/bin/python tests/test_perception_ui_offline.py

ADR-092 measured how much of a page the tool can see. ADR-093 made the measurement true. Neither made
it VISIBLE: `plan.json` carried `perception.worst_ratio` and the only reader in the entire repository
was a test. Go's plan decoder dropped the block at unmarshal, `report.html` read a different artefact
altogether, and the hub printed `coverage NN%` beside nothing at all — so the product kept reporting
a fraction without saying what it was a fraction of.

What this pins:
  * the breakdown DECOMPOSES — usable + blocked + no_role + unseen == the audit's own denominator.
    A breakdown that does not add up is decoration, and a category could otherwise absorb another
    without any test noticing;
  * "seen" and "usable" are different numbers, and the split is the brain's to make (the executor
    reports per-control facts; what they mean for planning is not its question);
  * an unmeasured page renders as unmeasured, never as a percentage — the ADR-092 rule, restated at
    each surface that now shows the number;
  * `report.html` reads plan.json, because the audit runs on the explore path and the heal report is
    written on the replay path — a report built from one can never mention the other;
  * a replay, which never audits, produces no section rather than a section full of zeroes.
"""
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "testdata" / "fixtures"
sys.path.insert(0, str(REPO))


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


# --- the load-bearing check -------------------------------------------------------------------------
def test_the_breakdown_adds_up_to_what_the_audit_measured():
    """Three categories that sum to the denominator, on EVERY fixture.

    This is what makes the breakdown a measurement rather than three plausible numbers. Driven through
    the real executor and the real brain, across the whole corpus, because the identity has to hold
    for a page with frames (l10), a page that is mostly blind spot (l8) and a page with hidden panels
    (l5) — each puts a different category to work, and any one of them alone would leave two
    categories unexercised."""
    from brain.graph import _elements_from_interactives, _perception_audit

    class _Ex:
        def __init__(self, results):
            self.results = results

        def call(self, method, **kw):
            return self.results[method]

    for fx in sorted(p.name for p in FIXTURES.glob("*.html")):
        res = _drive([("browser.navigate", {"url": "file://" + str(FIXTURES / fx)}),
                      ("browser.interactives", {}), ("browser.perceptionAudit", {})])
        if res is None:
            return
        _, inter, audit = res
        els = _elements_from_interactives(inter["elements"], "/" + fx)
        a = _perception_audit(_Ex({"browser.perceptionAudit": audit}), "/" + fx, els)
        parts = (a["usable"], a["blocked"], a["no_role"],
                 a["unseen"]["outside_selector"], a["unseen"]["iframe"])
        assert sum(parts) == a["total"], (
            f"{fx}: usable+blocked+no_role+outside+iframe = {sum(parts)} but the audit's denominator "
            f"is {a['total']}. A breakdown that does not decompose is decoration: a category can "
            f"absorb another and nothing notices. Parts: {parts}")
        assert a["usable"] + a["blocked"] + a["no_role"] == a["seen"], (
            f"{fx}: the seen side does not decompose either — {a['seen']} seen against "
            f"{a['usable']}+{a['blocked']}+{a['no_role']}")


def test_the_graph_actually_hands_the_page_model_to_the_audit():
    """The CALL SITE, which the check above does not touch.

    `test_the_breakdown_adds_up_to_what_the_audit_measured` calls `_perception_audit` directly with a
    page model, so it proves the function splits correctly and says nothing about whether `ground`
    ever gives it one. Dropping the argument leaves every other check here green and every real run
    without a breakdown. Caught by mutation; the same hole ADR-095 found one layer down, where the
    plan path was untested while the heal path was covered.

    Asserted on the call LINE rather than on the file: the docstring above it necessarily describes
    the argument, and a document-wide search would be satisfied by the prose explaining the
    requirement instead of the code meeting it."""
    src = (REPO / "brain" / "graph.py").read_text()
    i = src.index("perception[path] = _perception_audit(")
    line = src[i:src.index("\n", i)]
    assert "elements" in line, (
        f"`ground` calls the audit without the page model, so no run can produce a breakdown: {line.strip()}")
    # ...and the function must still work without one: a caller that has no model (a future surface,
    # or an older path) gets the audit rather than a crash.
    from brain.graph import _perception_audit

    class _Ex:
        def call(self, *a, **k):
            return {"seen": 1, "total": 1, "ratio": 1.0, "unseen": {}, "opaque": {}}

    bare = _perception_audit(_Ex(), "/p")
    assert "usable" not in bare, "with no page model there is no split to report, not a zeroed one"
    assert bare["ratio"] == 1.0


def test_the_hub_actually_renders_the_block_it_defines():
    """The other call site. The DOM gate exercises `perceptionBlock` through a named test seam, which
    proves the formatter and says nothing about whether the page ever calls it.

    This is the same hole a mutation found one file over, in `graph.py`: the function was covered and
    the call was not, so removing the call left every check green and every real run without the
    thing being tested. Anchored on the call line and on the assignment that commits the strip to the
    DOM — a function that is called into a string nobody renders is not rendered."""
    src = (REPO / "docs" / "index.html").read_text()
    assert "perceptionBlock(plan)" in src, "the hub defines the block and never calls it"
    i = src.index("html+=perceptionBlock(plan)")
    # It has to land in the same `html` the phases strip commits, not in a local nobody reads.
    tail = src[i:i + 3000]
    assert "box.innerHTML=" in tail and "+html+" in tail, (
        "the block is built into a string that is never written to the DOM")
    # And the seam stays a seam: it exists for pure functions, and a stateful member would be the
    # signal the gate has started testing the wrong thing (the comment beside it says so).
    assert "window.__gate = { perceptionBlock: perceptionBlock };" in src, \
        "the test seam changed shape; the DOM gate reaches the formatter through exactly this name"


def test_seen_and_usable_are_different_numbers():
    """`l5.html` is fully perceived and a third of it cannot be touched.

    Without this the two could be the same field under two names, and every downstream sentence about
    "seen, cannot act" would be describing nothing. Its controls sit in closed tab panels, so it is
    the corpus's one page where perfect visibility and partial usability coexist."""
    from brain.graph import _elements_from_interactives, _perception_audit

    class _Ex:
        def __init__(self, a):
            self.a = a

        def call(self, method, **kw):
            return self.a

    res = _drive([("browser.navigate", {"url": "file://" + str(FIXTURES / "l5.html")}),
                  ("browser.interactives", {}), ("browser.perceptionAudit", {})])
    if res is None:
        return
    _, inter, audit = res
    els = _elements_from_interactives(inter["elements"], "/l5")
    a = _perception_audit(_Ex(audit), "/l5", els)
    assert a["ratio"] == 1.0, f"l5 is fully perceived; got {a['ratio']}"
    assert a["blocked"] > 0, (
        "l5's controls in closed tab panels must count as seen-but-not-usable; with zero here the "
        "distinction this whole section rests on is untested")
    assert a["usable"] < a["seen"], (a["usable"], a["seen"])


def test_an_unmeasured_page_never_renders_as_a_percentage():
    """The ADR-092 rule, restated at the surface that now prints the number.

    An older executor fails open with `ratio: None`. A null that renders like 100% is the reassuring
    number this line of work exists to remove — and it would be a NEW instance of it, produced by the
    very code meant to prevent it."""
    from brain.report import _html
    rep = {"mode": "replay", "plan_id": "x", "exit_code": 0, "healed": 0, "failed": 0, "steps": [],
           "perception": {"worst_ratio": None,
                          "pages": {"/p": {"ratio": None, "reason": "executor too old"}}}}
    h = _html(rep)
    assert "Page visibility" in h, "an unmeasured page must still say something"
    # Anchored on the SECTION, not the document: the stylesheet contains `width:100%`, so a
    # document-wide search for "100%" is satisfied by CSS and would pass over a section that really
    # did print a percentage. (This assertion failed exactly that way when first written — the fourth
    # time this repository has stepped into "assert the cell, not the document".)
    section = h[h.index("<h2>Page visibility</h2>"):]
    section = section[:section.find("<h2>", 4) if section.find("<h2>", 4) > 0 else len(section)]
    assert "Not measured" in section, section[:250]
    assert "%" not in section, f"an unmeasured page rendered a percentage: {section[:250]}"
    assert "executor too old" in section, "the reason has to reach the reader, or 'not measured' is a shrug"

    # The positive control: a MEASURED page does print one, so the check above is about the null case
    # rather than about percentages being absent from reports altogether.
    rep["perception"] = {"worst_ratio": 0.5, "pages": {"/p": {"ratio": 0.5, "usable": 1, "blocked": 0,
                        "no_role": 0, "unseen": {"outside_selector": 1, "iframe": 0}, "opaque": {}}}}
    m = _html(rep)
    msec = m[m.index("<h2>Page visibility</h2>"):]
    assert "50%" in msec, msec[:250]


def test_a_run_that_never_audited_gets_no_section_rather_than_zeroes():
    """A replay does not audit. A section reading 0/0/0 would claim the tool saw nothing, which is a
    different and false statement from "this run did not ask"."""
    from brain.report import _html
    for perception in (None, {}, {"pages": {}}):
        rep = {"mode": "replay", "plan_id": "x", "exit_code": 0, "healed": 0, "failed": 0, "steps": []}
        if perception is not None:
            rep["perception"] = perception
        h = _html(rep)
        assert "Page visibility" not in h, f"a run with perception={perception!r} grew a section: {h[-300:]}"


def test_the_report_reads_the_plan_because_the_audit_runs_on_the_other_path():
    """`generate()` used to read `heal-report.json` alone. The audit runs during explore and the heal
    report is written during replay, so a report built from one can never mention the other.

    Exercised end to end through the real `generate()` on a temp run directory, then asserted on the
    HTML it wrote — the file is what a person opens."""
    import tempfile
    from brain.report import generate
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "heal-report.json").write_text(json.dumps(
        {"mode": "replay", "plan_id": "p1", "exit_code": 0, "healed": 0, "failed": 0, "steps": []}))
    (d / "plan.json").write_text(json.dumps({"perception": {
        "worst_ratio": 0.5,
        "pages": {"/p": {"ratio": 0.5, "usable": 3, "blocked": 1, "no_role": 0,
                         "unseen": {"outside_selector": 4, "iframe": 0}, "opaque": {"canvas": 2}}}}}))
    generate(str(d))
    h = (d / "report.html").read_text()
    assert "Page visibility" in h, "the report did not read plan.json"
    assert "50%" in h, h[h.find("Page visibility"):][:200]
    assert "canvas: 2" in h, "an uncountable zone must be named, apart from the three categories"
    # ...and the JSON mirror carries it too, or a machine reader sees a different run from a human one.
    assert "perception" in json.loads((d / "report.json").read_text()), "report.json lost the block"

    # A run with no plan.json at all must still produce a report: an imported plan replayed elsewhere
    # legitimately has no audit, and failing there would make the audit a prerequisite for reporting.
    d2 = pathlib.Path(tempfile.mkdtemp())
    (d2 / "heal-report.json").write_text(json.dumps(
        {"mode": "replay", "plan_id": "p2", "exit_code": 0, "healed": 0, "failed": 0, "steps": []}))
    generate(str(d2))
    assert (d2 / "report.html").exists(), "a run without plan.json must still get a report"
    assert "Page visibility" not in (d2 / "report.html").read_text()


def test_the_go_decoder_keeps_the_block_it_used_to_drop():
    """`planCoverage` decoded four fields and dropped `perception` at unmarshal, which is why
    `worst_ratio` had exactly one reader in the repository and it was a test.

    Asserted on the struct definition and on the emit site, because the alternative is a Go test in a
    Python suite. The pointer matters and is checked by name: a `float64` would make "never measured"
    indistinguishable from "measured at zero" — the same defect as a null reading as 1.0, pointing the
    other way."""
    src = (REPO / "cmd" / "control-api" / "main.go").read_text()

    def _struct(name):
        """The struct body, cut at a brace that STARTS A LINE.

        Cutting at the first `}` finds the one inside the comment `{plan: <model id>}` on the Models
        field and stops before the field under test — the check then reports the opposite of the
        truth. Found by this assertion failing against correct code."""
        i = src.index("type " + name + " struct")
        return src[i:src.index("\n}", i)]

    body = _struct("planCoverage")
    assert "Perception" in body, f"planCoverage still drops the perception block:\n{body}"
    pbody = _struct("planPerception")
    assert "*float64" in pbody, (
        f"worst_ratio must be a POINTER — nil is 'never measured', which is not 0.0:\n{pbody}")
    # And the number has to leave the decoder, or decoding it changes nothing.
    assert 'metricKV{"visibility"' in src, "the visibility metric point is not emitted"
    k = src.index('metricKV{"visibility"')
    guard = src[max(0, k - 300):k]
    assert "visibility != nil" in guard, (
        "the metric must be emitted only when measured; a series that gains a 0 from every replay "
        f"says the tool went blind, when in fact a replay never asks:\n{guard[-200:]}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} perception-UI tests passed")
