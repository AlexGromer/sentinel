"""Offline gate for application faults in the verdict (ADR-072) + the JUnit reporter (ADR-073).

Run:  .venv/bin/python tests/test_app_faults_junit_offline.py

ADR-072 pins the defect: a run could report `exit 0` PASSED while the page threw exceptions and answered
5xx for its whole duration. The events existed — in a log file nobody opens when the build is green. They
could not reach the brain either, because `brain/executor.py` inherits the executor's stderr rather than
piping it, so the tally has to come from the EMITTER via `browser.appFaults`.

ADR-073 pins the integration gap: before it, wiring Sentinel into a pipeline meant reading an exit code.
The mapping from Sentinel outcomes onto JUnit's four shapes is the actual design, so that is what the
assertions target — not the presence of a file.
"""
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.junit import to_junit                            # noqa: E402
from brain.replay import run_replay                         # noqa: E402
from brain.state import canonical_plan_hash                 # noqa: E402

PAGE = "file:///s/app.html"
GOOD = {"testid": "pay"}


class Ex:
    """Executor with a working locator and a configurable app-fault tally.

    `faults=None` models an OLDER executor that has no `browser.appFaults` method at all — the run must
    still complete. That path is not hypothetical: the brain and the executor are separately built
    artefacts, and a version skew between them is normal in the field.
    """

    def __init__(self, faults=None, raises=False):
        self.faults, self.raises = faults, raises
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
            if self.raises:
                raise RuntimeError("method not found")
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


def _run(ex, env=None):
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k); os.environ[k] = v
    try:
        return run_replay(ex, Store(), Heal(), _plan(), PAGE, tempfile.mkdtemp(), run_id="t")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


FAULTS = {"counts": {"app.js_error": 2, "app.http_error": 3, "app.console_warn": 7},
          "total": 12, "capped": False, "cap": 500}


# --- ADR-072: application faults reach the verdict -------------------------------------------------
def test_app_faults_reach_the_report_and_the_verdict():
    rep = _run(Ex(FAULTS))
    af = rep["app_faults"]
    assert af["total"] == 12, af
    # `errors` is the gateable subset: thrown, logged-error, failed request, 4xx/5xx. 2 + 3 = 5.
    assert af["errors"] == 5, af
    assert af["counts"]["app.console_warn"] == 7, af
    assert rep["verdict"] == "pass_with_app_faults", rep.get("verdict")
    assert rep["exit_code"] == 0, "reporting must not redden the build by default"


def test_console_warnings_alone_are_reported_but_not_counted_as_errors():
    # Gating on warnings would make the feature unusable on any real application, so a warning-only run
    # stays a plain pass. It is still visible in `counts`.
    rep = _run(Ex({"counts": {"app.console_warn": 4, "app.dialog": 1}, "total": 5}))
    assert rep["app_faults"]["errors"] == 0, rep["app_faults"]
    assert rep["app_faults"]["total"] == 5, rep["app_faults"]
    assert rep["verdict"] == "pass", rep.get("verdict")


def test_a_well_behaved_application_adds_no_noise():
    rep = _run(Ex({"counts": {}, "total": 0}))
    assert "app_faults" not in rep, rep.get("app_faults")
    assert rep["verdict"] == "pass", rep.get("verdict")


def test_fail_on_app_errors_gates_on_errors_not_on_the_total():
    # 5 errors out of 12 faults. A threshold of 6 must NOT fire: gating on the total would redden a build
    # over console warnings, which is the thing that makes teams disable the check.
    rep = _run(Ex(FAULTS), {"SENTINEL_FAIL_ON_APP_ERRORS": "6"})
    assert rep["exit_code"] == 0, rep["exit_code"]
    rep2 = _run(Ex(FAULTS), {"SENTINEL_FAIL_ON_APP_ERRORS": "5"})
    assert rep2["exit_code"] == 1, rep2["exit_code"]
    assert rep2["app_faults"]["failed_build"] is True, rep2["app_faults"]
    # A red build must NAME its cause. Plain "problem" would send the reader hunting for a failed step
    # that does not exist — a support burden this feature would otherwise create for itself.
    assert rep2["verdict"] == "problem_app_faults", rep2.get("verdict")


def test_a_real_step_failure_keeps_the_generic_verdict():
    # When a step actually failed, the step IS the story and the generic word is right. Only a
    # threshold-only red build gets the specific name.
    steps = [{"step_id": 1, "action_type": "click", "intent": "click missing", "semantic_id": "s",
              "is_milestone": False, "locator": {"testid": "nope"}, "alternatives": []}]
    plan = {"plan_id": "pf", "steps": steps, "plan_hash": canonical_plan_hash(steps), "target_url": PAGE}
    rep = run_replay(Ex(FAULTS), Store(), Heal(), plan, PAGE, tempfile.mkdtemp(), run_id="tf")
    assert rep["exit_code"] == 1 and rep["failed"] == 1, rep
    assert rep["verdict"] == "problem", rep.get("verdict")


def test_an_executor_without_the_method_does_not_break_the_run():
    # Version skew between separately built artefacts is normal; a missing tally is not a failure.
    rep = _run(Ex(None, raises=True))
    assert rep["exit_code"] == 0, rep
    assert "app_faults" not in rep, rep.get("app_faults")


def test_an_application_fault_outranks_our_own_drift_in_the_verdict():
    # A tester seeing both wants THEIR bug named first; our drift is our maintenance.
    steps = [{"step_id": 1, "action_type": "click", "intent": "click 'Pay'", "semantic_id": "s1",
              "is_milestone": False, "locator": {"testid": "old"},
              "alternatives": [{"strategy": "testid", "locator": {"testid": "old"}, "prior": 0.95},
                               {"strategy": "role_name", "locator": GOOD, "prior": 0.9}]}]
    plan = {"plan_id": "p2", "steps": steps, "plan_hash": canonical_plan_hash(steps), "target_url": PAGE}

    class H:
        def heal(self, ctx):
            return {"locator": GOOD, "strategy": "role_name", "confidence": 0.9, "outcome": "auto_healed"}

    rep = run_replay(Ex(FAULTS), Store(), H(), plan, PAGE, tempfile.mkdtemp(), run_id="t2")
    assert rep["drift"]["rebind"] == 1, rep["drift"]
    assert rep["verdict"] == "pass_with_app_faults", rep.get("verdict")


# --- ADR-073: the JUnit mapping -------------------------------------------------------------------
def _suite(xml):
    return ET.fromstring(xml).find("testsuite")


def test_a_failed_step_is_a_failure_and_a_quarantined_one_is_skipped():
    # The two must not collapse: quarantine (ADR-013) suppresses exit 1, so reporting it as passed would
    # hide a known flake and reporting it as failed would cry wolf. JUnit has `skipped` for exactly this.
    rep = {"plan_id": "p", "mode": "replay", "exit_code": 1, "verdict": "problem", "healed": 0,
           "steps": [{"step_id": 1, "type": "click", "intent": "click A", "outcome": "failed",
                      "error": "boom"},
                     {"step_id": 2, "type": "click", "intent": "click B", "outcome": "failed",
                      "quarantined": True}]}
    su = _suite(to_junit(rep))
    assert su.get("tests") == "2" and su.get("failures") == "1" and su.get("skipped") == "1", su.attrib
    cases = su.findall("testcase")
    assert cases[0].find("failure") is not None and "boom" in cases[0].find("failure").get("message")
    assert cases[1].find("skipped") is not None and cases[1].find("failure") is None


def test_a_golden_regression_is_a_failure_with_its_own_type():
    rep = {"plan_id": "p", "mode": "replay", "exit_code": 2, "verdict": "regression", "healed": 0,
           "steps": [{"step_id": 1, "type": "click", "outcome": "ok", "regression": ["a11y"]}]}
    su = _suite(to_junit(rep))
    f = su.find("testcase/failure")
    assert f is not None and f.get("type") == "regression", su.attrib
    assert "a11y" in f.get("message")


def test_a_reground_goes_to_stderr_and_a_rebind_to_stdout():
    # Both steps PASSED, so neither is a failure. But "we chose a new selector and did not verify it is
    # the same element" is what a reviewer must see without opening another artefact — stderr is how CI
    # surfaces that.
    rep = {"plan_id": "p", "mode": "replay", "exit_code": 0, "verdict": "pass_with_drift", "healed": 2,
           "drift": {"rebind": 1, "reground": 1, "elements": [
               {"step": 1, "kind": "rebind", "from": {"testid": "a"}, "to": {"role": "button"}},
               {"step": 2, "kind": "reground", "from": {"testid": "b"}, "to": {"css": "#x"}}]},
           "steps": [{"step_id": 1, "type": "click", "outcome": "healed",
                      "heal": {"strategy": "role_name", "confidence": 0.9, "outcome": "auto_healed"}},
                     {"step_id": 2, "type": "click", "outcome": "healed",
                      "heal": {"strategy": "css", "confidence": 0.58, "outcome": "flagged"}}]}
    su = _suite(to_junit(rep))
    assert su.get("failures") == "0", su.attrib
    cases = su.findall("testcase")
    assert cases[0].find("system-out") is not None and cases[0].find("system-err") is None
    assert cases[1].find("system-err") is not None, "a re-ground must surface on stderr"
    assert "reground" in cases[1].find("system-err").text
    assert "#x" in cases[1].find("system-err").text, "the new locator must be readable"
    assert "interface drift" in su.get("name"), su.get("name")


def test_app_faults_attach_to_the_suite_not_to_a_step():
    # The emitter cannot attribute a console error to a step, so pretending otherwise would be a lie.
    rep = {"plan_id": "p", "mode": "replay", "exit_code": 0, "verdict": "pass_with_app_faults",
           "healed": 0, "steps": [{"step_id": 1, "type": "click", "outcome": "ok"}],
           "app_faults": {"total": 12, "errors": 5, "counts": {"app.js_error": 2}, "capped": True,
                          "cap": 500}}
    su = _suite(to_junit(rep))
    err = su.find("system-err")
    assert err is not None and "5 error" in err.text, err.text if err is not None else None
    assert "app.js_error: 2" in err.text
    assert "capped" in err.text, "a truncated capture must say so"
    assert su.find("testcase/system-err") is None, "suite-level output must not land on a step"


def test_an_integrity_abort_is_an_error_not_a_failure():
    # exit 3 means NOTHING executed. Zero tests would read as "nothing to run", and `failure` is JUnit's
    # word for a failed assertion — `error` is its word for the harness breaking.
    rep = {"plan_id": "p", "mode": "replay", "exit_code": 3, "verdict": "integrity", "healed": 0,
           "steps": [], "reason": "plan_hash mismatch stored=abc computed=def"}
    su = _suite(to_junit(rep))
    assert su.get("tests") == "1" and su.get("errors") == "1" and su.get("failures") == "0", su.attrib
    e = su.find("testcase/error")
    assert e is not None and e.get("type") == "integrity" and "plan_hash" in e.get("message")


def test_the_header_counts_cannot_disagree_with_the_body():
    rep = {"plan_id": "p", "mode": "replay", "exit_code": 1, "verdict": "problem", "healed": 0,
           "steps": [{"step_id": i, "type": "click",
                      "outcome": "failed" if i < 3 else "ok"} for i in range(1, 6)]}
    su = _suite(to_junit(rep))
    assert su.get("failures") == str(len(su.findall("testcase/failure"))), su.attrib
    assert su.get("tests") == str(len(su.findall("testcase"))), su.attrib


def test_the_output_is_byte_identical_across_renders():
    # It is a CI artefact: a diff of two runs must show what changed in the RUN, not when it rendered.
    rep = {"plan_id": "p", "mode": "replay", "exit_code": 0, "verdict": "pass", "healed": 0,
           "steps": [{"step_id": 1, "type": "click", "outcome": "ok", "duration_ms": 120}]}
    assert to_junit(rep) == to_junit(rep)
    assert ET.fromstring(to_junit(rep)) is not None, "must be well-formed XML"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}\n       {e}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}\n       {type(e).__name__}: {e}")
    print(f"\napp-faults + junit: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
