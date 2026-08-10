#!/usr/bin/env python3
"""PLAN-NOT-GROUNDED-SILENT — a run that grounded NOTHING says so, shows what did not bind, and is
not signed with somebody else's name.

WHAT WAS MEASURED, twice, on a live model (qwen3:8b, 2026-08-09 and 2026-08-10):

    scenario_authored: Scenario authored: 0 steps grounded, 4 unmatched   -> process exit 1
    verdict: {"verdict":"ok","exit_code":0}                               <- the SAME log file

Three separate silences produced that, and this file pins all three:

 1. THE WORST OUTCOME WAS LOGGED LEAST. `plan.partially_grounded` is guarded by `sc and unmatched`,
    so grounding 3 of 10 warned and grounding 0 of 4 said nothing beyond a neutral counter. Partial
    grounding DEGRADES a result; zero grounding means there is no test at all, and collapsing them
    would let a reader take "some of it worked" from a run where none did.
 2. THE EVIDENCE WAS THROWN AWAY EXACTLY WHEN IT MATTERED. The per-ref list of what failed to bind
    was written for describe mode only; goal mode recorded the NUMBER and dropped the refs on the
    spot. The serialiser existed, the list existed, the file was already whitelisted — one `if`
    stood between a person and the answer.
 3. THE HEADLINE EVENT DISAGREED WITH THE EXIT CODE. The AG-UI `verdict` frame was computed from
    explore-phase `errors` alone, and the scenario node writes none — so the UI's headline said the
    run was fine while the process said it was not.

⚠ ASSERT THE CELL, NOT THE DOCUMENT. Each check below names the field it is about. A check that the
report "mentions" a ref would pass on a file that mentions it in a comment, which is how the earlier
`измерено` DOM check passed over literal markup.

Offline: no browser, no network, no model. Drives the SHIPPED functions, not copies.
"""
import io
import json
import os
import pathlib
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:400])


# The shape of a REAL failure, taken from the run that was measured rather than invented: the model
# answered with four dicts whose `ref` was a URL fragment, and no fragment existed in the site map.
REAL_UNMATCHED = [
    {"ref": "testdata/site/page-b.html", "reason": "ref not in site map"},
    {"ref": "testdata/site/page-a.html", "reason": "ref not in site map"},
    {"ref": "Action One", "reason": "ref not in site map"},
    {"ref": "Action Two", "reason": "ref not in site map"},
]


def author(grounded, unmatched, tmp, is_describe=False):
    """Drive the SHIPPED _write_scenario and capture what it said."""
    from brain.__main__ import _write_scenario
    steps = [{"action_type": "click", "intent": f"s{i}", "locator": {"role": "button", "name": f"b{i}"}}
             for i in range(grounded)]
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = _write_scenario(tmp, "r", "file:///s/app.html", steps, unmatched, is_describe)
    return rc, buf.getvalue()


def test_a_goal_run_that_grounded_nothing_says_so_and_shows_what_did_not_bind():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        rc, out = author(0, REAL_UNMATCHED, tmp)

        check("a run that grounded nothing exits 1 (there is no test to hand anybody)", rc == 1, rc)
        check("it emits plan.not_grounded — the worst outcome is no longer the quietest",
              "plan.not_grounded" in out, out[-400:])
        check("...naming HOW MANY did not bind", "4" in out, out[-400:])
        check("...and naming at least one of the refs, so the reader can look it up",
              "page-b.html" in out, out[-400:])
        check("it does NOT claim partial grounding — 'some of it worked' is a different fact",
              "plan.partially_grounded" not in out, out[-400:])

        rp = tmp / "reconcile-report.json"
        check("the evidence file is written in GOAL mode too (it used to be describe-only)", rp.exists())
        if rp.exists():
            j = json.loads(rp.read_text())
            # The CELL, not the document: a report that merely contains the string would pass a
            # containment check while carrying a count and no refs.
            check("...carrying the per-ref list, not a number",
                  isinstance(j.get("unmatched"), list) and len(j["unmatched"]) == 4, j.get("unmatched"))
            check("...with the ref a person can search for",
                  j["unmatched"][0]["ref"] == "testdata/site/page-b.html", j.get("unmatched"))
            check("...and the reason it did not bind",
                  "not in site map" in j["unmatched"][0].get("reason", ""), j.get("unmatched"))
            check("...and it says which mode produced it", j.get("mode") == "goal", j.get("mode"))
            check("...and grounded is the number of steps, not of refs", j.get("grounded") == 0, j.get("grounded"))


def test_the_diagnostic_never_raises_on_the_other_shape_of_unmatched():
    """`unmatched` arrives as dicts from scenario.py and as bare strings from older callers.

    Found by the FULL suite, not by this file: a neighbouring test passes strings, and the first
    draft of the new log line called `.get` on them. A diagnostic that raises is worse than the
    silence it replaces — it converts "the scenario did not ground" into a crash blamed on our own
    logging, in exactly the run that already had the least to show."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        rc, out = author(0, ["ref0", "ref1", "ref2"], tmp)
        check("a string-shaped unmatched list still exits 1", rc == 1, rc)
        check("...and still emits plan.not_grounded rather than a traceback",
              "plan.not_grounded" in out and "Traceback" not in out, out[-300:])
        check("...naming the refs it was given", "ref0" in out, out[-300:])


def test_partial_grounding_is_still_a_different_answer_from_none():
    """The old warning must keep working, and must NOT be replaced by the new one.

    Two failures that share a message are two failures nobody can tell apart."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        rc, out = author(3, REAL_UNMATCHED, tmp)
        check("a partly grounded goal run still exits 0 (a goal is a direction, not a spec)", rc == 0, rc)
        check("...and warns with plan.partially_grounded", "plan.partially_grounded" in out, out[-300:])
        check("...and NOT with plan.not_grounded, which means something else",
              "plan.not_grounded" not in out, out[-300:])
        check("...naming the proportion a person can act on",
              "3" in out and "4" in out and "7" in out, out[-300:])


def test_a_fully_grounded_run_stays_quiet_and_green():
    """The negative control. Without it, a gate that fires on everything would look like a gate."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        rc, out = author(5, [], tmp)
        check("a fully grounded run exits 0", rc == 0, rc)
        check("...and says neither of the two failure lines",
              "plan.not_grounded" not in out and "plan.partially_grounded" not in out, out[-300:])
        check("...but still writes the evidence file, so its absence never means 'not looked'",
              (tmp / "reconcile-report.json").exists())


def test_the_new_code_speaks_both_languages_and_blames_the_right_party():
    """A code the catalogue does not carry is rendered to the reader as `eventlog.uncatalogued`.

    And the attribution matters more than usual here: exit 1 in this product reads as "the test found
    a problem in YOUR application". An authoring failure is OURS, and the verdict text has to say so
    rather than send a person to debug an application that did nothing wrong."""
    cat = json.loads((pathlib.Path(ROOT) / "brain" / "events.json").read_text())
    ev = cat["events"].get("plan.not_grounded")
    check("plan.not_grounded is in the catalogue", ev is not None)
    if not ev:
        return
    check("...in Russian", bool(ev.get("ru")), ev.get("ru"))
    check("...and in English", bool(ev.get("en")), ev.get("en"))
    check("...at error level: there is no test, which is not a warning", ev.get("lvl") == "error", ev.get("lvl"))
    check("...and it is NOT marked `degrades` — degradation means less than asked, not nothing at all",
          "degrades" not in ev, ev.get("degrades"))
    verdict = (ev.get("ru_verdict") or "") + (ev.get("en_verdict") or "")
    check("...and the verdict text blames the AUTHORING, not the application under test",
          ("авторинг" in verdict.lower() or "authoring" in verdict.lower()), verdict[:200])


def test_the_headline_verdict_no_longer_contradicts_the_exit_code():
    """The third silence: the AG-UI `verdict` frame was computed from explore errors alone.

    ⚠ DRIVEN THROUGH THE REAL GRAPH, not asserted about the source. The first draft of this check
    re-implemented the rule inside the test and compared it to itself — a surrogate that any mutation
    in the product walks straight through, which is the exact failure this repository keeps meeting
    in its own tests. Here the shipped graph runs, its scenario head returns refs that exist in no
    site map (the measured failure), and the assertion is on the FRAME the UI would receive.
    """
    import json as _json
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_m9_2b_offline import WalkEx, _explore_init, _invoke, _LOGIN, FakeBackend  # noqa: E402
    from brain.planner import GoalPlanner  # noqa: E402
    from brain import budget  # noqa: E402

    budget.reset(plan_limit=100000, heal_limit=100000)
    ex = WalkEx()
    # Every ref is a URL fragment — exactly what the live model returned on the measured failure.
    fb = FakeBackend(_json.dumps({"steps": [
        {"ref": "testdata/site/page-b.html", "verb": "click"},
        {"ref": "Action One", "verb": "click"},
    ]}))
    ex.call("browser.navigate", url=_LOGIN)
    # ⚠ AG-UI frames go to STDOUT, the diagnostics log to STDERR. The first draft captured stderr and
    # found no verdict frame at all — and had the assertion been "no failed verdict", it would have
    # passed on an empty capture. Both streams are taken, so a stream that moves cannot make this
    # check pass by finding nothing.
    obuf, ebuf = io.StringIO(), io.StringIO()
    with redirect_stdout(obuf), redirect_stderr(ebuf):
        final = _invoke(ex, GoalPlanner(goal="open the actions page", backend=fb),
                        _explore_init(goal="open the actions page"))
    out = obuf.getvalue() + ebuf.getvalue()

    check("the run really did ground nothing (the fixture reproduces the measured failure)",
          not final.get("scenario_steps") and final.get("scenario_unmatched"),
          {"steps": final.get("scenario_steps"), "unmatched": final.get("scenario_unmatched")})

    frames = [_json.loads(l.split("@@AGUI ", 1)[1]) for l in out.splitlines() if "@@AGUI " in l]
    verdicts = [f for f in frames if f.get("type") == "verdict"]
    check("the run emitted a verdict frame at all", len(verdicts) == 1, len(verdicts))
    if verdicts:
        v = verdicts[-1]["data"]
        check("the headline verdict says FAILED, not ok", v.get("verdict") == "failed", v)
        check("...and its exit_code agrees with the process, which exits 1", v.get("exit_code") == 1, v)


def test_a_partly_grounded_run_is_not_reddened_by_the_new_rule():
    """The other side of the same rule, and it needs its own REAL run.

    Found by mutation: widening `not_grounded` to fire on ANY unmatched left every check green,
    because the only partial-grounding assertion was a re-implementation of the rule inside the test.
    A rule compared to a copy of itself proves nothing. So a second graph is driven, with one ref
    that binds and one that does not — the shape that must DEGRADE, not fail."""
    import json as _json
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_m9_2b_offline import WalkEx, _explore_init, _invoke, _LOGIN, FakeBackend  # noqa: E402
    from brain.planner import GoalPlanner  # noqa: E402
    from brain.state import normalize_url, semantic_id  # noqa: E402
    from brain import budget  # noqa: E402

    budget.reset(plan_limit=100000, heal_limit=100000)
    ex = WalkEx()
    good = semantic_id(normalize_url(_LOGIN), "textbox", "Username")
    fb = FakeBackend(_json.dumps({"steps": [
        {"ref": good, "verb": "fill", "value": "alice"},
        {"ref": "testdata/site/page-b.html", "verb": "click"},
    ]}))
    ex.call("browser.navigate", url=_LOGIN)
    obuf, ebuf = io.StringIO(), io.StringIO()
    with redirect_stdout(obuf), redirect_stderr(ebuf):
        final = _invoke(ex, GoalPlanner(goal="log in", backend=fb), _explore_init(goal="log in"))
    out = obuf.getvalue() + ebuf.getvalue()

    # The PROPERTY, not a count: one bound ref yields TWO steps here, because a cross-page navigate
    # is synthesised in code beside it. Asserting "== 1" measured the fixture, not the rule.
    check("the fixture really is PARTLY grounded (something bound, something did not)",
          bool(final.get("scenario_steps")) and bool(final.get("scenario_unmatched")),
          {"steps": final.get("scenario_steps"), "unmatched": final.get("scenario_unmatched")})
    verdicts = [_json.loads(l.split("@@AGUI ", 1)[1]) for l in out.splitlines() if "@@AGUI " in l]
    verdicts = [f for f in verdicts if f.get("type") == "verdict"]
    check("a partly grounded run emitted a verdict", len(verdicts) == 1, len(verdicts))
    if verdicts:
        v = verdicts[-1]["data"]
        check("...and it stays ok: partial grounding DEGRADES, it does not fail the run",
              v.get("verdict") == "ok", v)
        check("...with exit_code 0, matching what _write_scenario returns for the same case",
              v.get("exit_code") == 0, v)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    if FAILS:
        print(f"\nFAIL — {len(FAILS)} check(s): " + "; ".join(FAILS))
        sys.exit(1)
    print(f"\nALL PASS ({len(fns)} grounding-gate tests)")
