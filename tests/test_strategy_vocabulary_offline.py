"""Offline gate: one strategy vocabulary, and a plan's prior can only lower confidence (ADR-083).

Run:  .venv/bin/python tests/test_strategy_vocabulary_offline.py

Two producers write `alternatives[].strategy` — the explorer (`graph.py`) and the MV3 recorder bridge
(`record_bridge.py`) — and one consumer reads it (`healing.py`). Each kept its own copy of the
vocabulary, and they drifted: the recorder wrote `text` where the explorer wrote `text_role`, and
`PRIORS` knew only the latter. A recorded plan's text alternative therefore scored the
unknown-strategy default of 0.5 — BELOW FLAG (0.60) — so it was found, discarded, and never applied.
Silently. The heal did not happen and nothing said why.

The second half is the `prior` field. Both producers wrote it and NOBODY read it, so an importer that
carefully ranked a foreign suite's locators would have had its ranking thrown away. It is read now,
but only downward: a plan cannot vouch for itself.
"""
import os
import pathlib
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import strategies as S                                  # noqa: E402
from brain.healing import FLAG, HealingEngine, is_reground         # noqa: E402
from brain.record_bridge import build_scenario                     # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
PAGE = "file:///s/app.html"
TEXT_LOC = {"text": "Pay now"}


class Ex:
    """Only the TEXT locator resolves: the testid and role+name keys are gone."""

    def __init__(self, resolves):
        self.resolves = resolves

    def call(self, m, **p):
        if m == "browser.probe":
            return {"count": 1 if p.get("locator") in self.resolves else 0}
        return {}


class Store:
    def lookup(self, *a, **k): return None
    def evict_stale(self, *a, **k): return None
    def bump_used(self, *a, **k): return None
    def save_locator(self, *a, **k): return None
    def audit(self, **row): return None


def _ctx(alts):
    return {"step": 1, "semantic_id": "sid", "page_path": PAGE, "intent": "click 'Pay now'",
            "attempted_locator": {"testid": "pay"}, "alternatives": alts, "dom_hash": "d",
            "interactives": [], "probe_count": 0}


# --- the spelling that broke healing --------------------------------------------------------------
def test_a_recorded_plan_heals_through_its_text_alternative():
    """The defect, end to end. `text` is what the recorder has always written; before ADR-083 it
    resolved to 0.5, below FLAG, so this heal produced `needs_review` and the step failed."""
    alts = [{"strategy": "text", "locator": TEXT_LOC, "prior": 0.80}]   # the RECORDER's spelling
    r = HealingEngine(Ex([TEXT_LOC]), Store(), "r", use_llm=False).heal(_ctx(alts))
    assert r["outcome"] in ("auto_healed", "flagged"), r     # applied, not discarded
    assert r["confidence"] == S.PRIORS[S.TEXT_ROLE], r        # 0.80, not the 0.5 default
    assert r["confidence"] >= FLAG, r


def test_the_explorer_spelling_still_heals_identically():
    """The negative control: fixing the alias must not have moved the canonical name."""
    alts = [{"strategy": S.TEXT_ROLE, "locator": TEXT_LOC, "prior": 0.80}]
    r = HealingEngine(Ex([TEXT_LOC]), Store(), "r", use_llm=False).heal(_ctx(alts))
    assert r["confidence"] == S.PRIORS[S.TEXT_ROLE], r


def test_an_unknown_strategy_still_falls_to_the_low_default():
    """The default is not the bug — reaching it by ACCIDENT was. A genuinely unknown key must still
    score below FLAG, or this fix would have turned a safety default into a rubber stamp."""
    alts = [{"strategy": "handwave", "locator": TEXT_LOC, "prior": 0.80}]
    r = HealingEngine(Ex([TEXT_LOC]), Store(), "r", use_llm=False).heal(_ctx(alts))
    assert S.UNKNOWN_PRIOR < FLAG, S.UNKNOWN_PRIOR
    assert r["outcome"] == "needs_review", r


def test_the_alias_is_resolved_on_both_sides_of_the_reground_test():
    """`is_reground` compares a healed strategy against the names the plan froze. Canonicalising only
    one side would classify a recorded plan's own key as a re-ground — flagging every recorded run as
    unverified drift. Both sides, or neither."""
    recorded = [{"strategy": "text", "locator": TEXT_LOC}]
    assert not is_reground("text", recorded), "the plan's own spelling"
    assert not is_reground(S.TEXT_ROLE, recorded), "the same strategy, canonical spelling"
    explored = [{"strategy": S.TEXT_ROLE, "locator": TEXT_LOC}]
    assert not is_reground("text", explored), "the same strategy, legacy spelling"
    # And the control: a key the plan never froze is still a re-ground.
    assert is_reground(S.LLM_PICK, recorded) and is_reground(S.VISUAL, explored)


# --- the plan's prior: downward only ---------------------------------------------------------------
def test_a_plan_may_lower_its_own_confidence_but_never_raise_it():
    """An importer that ranked a foreign suite's locators conservatively deserves to be respected;
    a plan that claims 0.99 for a css key must not be. `testid` sits at 0.95 against AUTO 0.85, so
    reading the field as-is would let any file promote a weak locator into the silently-applied
    band — and an imported plan is by definition someone else's."""
    assert S.prior_for(S.CSS, 0.99) == S.PRIORS[S.CSS], "a plan cannot vouch for itself"
    assert S.prior_for(S.TESTID, 0.30) == 0.30, "a conservative importer must be respected"
    assert S.prior_for(S.TESTID) == S.PRIORS[S.TESTID], "no claim = the table"


def test_the_engine_honours_a_lowered_prior_and_ignores_an_inflated_one():
    """The same property through the engine, not just the helper — the rotation is where it matters."""
    low = [{"strategy": S.TEXT_ROLE, "locator": TEXT_LOC, "prior": 0.10}]
    r = HealingEngine(Ex([TEXT_LOC]), Store(), "r", use_llm=False).heal(_ctx(low))
    assert r["confidence"] == 0.10 and r["outcome"] == "needs_review", r
    high = [{"strategy": S.CSS, "locator": TEXT_LOC, "prior": 0.99}]
    r2 = HealingEngine(Ex([TEXT_LOC]), Store(), "r", use_llm=False).heal(_ctx(high))
    assert r2["confidence"] == S.PRIORS[S.CSS], r2


def test_a_malformed_prior_falls_back_to_the_table_exactly():
    """Same rule the confidence thresholds follow: a typo in someone's plan must not turn a passing
    replay into a stack trace.

    Asserted as EQUALITY with the table, not as "no higher than". A mutation proved why: dropping the
    range check leaves `min(0.95, -1) == -1`, which is still "no higher than" and passed the weaker
    assertion — a negative confidence, quietly accepted from a plan file. Out of range is garbage,
    and garbage falls back; it does not get partially applied."""
    for junk in ("high", None, -1, 42, [0.9], float("nan"), float("inf"), True):
        assert S.prior_for(S.TESTID, junk) == S.PRIORS[S.TESTID], (junk, S.prior_for(S.TESTID, junk))
    # And the control: a well-formed lower value is still honoured, so the fallback is narrow.
    assert S.prior_for(S.TESTID, 0.42) == 0.42


# --- the recorder produces canonical names ---------------------------------------------------------
def test_the_recorder_writes_the_canonical_spelling():
    # A BARE locator dict, so the strategy is INFERRED — that inference is where the recorder used to
    # produce the drifted spelling. Passing an explicit {"strategy": ...} would test nothing.
    events = [{"type": "click", "url": PAGE, "ts": 1,
               "selectorCandidates": [{"text": "Pay now"}, {"css": "#pay"}]}]
    sc, _unmatched = build_scenario(events, target_url=PAGE)
    alts = [a for s in sc["steps"] for a in (s.get("alternatives") or [])]
    assert alts, "the fixture produced no alternatives — the assertion below would be vacuous"
    names = {a["strategy"] for a in alts} | {s.get("strategy") for s in sc["steps"]}
    assert "text" not in names, names          # the drifted spelling is gone at the source
    for a in alts:
        assert S.canonical(a["strategy"]) == a["strategy"], a


# --- structural: one vocabulary, not three ---------------------------------------------------------
def test_no_module_keeps_a_private_copy_of_the_vocabulary():
    """The drift was possible because three files each held their own literal table. This is the gate
    against re-introducing one: a PRIORS-shaped dict literal may exist in exactly one module."""
    pattern = re.compile(r'\{\s*["\']testid["\']\s*:\s*0\.\d+')
    offenders = [p.name for p in sorted((REPO / "brain").glob("*.py"))
                 if pattern.search(p.read_text()) and p.name != "strategies.py"]
    assert not offenders, f"a private copy of the strategy priors is back in: {offenders}"


def test_every_locator_key_maps_to_a_known_strategy():
    """The executor accepts exactly six locator shapes; each must name a strategy the priors know, or
    a plan can carry a key whose confidence silently falls to the unknown default again."""
    assert set(S.STRATEGY_BY_LOCATOR_KEY) == {"testid", "role", "label", "text", "css", "xpath"}
    for key, strat in S.STRATEGY_BY_LOCATOR_KEY.items():
        assert strat in S.PRIORS, (key, strat)
    for alias, target in S.ALIASES.items():
        assert alias not in S.PRIORS, f"an alias must not also be a strategy: {alias}"
        assert target in S.PRIORS, (alias, target)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} strategy-vocabulary tests passed")
