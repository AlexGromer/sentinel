"""The locator-strategy vocabulary: one name per strategy, one prior per name (ADR-083).

This exists as its own module because the vocabulary has TWO producers and one consumer, and the
producers drifted. `graph.py` (the explorer) writes `text_role`; `record_bridge.py` (the MV3 recorder)
writes `text`; `healing.PRIORS` knows only the first. A recorded plan's text alternative therefore
resolved to the unknown-strategy default of 0.5 — BELOW the FLAG threshold, so it was never applied
— and nothing said so. The heal simply did not happen.

Kept import-light on purpose: `record_bridge` must be able to import it without pulling in the LLM
backends that `healing` needs, which is the reason the bridge kept its own copy in the first place.

Two rules live here because they are the same decision:

  * a strategy has ONE canonical name, and legacy spellings are ALIASES rather than separate
    strategies — a plan written by an older recorder must keep healing;
  * a `prior` carried by a plan may only LOWER the confidence, never raise it. Both producers write
    the field and, before ADR-083, nothing read it. Reading it as-is would have handed any plan the
    ability to promote a weak locator into the silently-applied band (`testid` already sits at 0.95
    against AUTO 0.85), and an imported plan is by definition someone else's file. `min` keeps the
    ranking information an importer provides without letting it vouch for itself.
"""
from __future__ import annotations

TESTID = "testid"
ROLE_NAME = "role_name"
LABEL = "label"
TEXT_ROLE = "text_role"
CSS = "css"
XPATH = "xpath"
VISUAL = "visual"
LLM_PICK = "llm_pick"

# Per-strategy base PRIORS (docs/SELF_HEALING.md). Keys match `alternatives[].strategy` values.
#
# These are PRIORS, not probabilities. Nothing measures them: there is no confidence model, no
# calibration, and no record of how often a strategy was right — a heal's number is looked up from
# this table by strategy NAME and nothing else. Said plainly because GAPS.md used to claim an adaptive
# mechanism ("threshold 0.90 until N human-labelled outcomes") that was never built, and a reader who
# believed it would trust these numbers far more than they deserve. ADR-080.
PRIORS = {TESTID: 0.95, ROLE_NAME: 0.90, LABEL: 0.88, TEXT_ROLE: 0.80, CSS: 0.65,
          XPATH: 0.45, VISUAL: 0.80}  # visual (set-of-marks) lands in the FLAGGED band by design

# A strategy whose name we do not recognise gets this. It is deliberately below FLAG (0.60): an
# unknown key is not something to apply optimistically. What made that a defect rather than a policy
# was reaching it by ACCIDENT, through a spelling difference between two of our own files.
UNKNOWN_PRIOR = 0.5

# Spellings that mean an existing strategy under another name. `text` is what the MV3 recorder has
# always written for a text locator; the explorer calls the same thing `text_role`. Old recorded plans
# exist with that spelling, so it is resolved rather than rejected — and NOT added to PRIORS, because
# a synonym is not a strategy and having two keys for one thing is how this drifted to begin with.
ALIASES = {"text": TEXT_ROLE}

# pw-executor locator key -> the strategy that key represents. One map, so a producer cannot invent a
# strategy name by inferring it locally (`record_bridge` used to).
STRATEGY_BY_LOCATOR_KEY = {"testid": TESTID, "role": ROLE_NAME, "label": LABEL,
                           "text": TEXT_ROLE, "css": CSS, "xpath": XPATH}


def canonical(strategy) -> str:
    """The canonical name for `strategy`, resolving legacy spellings. Unknown names pass through
    unchanged: they still have to compare equal to themselves for re-bind/re-ground classification."""
    s = strategy or ""
    return ALIASES.get(s, s)


def prior_for(strategy, plan_prior=None) -> float:
    """The confidence to attach to a frozen key.

    `plan_prior` is the `prior` field the plan carries for this alternative. It may only lower the
    table's value — see the module docstring: a plan cannot vouch for itself, but an importer that
    ranked a foreign suite's locators conservatively deserves to have that respected. Garbage (None,
    a string, out of [0,1]) falls back to the table rather than raising, the same rule the confidence
    thresholds follow: a malformed plan must not turn a passing replay into a crash.
    """
    base = PRIORS.get(canonical(strategy), UNKNOWN_PRIOR)
    if plan_prior is None:
        return base
    try:
        p = float(plan_prior)
    except (TypeError, ValueError):
        return base
    if not 0.0 <= p <= 1.0:
        return base
    return min(base, p)
