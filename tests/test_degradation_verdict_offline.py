"""Offline gate for degradation reaching the verdict (ADR-077).

Run:  .venv/bin/python tests/test_degradation_verdict_offline.py

The defect: `brain/events.json` has always marked which codes mean the run LOST QUALITY — no LLM key, a
spent budget, locators re-grounded from the live page — and has always carried a `{ru,en}_verdict`
sentence saying what that means for the result. Nothing read either field. A run that finished without
an LLM produced a verdict, a `report.html` and a `junit.xml` that read exactly like a clean one, and the
only trace was a line in a log file nobody opens when the build is green.

The assertions are about the three surfaces a person actually looks at — the report dict, the HTML
report and the JUnit XML — plus the one property that makes the whole thing trustworthy: the tally is
taken BEFORE the log-level filter, so raising verbosity cannot change the verdict.
"""
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import eventlog                                  # noqa: E402
from brain.junit import to_junit                            # noqa: E402
from brain.report import _html as render_html                       # noqa: E402
from brain.replay import run_replay                         # noqa: E402
from brain.state import canonical_plan_hash                 # noqa: E402

PAGE = "file:///s/app.html"
GOOD = {"testid": "pay"}


class Ex:
    """A cooperative executor: every step passes, so nothing but degradation can colour the verdict."""

    def __init__(self, faults=None):
        self.faults = faults
        self.url = PAGE

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", self.url); return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- page app", "nodeCount": 2}
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.links":
            return {"links": []}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.probe":
            return {"count": 1 if p.get("locator") == GOOD else 0}
        if m == "browser.click":
            if p.get("locator") == GOOD:
                return {"ok": True}
            raise RuntimeError("not found")
        if m == "browser.appFaults":
            return self.faults or {}
        return {}

    def close(self):
        pass


class Heal:
    def heal(self, ctx):
        return {"locator": None, "strategy": None, "confidence": 0.0, "outcome": "failed"}


class Store:
    def record_step(self, *a, **k): return False
    def get_golden(self, *a, **k): return None
    def save_golden(self, *a, **k): return None
    def audit(self, *a, **k): return None


def _plan():
    steps = [{"step_id": 1, "action_type": "click", "intent": "click button 'Pay'",
              "semantic_id": "sid-pay", "is_milestone": False,
              "locator": GOOD, "alternatives": []}]
    return {"plan_id": "p1", "steps": steps, "plan_hash": canonical_plan_hash(steps),
            "target_url": PAGE}


def _run(ex, env=None, pre_log=()):
    """One replay. `pre_log` fires catalogued events first — the way the LLM and planner layers do
    before replay is even reached, which is exactly how a real run acquires most of its degradations.

    `reset_cache()` around the env is load-bearing, not hygiene: `_levels()` parses the environment ONCE
    per process, so setting SENTINEL_LOG_LEVEL without it changes nothing and a test that varies
    verbosity would compare two identical runs. A mutation proved that — moving the tally to AFTER the
    level filter survived, because the "quiet" run had never actually been quiet."""
    eventlog.reset_degradations()
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k); os.environ[k] = v
    eventlog.reset_cache()
    try:
        for code, fields in pre_log:
            eventlog.log(code, **fields)
        return run_replay(ex, Store(), Heal(), _plan(), PAGE, tempfile.mkdtemp(), run_id="t")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        eventlog.reset_cache()


NO_KEY = ("llm.no_anthropic_key", {"role": "planner"})
BUDGET = ("heal.budget_exhausted", {"spent": 5, "cap": 5})


# --- the report ------------------------------------------------------------------------------------
def test_a_clean_run_claims_no_degradation():
    """The control. Without it, every assertion below could be satisfied by a function that always
    reports degradation."""
    rep = _run(Ex())
    assert rep.get("degradations") == [], rep.get("degradations")


def test_degradations_reach_the_report_deduped_and_in_order():
    rep = _run(Ex(), pre_log=[NO_KEY, BUDGET, NO_KEY])
    degr = rep.get("degradations")
    assert degr, "the report carries no degradations at all — everything below would be vacuous"
    assert degr == ["llm.no_anthropic_key", "heal.budget_exhausted"], degr


def test_a_non_degrading_event_is_not_counted():
    """`degrades` is a property of specific codes, not of warnings in general — a run that merely logged
    something must not be reported as having lost quality."""
    rep = _run(Ex(), pre_log=[("run.store_mode", {"mode": "local"})])
    assert rep.get("degradations") == [], rep.get("degradations")


def test_verbosity_cannot_change_the_verdict():
    """The tally is taken BEFORE the level filter. Recording after it would mean the same run reported
    a cleaner verdict at SENTINEL_LOG_LEVEL=error than at default — a silent degradation produced by the
    very mechanism meant to expose silent degradation."""
    loud = _run(Ex(), pre_log=[NO_KEY, BUDGET])
    quiet = _run(Ex(), env={"SENTINEL_LOG_LEVEL": "error"}, pre_log=[NO_KEY, BUDGET])
    assert loud["degradations"], loud["degradations"]
    assert quiet["degradations"] == loud["degradations"], (quiet["degradations"], loud["degradations"])


def test_drift_marks_the_run_degraded_without_any_llm_event():
    """Degradation is not only about the LLM: re-grounding a locator from the live page is a quality
    loss the catalogue already marks, and it arises inside replay rather than before it."""
    rep = _run(Ex(), pre_log=[])
    assert rep["degradations"] == [], "precondition: this fixture drifts nothing"
    faults = {"counts": {"app.js_error": 1}, "total": 1, "capped": False, "cap": 500}
    rep2 = _run(Ex(faults))
    assert "app.faults_summary" in rep2["degradations"], rep2["degradations"]


# --- the HTML report -------------------------------------------------------------------------------
def test_html_report_states_the_loss_in_words_not_codes():
    rep = _run(Ex(), pre_log=[NO_KEY])
    html = render_html(rep)
    assert "Degraded quality" in html, "the report does not mention degradation at all"
    # The catalogue's verdict sentence, not the log phrasing: the report answers "what does this mean
    # for the result", and a bare code answers nothing.
    sentence = eventlog.verdict_sentence("llm.no_anthropic_key")
    assert sentence and sentence != "llm.no_anthropic_key", "the catalogue lost its verdict sentence"
    assert sentence in html, f"the report shows the code but not the sentence: {sentence!r}"


def test_html_report_of_a_clean_run_has_no_degradation_block():
    assert "Degraded quality" not in render_html(_run(Ex()))


# --- JUnit -----------------------------------------------------------------------------------------
def test_junit_carries_degradation_as_a_property_and_on_the_suite():
    rep = _run(Ex(), pre_log=[NO_KEY, BUDGET])
    root = ET.fromstring(to_junit(rep))
    su = root.find("testsuite")
    props = {p.get("name"): p.get("value") for p in su.find("properties").findall("property")}
    assert "degradations" in props, sorted(props)
    assert props["degradations"] == "llm.no_anthropic_key,heal.budget_exhausted", props["degradations"]
    # system-out, not a failure: the run PASSED — it passed with less than it was meant to have.
    out = su.find("system-out")
    assert out is not None and "llm.no_anthropic_key" in (out.text or ""), ET.tostring(su)
    assert su.get("failures") == "0" and su.get("errors") == "0", su.attrib


def test_junit_of_a_clean_run_says_nothing_about_degradation():
    root = ET.fromstring(to_junit(_run(Ex())))
    su = root.find("testsuite")
    props = {p.get("name"): p.get("value") for p in su.find("properties").findall("property")}
    assert "degradations" not in props, props
    assert su.find("system-out") is None, ET.tostring(su)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok   {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} checks passed")
