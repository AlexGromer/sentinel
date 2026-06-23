"""Sentinel healing engine (M2) — deterministic locator re-grounding.

Implements the M2 subset of docs/SELF_HEALING.md (see docs/M2_CONTRACT.md): cache lookup with
dom-hash amortization → L1–L6 strategy rotation (offline, no LLM) → optional Sonnet re-grounding
→ verify-before-accept (live re-probe) → confidence gate → persist + append-only audit.

The offline path (cache + L1–L6 + probe) needs no network or API key. A locator is a dict in
pw-executor format, one of: {testid}, {role,name}, {label}, {text}, {css}, {xpath}.
"""
from __future__ import annotations

import json
import os
import sys

# Per-strategy base priors (docs/SELF_HEALING.md). Keys match the `alternatives[].strategy` values.
PRIORS = {"testid": 0.95, "role_name": 0.90, "label": 0.88, "text_role": 0.80, "css": 0.65, "xpath": 0.45}
AUTO, FLAG = 0.85, 0.60  # confidence gate thresholds


def log(*a: object) -> None:
    print("[heal]", *a, file=sys.stderr, flush=True)


class HealingEngine:
    """Re-grounds a broken locator. `ex` = pw-executor client; `store` = interim locator store."""

    def __init__(self, ex, store, run_id: str, use_llm: bool = False) -> None:
        self.ex, self.store, self.run_id, self.use_llm = ex, store, run_id, use_llm
        self._llm = None
        if use_llm:
            try:
                import anthropic
                if os.environ.get("ANTHROPIC_API_KEY"):
                    self._llm = anthropic.Anthropic()
                else:
                    log("no ANTHROPIC_API_KEY -> deterministic L1-L6 only")
            except Exception as e:  # import/env guard
                log("anthropic unavailable -> L1-L6 only:", e)

    def _probe(self, locator: dict) -> int:
        try:
            return int(self.ex.call("browser.probe", locator=locator).get("count", 0))
        except Exception as e:
            log("probe error:", e)
            return 0

    def heal(self, ctx: dict) -> dict:
        """ctx = {step, semantic_id, page_path, intent, attempted_locator, alternatives, dom_hash, interactives}.

        Returns {locator, strategy, confidence, outcome} where outcome is one of
        cache_hit | auto_healed | flagged | needs_review | failed.
        """
        page, sid, dom = ctx["page_path"], ctx["semantic_id"], ctx["dom_hash"]

        # 3. cache lookup (amortization) — reuse a prior heal if the page hash still matches.
        cached = self.store.lookup(page, sid, dom)
        if cached:
            loc = json.loads(cached["value"])
            if self._probe(loc) == 1:
                self.store.bump_used(page, sid, dom)
                self._audit(ctx, cached["strategy"], cached["value"], cached["confidence"], "cache_hit")
                return {"locator": loc, "strategy": cached["strategy"],
                        "confidence": cached["confidence"], "outcome": "cache_hit"}
        self.store.evict_stale(page, sid, dom)

        # 4. L1–L6 deterministic rotation (offline): first recorded alternative that resolves to 1.
        chosen = None
        for alt in ctx.get("alternatives", []):
            strat, loc = alt.get("strategy"), alt.get("locator")
            if loc and self._probe(loc) == 1:
                chosen = (strat, loc, PRIORS.get(strat, 0.5))
                break

        # 5. optional LLM re-grounding (Sonnet) — only if deterministic rotation failed.
        if not chosen and self._llm:
            chosen = self._llm_reground(ctx)

        if not chosen:
            self._audit(ctx, "none", "null", 0.0, "failed")
            return {"outcome": "failed", "confidence": 0.0}

        strat, loc, conf = chosen
        # 6. verify-before-accept: the candidate MUST resolve to exactly 1 live element.
        if self._probe(loc) != 1:
            conf = 0.0
        val = json.dumps(loc)

        # 7. confidence gate.
        if conf >= AUTO:
            self.store.save_locator(page, sid, strat, val, conf, dom, "active")
            self._audit(ctx, strat, val, conf, "auto_healed")
            return {"locator": loc, "strategy": strat, "confidence": conf, "outcome": "auto_healed"}
        if conf >= FLAG:
            self.store.save_locator(page, sid, strat, val, conf, dom, "flagged")
            self._audit(ctx, strat, val, conf, "flagged")
            return {"locator": loc, "strategy": strat, "confidence": conf, "outcome": "flagged"}
        self._audit(ctx, strat, val, conf, "needs_review")
        return {"outcome": "needs_review", "confidence": conf}

    def _llm_reground(self, ctx: dict):
        """Sonnet 4.6 fallback: pick a CSS selector for the broken element. Returns (strategy,locator,conf)|None."""
        try:
            prompt = (
                "A UI locator broke after a DOM change. Choose the current element matching the "
                "intent and return a precise CSS selector.\n"
                f"intent: {ctx.get('intent')}\n"
                f"original_locator: {json.dumps(ctx.get('attempted_locator'))}\n"
                f"current_elements: {json.dumps(ctx.get('interactives', []))[:3000]}\n"
                'Reply with ONLY JSON: {"css": "<selector>"} or {"none": true}.'
            )
            msg = self._llm.messages.create(
                model="claude-sonnet-4-6", max_tokens=200, temperature=0,
                messages=[{"role": "user", "content": prompt}])
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            j = json.loads(text[text.find("{"): text.rfind("}") + 1])
            if j.get("css"):
                return ("css", {"css": j["css"]}, PRIORS["css"] * 0.90)  # overconfidence discount
        except Exception as e:
            log("llm reground error:", e)
        return None

    def _audit(self, ctx: dict, strategy: str, healed: str, conf: float, outcome: str) -> None:
        self.store.audit(run_id=self.run_id, step=ctx.get("step"), semantic_id=ctx["semantic_id"],
                         page_path=ctx["page_path"], strategy=strategy,
                         original=json.dumps(ctx.get("attempted_locator")), healed=healed,
                         confidence=conf, outcome=outcome, dom_hash=ctx.get("dom_hash"))
