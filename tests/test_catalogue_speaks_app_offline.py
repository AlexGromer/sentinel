#!/usr/bin/env python3
"""HEALTH-004 PR-1c — the catalogue starts saying things about the APPLICATION.

Run:  .venv/bin/python tests/test_catalogue_speaks_app_offline.py

Three measured gaps, one theme: of 34 codes flagged `degrades`, THIRTY-THREE were about the tool.
The product could barely say anything about the system it exists to test.

  1. Interface drift was filed under the tool. `heal.drift_*` are emitted by the healer, so the
     derived source made them tool-side — and «the interface changed» is a fact about the
     application. A reader filtering `business` saw nothing about the one thing that moved.
  2. Partial grounding exited 0 in silence. A goal run that bound 3 of 10 references reported a
     counter and went green — the exact "passed, quietly worth less" shape `degrades` exists for.
  3. Speed did not exist. "The request failed" was catalogued; "the request was slow" was not, while
     Async Wait is the most frequent cause of flake in the literature.

What is asserted is the CONSEQUENCE, not the presence of a field: which audience a record lands in,
whether a green run now says what it cost, and that the speed number comes from the application
rather than from our own stopwatch.
"""
import io
import json
import os
import sys
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAT = json.load(open(os.path.join(REPO, "brain", "events.json")))
failures: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: {detail}")
        failures.append(f"{name}: {detail}")


def source_of(code):
    """The source a RECORD carries — the override first, exactly as logsink resolves it."""
    e = CAT["events"][code]
    if e.get("src"):
        return e["src"]
    return next(s for s, m in CAT["sources"].items() if e["cat"] in m["cats"])


def audience_of(code):
    return next(a for a, m in CAT["audiences"].items() if source_of(code) in m["sources"])


# --------------------------------------------------------------------------------------------
# 1. Interface drift is a statement about the application.
# --------------------------------------------------------------------------------------------
def test_drift_is_visible_to_someone_filtering_the_business_side():
    for code in ("heal.drift_rebind", "heal.drift_reground", "heal.drift_summary"):
        check(f"{code} reaches the business audience", audience_of(code) == "business",
              f"{audience_of(code)} (src={source_of(code)})")
        # And it must NOT have been achieved by moving the category: a reader who filters `heal`
        # because they are looking at self-healing has to keep finding drift there.
        check(f"{code} is still a healing record", CAT["events"][code]["cat"] == "heal",
              CAT["events"][code]["cat"])
        check(f"{code} justifies its override", bool((CAT["events"][code].get("src_why") or "").strip()))
    # The healer's own diagnostics must stay tool-side — an override applied to the whole category
    # would satisfy every line above and quietly re-file "our heal budget ran out" as the app's fault.
    for code in ("heal.budget_exhausted", "heal.no_llm_backend"):
        check(f"{code} stays with the tool", audience_of(code) == "tool", audience_of(code))


# --------------------------------------------------------------------------------------------
# 2. Partial grounding is no longer silent.
# --------------------------------------------------------------------------------------------
def _author(grounded, unmatched, tmp, is_describe=False):
    """Drive the SHIPPED _write_scenario and capture what it said."""
    from brain.__main__ import _write_scenario
    steps = [{"action_type": "click", "intent": f"s{i}", "locator": {"role": "button", "name": f"b{i}"}}
             for i in range(grounded)]
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = _write_scenario(tmp, "r", "file:///s/app.html", steps,
                             [f"ref{i}" for i in range(unmatched)], is_describe)
    return rc, buf.getvalue()


def test_a_goal_run_that_grounded_part_of_what_it_was_asked_says_so():
    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        rc, out = _author(3, 7, tmp)
        check("a partly grounded goal run still exits 0 (a goal is a direction, not a spec)", rc == 0, str(rc))
        check("...and no longer does it silently", "plan.partially_grounded" in out, out[-300:])
        check("...naming the numbers a person can act on", "3" in out and "7" in out and "10" in out, out[-300:])
        check("the code is marked as a degradation", CAT["events"]["plan.partially_grounded"].get("degrades") is True)
        check("...so it reaches the verdict, not just the log",
              bool(CAT["events"]["plan.partially_grounded"].get("ru_verdict")))

        # The narrow half: a run that grounded everything must NOT be decorated with a warning. A
        # change that always fired would satisfy the assertions above.
        rc2, out2 = _author(5, 0, tmp)
        check("a fully grounded run stays quiet", "plan.partially_grounded" not in out2, out2[-200:])
        check("...and still exits 0", rc2 == 0, str(rc2))

        # And a run that grounded NOTHING is a failure, not a degradation — it has nothing to degrade.
        rc3, out3 = _author(0, 4, tmp)
        check("a run that grounded nothing fails rather than warns", rc3 == 1, str(rc3))
        check("...and does not also claim partial success", "plan.partially_grounded" not in out3, out3[-200:])


# --------------------------------------------------------------------------------------------
# 3. Speed: the application's own number, and only when asked for.
# --------------------------------------------------------------------------------------------
def _speed(timing, limit=None):
    from brain.replay import note_load_speed
    old = os.environ.get("SENTINEL_SLOW_LOAD_MS")
    if limit is None:
        os.environ.pop("SENTINEL_SLOW_LOAD_MS", None)
    else:
        os.environ["SENTINEL_SLOW_LOAD_MS"] = str(limit)
    buf = io.StringIO()
    try:
        with redirect_stderr(buf):
            note_load_speed({"timing": timing} if timing is not None else {}, "file:///s/slow.html")
    finally:
        if old is None:
            os.environ.pop("SENTINEL_SLOW_LOAD_MS", None)
        else:
            os.environ["SENTINEL_SLOW_LOAD_MS"] = old
    return buf.getvalue()


def test_slowness_is_reported_only_when_a_threshold_was_asked_for():
    check("no threshold set -> silent (a default would fire on every real site or on none)",
          _speed({"response_ms": 9000, "dom_ms": 9500}) == "", _speed({"response_ms": 9000, "dom_ms": 9500}))
    fired = _speed({"response_ms": 2000, "dom_ms": 4200}, limit=3000)
    check("over the threshold -> app.slow_load", "app.slow_load" in fired, fired)
    check("...carrying the measured number and the threshold it broke",
          "4200" in fired and "3000" in fired, fired)
    check("under the threshold -> silent", _speed({"response_ms": 100, "dom_ms": 200}, limit=3000) == "")
    check("no timing at all (older executor, page navigated away) -> silent, not zero",
          _speed(None, limit=3000) == "" and _speed({}, limit=3000) == "")
    check("a zero measurement is not reported as instant success",
          _speed({"response_ms": 0, "dom_ms": 0}, limit=1) == "")


def test_the_speed_number_comes_from_the_page_not_from_our_stopwatch():
    """The surrogate this avoids: timing the step. That clock includes locator resolution, healing and
    RPC, so a slow TOOL would be published as a slow APPLICATION — the substitution the whole fault
    axis exists to prevent. Asserted against the executor's source: the value must come from
    PerformanceNavigationTiming, which is the browser measuring the document exchange."""
    src = open(os.path.join(REPO, "pw-executor", "src", "server.ts")).read()
    check("the executor reads the page's own navigation timing",
          "getEntriesByType('navigation')" in src)
    check("...and returns it from browser.navigate", "timing" in src.split("case 'browser.navigate'")[1][:2000])
    # loadEventEnd is deliberately not used: we wait for domcontentloaded, so it can still be 0 and a
    # fast page would be indistinguishable from an unfinished one.
    nav = src.split("case 'browser.navigate'")[1][:2000]
    check("loadEventEnd is not used as the number (it can still be 0 at domcontentloaded)",
          "loadEventEnd" not in nav or "n.loadEventEnd ||" not in nav, nav[-300:])
    check("the code is filed as the application's", audience_of("app.slow_load") == "business",
          audience_of("app.slow_load"))


# --------------------------------------------------------------------------------------------
# The theme, measured.
# --------------------------------------------------------------------------------------------
def test_no_message_hands_its_own_content_to_the_redactor():
    """A catalogue sentence must not put a credential-shaped word immediately before the value it is
    trying to show.

    Measured live 2026-08-04, on the very message this PR promoted to the business audience: the
    English text read `…found again by another key: "{element}"`, the log redactor read `key: <value>`
    as a named credential, and the drift line arrived as `by another key: "[REDACTED]"`. The message
    lost the one thing it exists to say, at the moment it mattered.

    The redactor is RIGHT and is not weakened — a security control must not be relaxed to make a
    sentence prettier. The sentence moves its punctuation instead.

    Checked as a PROPERTY over every entry rather than as a list of known-bad ones, because the next
    such sentence will be written by someone who never read this. Only ASCII names are considered:
    the scanner's name characters are ASCII, so a Russian «ключу:» is not a match — which is exactly
    why only the English half was broken and why a check on the Russian text alone would pass.
    """
    import re as _re
    secretish = _re.compile(r"\b([A-Za-z_][A-Za-z0-9_.-]*)\s*[:=]\s*[\"«{]")
    words = ("key", "token", "secret", "password", "passwd", "credential", "auth", "apikey")
    for code, e in CAT["events"].items():
        for lang in ("ru", "en"):
            text = e.get(lang) or ""
            for m in secretish.finditer(text):
                name = m.group(1).lower().replace("-", "_").replace(".", "_")
                if any(w == name or name.endswith("_" + w) or name.startswith(w + "_") for w in words):
                    check(f"{code}.{lang} does not feed its own value to the redactor", False,
                          f"«{name}» sits before the value in: {text[:120]}")
    check("no catalogue sentence puts a credential-shaped word before its own value", True)


def test_the_degradation_map_is_no_longer_almost_entirely_about_the_tool():
    deg = {c: e for c, e in CAT["events"].items() if e.get("degrades")}
    app_side = [c for c in deg if audience_of(c) == "business"]
    check("more than one degradation is about the application under test",
          len(app_side) >= 3, f"{len(app_side)} of {len(deg)}: {sorted(app_side)}")
    for want in ("app.faults_summary", "app.slow_load", "heal.drift_summary"):
        check(f"{want} is one of them", want in app_side, f"audience={audience_of(want)}")

    # `plan.partially_grounded` is deliberately NOT among them, and this line exists so that stays a
    # decision rather than an accident. "7 of 10 references found no element" has a genuinely mixed
    # cause — the page may not contain those controls, or the model phrased the reference badly, or
    # our matcher is weak — and the honest default for a mixed cause is that OUR authoring
    # under-delivered. Same family as plan.scenario_error_empty (PR-1a). Filing it as the
    # application's would send a reader to inspect a page that may be perfectly correct.
    check("partial grounding stays the tool's own shortfall",
          audience_of("plan.partially_grounded") == "tool", audience_of("plan.partially_grounded"))


def main() -> int:
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        print(f"-- {fn.__name__}")
        fn()
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\ncatalogue-speaks-app gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
