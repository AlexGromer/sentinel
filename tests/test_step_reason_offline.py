#!/usr/bin/env python3
"""HEALTH-004 PR-1b — a failed step says WHY, and says WHOSE problem it is.

Run:  .venv/bin/python tests/test_step_reason_offline.py

THE DEFECT. `brain/replay.py` builds a rich record for every step — the exception text, the
assertion's condition/expected/observed, the healing outcome — and the log line carried
`step` and `type`. So in the stream a person actually searches, these were the same sentence:

    the application returned the wrong value
    the executor died and we never reached the page

The detail existed in `heal-report.json`, which replay and baseline runs produce and goal/explore
runs do not — so for the commonest runs it existed nowhere.

WHAT IS ASSERTED. Not that a field is present: that the two are TOLD APART, and told apart on the
axis the UI filters by. The domain picks the CODE (`test.*` -> audience `business`,
`browser.*` -> `tool`), so a reader filtering "the tool's problems" sees the second and not the
first. A field could not do that — audience is derived from the category.

WHY THE CLASSIFIER LOOKS AT THE EXCEPTION TYPE. `replay._fault_of` asks which raise site fired at the
executor boundary, not what the message says. Matching driver text for "Timeout" or "ECONNRESET"
would be a surrogate for that question: it correlates today and breaks silently the first time
Playwright rewords a message. The last test here pins that directly.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.__main__ import log_step_outcome                    # noqa: E402
from brain.executor import ExecutorTransportError              # noqa: E402
from brain.replay import _fault_of, run_replay                 # noqa: E402
from brain.state import canonical_plan_hash                    # noqa: E402

PAGE = "file:///s/app.html"
CATALOG = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "brain", "events.json")))
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: {detail}")
        failures.append(f"{name}: {detail}")


def emitted(rec: dict) -> str:
    """Drive the SHIPPED emitter and capture the wire line. eventlog writes to stderr."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        log_step_outcome(rec)
    return buf.getvalue()


def audience_of(code: str) -> str:
    cat = CATALOG["events"][code]["cat"]
    src = next(s for s, m in CATALOG["sources"].items() if cat in m["cats"])
    return next(a for a, m in CATALOG["audiences"].items() if src in m["sources"])


# --------------------------------------------------------------------------------------------
# 1. The classifier, asked directly. One question: did the transport fail?
# --------------------------------------------------------------------------------------------
def test_the_classifier_asks_the_transport_not_the_message():
    check("a dead executor is OURS",
          _fault_of(ExecutorTransportError("executor closed during 'browser.click'")) == "tool")
    check("an error the executor REPORTED is about the page",
          _fault_of(RuntimeError("browser.click: Timeout 30000ms exceeded")) == "app")
    check("no exception at all (assert mismatch, heal exhausted) is about the page",
          _fault_of(None) == "app")
    # The surrogate this deliberately avoids: the two messages below are near-identical English, and a
    # text matcher would have to get them both right. The type answers without reading either.
    same_words_ours = ExecutorTransportError("the executor did not answer 'browser.click' within 60s")
    same_words_theirs = RuntimeError("browser.click: Timeout 30000ms exceeded waiting for locator")
    check("two timeouts, opposite owners — decided by type, not by the word 'timeout'",
          _fault_of(same_words_ours) == "tool" and _fault_of(same_words_theirs) == "app",
          f"{_fault_of(same_words_ours)} / {_fault_of(same_words_theirs)}")


# --------------------------------------------------------------------------------------------
# 2. The emitter: the domain picks the CODE, so the split lands in the audience filter.
# --------------------------------------------------------------------------------------------
def test_the_real_executor_boundary_raises_the_transport_type():
    """The classifier is only as good as the boundary that feeds it.

    Added after a mutation SURVIVED: replacing `raise ExecutorTransportError` with a plain
    `RuntimeError` in brain/executor.py left every test above green, because the fakes raise the
    transport type directly and never touch the real raise site. That is this project's recurring
    trap — measuring a copy of the capability instead of the capability.

    So this drives the SHIPPED `Executor` against a subprocess that exits immediately: the read
    returns nothing, which is exactly what a dead executor looks like.
    """
    from brain.executor import Executor
    ex = Executor(f"{sys.executable} -c pass")
    try:
        ex.call("browser.click", locator={"role": "button"})
    except ExecutorTransportError as e:
        check("a subprocess that closed raises the TRANSPORT type from the real client", True)
        check("...and the classifier then owns it", _fault_of(e) == "tool")
    except Exception as e:                      # noqa: BLE001 — the point is which type arrived
        check("a subprocess that closed raises the TRANSPORT type from the real client", False,
              f"got {type(e).__name__}: {e}")
        check("...and the classifier then owns it", False, f"_fault_of -> {_fault_of(e)}")
    else:
        check("a subprocess that closed raises the TRANSPORT type from the real client", False,
              "the call returned normally against a dead subprocess")
        check("...and the classifier then owns it", False, "no exception to classify")
    finally:
        ex.close()


def test_the_two_failures_land_in_different_audiences():
    app_line = emitted({"step_id": 4, "type": "assert", "outcome": "failed", "fault": "app",
                        "assert": {"condition": "text_equals", "expect_ok": True,
                                   "observed": False, "actual": "Ошибка 500"}})
    tool_line = emitted({"step_id": 4, "type": "click", "outcome": "failed", "fault": "tool",
                         "error": "executor closed during 'browser.click'"})

    check("the application's failure keeps test.step_failed", "test.step_failed" in app_line, app_line)
    check("our own failure gets its own code", "test.step_unresolved" in tool_line, tool_line)
    check("...and they are NOT the same code",
          ("test.step_unresolved" in tool_line) and ("test.step_unresolved" not in app_line))

    check("the application's failure is filed under `business`",
          audience_of("test.step_failed") == "business", audience_of("test.step_failed"))
    check("ours is filed under `tool`, so a filter can separate them",
          audience_of("test.step_unresolved") == "tool", audience_of("test.step_unresolved"))
    # The wire line carries the source marker the log view reads.
    check("the tool-side line is emitted in a tool category", "|browser]" in tool_line, tool_line)
    check("the app-side line is emitted in a testing category", "|test]" in app_line, app_line)


def test_the_reason_reaches_the_line_and_not_only_the_artifact():
    line = emitted({"step_id": 7, "type": "assert", "outcome": "failed", "fault": "app",
                    "assert": {"condition": "text_equals", "expect_ok": True,
                               "observed": False, "actual": "Ошибка 500"}})
    check("the assertion's condition is on the line", "text_equals" in line, line)
    check("WHAT THE PAGE ACTUALLY SHOWED is on the line", "Ошибка 500" in line, line)
    # Asserted on the OBSERVATION SLOT, not just on the value appearing somewhere. A mutation that
    # dropped `actual` from _observed_of survived the line above, because the same string also reaches
    # the sentence through `reason` — the assertion was satisfied by a different channel than the one
    # it meant to test.
    check("the observation slot carries the captured value, not the boolean",
          "observed Ошибка 500" in line, line)

    thrown = emitted({"step_id": 8, "type": "click", "outcome": "failed", "fault": "app",
                      "error": "browser.click: Timeout 30000ms exceeded waiting for locator"})
    check("a thrown verb puts the driver's own words on the line",
          "Timeout 30000ms" in thrown, thrown)

    # Heal exhausted: no exception and no assertion. THE COMMONEST FAILURE THERE IS, and the one the
    # backlog entry names ("we could not find the button"). Measured live on 2026-08-04: three of eight
    # failures in a real replay took this path and rendered the "no reason recorded" fallback, because
    # the first version of this change only read `error` and `assert`. The information was in the
    # record the whole time.
    exhausted = emitted({"step_id": 9, "type": "click", "outcome": "failed", "fault": "app",
                         "heal": {"outcome": "no_candidate", "strategy": None}})
    check("a heal-exhausted step says the locator did not resolve",
          "no_candidate" in exhausted, exhausted)
    check("...and does NOT fall back to 'no reason recorded'",
          "не записана" not in exhausted and "no reason recorded" not in exhausted, exhausted)
    check("...and never renders a blank observation",
          "observed=" not in exhausted or "observed=None" not in exhausted, exhausted)


def test_a_passing_step_is_untouched():
    # The narrow-gate half: a change that made every step shout would also satisfy the tests above.
    line = emitted({"step_id": 1, "type": "click", "outcome": "ok"})
    check("a passing step still emits test.step_passed", "test.step_passed" in line, line)
    check("a passing step claims no fault",
          "step_unresolved" not in line and "reason=" not in line, line)
    healed = emitted({"step_id": 2, "type": "click", "outcome": "healed",
                      "heal": {"strategy": "role", "confidence": 0.9}})
    check("a healed step still emits test.step_healed", "test.step_healed" in healed, healed)


# --------------------------------------------------------------------------------------------
# 3. End to end through run_replay: the record really carries the domain the executor implies.
# --------------------------------------------------------------------------------------------
class _Ex:
    """Minimal executor. `mode` decides how the single click step fails."""

    def __init__(self, mode):
        self.mode = mode
        self.url = PAGE

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", self.url); return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- page app", "nodeCount": 3}
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.links":
            return {"links": []}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.appFaults":
            return {"counts": {}}
        if m == "browser.probe":
            return {"count": 1}
        if m == "browser.click":
            if self.mode == "transport":
                raise ExecutorTransportError("executor closed during 'browser.click'")
            raise RuntimeError("browser.click: Timeout 30000ms exceeded waiting for locator")
        return {}


class _Store:
    """The three methods replay.py actually calls — enumerated from the source, not guessed. A stub
    missing one raises mid-replay and the failure reads like a product defect."""

    def record_step(self, *a, **k):
        return False           # never quarantined: a suppressed failure would hide what is measured

    def get_golden(self, *a, **k):
        return None            # no baseline recorded -> the golden-diff path is a no-op

    def save_golden(self, *a, **k):
        pass

    def close(self):
        pass


class _Heal:
    def heal(self, ctx):
        return {"outcome": "no_candidate"}


def _plan():
    steps = [{"step_id": 1, "action_type": "click", "intent": "pay",
              "semantic_id": "sid-pay", "locator": {"role": "button", "name": "Pay"},
              "alternatives": []}]
    return {"plan_id": "p", "target_url": PAGE, "steps": steps,
            "plan_hash": canonical_plan_hash(steps)}


def _replay(mode):
    with tempfile.TemporaryDirectory() as d:
        rep = run_replay(_Ex(mode), _Store(), _Heal(), _plan(), PAGE, d, run_id="r")
        return rep


def test_run_replay_records_the_domain_where_the_failure_happens():
    ours = _replay("transport")["steps"][0]
    theirs = _replay("remote")["steps"][0]
    check("a transport failure is recorded as ours", ours.get("fault") == "tool", str(ours))
    check("an error the executor reported is recorded as the application's",
          theirs.get("fault") == "app", str(theirs))
    check("both still failed the step", ours["outcome"] == "failed" and theirs["outcome"] == "failed")
    # And the record is what the emitter consumes — the two halves meet on real data, not on a fixture
    # hand-written to match.
    check("the record drives the emitter to the tool code",
          "test.step_unresolved" in emitted(ours), emitted(ours))
    check("the record drives the emitter to the application code",
          "test.step_failed" in emitted(theirs), emitted(theirs))


def main() -> int:
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        print(f"-- {fn.__name__}")
        fn()
    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nstep-reason gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
