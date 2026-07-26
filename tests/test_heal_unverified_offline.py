"""Offline gate: a heal applied without full confidence stops being invisible (ADR-080).

Run:  .venv/bin/python tests/test_heal_unverified_offline.py

`docs/SELF_HEALING.md` §194 describes the 0.60–0.84 band as "apply optimistically, set
`review_required=true`, show in the run report's healing-audit section". Only the first third was ever
built: `review_required` does not appear anywhere in the repository, and `flagged` appeared in neither
`report.py`, `junit.py` nor `docs/index.html`. So the tool repaired itself with a locator it did not
vouch for and produced a verdict, a report and a JUnit file that read exactly like a clean run.

Behaviour is deliberately UNCHANGED — ADR-005/017 chose optimistic application on purpose, and the
run still proceeds. What changes is that it says so.

Second claim: a re-ground can never be accepted outright. That used to hold only by arithmetic
(0.65 × 0.90 = 0.585 vs FLAG 0.60, a margin of 0.015) with a test pinning the number rather than the
property. It is a rule now.
"""
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import eventlog                                  # noqa: E402
from brain.healing import AUTO, FLAG, HealingEngine, is_reground   # noqa: E402
from brain.junit import to_junit                            # noqa: E402
from brain.report import _html as render_html               # noqa: E402
from brain.replay import run_replay                         # noqa: E402
from brain.state import canonical_plan_hash                 # noqa: E402

PAGE = "file:///s/app.html"
FROZEN = {"testid": "pay"}                  # what the plan asked for
MOVED = {"role": "button", "name": "Pay"}   # a key the plan ALSO froze, prior 0.90 -> accepted outright
WEAK = {"text": "Pay"}                      # ALSO frozen, prior 0.80 -> APPLIED OPTIMISTICALLY (flagged)
NEW = {"css": "#pay-v2"}                    # nothing in the plan vouches for this -> re-ground

# The applied-but-unconfident band is 0.60-0.84, and reaching it took a corrected fixture: the first
# version drove it with a css re-ground, which scores 0.585 and is therefore NEVER APPLIED — the very
# property asserted two tests below. `text_role` at 0.80 is the honest case: a key the plan froze,
# strong enough to execute, too weak to accept silently.


class Ex:
    """Executor where the frozen primary locator is GONE and a chosen replacement resolves."""

    def __init__(self, resolves):
        self.resolves = resolves          # list of locator dicts that probe to exactly 1
        self.url = PAGE

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", self.url); return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- page app", "nodeCount": 2}
        if m in ("browser.interactives", "browser.links"):
            return {"elements": [], "links": []}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.probe":
            return {"count": 1 if p.get("locator") in self.resolves else 0}
        if m in ("browser.click", "browser.fill", "browser.type", "browser.select"):
            if p.get("locator") in self.resolves:
                return {"ok": True}
            raise RuntimeError("not found")
        return {}

    def close(self):
        pass


class Store:
    def lookup(self, *a, **k): return None
    def evict_stale(self, *a, **k): return None
    def bump_used(self, *a, **k): return None
    def save_locator(self, *a, **k): return None
    def audit(self, *a, **k): return None
    def record_step(self, *a, **k): return False
    def get_golden(self, *a, **k): return None
    def save_golden(self, *a, **k): return None


class Backend:
    """A heal backend that always answers with the same CSS selector."""
    model = "fake"
    supports_vision = False

    def complete(self, prompt, **kw):
        return type("R", (), {"text": '{"css": "#pay-v2"}', "data": {"css": "#pay-v2"},
                              "usage": {}, "prompt_tokens": 0, "completion_tokens": 0})()


def _plan():
    steps = [{"step_id": 1, "action_type": "click", "intent": "click button 'Pay'",
              "semantic_id": "sid-pay", "is_milestone": False, "locator": FROZEN,
              "alternatives": [{"strategy": "testid", "locator": FROZEN, "prior": 0.95},
                               {"strategy": "role_name", "locator": MOVED, "prior": 0.90},
                               {"strategy": "text_role", "locator": WEAK, "prior": 0.80}]}]
    return {"plan_id": "p1", "steps": steps, "plan_hash": canonical_plan_hash(steps),
            "target_url": PAGE}


class Heal:
    """Wraps the real engine so the replay path exercises the real gate."""

    def __init__(self, ex, use_llm=False):
        self.e = HealingEngine(ex, Store(), "r", use_llm=use_llm)
        if use_llm:
            self.e._backend = Backend()
        self._backend = self.e._backend

    def heal(self, ctx):
        return self.e.heal(ctx)


def _run(ex, use_llm=False):
    eventlog.reset_degradations()
    eventlog.reset_cache()
    return run_replay(ex, Store(), Heal(ex, use_llm), _plan(), PAGE, tempfile.mkdtemp(), run_id="t")


# --- the rule: a re-ground is never accepted outright ----------------------------------------------
def test_a_reground_is_never_accepted_outright():
    """The claim is about the RULE, so it is asserted against the gate directly and at every threshold
    setting — not against one arithmetic coincidence."""
    assert is_reground("css") and is_reground("visual"), "the re-ground set lost a member"
    assert not is_reground("testid") and not is_reground("role_name"), "a frozen key is not a re-ground"

    ex = Ex([NEW])                       # only the LLM's selector resolves -> re-ground path
    r = Heal(ex, use_llm=True).heal({
        "step": 1, "semantic_id": "sid-pay", "page_path": PAGE, "intent": "click 'Pay'",
        "attempted_locator": FROZEN, "alternatives": _plan()["steps"][0]["alternatives"],
        "dom_hash": "d", "interactives": []})
    assert r["outcome"] != "auto_healed", r
    assert r["confidence"] < AUTO, r


def test_the_rule_holds_even_if_a_reground_prior_is_raised():
    """The rule guards a FUTURE change, so the future is what the test must create.

    Today no re-ground reaches AUTO on its own (css 0.585, visual 0.80 against 0.85), which means the
    cap never fires and removing it breaks nothing — a mutation proved exactly that. What the cap is
    FOR is the day someone raises a prior or softens the overconfidence discount: before ADR-080 that
    edit would have promoted an unverified re-ground into the silently-applied band with every test
    still green. So the test raises the prior itself and asserts the rule survives it."""
    import brain.healing as h
    original = dict(h.PRIORS)
    try:
        h.PRIORS["visual"] = 0.99          # far above AUTO — the "someone bumped it" future
        ex = Ex([NEW])
        eng = HealingEngine(ex, Store(), "r", use_llm=False)
        # Feed the gate directly: this is about the RULE, not about how a candidate is produced.
        r = eng._gate({"step": 1, "semantic_id": "sid", "page_path": PAGE, "intent": "i",
                       "attempted_locator": FROZEN, "dom_hash": "d"},
                      "visual", NEW, h.PRIORS["visual"])
        assert r["outcome"] != "auto_healed", r
        assert r["confidence"] < h.AUTO, r
        # And the control: a FROZEN key at the same raised confidence still is accepted outright, so
        # the cap is narrow rather than a blanket refusal.
        r2 = eng._gate({"step": 1, "semantic_id": "sid", "page_path": PAGE, "intent": "i",
                        "attempted_locator": FROZEN, "dom_hash": "d"},
                       "role_name", MOVED, 0.99)
        assert r2["outcome"] == "auto_healed", r2
    finally:
        h.PRIORS.clear()
        h.PRIORS.update(original)


def test_a_frozen_key_still_is_accepted_outright():
    """The negative control. Without it the rule above would also be satisfied by a gate that refuses
    everything — and self-healing would be dead while every assertion stayed green."""
    ex = Ex([MOVED])                     # the testid is gone; role+name (FROZEN WITH THE PLAN) resolves
    r = Heal(ex).heal({
        "step": 1, "semantic_id": "sid-pay", "page_path": PAGE, "intent": "click 'Pay'",
        "attempted_locator": FROZEN, "alternatives": _plan()["steps"][0]["alternatives"],
        "dom_hash": "d", "interactives": []})
    assert r["outcome"] == "auto_healed", r
    assert r["confidence"] >= AUTO, r
    assert r["strategy"] == "role_name", r


# --- the run says so -------------------------------------------------------------------------------
def test_a_clean_run_claims_no_unverified_heal():
    """Control: nothing drifted, so nothing may be reported as applied-without-confidence."""
    ex = Ex([FROZEN])                    # the frozen locator still works; no heal at all
    rep = _run(ex)
    assert rep["drift"]["rebind"] == 0 and rep["drift"]["reground"] == 0, rep["drift"]
    assert "heal.applied_unverified" not in (rep.get("degradations") or []), rep.get("degradations")
    assert "unverified" not in rep["drift"], rep["drift"]


def test_an_optimistically_applied_heal_reaches_the_verdict():
    """A heal below AUTO that was nevertheless APPLIED must appear as a degradation, which is what
    carries it to the hub badge, report.html and junit.xml (ADR-077)."""
    ex = Ex([WEAK])
    rep = _run(ex)
    # Precondition: the step actually RAN on the healed locator. Without it these assertions would be
    # satisfied by a run that simply failed, which is the opposite of the case under test.
    assert rep["exit_code"] == 0, rep
    assert rep["drift"]["rebind"] == 1, rep["drift"]
    assert rep["drift"].get("unverified") == 1, rep["drift"]
    assert "heal.applied_unverified" in (rep.get("degradations") or []), rep.get("degradations")


def test_the_drift_row_says_how_the_heal_was_accepted():
    ex = Ex([WEAK])
    rep = _run(ex)
    el = rep["drift"]["elements"][0]
    assert el.get("outcome"), "the drift row carries no outcome at all"
    assert el["outcome"] != "auto_healed", el
    assert el.get("confidence") is not None and el["confidence"] < AUTO, el


# --- the three surfaces ----------------------------------------------------------------------------
def test_report_html_names_the_acceptance_and_the_loss():
    ex = Ex([WEAK])
    rep = _run(ex)
    html = render_html(rep)
    assert "accepted as" in html, "the drift table gained no acceptance column"
    assert "unverified" in html, "an optimistically applied heal is not marked in the table"
    sentence = eventlog.verdict_sentence("heal.applied_unverified")
    assert sentence and sentence != "heal.applied_unverified", "the catalogue lost its verdict sentence"
    assert sentence in html, "the report does not say what the unverified heal MEANS"


def test_junit_marks_the_case_and_the_suite():
    ex = Ex([WEAK])
    rep = _run(ex)
    root = ET.fromstring(to_junit(rep))
    su = root.find("testsuite")
    props = {p.get("name"): p.get("value") for p in su.find("properties").findall("property")}
    assert "heal.applied_unverified" in (props.get("degradations") or ""), props
    blob = ET.tostring(su, encoding="unicode")
    assert "WITHOUT FULL CONFIDENCE" in blob, blob[:400]
    # Still a PASS: ADR-005/017 chose optimistic application, and this change does not revisit that.
    assert su.get("failures") == "0" and su.get("errors") == "0", su.attrib


def test_junit_of_a_confident_heal_says_nothing_of_the_kind():
    ex = Ex([MOVED])                     # rebind at 0.90 -> auto_healed
    rep = _run(ex)
    blob = ET.tostring(ET.fromstring(to_junit(rep)).find("testsuite"), encoding="unicode")
    assert "WITHOUT FULL CONFIDENCE" not in blob, blob[:400]


# --- thresholds are configurable and honest --------------------------------------------------------
def test_thresholds_come_from_the_environment_and_survive_garbage():
    import importlib
    import brain.healing as h
    for val, want in (("0.70", 0.70), ("nonsense", 0.85), ("5", 0.85), ("", 0.85)):
        os.environ["SENTINEL_HEAL_AUTO"] = val
        importlib.reload(h)
        assert h.AUTO == want, (val, h.AUTO)
    os.environ.pop("SENTINEL_HEAL_AUTO", None)
    importlib.reload(h)
    assert h.AUTO == 0.85 and h.FLAG == 0.60, (h.AUTO, h.FLAG)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok   {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} checks passed")
