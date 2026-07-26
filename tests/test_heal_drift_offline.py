"""Offline gate for the UI-drift verdict (ADR-071).

Run:  .venv/bin/python tests/test_heal_drift_offline.py

The defect this pins: a replay that needed N heals reported `exit 0` and a bare `healed: N` counter, so a
green build was indistinguishable from a green build under an interface that had moved. Worse, the counter
merged two different events — re-binding the SAME element by another key frozen with the plan (repairing
the test, which is what healing is for) and re-grounding to a NEW selector chosen from the page as it is
now (which may be a different element that merely looks right; nothing verifies identity).

Assertions target the CLASSIFICATION and the VERDICT rather than the count, because the count was never
the problem — its silence about what it counted was.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import replay as replay_mod                      # noqa: E402
from brain.replay import run_replay                         # noqa: E402
from brain.state import canonical_plan_hash                 # noqa: E402

PAGE = "file:///s/app.html"


class Ex:
    """Executor where exactly ONE locator resolves — `works`. Everything else probes to 0 and clicks fail.

    `browser.probe` matters: `replay.py` chooses between "use the frozen locator" and "heal" purely on
    `probe(primary) == 1`. A stub that left probe unimplemented would send EVERY step down the heal path,
    and the tests would pass for the wrong reason — which is exactly what the first draft of this file did,
    until the clean-pass test caught it.
    """

    def __init__(self, works):
        self.works = works          # the single locator that resolves and can be clicked
        self.url = PAGE

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", self.url); return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- page app", "nodeCount": 3}
        if m == "browser.interactives":
            return {"elements": [{"tag": "button", "role": "button", "name": "Pay",
                                  "testid": "pay-v2", "text": "Pay", "disabled": False}]}
        if m == "browser.links":
            return {"links": []}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.probe":
            return {"count": 1 if p.get("locator") == self.works else 0}
        if m == "browser.click":
            if p.get("locator") == self.works:
                return {"ok": True}
            raise RuntimeError(f"locator not found: {p.get('locator')}")
        return {}

    def close(self):
        pass


class Heal:
    """Stands in for HealEngine: returns a fixed verdict, so the test drives CLASSIFICATION, not healing."""

    def __init__(self, locator, strategy, confidence=0.9, outcome="auto_healed"):
        self.r = {"locator": locator, "strategy": strategy,
                  "confidence": confidence, "outcome": outcome}

    def heal(self, ctx):
        return dict(self.r)


class Store:
    """Golden/quarantine store stub: no goldens, nothing quarantined, audit swallowed."""

    def record_step(self, *a, **k):
        return False

    def get_golden(self, *a, **k):
        return None

    def save_golden(self, *a, **k):
        return None

    def audit(self, *a, **k):
        return None


def _plan(alternatives):
    steps = [{"step_id": 1, "action_type": "click", "intent": "click button 'Pay'",
              "semantic_id": "sid-pay", "is_milestone": False,
              "locator": {"testid": "pay-v1"}, "alternatives": alternatives}]
    return {"plan_id": "p1", "steps": steps, "plan_hash": canonical_plan_hash(steps),
            "target_url": PAGE}


FROZEN_ALTS = [{"strategy": "testid", "locator": {"testid": "pay-v1"}, "prior": 0.95},
               {"strategy": "role_name", "locator": {"role": "button", "name": "Pay"}, "prior": 0.90}]


def _run(plan, heal, works, env=None):
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        return run_replay(Ex(works), Store(), heal, plan, PAGE, tempfile.mkdtemp(), run_id="t")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- classification: the whole point --------------------------------------------------------------
def test_a_strategy_frozen_with_the_plan_is_a_rebind():
    works = {"role": "button", "name": "Pay"}
    rep = _run(_plan(FROZEN_ALTS), Heal(works, "role_name"), works)
    assert rep["exit_code"] == 0, rep
    d = rep["drift"]
    assert d["rebind"] == 1 and d["reground"] == 0, d
    e = d["elements"][0]
    assert e["kind"] == "rebind", e
    assert e["from"] == {"testid": "pay-v1"}, e     # what the frozen plan asked for
    assert e["to"] == works, e                      # what actually worked
    assert e["name"] == "click button 'Pay'", e     # the handle a human recognises
    assert e["step"] == 1 and e["strategy"] == "role_name", e


def test_a_strategy_absent_from_the_plan_is_a_reground():
    # `css` is produced by the LLM re-ground at heal time — it was never frozen, so identity is unverified.
    works = {"css": "#pay-v2"}
    rep = _run(_plan(FROZEN_ALTS), Heal(works, "css", confidence=0.58, outcome="flagged"), works)
    d = rep["drift"]
    assert d["reground"] == 1 and d["rebind"] == 0, d
    e = d["elements"][0]
    assert e["kind"] == "reground" and e["outcome"] == "flagged", e
    assert e["to"] == {"css": "#pay-v2"}, e


def test_the_class_follows_the_FROZEN_plan_not_a_hardcoded_strategy_list():
    # The same `css` strategy is a REBIND when the plan itself froze a css alternative. Deriving the class
    # from membership rather than from a strategy blocklist is what makes an imported plan classify right.
    alts = FROZEN_ALTS + [{"strategy": "css", "locator": {"css": "#pay-v2"}, "prior": 0.65}]
    works = {"css": "#pay-v2"}
    rep = _run(_plan(alts), Heal(works, "css"), works)
    assert rep["drift"]["rebind"] == 1, rep["drift"]
    assert rep["drift"]["elements"][0]["kind"] == "rebind", rep["drift"]


# --- verdict --------------------------------------------------------------------------------------
def test_a_pass_that_needed_healing_is_its_own_verdict_state():
    works = {"role": "button", "name": "Pay"}
    rep = _run(_plan(FROZEN_ALTS), Heal(works, "role_name"), works)
    assert rep["verdict"] == "pass_with_drift", rep.get("verdict")
    assert rep["exit_code"] == 0, "drift must not redden the build by default"


def test_a_clean_pass_is_not_labelled_as_drift():
    # No healing needed: the frozen locator works. The state must stay a plain pass, or the label becomes
    # noise and stops meaning anything.
    works = {"testid": "pay-v1"}
    rep = _run(_plan(FROZEN_ALTS), Heal(works, "role_name"), works)
    assert rep["drift"]["rebind"] == 0 and rep["drift"]["reground"] == 0, rep["drift"]
    assert rep["verdict"] == "pass", rep.get("verdict")
    assert rep["healed"] == 0, rep


def test_fail_on_heal_reddens_the_build_only_when_the_threshold_is_reached():
    works = {"role": "button", "name": "Pay"}
    # Threshold 1: one drifted element is enough.
    rep = _run(_plan(FROZEN_ALTS), Heal(works, "role_name"), works, {"SENTINEL_FAIL_ON_HEAL": "1"})
    assert rep["exit_code"] == 1, rep
    assert rep["drift"]["failed_build"] is True and rep["drift"]["threshold"] == 1, rep["drift"]
    # Threshold 2: one drifted element is below it, so the build stays green.
    rep2 = _run(_plan(FROZEN_ALTS), Heal(works, "role_name"), works, {"SENTINEL_FAIL_ON_HEAL": "2"})
    assert rep2["exit_code"] == 0, rep2
    assert "failed_build" not in rep2["drift"], rep2["drift"]
    # Unset: off. This is the default and it must stay the default.
    rep3 = _run(_plan(FROZEN_ALTS), Heal(works, "role_name"), works)
    assert rep3["exit_code"] == 0, rep3


def test_a_typo_in_the_ci_variable_does_not_crash_the_replay():
    # A bad CI value must fall back to "off", not turn a passing replay into an exception.
    works = {"role": "button", "name": "Pay"}
    for bad in ("yes", "-3", "", "  "):
        rep = _run(_plan(FROZEN_ALTS), Heal(works, "role_name"), works,
                   {"SENTINEL_FAIL_ON_HEAL": bad})
        assert rep["exit_code"] == 0, (bad, rep["exit_code"])


def test_a_real_failure_still_outranks_drift_in_the_verdict():
    # Healing refuses -> the step fails. Drift must not soften that into pass_with_drift.
    rep = _run(_plan(FROZEN_ALTS), Heal(None, None, 0.1, "failed"), {"testid": "nope"})
    assert rep["exit_code"] == 1 and rep["failed"] == 1, rep
    assert rep["verdict"] == "problem", rep.get("verdict")


# --- the report renders it ------------------------------------------------------------------------
def test_the_html_report_shows_the_before_and_after_locator():
    from brain.report import _html
    works = {"css": "#pay-v2"}
    rep = _run(_plan(FROZEN_ALTS), Heal(works, "css", confidence=0.58, outcome="flagged"), works)
    out = _html(rep)
    assert "Interface drift" in out, "the drift section is missing from the report"
    assert "re-ground" in out, out[-600:]
    assert "pay-v1" in out and "pay-v2" in out, "the before -> after locators must both be visible"
    assert "pass_with_drift" in out, "the verdict state must appear in the header"


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
    print(f"\nheal-drift: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
