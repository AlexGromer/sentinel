"""Offline gate: a re-grounded locator is checked against the element the plan froze (ADR-082).

Run:  .venv/bin/python tests/test_heal_identity_offline.py

`healing.py` step 6 used to establish one thing only — that the candidate resolves to EXACTLY ONE live
element — and said so in a comment: "One match is one match, not the right one." Nothing compared the
element found with the element the plan meant, so a re-ground could bind a different control and the
run reported an ordinary heal.

What this gate pins:
  * identity is a PREDICATE, not a score — there is no threshold to calibrate, which is the whole
    reason it can exist at all while GAP-RISK-002 stands (PRIORS are admittedly unmeasured);
  * it is STRICTLY STRONGER than the probe that produced the candidate, because Playwright's
    getByRole matches an accessible name case-insensitively and by substring;
  * a re-BIND is not annotated at all — the plan vouches for a frozen key, and inventing a doubt
    there would be as dishonest as hiding one on a re-ground;
  * the cache no longer smuggles a re-ground past the gate;
  * `healing.is_reground` and `replay._drift_entry` answer the same question the same way.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import eventlog                                             # noqa: E402
from brain.healing import (AUTO, FLAG, CONTRADICTED, UNVERIFIABLE, VERIFIED,  # noqa: E402
                           HealingEngine, descriptor_to_locator, identity,
                           is_reground, pick_confidence)
from brain.replay import _drift_entry, run_replay                      # noqa: E402
from brain.state import canonical_plan_hash                            # noqa: E402

PAGE = "file:///s/app.html"
FROZEN = {"role": "button", "name": "Pay now"}     # the primary locator every real plan carries
ALTS = [{"strategy": "testid", "locator": {"testid": "pay"}, "prior": 0.95},
        {"strategy": "role_name", "locator": FROZEN, "prior": 0.90}]

# The drifted page: the control was renamed and re-tagged, so BOTH frozen keys are dead and the run
# reaches the re-ground tier. This is what a re-ground actually needs — the fixture used by
# test_heal_unverified_offline drives a re-BIND and could never exercise this file.
LIVE_RENAMED = [{"role": "button", "name": "Confirm payment", "testid": "confirm-v2"}]
LIVE_SAME_NAME = [{"role": "button", "name": "Pay Now", "testid": "pay-v2"}]  # same name, new testid


class Ex:
    def __init__(self, resolves, counts=None):
        self.resolves, self.counts, self.url = resolves, counts or {}, PAGE
        self.probes = []

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", self.url); return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- page app", "nodeCount": 2}
        if m == "browser.interactives":
            return {"elements": self.elements}
        if m == "browser.links":
            return {"links": []}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.setOfMarks":
            with open(p["path"], "wb") as f:
                f.write(b"\x89PNG fake")
            return {"marks": self.marks, "path": p["path"]}
        if m == "browser.probe":
            loc = p.get("locator")
            self.probes.append(loc)
            key = json.dumps(loc, sort_keys=True)
            if key in self.counts:
                return {"count": self.counts[key]}
            return {"count": 1 if loc in self.resolves else 0}
        if m in ("browser.click", "browser.fill", "browser.type", "browser.select"):
            if p.get("locator") in self.resolves:
                return {"ok": True}
            raise RuntimeError("not found")
        return {}

    elements: list = []
    marks: list = []

    def close(self):
        pass


class Store:
    """Store stub with an injectable cache row, so the cache path can be driven directly."""

    def __init__(self, cached=None):
        self.cached, self.saved, self.audited = cached, [], []

    def lookup(self, *a, **k): return self.cached
    def evict_stale(self, *a, **k): return None
    def bump_used(self, *a, **k): return None
    def save_locator(self, *a, **k): self.saved.append(a)
    def audit(self, **row): self.audited.append(row)
    def record_step(self, *a, **k): return False
    def get_golden(self, *a, **k): return None
    def save_golden(self, *a, **k): return None


class Backend:
    """Picks element `index` from whatever list the tier offers."""
    model, supports_vision = "fake", False

    def __init__(self, index=0):
        self.index = index

    def complete(self, prompt, **kw):
        return type("R", (), {"text": json.dumps({"index": self.index}),
                              "data": {"index": self.index},
                              "usage": {}, "prompt_tokens": 0, "completion_tokens": 0})()


class VisionBackend:
    """Answers with a MARK number — the tier-7 path, which is the only re-ground applied today."""
    model, supports_vision = "fake-vision", True

    def __init__(self, mark=0):
        self.mark = mark

    def complete_vision(self, prompt, image_b64, **kw):
        return type("R", (), {"text": json.dumps({"mark": self.mark}), "data": None,
                              "usage": {}, "prompt_tokens": 0, "completion_tokens": 0})()


def _engine(ex, store=None, index=0):
    eng = HealingEngine(ex, store or Store(), "r", use_llm=True, backend=Backend(index))
    return eng


def _ctx(live, frozen=FROZEN, alts=ALTS, probe_count=0):
    return {"step": 1, "semantic_id": "sid-pay", "page_path": PAGE, "intent": "click 'Pay now'",
            "attempted_locator": frozen, "alternatives": list(alts), "dom_hash": "d",
            "interactives": live, "probe_count": probe_count}


# --- the predicate ---------------------------------------------------------------------------------
def test_identity_is_a_predicate_over_role_and_name():
    """Positive and negative in one place: without the negative, a function returning True forever
    would satisfy the positive and the whole mechanism would be decorative."""
    frozen = {"role": "button", "name": "Pay now"}
    assert frozen.get("name"), "a comparison against an empty frozen name is vacuous"
    assert identity(frozen, {"role": "button", "name": "Pay now"}) is True
    assert identity(frozen, {"role": "button", "name": "Confirm payment"}) is False
    assert identity(frozen, {"role": "link", "name": "Pay now"}) is False, "role is part of identity"


def test_identity_normalises_case_and_whitespace_but_nothing_else():
    """The accessible name arrives trimmed and collapsed by the executor; matching must survive that
    without becoming a fuzzy match, which would quietly reintroduce a threshold."""
    frozen = {"role": "button", "name": "Pay now"}
    assert identity(frozen, {"role": "BUTTON", "name": "  pay   NOW "}) is True
    assert identity(frozen, {"role": "button", "name": "Paynow"}) is False, "no fuzzy matching"


def test_identity_is_stricter_than_the_probe_that_produced_the_candidate():
    """The point of the check. `buildLocator` passes `{ name }` to getByRole with no `exact` flag, so
    Playwright matches by SUBSTRING, case-insensitively: a probe for "Pay" is satisfied by "Pay now".
    Equality is not, and that difference is exactly the class of wrong binding this closes."""
    assert identity({"role": "button", "name": "Pay"},
                    {"role": "button", "name": "Pay now"}) is False


def test_identity_reports_unverifiable_rather_than_guessing():
    """None is a third answer, never a synonym for False: refusing a heal because nobody recorded the
    evidence would punish a plan for a decision it never made (fail-open by DATA, Alex 2026-07-26)."""
    assert identity({"testid": "pay"}, {"role": "button", "name": "X"}) is None, "no frozen name"
    assert identity(FROZEN, {}) is None, "the tier observed nothing"
    assert identity({}, {"role": "button", "name": "X"}) is None
    assert identity(FROZEN, {"role": "", "name": ""}) is None


# --- the class: frozen or not ----------------------------------------------------------------------
def test_the_class_is_membership_in_the_frozen_plan_not_a_hardcoded_list():
    """ADR-071 chose membership; `healing.is_reground` used to hold a literal {css, visual} instead,
    so one heal could be a re-bind on the drift report and a re-ground at the gate."""
    assert ALTS, "membership over an empty list would be vacuously true for everything"
    assert not is_reground("role_name", ALTS) and not is_reground("testid", ALTS)
    assert is_reground("llm_pick", ALTS) and is_reground("visual", ALTS)
    # An authored or imported plan that legitimately freezes a css key makes USING it a re-bind.
    assert not is_reground("css", [{"strategy": "css", "locator": {"css": "#x"}}])
    # A step that froze nothing vouches for nothing.
    assert is_reground("testid", [])


def test_the_gate_and_the_drift_report_agree_on_the_class():
    """The two definitions were independent and pinned by separate tests, so nothing would have caught
    them diverging. This asserts them against the SAME inputs."""
    step = {"step_id": 1, "semantic_id": "s", "alternatives": ALTS}
    for strategy in ("testid", "role_name", "llm_pick", "visual", "css"):
        row = _drift_entry(step, FROZEN, {"strategy": strategy, "confidence": 0.7,
                                          "outcome": "flagged", "locator": {}}, PAGE)
        expected = "reground" if is_reground(strategy, ALTS) else "rebind"
        assert row["kind"] == expected, (strategy, row["kind"], expected)


# --- end to end through the engine -----------------------------------------------------------------
def test_a_renamed_control_is_healed_but_the_contradiction_is_reported():
    """The product decision (Alex 2026-07-26): a re-ground whose identity is CONTRADICTED is still
    applied — repairing a renamed control is what self-healing is for — but it is applied visibly."""
    eventlog.reset_degradations()
    ex = Ex([{"testid": "confirm-v2"}])
    ex.elements = LIVE_RENAMED
    r = _engine(ex).heal(_ctx(LIVE_RENAMED))
    assert r["strategy"] == "llm_pick" and r["locator"] == {"testid": "confirm-v2"}, r
    assert r["outcome"] == "flagged", r                  # applied, not refused
    assert r["identity"] == CONTRADICTED, r
    assert "heal.identity_contradicted" in eventlog.degradations(), eventlog.degradations()


def test_a_re_ground_onto_the_same_named_control_is_verified():
    """The negative control for the test above. Without it, a mechanism that answered CONTRADICTED to
    everything would satisfy every other assertion in this file."""
    eventlog.reset_degradations()
    ex = Ex([{"testid": "pay-v2"}])
    ex.elements = LIVE_SAME_NAME
    r = _engine(ex).heal(_ctx(LIVE_SAME_NAME))
    assert r["outcome"] == "flagged", r
    assert r["identity"] == VERIFIED, r
    assert "heal.identity_contradicted" not in eventlog.degradations(), eventlog.degradations()
    assert "heal.identity_unverifiable" not in eventlog.degradations(), eventlog.degradations()


def test_a_rebind_carries_no_identity_annotation():
    """A key the plan froze needs no identity check, and claiming one would be noise. `None`, not
    UNVERIFIABLE: the run has no doubt here to report."""
    ex = Ex([{"testid": "pay"}])                 # the FROZEN testid alternative still resolves
    ex.elements = LIVE_RENAMED
    r = _engine(ex).heal(_ctx(LIVE_RENAMED))
    assert r["strategy"] == "testid" and r["outcome"] == "auto_healed", r
    assert r["identity"] is None, r


def test_a_verified_re_ground_is_still_not_applied_silently():
    """Identity does not repeal ADR-080. Confirming role+name cannot rule out a SECOND control with
    the same role and name, so a re-ground stays capped even when verified — and the test creates the
    future in which that matters, since no strategy reaches AUTO on its own today."""
    import brain.healing as h
    original = dict(h.PRIORS)
    try:
        h.PRIORS["testid"] = 0.99            # the "someone raised a prior" future
        ex = Ex([{"testid": "pay-v2"}])
        ex.elements = LIVE_SAME_NAME
        r = _engine(ex).heal(_ctx(LIVE_SAME_NAME))
        assert r["identity"] == VERIFIED, r
        assert r["outcome"] != "auto_healed", r
        assert r["confidence"] < h.AUTO, r
    finally:
        h.PRIORS.clear()
        h.PRIORS.update(original)


def test_an_unverifiable_re_ground_is_applied_and_says_so():
    """Fail-open by DATA (Alex 2026-07-26): a testid-only primary froze no name to compare against, so
    the heal proceeds — but it is reported as unverifiable, never as verified."""
    eventlog.reset_degradations()
    ex = Ex([{"testid": "confirm-v2"}])
    ex.elements = LIVE_RENAMED
    r = _engine(ex).heal(_ctx(LIVE_RENAMED, frozen={"testid": "pay"}))
    assert r["outcome"] == "flagged", r
    assert r["identity"] == UNVERIFIABLE, r
    assert "heal.identity_unverifiable" in eventlog.degradations(), eventlog.degradations()


# --- the cache no longer smuggles a re-ground past the gate ----------------------------------------
def test_a_cached_re_ground_is_capped_and_checked_like_a_fresh_one():
    """`cache_hit` returned before `_gate`, so a locator accepted optimistically ONCE was replayed at
    full stored confidence forever — past the ADR-080 cap and past this check. The cache may amortize
    the model call; it may not amortize the doubt."""
    eventlog.reset_degradations()
    cached = {"value": json.dumps({"role": "button", "name": "Confirm payment"}),
              "strategy": "llm_pick", "confidence": 0.99}     # a stored value ABOVE AUTO
    ex = Ex([{"role": "button", "name": "Confirm payment"}])
    ex.elements = LIVE_RENAMED
    r = _engine(ex, Store(cached)).heal(_ctx(LIVE_RENAMED))
    assert r["outcome"] == "cache_hit", r
    assert r["confidence"] < AUTO, r                  # the cap applies on the cache path too
    assert r["identity"] == CONTRADICTED, r
    assert "heal.identity_contradicted" in eventlog.degradations(), eventlog.degradations()


def test_a_cached_rebind_is_left_alone():
    """The negative control: the cap and the check are narrow. A cached FROZEN key keeps its stored
    confidence, or the cache fix would have quietly disabled amortization for everything."""
    eventlog.reset_degradations()
    cached = {"value": json.dumps({"testid": "pay"}), "strategy": "testid", "confidence": 0.95}
    ex = Ex([{"testid": "pay"}])
    ex.elements = LIVE_RENAMED
    r = _engine(ex, Store(cached)).heal(_ctx(LIVE_RENAMED))
    assert r["outcome"] == "cache_hit" and r["confidence"] == 0.95, r
    assert r["identity"] is None, r
    assert eventlog.degradations() == [], eventlog.degradations()


# --- the grounded pick -----------------------------------------------------------------------------
def test_the_pick_yields_a_real_key_and_its_confidence_comes_from_that_key():
    """No new constant enters the system: the prior is looked up for the locator the pick produced,
    discounted once by the same 0.90 the css tier already applied."""
    import brain.healing as h
    assert descriptor_to_locator({"role": "button", "name": "X", "testid": "t"}) == {"testid": "t"}
    assert descriptor_to_locator({"role": "button", "name": "X"}) == {"role": "button", "name": "X"}
    assert descriptor_to_locator({"role": "button"}) is None, "half a descriptor is not a locator"
    assert pick_confidence({"testid": "t"}) == h.PRIORS["testid"] * h.PICK_DISCOUNT
    assert pick_confidence({"role": "button", "name": "X"}) == h.PRIORS["role_name"] * h.PICK_DISCOUNT
    # And the discounted values still clear FLAG, or the tier would be dead code again.
    assert pick_confidence({"testid": "t"}) >= FLAG and pick_confidence({"role": "b", "name": "X"}) >= FLAG


def test_the_visual_tier_is_checked_too_and_needs_no_new_executor_method():
    """Tier-7 is the ONLY re-ground applied today — the text tier scored below FLAG until ADR-082 —
    so a mechanism that checked only the text tier would leave the live path uncovered. A mutation
    proved exactly that: discarding the chosen mark broke nothing until this test existed.

    The mark already carries role/name/testid; the tier simply threw them away after building the
    locator. That is why identity here costs no new RPC.
    """
    eventlog.reset_degradations()
    ex = Ex([{"testid": "confirm-v2"}])
    ex.elements = []                     # text tier has nothing to pick -> escalate to vision
    ex.marks = [{"mark": 0, "role": "button", "name": "Confirm payment", "testid": "confirm-v2",
                 "bbox": {}}]
    assert ex.marks[0]["name"] != FROZEN["name"], "the fixture must actually differ, or VERIFIED is vacuous"
    eng = HealingEngine(ex, Store(), "r", use_llm=True, use_visual=True, backend=VisionBackend(0))
    r = eng.heal(_ctx([]))
    assert r["strategy"] == "visual" and r["outcome"] == "flagged", r
    assert r["identity"] == CONTRADICTED, r
    assert "heal.identity_contradicted" in eventlog.degradations(), eventlog.degradations()

    # Negative control on the same path: a mark whose role+name match the frozen locator verifies.
    eventlog.reset_degradations()
    ex2 = Ex([{"testid": "pay-v2"}])
    ex2.elements = []
    ex2.marks = [{"mark": 0, "role": "button", "name": "Pay now", "testid": "pay-v2", "bbox": {}}]
    eng2 = HealingEngine(ex2, Store(), "r", use_llm=True, use_visual=True, backend=VisionBackend(0))
    r2 = eng2.heal(_ctx([]))
    assert r2["strategy"] == "visual" and r2["identity"] == VERIFIED, r2
    assert "heal.identity_contradicted" not in eventlog.degradations(), eventlog.degradations()


def test_the_model_cannot_reach_an_element_the_executor_never_reported():
    """The grounding guarantee, inherited from ADR-022/027: the answer space IS the live element list,
    so there is no reply that produces a locator for an element nobody observed."""
    ex = Ex([])
    ex.elements = LIVE_RENAMED
    for bad in (99, -1):
        r = _engine(ex, index=bad).heal(_ctx(LIVE_RENAMED))
        assert r["outcome"] == "failed", (bad, r)


# --- the run carries it ----------------------------------------------------------------------------
def test_replay_tells_the_engine_why_the_frozen_locator_failed():
    """`probe_count` was computed in replay and thrown away, collapsing "the element moved" (0) and
    "the name is now ambiguous" (>= 2) into one. The second is the case identity verification cannot
    resolve, so counting it is what will eventually make the residual hole measurable."""
    seen = {}

    class Spy:
        def heal(self, ctx):
            seen.update(ctx)
            return {"outcome": "failed", "confidence": 0.0}

    steps = [{"step_id": 1, "action_type": "click", "intent": "click button 'Pay now'",
              "semantic_id": "sid-pay", "is_milestone": False, "locator": FROZEN,
              "alternatives": list(ALTS)}]
    plan = {"plan_id": "p1", "steps": steps, "plan_hash": canonical_plan_hash(steps),
            "target_url": PAGE}
    ex = Ex([], counts={json.dumps(FROZEN, sort_keys=True): 3})    # ambiguous, not missing
    ex.elements = LIVE_RENAMED
    eventlog.reset_degradations()
    eventlog.reset_cache()
    run_replay(ex, Store(), Spy(), plan, PAGE, tempfile.mkdtemp(), run_id="t")
    assert seen, "the heal path was never reached — the fixture proves nothing"
    assert seen.get("probe_count") == 3, seen.get("probe_count")


def test_the_drift_row_names_the_identity_outcome():
    """The reader of report.html/junit must see WHICH of the three happened, not just that a re-ground
    occurred."""
    step = {"step_id": 1, "semantic_id": "s", "alternatives": ALTS, "intent": "click 'Pay now'"}
    row = _drift_entry(step, FROZEN, {"strategy": "llm_pick", "confidence": 0.725,
                                      "outcome": "flagged", "locator": {"testid": "confirm-v2"},
                                      "identity": CONTRADICTED}, PAGE)
    assert row["kind"] == "reground" and row["identity"] == CONTRADICTED, row
    rebind = _drift_entry(step, FROZEN, {"strategy": "role_name", "confidence": 0.9,
                                         "outcome": "auto_healed", "locator": FROZEN,
                                         "identity": None}, PAGE)
    assert rebind["kind"] == "rebind" and rebind["identity"] is None, rebind


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} identity-verification tests passed")
