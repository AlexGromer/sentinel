"""Sentinel healing engine (M2) — deterministic locator re-grounding.

Implements the M2 subset of docs/SELF_HEALING.md (see docs/M2_CONTRACT.md): cache lookup with
dom-hash amortization → L1–L6 strategy rotation (offline, no LLM) → optional LLM re-grounding
→ verify-before-accept (live re-probe + identity check, ADR-082) → confidence gate → persist +
append-only audit.

The offline path (cache + L1–L6 + probe) needs no network or API key. A locator is a dict in
pw-executor format, one of: {testid}, {role,name}, {label}, {text}, {css}, {xpath}.
"""
from __future__ import annotations

import json
import os

from .eventlog import log
from .llm import complete_structured, extract_json
from .otel import prompt_hash, set_llm_tokens, span
from .sanitize import fit_json_list, partial_note, safe_json, safe_text
from .strategies import PRIORS as _PRIORS
from .strategies import canonical, prior_for

# Structured-output schema for the text re-grounding call (an INDEX into the live element list | none).
# ADR-082 replaced a model-AUTHORED css selector with a choice among elements the executor actually
# reported — the same grounding pattern the planner uses (planner.py: `candidates[idx]`, ADR-022/027).
# Vision heal stays on extract_json (native vision+structured is the least-portable provider combo).
_SCHEMA_PICK = {"type": "object", "properties": {
    "index": {"type": "integer", "description": "0-based index of the element in current_elements"},
    "none": {"type": "boolean", "description": "true if no element matches the intent"}}}

# The strategy vocabulary and its priors live in `strategies.py` — one module, two producers
# (graph.py's explorer and record_bridge's recorder) and this consumer. They used to hold private
# copies, which is how `text` and `text_role` came to mean the same thing under two spellings, with
# only one of them known here (ADR-083). Re-exported so `PRIORS` keeps its name at the call sites and
# in the tests that raise a prior to create the future ADR-080 guards against.
PRIORS = _PRIORS

# The discount applied to a locator the MODEL chose rather than the plan froze. Not a new number: it is
# the same 0.90 the css tier already applied, kept so ADR-082 introduces no uncalibrated constant.
PICK_DISCOUNT = 0.90


def _env_conf(name: str, default: float) -> float:
    """A confidence threshold from the environment, clamped to [0,1]; garbage falls back to `default`.

    Falling back rather than raising: a typo in a CI variable must not turn a passing replay into a
    crash — the same rule replay.py applies to its own thresholds."""
    try:
        v = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return v if 0.0 <= v <= 1.0 else default


AUTO = _env_conf("SENTINEL_HEAL_AUTO", 0.85)  # >= this: applied silently
FLAG = _env_conf("SENTINEL_HEAL_FLAG", 0.60)  # >= this: applied OPTIMISTICALLY and reported (ADR-005/017)

# --- re-ground: a key the plan did NOT freeze ------------------------------------------------------
def is_reground(strategy: str, alternatives=None) -> bool:
    """True when nothing in the frozen plan vouches for this key (ADR-071 calls that a re-ground).

    Derived from MEMBERSHIP in `alternatives[]`, not from a hardcoded strategy list. ADR-071 chose
    membership deliberately — "the real question is 'was this key frozen with the plan?'" — and
    replay.py::_drift_entry has classified that way all along. This function used to hold a literal
    `{"css", "visual"}` instead, so the same heal could be a re-bind on the drift report and a
    re-ground at the gate; ADR-082 makes one definition serve both, and a test pins their agreement.

    A step that froze NO alternatives therefore vouches for nothing, and every key is a re-ground —
    which is the honest reading, not a degenerate one.
    """
    frozen = {canonical(a.get("strategy")) for a in (alternatives or []) if isinstance(a, dict)}
    return canonical(strategy) not in frozen


# --- identity: is the re-grounded element the one the plan meant? ----------------------------------
# Deliberately TRI-STATE and deliberately NOT a score. `PRIORS` above already says nothing measures
# it; a similarity threshold here would be a second uncalibrated number sitting next to the first
# (GAP-RISK-002). Equality of role and normalised name is a property, and a property is testable.
VERIFIED, CONTRADICTED, UNVERIFIABLE = "verified", "contradicted", "unverifiable"


def _norm(s) -> str:
    """Case-folded, whitespace-collapsed — the shape a comparison of accessible names has to use."""
    return " ".join(str(s or "").split()).casefold()


def identity(frozen: dict, live: dict):
    """True | False | None — is `live` the element `frozen` describes? None = nothing to compare.

    This is STRICTLY STRONGER than the probe that produced the candidate: `buildLocator` passes
    `{ name }` to getByRole with no `exact` flag (pw-executor/src/server.ts), so Playwright matches
    the accessible name case-insensitively and by SUBSTRING — "Pay" resolves against "Pay now".
    Python equality does not, which is exactly why a locator that probes to 1 can still be a
    different element.

    None rather than False when the plan froze no name (a testid-only primary) or the tier observed
    nothing: refusing a heal because nobody recorded the evidence would punish a plan for a decision
    it never made. UNVERIFIABLE is reported as its own state, never as verified.
    """
    if not frozen or not live:
        return None
    f_role, f_name = _norm(frozen.get("role")), _norm(frozen.get("name"))
    if not f_role or not f_name:
        return None                     # the plan froze nothing to compare against
    l_role, l_name = _norm(live.get("role")), _norm(live.get("name"))
    if not l_role and not l_name:
        return None                     # the tier observed nothing about the element
    return f_role == l_role and f_name == l_name


def _identity_state(verdict) -> str:
    if verdict is True:
        return VERIFIED
    if verdict is False:
        return CONTRADICTED
    return UNVERIFIABLE


def descriptor_to_locator(d: dict):
    """A live element descriptor -> a REAL locator (testid > role+name), never a coordinate (ADR-005).

    Shared by both re-ground tiers: `browser.interactives` elements and `browser.setOfMarks` marks
    carry the same three fields, so one mapping serves the text tier and the visual tier alike.

    ADR-095: `frame` rides along when the live element has one. It is a SCOPE, so it does not change
    which strategy this is — `pick_confidence` below still sees a testid or a role+name and scores it
    from the same table. Dropping it here would have been the silent kind of wrong: the heal would
    report a locator, the locator would name a control that exists only inside a frame, and the
    replay would fail on a step the audit had just called healed.
    """
    if not d:
        return None
    fr = {"frame": d["frame"]} if d.get("frame") else {}
    if d.get("testid"):
        return {"testid": d["testid"], **fr}
    if d.get("role") and d.get("name"):
        return {"role": d["role"], "name": d["name"], **fr}
    return None


def pick_confidence(loc: dict) -> float:
    """Confidence for a locator whose ELEMENT was chosen by a model rather than frozen by the plan.

    The prior comes from the locator we ended up with — after ADR-082 a re-ground yields a testid or
    a role+name, i.e. the same key classes the plan freezes — discounted once for the choice having
    been a model's. Both factors already existed; no new constant enters the system. The ADR-080 cap
    still applies afterwards, so the result lands in the FLAGGED band rather than being applied
    silently.
    """
    return PRIORS["testid" if loc.get("testid") else "role_name"] * PICK_DISCOUNT




# --- ADR-136: сколько символов перечня элементов уезжает в промпт лечения -------------------------
#
# Прежний предел текстового тира, перенесённый без изменения: волна меняет ФОРМУ укладки, а не порог.
# Тот же бюджет получил и визуальный тир, у которого кепа не было вовсе.
HEAL_MENU_CHARS = 3000


class HealingEngine:
    """Re-grounds a broken locator. `ex` = pw-executor client; `store` = interim locator store."""

    def __init__(self, ex, store, run_id: str, use_llm: bool = False, use_visual: bool = False,
                 backend=None) -> None:
        self.ex, self.store, self.run_id, self.use_llm = ex, store, run_id, use_llm
        self.use_visual = use_visual  # Tier-7 set-of-marks visual heal (M5-2, gated off by default)
        self._backend = None
        if use_llm:
            from .llm import make_backend
            self._backend = backend if backend is not None else make_backend("heal")
            if not self._backend:
                log("heal.no_llm_backend")

    def _probe(self, locator: dict) -> int:
        try:
            return int(self.ex.call("browser.probe", locator=locator).get("count", 0))
        except Exception as e:
            log("heal.probe_error", error=e)
            return 0

    def heal(self, ctx: dict) -> dict:
        """ctx = {step, semantic_id, page_path, intent, attempted_locator, alternatives, dom_hash, interactives}.

        Returns {locator, strategy, confidence, outcome} where outcome is one of
        cache_hit | auto_healed | flagged | needs_review | failed.
        """
        page, sid, dom = ctx["page_path"], ctx["semantic_id"], ctx["dom_hash"]

        alts = ctx.get("alternatives") or []

        # 3. cache lookup (amortization) — reuse a prior heal if the page hash still matches.
        cached = self.store.lookup(page, sid, dom)
        if cached:
            loc = json.loads(cached["value"])
            if self._probe(loc) == 1:
                strat, conf = cached["strategy"], cached["confidence"]
                # ADR-082: a cached RE-GROUND used to return here untouched, ahead of `_gate` — so a
                # locator accepted optimistically once was replayed at full stored confidence forever,
                # past the ADR-080 cap and past any identity check. The cache may amortize the LLM
                # call; it may not amortize the doubt. The cached locator is its own descriptor (after
                # ADR-082 a re-ground yields testid or role+name), which is what makes this checkable
                # without a live probe of the element's properties.
                state = None
                if is_reground(strat, alts):
                    ident = identity(ctx.get("attempted_locator") or {}, loc)
                    conf = self._cap(conf)
                    state = _identity_state(ident)
                    self._log_identity(ctx, strat, ident)
                self.store.bump_used(page, sid, dom)
                self._audit(ctx, strat, cached["value"], conf, "cache_hit", state)
                return {"locator": loc, "strategy": strat, "confidence": conf, "outcome": "cache_hit",
                        "identity": state}
        self.store.evict_stale(page, sid, dom)

        # 4. L1–L6 deterministic rotation (offline): first recorded alternative that resolves to 1.
        # A frozen key needs no identity check — the plan itself vouches for it (`live` stays None).
        chosen = None
        for alt in alts:
            strat, loc = alt.get("strategy"), alt.get("locator")
            if loc and self._probe(loc) == 1:
                chosen = (strat, loc, prior_for(strat, alt.get("prior")), None)
                break

        # 5. optional LLM re-grounding — only if deterministic rotation failed.
        if not chosen and self._backend:
            chosen = self._llm_reground(ctx)

        # Tier-7: set-of-marks VISUAL re-grounding (M5-2, ADR-005/017) — gated last resort.
        # Requires a vision-capable backend; a text-only provider skips straight to failed.
        if not chosen and self._backend and self.use_visual and self._backend.supports_vision:
            chosen = self._visual_reground(ctx)

        if not chosen:
            self._audit(ctx, "none", "null", 0.0, "failed")
            return {"outcome": "failed", "confidence": 0.0}

        strat, loc, conf, live = chosen
        # 6. verify-before-accept: the candidate MUST resolve to exactly 1 live element — AND, when
        # nothing in the plan vouches for the key, must not be CONTRADICTED by what the plan froze.
        #
        # One match is one match, not the right one: that is what the cardinality probe alone could
        # never establish, and it is why ADR-082 added the identity check below.
        if self._probe(loc) != 1:
            conf = 0.0
        return self._gate(ctx, strat, loc, conf,
                          identity(ctx.get("attempted_locator") or {}, live or {}))

    @staticmethod
    def _cap(conf: float) -> float:
        """ADR-080's cap, as a function so the cache path and the gate cannot drift apart."""
        return min(conf, (AUTO + FLAG) / 2) if conf >= AUTO else conf

    def _log_identity(self, ctx: dict, strat: str, verdict) -> None:
        """Say which of the three the identity check reached. Three explicit calls, not one
        parametrised line: the event catalogue is a static gate and rejects a computed code."""
        state = _identity_state(verdict)
        el = ctx.get("intent") or ctx.get("semantic_id") or ""
        if state == VERIFIED:
            log("heal.identity_verified", element=el, step=ctx.get("step"), strategy=strat)
        elif state == CONTRADICTED:
            log("heal.identity_contradicted", element=el, step=ctx.get("step"), strategy=strat,
                frozen=(ctx.get("attempted_locator") or {}).get("name"))
        else:
            log("heal.identity_unverifiable", element=el, step=ctx.get("step"), strategy=strat)

    def _gate(self, ctx: dict, strat: str, loc: dict, conf: float, ident=None) -> dict:
        """7. The confidence gate: turn a candidate + its confidence into an outcome.

        Split out of `heal` so the RULE below can be tested as a rule. It guards a future edit rather
        than today's numbers — a re-ground does not reach AUTO on its own — so a test that only drives
        today's strategies cannot tell whether the guard is there at all. A mutation proved precisely
        that: deleting the cap broke nothing.

        THE RULE: a re-ground can never be applied SILENTLY. It used to hold only by arithmetic —
        css scored PRIORS["css"] * 0.90 = 0.585 against FLAG 0.60 — and the test pinned the number
        rather than the property. Raising a prior, or softening the overconfidence discount to 0.95,
        would have promoted an unverified locator into the band that EXECUTES (replay.py applies
        auto_healed | flagged | cache_hit alike) with everything still green. The cap says what was
        always meant: a locator nothing in the plan vouches for is applied optimistically at best,
        never quietly. ADR-080.

        ADR-082 adds the second half of that sentence. The cap said "not silently"; it could not say
        WHETHER THE ELEMENT IS THE RIGHT ONE, because nothing compared the element found against the
        element the plan froze. `ident` is that comparison, and it is reported rather than scored:
        a CONTRADICTED re-ground is still applied — the product exists to repair a drifted test, and
        a renamed control is the commonest drift there is — but it is applied VISIBLY, on the verdict
        and in the report, instead of passing for an ordinary heal.
        """
        page, sid, dom = ctx["page_path"], ctx["semantic_id"], ctx["dom_hash"]
        val = json.dumps(loc)
        # `None` and not UNVERIFIABLE for a re-BIND: the plan froze the key, so identity was never in
        # question and reporting "unverifiable" would invent a doubt the run does not have.
        state = None
        if is_reground(strat, ctx.get("alternatives")):
            conf = self._cap(conf)
            state = _identity_state(ident)
            self._log_identity(ctx, strat, ident)
        if conf >= AUTO:
            self.store.save_locator(page, sid, strat, val, conf, dom, "active")
            self._audit(ctx, strat, val, conf, "auto_healed", state)
            return {"locator": loc, "strategy": strat, "confidence": conf, "outcome": "auto_healed",
                    "identity": state}
        if conf >= FLAG:
            self.store.save_locator(page, sid, strat, val, conf, dom, "flagged")
            self._audit(ctx, strat, val, conf, "flagged", state)
            return {"locator": loc, "strategy": strat, "confidence": conf, "outcome": "flagged",
                    "identity": state}
        self._audit(ctx, strat, val, conf, "needs_review", state)
        return {"outcome": "needs_review", "confidence": conf, "identity": state}

    def _llm_reground(self, ctx: dict):
        """Text tier: pick WHICH live element matches the intent. Returns (strategy,locator,conf,live)|None.

        ADR-082 turned this from "author a CSS selector" into "choose an index", and the reasons
        compound:

        * It was the only place in the product where a model authored a selector, which ADR-022/027
          rejected everywhere else — `planner.py` returns `candidates[idx]` precisely so a
          hallucinated selector cannot exist. The live element list was ALREADY in this prompt; the
          model was being handed the grounded answer and asked for an ungrounded one.
        * A CSS string cannot be checked. Nothing can read back which element `#pay-v2` hit, so
          identity was structurally unverifiable — the hole PROD-REGROUND-VERIFY is about. An index
          yields the element's own descriptor, and that descriptor is comparable with what the plan
          froze.
        * The old tier could never be applied anyway: PRIORS["css"] * 0.90 = 0.585 against FLAG 0.60,
          so every css re-ground ended in needs_review. Worse, returning a candidate SUPPRESSED the
          visual tier (`heal` tries it only `if not chosen`), so a tier that could never heal was
          shadowing the one that can.

        No reach is lost: an element outside the executor's perception selector cannot be in the plan
        either — explore never saw it — and the visual tier has the same ceiling.
        """
        from . import budget
        if budget.tracker().exceeded("heal"):
            log("heal.budget_exhausted")
            return None
        try:
            elements = ctx.get("interactives") or []
            if not elements:
                return None                      # nothing to choose from; let the visual tier try
            menu = [{"index": i, "role": e.get("role"), "name": e.get("name"),
                     "testid": e.get("testid")} for i, e in enumerate(elements)]
            # ADR-136: укладка ЦЕЛЫМИ записями — тот же класс, что `planner.py`. Прежний
            # `json.dumps(...)[:3000]` рубил сериализованную строку посреди дескриптора, и, поскольку
            # "index" — ПЕРВЫЙ ключ записи, оборванный хвостовой объект оставался «читаемым» по
            # индексу, а описание элемента терялось: проверка границ такой пик пропускала.
            # ⚠ Обрезка отъедала прежде всего элементы ФРЕЙМОВ: `browser.interactives` кладёт их
            # ПОСЛЕ элементов верхнего документа (ADR-095), то есть в хвост.
            menu_text, heal_dropped = fit_json_list(menu, HEAL_MENU_CHARS)
            if heal_dropped:
                log("heal.menu_truncated", shown=len(menu) - heal_dropped, total=len(menu),
                    budget=HEAL_MENU_CHARS)
            # The framing is load-bearing, and a live run proved it. Asked to find "the element
            # matching the intent", qwen3:14b and qwen2.5vl:7b both answered {"none": true} for every
            # rename we offered — correctly, in a literal reading: no element carries that name any
            # more, which is precisely why the step failed. Asked which element now serves the same
            # PURPOSE, the same models pick it. The tier's job is to propose a candidate; deciding
            # whether the candidate is really the same element is the identity check's job, not the
            # model's, and a prompt that makes the model do both gets neither.
            prompt = (
                "A UI test step can no longer find its element because the page changed. Choose "
                "which element below now serves the SAME PURPOSE as the one the step wants, and "
                "return its index. The element may have been renamed or re-tagged; choose it "
                "anyway. Return none only if no element could plausibly serve that purpose.\n"
                f"step intent: {safe_text(ctx.get('intent'))}\n"
                f"element the step used to use: {json.dumps(safe_json(ctx.get('attempted_locator')))}\n"
                + partial_note(len(menu), heal_dropped)
                + f"elements on the page now: {menu_text}\n"
                'Reply with ONLY JSON: {"index": <int>} or {"none": true}.'
            )
            with span("heal.llm", model=self._backend.model, prompt_hash=prompt_hash(prompt)) as _sp:
                result = complete_structured(self._backend, prompt, _SCHEMA_PICK,
                                             max_tokens=200, temperature=0)
                budget.tracker().add("heal", result)
                set_llm_tokens(_sp, result)
            j = result.data
            if j is None or j.get("none") or j.get("index") is None:
                return None                      # j None on an unparseable reply
            idx = int(j["index"])
            if not 0 <= idx < len(elements):     # out of bounds -> no pick (planner.py:218-222)
                log("heal.pick_out_of_range")
                return None
            live = elements[idx]
            loc = descriptor_to_locator(live)
            if loc:
                return ("llm_pick", loc, pick_confidence(loc), live)
        except Exception as e:
            log("heal.llm_reground_error", error=e)
        return None

    def _visual_reground(self, ctx: dict):
        """Tier-7 (M5-2): overlay numbered marks, ask a vision model to pick the element matching the
        intent, map the chosen mark to a real locator. Discounted to the FLAGGED band. Returns
        (strategy, locator, conf, live) | None. Gated by use_visual + a live vision LLM.

        The chosen mark carries role/name/testid, so this tier could always have said WHICH element it
        picked — it simply threw that away after building the locator. ADR-082 returns it, which is
        why identity verification here needs no new executor RPC at all."""
        from . import budget
        if budget.tracker().exceeded("heal"):
            return None
        import base64
        import os as _os
        import tempfile
        img = None
        try:
            fd, img = tempfile.mkstemp(suffix=".png")
            _os.close(fd)
            som = self.ex.call("browser.setOfMarks", path=img)
            marks = som.get("marks", [])
            if not marks:
                return None
            with open(img, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            menu = [{"mark": m["mark"], "role": m.get("role"), "name": m.get("name")} for m in marks]
            # ⚠ У ЭТОГО ТИРА КЕПА НЕ БЫЛО ВООБЩЕ, хотя документация называла его меню «крошечным».
            # Марки ставит `browser.setOfMarks` по числу видимых контролов, и на плотном экране их
            # сотни; вдобавок этот вызов несёт КАРТИНКУ, то есть самый дорогой запрос прогона. Тот же
            # помощник и тот же бюджет: одна политика на оба тира лечения.
            marks_text, marks_dropped = fit_json_list(menu, HEAL_MENU_CHARS)
            if marks_dropped:
                log("heal.menu_truncated", shown=len(menu) - marks_dropped, total=len(menu),
                    budget=HEAL_MENU_CHARS)
            prompt = (
                "Numbered red marks overlay interactive UI elements. Pick the mark number for the "
                f"element matching this intent: {safe_text(ctx.get('intent'))}\n"
                + partial_note(len(menu), marks_dropped)
                + f"marks: {marks_text}\n"
                'Reply with ONLY JSON: {"mark": <int>} or {"none": true}.')
            result = self._backend.complete_vision(prompt, b64, max_tokens=100, temperature=0)
            budget.tracker().add("heal", result)
            j = extract_json(result.text)
            if j.get("none"):
                return None
            chosen = next((m for m in marks if m["mark"] == int(j["mark"])), None)
            loc = descriptor_to_locator(chosen) if chosen else None
            return ("visual", loc, PRIORS["visual"], chosen) if loc else None
        except Exception as e:
            log("heal.visual_reground_error", error=e)
            return None
        finally:
            if img:
                try:
                    _os.remove(img)
                except Exception:
                    pass

    def _audit(self, ctx: dict, strategy: str, healed: str, conf: float, outcome: str,
               identity_state: str = "") -> None:
        # ADR-082: the audit row is the only place a heal outlives its run, so the identity verdict
        # has to land here or the dataset RISK-002 needs can never be assembled. Empty means "no
        # claim" — a re-bind, or a row written before this existed.
        self.store.audit(run_id=self.run_id, step=ctx.get("step"), semantic_id=ctx["semantic_id"],
                         page_path=ctx["page_path"], strategy=strategy,
                         original=json.dumps(ctx.get("attempted_locator")), healed=healed,
                         confidence=conf, outcome=outcome, dom_hash=ctx.get("dom_hash"),
                         identity=identity_state or "")
