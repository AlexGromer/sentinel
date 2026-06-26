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
