"""Sentinel brain — planners (ADR-011: pluggable).

A planner decides the next exploration action given the current RunState and a list of
candidate actions assembled by the `plan` node. Two implementations ship:

- HeuristicPlanner — deterministic, offline, zero-cost. The default, and also the
  graceful-degradation path when an LLM budget is exhausted.
- LLMPlanner — Opus 4.8 (temperature 0). Falls back to the heuristic when no API key is
  present or on any error, so a missing key never breaks a run.

A "candidate" is a dict: {kind: 'click'|'navigate', semantic_id, role, name, target, intent}.
`propose(state, candidates)` returns:
    {action: <candidate>|None, done: bool, reason: str, tokens: {prompt,completion}|None}
The convergence decision (coverage target reached) is enforced by the graph, not the planner.
See ../docs/M1_CONTRACT.md.
"""
from __future__ import annotations

import json
import os
from typing import Optional, Protocol

from .executor import log
from .llm import complete_structured
from .sanitize import safe_json, safe_text


def _tok_budget(env_key: str, default: int) -> int:
    """A positive int override from the environment, else `default`."""
    try:
        v = int(os.environ.get(env_key, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# max_tokens is a CEILING, not a target. A non-reasoning model (Claude, GPT) emits its small JSON and
# stops (finish_reason="stop"), so a generous ceiling costs it nothing. A reasoning model (qwen3, R1,
# o-series) emits THINK tokens first and, under a tight ceiling, hits the cap before any answer content
# (finish_reason="length", empty content) — which the caller then reads as "no JSON" and silently
# degrades. The old 200/800 caps assumed non-reasoning models; M9-LIVE (2026-07-23) saw qwen3:14b
# starve 4 of 6 fixtures at 800. Ceilings below are reasoning-aware; env-tunable for exotic models.
_PICK_TOKENS = _tok_budget("LLM_MAX_TOKENS_PICK", 1024)       # per-action index pick (small output)
_SCENARIO_TOKENS = _tok_budget("LLM_MAX_TOKENS_SCENARIO", 3072)  # whole-scenario / draft authoring


def _log_unparsed(where: str, result) -> None:
    """A structured call returned nothing parseable — never let that pass silently (it presents as an
    empty scenario / a heuristic fallback with no reason). Name the likely cause from finish_reason."""
    fr = getattr(result, "finish_reason", None)
    hint = (" — finish_reason=length: the model hit max_tokens mid-output (raise LLM_MAX_TOKENS_* or "
            "use a non-reasoning model)") if fr == "length" else f" (finish_reason={fr})"
    log(f"{where}: no parseable structured output{hint}; raw[:300]:",
        repr(getattr(result, "text", ""))[:300])

# JSON schemas for the structured-output planners (ADR-057). Rich-guiding: they encode the real
# artifact contract (verb enum, required ref/index, step shape) so a capable backend emits conformant
# steps BY CONSTRUCTION — on the native path (Anthropic tool_use / OpenAI json_schema, NON-strict for
# cross-provider portability). The fallback (`extract_json`) ignores the schema; grounding (ADR-022,
# scenario.ground_scenario/reconcile) stays the final validator and the index/done union is enforced
# in Python. Tighter schemas also cut verb/shape variance → steadier LLM-authoring plan_hash.
_VERBS = ["click", "fill", "type", "select", "press", "assert"]
_SCHEMA_PICK = {"type": "object", "properties": {
    "index": {"type": "integer", "description": "0-based index into the candidate list to act on"},
    "done": {"type": "boolean", "description": "true to stop (goal met or exploration complete)"},
    "reason": {"type": "string", "description": "why, when done"}}}
_SCHEMA_STEPS = {"type": "object", "properties": {"steps": {"type": "array", "items": {
    "type": "object", "properties": {
        "ref": {"type": "string", "description": "semantic_id of a real element from the map"},
        "verb": {"type": "string", "enum": _VERBS},
        "value": {"type": "string", "description": "value for fill/type/select"}},
    "required": ["ref", "verb"]}}}, "required": ["steps"]}
_SCHEMA_DRAFT = {"type": "object", "properties": {"steps": {"type": "array", "items": {
    "type": "object", "properties": {
        "verb": {"type": "string", "enum": _VERBS},
        "intent": {"type": "string", "description": "short goal of this step"},
        "hypothesized_target": {"type": "object", "properties": {
            "role": {"type": "string"}, "name": {"type": "string"}, "text": {"type": "string"}}},
        "value": {"type": "string"}},
    "required": ["verb", "intent"]}}}, "required": ["steps"]}


class Planner(Protocol):
    """Interface every planner implements (duck-typed; used for documentation/typing)."""
    name: str
    model: Optional[str]

    def propose(self, state: dict, candidates: list) -> dict: ...


class HeuristicPlanner:
    """Deterministic explorer: exhaust clickables on the page, then walk the frontier."""

    name = "heuristic"
    model = None

    def propose(self, state: dict, candidates: list) -> dict:
        clicks = [c for c in candidates if c["kind"] == "click"]
        if clicks:
            c = clicks[0]
            return {"action": c, "done": False,
                    "reason": f"first unexercised {c['role']} '{c['name']}'", "tokens": None}
        navs = [c for c in candidates if c["kind"] == "navigate"]
        if navs:
            c = navs[0]
            return {"action": c, "done": False,
                    "reason": f"frontier navigate {c['target']}", "tokens": None}
        return {"action": None, "done": True, "reason": "no candidates", "tokens": None}


class LLMPlanner:
    """LLM explorer via a provider-agnostic backend (ADR-019); falls back to the heuristic when no
    backend is configured (no key/SDK) or on any error. Best-effort — not plan_hash-stable."""

    name = "llm"

    def __init__(self, backend=None) -> None:
        from .llm import make_backend
        self._backend = backend if backend is not None else make_backend("planner")
        # `model` is a transcript label: the real model when configured, else the historical default.
        self.model = self._backend.model if self._backend else "claude-opus-4-8"
        self._fallback = HeuristicPlanner()

    def propose(self, state: dict, candidates: list) -> dict:
        if not self._backend:
            return self._fallback.propose(state, candidates)
        from . import budget
        if budget.tracker().exceeded("plan"):
            log("LLMPlanner: plan token budget exceeded -> heuristic (M8, ADR-021)")
            return self._fallback.propose(state, candidates)
        try:
            menu = [{"i": i, "kind": c["kind"], "role": c.get("role"),
                     "name": c.get("name"), "target": c.get("target")}
                    for i, c in enumerate(candidates)]
            prompt = (
                "You are an autonomous UI explorer. Choose the single best next action to "
                "maximize coverage of distinct interactive elements, or stop if exploration is "
                "complete.\n"
                f"current_url: {safe_text(state.get('current_url'))}\n"
                f"coverage_achieved: {state.get('coverage_achieved', 0.0):.2f} "
                f"target: {state.get('coverage_target')}\n"
                f"candidates: {json.dumps(safe_json(menu))}\n"
                'Reply with ONLY JSON: {"index": <int>} to act, or {"done": true} to stop.'
            )
            result = complete_structured(self._backend, prompt, _SCHEMA_PICK,
                                         max_tokens=_PICK_TOKENS, temperature=0, role="plan")
            budget.tracker().add("plan", result)
            tokens = {"prompt": result.prompt_tokens, "completion": result.completion_tokens}
            j = result.data
            if j is None:  # no parseable structured output -> deterministic explore (budget charged)
                _log_unparsed(f"{type(self).__name__}.propose", result)
                return self._fallback.propose(state, candidates)
            if j.get("done"):
                return {"action": None, "done": True, "reason": "llm: done", "tokens": tokens}
            idx = int(j["index"])
            if 0 <= idx < len(candidates):
                return {"action": candidates[idx], "done": False,
                        "reason": f"llm picked #{idx}", "tokens": tokens}
            return {"action": None, "done": True, "reason": "llm index OOB", "tokens": tokens}
        except Exception as e:
            log("LLMPlanner error -> heuristic:", e)
            return self._fallback.propose(state, candidates)


class GoalPlanner:
    """Goal-directed explorer (M9.2a, ADR-027): given an NL goal + the live candidate map, pick the next
    REAL action that advances the goal, or stop when the goal is met / unreachable.

    GROUNDING (ADR-022): the LLM picks an INDEX into `candidates` (the real elements the `plan` node
    discovered), so it can never author a selector that isn't on the map. `propose` returns ONLY
    `candidates[idx]` or `done` — an invalid/OOB index degrades to `done`, never a fabricated action.

    Falls back to the heuristic when there's no goal / no backend (no key/SDK) or the plan budget is
    exhausted. Best-effort — not plan_hash-stable (like LLMPlanner; replay stays deterministic)."""

    name = "goal"

    def __init__(self, goal: str = "", backend=None) -> None:
        from .llm import make_backend
        self.goal = (goal or "").strip()
        self._backend = backend if backend is not None else make_backend("planner")
        self.model = self._backend.model if self._backend else "claude-opus-4-8"
        self._fallback = HeuristicPlanner()

    def propose(self, state: dict, candidates: list) -> dict:
        if not self.goal or not self._backend:
            return self._fallback.propose(state, candidates)   # no goal/backend -> deterministic explore
        from . import budget
        if budget.tracker().exceeded("plan"):
            log("GoalPlanner: plan token budget exceeded -> heuristic (M8, ADR-021)")
            return self._fallback.propose(state, candidates)
        try:
            menu = [{"i": i, "kind": c["kind"], "role": c.get("role"), "name": c.get("name"),
                     "target": c.get("target"), "intent": c.get("intent")}
                    for i, c in enumerate(candidates)]
            prompt = (
                "You are an autonomous UI agent pursuing a specific GOAL. Choose the single best next "
                "action from the candidate list to advance the goal, or stop when the goal is achieved "
                "or unreachable.\n"
                f"goal: {self.goal}\n"
                f"current_url: {safe_text(state.get('current_url'))}\n"
                f"steps_taken: {state.get('current_step', 0)} of max {state.get('max_steps')}\n"
                f"candidates: {json.dumps(safe_json(menu))}\n"
                'Reply with ONLY JSON: {"index": <int>} to take that candidate action, or '
                '{"done": true, "reason": "<why the goal is met or unreachable>"}.'
            )
            result = complete_structured(self._backend, prompt, _SCHEMA_PICK,
                                         max_tokens=_PICK_TOKENS, temperature=0, role="plan")
            budget.tracker().add("plan", result)
            tokens = {"prompt": result.prompt_tokens, "completion": result.completion_tokens}
            j = result.data
            if j is None:  # no parseable structured output -> deterministic explore (budget charged)
                _log_unparsed(f"{type(self).__name__}.propose", result)
                return self._fallback.propose(state, candidates)
            if j.get("done"):
                return {"action": None, "done": True,
                        "reason": f"goal: {j.get('reason', 'done')}", "tokens": tokens}
            idx = int(j["index"])
            if 0 <= idx < len(candidates):
                return {"action": candidates[idx], "done": False,
                        "reason": f"goal -> #{idx}", "tokens": tokens}   # GROUNDED: a real candidate only
            return {"action": None, "done": True, "reason": "goal: index OOB", "tokens": tokens}
        except Exception as e:
            log("GoalPlanner error -> heuristic:", e)
            return self._fallback.propose(state, candidates)

    def build_scenario(self, flat_map: list, goal: str = None, history: list = None) -> dict:
        """M9.2b (ADR-028) scenario head: given the COMPLETE flattened site map + goal, return ordered
        refs `{"refs":[{ref,verb,value?}], "tokens":...}`. Returns empty on no-goal/no-backend/budget/
        error (the caller authors nothing). The actual grounding (ref must exist in the map) is enforced
        downstream in brain/scenario.ground_scenario — this just proposes candidate refs.

        M9.10 (ADR-048): `history` (prior user turns, oldest first) refines the scenario across a
        multi-turn conversation; empty/None ⇒ one-shot prompt unchanged (byte-identical)."""
        goal = (goal or self.goal or "").strip()
        if not goal or not self._backend:
            return {"refs": [], "tokens": None}
        from . import budget
        if budget.tracker().exceeded("plan"):
            log("GoalPlanner.build_scenario: plan budget exceeded -> empty scenario")
            return {"refs": [], "tokens": None}
        try:
            menu = [{"ref": e["semantic_id"], "page": e.get("page"), "role": e.get("role"),
                     "name": e.get("name")} for e in flat_map]
            convo = ""
            if history:   # M9.10 (ADR-048): multi-turn refine context — prior conversation turns
                convo = ("prior conversation turns (oldest first) — REFINE the scenario to satisfy ALL of "
                         "them plus the current goal:\n" + "\n".join(f"- {h}" for h in history) + "\n")
            prompt = (
                "You are authoring an end-to-end UI test scenario toward a GOAL, choosing ONLY from the "
                "real elements discovered across the whole site. Output the ordered actions.\n"
                f"goal: {goal}\n"
                + convo
                + f"elements: {json.dumps(safe_json(menu))[:8000]}\n"
                + 'Reply with ONLY JSON: {"steps": [{"ref": "<semantic_id from elements>", '
                '"verb": "click|fill|type|select|press|assert", "value": "<optional>"}]}. '
                "Use only refs present in elements; omit anything not present."
            )
            result = complete_structured(self._backend, prompt, _SCHEMA_STEPS,
                                         max_tokens=_SCENARIO_TOKENS, temperature=0, role="plan")
            budget.tracker().add("plan", result)
            j = result.data
            if j is None:  # no parseable structured output -> author nothing (budget charged)
                _log_unparsed("GoalPlanner.build_scenario", result)
                return {"refs": [], "tokens": None}
            refs = [r for r in (j.get("steps") or j.get("refs") or []) if isinstance(r, dict) and r.get("ref")]
            return {"refs": refs,
                    "tokens": {"prompt": result.prompt_tokens, "completion": result.completion_tokens}}
        except Exception as e:
            log("GoalPlanner.build_scenario error -> empty:", e)
            return {"refs": [], "tokens": None}


class DescribePlanner:
    """describe-first (M9.2b, ADR-028): the LLM proposes an ungrounded DRAFT (intent + hypothesized
    target by role/name/text); the deterministic `brain/scenario.reconcile` binds it to real elements.
    The LLM never picks a selector or index — stronger grounding than GoalPlanner. Best-effort."""

    name = "describe"

    def __init__(self, description: str = "", backend=None) -> None:
        from .llm import make_backend
        self.description = (description or "").strip()
        self._backend = backend if backend is not None else make_backend("planner")
        self.model = self._backend.model if self._backend else "claude-opus-4-8"

    def draft(self, history: list = None) -> dict:
        """Return `{"draft":[{verb,intent,hypothesized_target,value?}], "tokens":...}`; empty on
        no-description/no-backend/budget/error.

        M9.10 (ADR-048): `history` (prior user turns, oldest first) refines the draft across a multi-turn
        conversation; empty/None ⇒ one-shot prompt unchanged (byte-identical)."""
        if not self.description or not self._backend:
            return {"draft": [], "tokens": None}
        from . import budget
        if budget.tracker().exceeded("plan"):
            log("DescribePlanner: plan budget exceeded -> empty draft")
            return {"draft": [], "tokens": None}
        try:
            convo = ""
            if history:   # M9.10 (ADR-048): multi-turn refine context — prior conversation turns
                convo = ("prior conversation turns (oldest first) — REFINE the draft to satisfy ALL of "
                         "them plus the current description:\n" + "\n".join(f"- {h}" for h in history) + "\n")
            prompt = (
                "Convert this NL description of a UI flow into an ordered DRAFT of intended steps. Do NOT "
                "invent selectors; describe each target by role/name/text so it can be matched against the "
                "real page later.\n"
                f"description: {self.description}\n"
                + convo
                + 'Reply with ONLY JSON: {"steps": [{"verb": "click|fill|type|select|press|assert", '
                '"intent": "<short>", "hypothesized_target": {"role": "<opt>", "name": "<opt>", '
                '"text": "<opt>"}, "value": "<opt>"}]}.'
            )
            result = complete_structured(self._backend, prompt, _SCHEMA_DRAFT,
                                         max_tokens=_SCENARIO_TOKENS, temperature=0, role="plan")
            budget.tracker().add("plan", result)
            j = result.data
            if j is None:  # no parseable structured output -> empty draft (budget charged)
                _log_unparsed("DescribePlanner", result)
                return {"draft": [], "tokens": None}
            draft = [d for d in (j.get("steps") or j.get("draft") or []) if isinstance(d, dict)]
            return {"draft": draft,
                    "tokens": {"prompt": result.prompt_tokens, "completion": result.completion_tokens}}
        except Exception as e:
            log("DescribePlanner error -> empty:", e)
            return {"draft": [], "tokens": None}


def make_planner(env=None):
    """Select the planner per env (M9.2a, ADR-027). Authoring mode is chosen by `--goal` presence
    (auto-default, M9_CONTRACT §C) or an explicit `PLANNER=goal|llm` — NOT via `--mode` (= RUN_MODE).

    `GOAL` set (and PLANNER unset/default) -> GoalPlanner; `PLANNER=goal` -> GoalPlanner;
    `PLANNER=llm` -> LLMPlanner; else HeuristicPlanner.
    """
    import os
    env = os.environ if env is None else env
    planner_name = (env.get("PLANNER") or "heuristic").strip().lower()
    goal = (env.get("GOAL") or "").strip()
    if planner_name == "goal" or (goal and planner_name in ("", "heuristic")):
        return GoalPlanner(goal=goal)
    if planner_name == "llm":
        return LLMPlanner()
    return HeuristicPlanner()
