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
from typing import Optional, Protocol

from .executor import log


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
                f"current_url: {state.get('current_url')}\n"
                f"coverage_achieved: {state.get('coverage_achieved', 0.0):.2f} "
                f"target: {state.get('coverage_target')}\n"
                f"candidates: {json.dumps(menu)}\n"
                'Reply with ONLY JSON: {"index": <int>} to act, or {"done": true} to stop.'
            )
            result = self._backend.complete(prompt, max_tokens=200, temperature=0)
            budget.tracker().add("plan", result)
            text = result.text
            tokens = {"prompt": result.prompt_tokens, "completion": result.completion_tokens}
            j = json.loads(text[text.find("{"): text.rfind("}") + 1])
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
                f"current_url: {state.get('current_url')}\n"
                f"steps_taken: {state.get('current_step', 0)} of max {state.get('max_steps')}\n"
                f"candidates: {json.dumps(menu)}\n"
                'Reply with ONLY JSON: {"index": <int>} to take that candidate action, or '
                '{"done": true, "reason": "<why the goal is met or unreachable>"}.'
            )
            result = self._backend.complete(prompt, max_tokens=200, temperature=0)
            budget.tracker().add("plan", result)
            text = result.text
            tokens = {"prompt": result.prompt_tokens, "completion": result.completion_tokens}
            j = json.loads(text[text.find("{"): text.rfind("}") + 1])
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

    def build_scenario(self, flat_map: list, goal: str = None) -> dict:
        """M9.2b (ADR-028) ONE-SHOT scenario head: given the COMPLETE flattened site map + goal, return
        ordered refs `{"refs":[{ref,verb,value?}], "tokens":...}`. Returns empty on no-goal/no-backend/
        budget/error (the caller authors nothing). The actual grounding (ref must exist in the map) is
        enforced downstream in brain/scenario.ground_scenario — this just proposes candidate refs."""
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
            prompt = (
                "You are authoring an end-to-end UI test scenario toward a GOAL, choosing ONLY from the "
                "real elements discovered across the whole site. Output the ordered actions.\n"
                f"goal: {goal}\n"
                f"elements: {json.dumps(menu)[:8000]}\n"
                'Reply with ONLY JSON: {"steps": [{"ref": "<semantic_id from elements>", '
                '"verb": "click|fill|type|select|press|assert", "value": "<optional>"}]}. '
                "Use only refs present in elements; omit anything not present."
            )
            result = self._backend.complete(prompt, max_tokens=800, temperature=0)
            budget.tracker().add("plan", result)
            text = result.text
            j = json.loads(text[text.find("{"): text.rfind("}") + 1])
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

    def draft(self) -> dict:
        """Return `{"draft":[{verb,intent,hypothesized_target,value?}], "tokens":...}`; empty on
        no-description/no-backend/budget/error."""
        if not self.description or not self._backend:
            return {"draft": [], "tokens": None}
        from . import budget
        if budget.tracker().exceeded("plan"):
            log("DescribePlanner: plan budget exceeded -> empty draft")
            return {"draft": [], "tokens": None}
        try:
            prompt = (
                "Convert this NL description of a UI flow into an ordered DRAFT of intended steps. Do NOT "
                "invent selectors; describe each target by role/name/text so it can be matched against the "
                "real page later.\n"
                f"description: {self.description}\n"
                'Reply with ONLY JSON: {"steps": [{"verb": "click|fill|type|select|press|assert", '
                '"intent": "<short>", "hypothesized_target": {"role": "<opt>", "name": "<opt>", '
                '"text": "<opt>"}, "value": "<opt>"}]}.'
            )
            result = self._backend.complete(prompt, max_tokens=800, temperature=0)
            budget.tracker().add("plan", result)
            text = result.text
            j = json.loads(text[text.find("{"): text.rfind("}") + 1])
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
