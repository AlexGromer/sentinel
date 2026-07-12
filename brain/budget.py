"""Sentinel brain — token budget accumulator + cooperative ceiling (M8, ADR-021).

Accumulates LLM token spend per role ("plan" / "heal"). The pre-call guard `exceeded(role)` lets the
planner fall back to the heuristic and healing to deterministic L1–L6 when a per-role (or total) limit
is hit — graceful degradation (consistent with §8 / ADR-011). A Go orchestrator enforces a
model-INDEPENDENT hard kill on top via `RunControl` (proto/runcontrol.proto); this module is the
in-process cooperative half. The default heuristic / replay paths spend no tokens, so the ceiling is
inert there — which is why it was deferred from M4b.

Limits come from env (per-run, set by agentctl/orchestrator): `PLAN_TOKEN_LIMIT` (default 50000),
`HEAL_TOKEN_LIMIT` (default 20000), `TOTAL_TOKEN_LIMIT` (default 0 = off). A limit of 0 disables that gate.
"""
from __future__ import annotations

import os

from .executor import log


def _limit(env: str, default: int) -> int:
    try:
        v = int(os.environ.get(env, ""))
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


class BudgetTracker:
    """Per-role token accumulator with limit checks. Roles: 'plan', 'heal'."""

    def __init__(self, plan_limit: int | None = None, heal_limit: int | None = None,
                 total_limit: int | None = None) -> None:
        self.plan_limit = plan_limit if plan_limit is not None else _limit("PLAN_TOKEN_LIMIT", 50000)
        self.heal_limit = heal_limit if heal_limit is not None else _limit("HEAL_TOKEN_LIMIT", 20000)
        self.total_limit = total_limit if total_limit is not None else _limit("TOTAL_TOKEN_LIMIT", 0)
        self.spent: dict = {"plan": 0, "heal": 0}
        # M15.1: prompt/completion split (spent[role] is their sum) so a run's cost can price in-vs-out
        # tokens separately (they have different $/1M). Accumulated in add(), summarized in summary().
        self.prompt: dict = {"plan": 0, "heal": 0}
        self.completion: dict = {"plan": 0, "heal": 0}

    def add(self, role: str, result) -> None:
        p = int(getattr(result, "prompt_tokens", 0) or 0)
        c = int(getattr(result, "completion_tokens", 0) or 0)
        self.spent[role] = self.spent.get(role, 0) + p + c
        self.prompt[role] = self.prompt.get(role, 0) + p
        self.completion[role] = self.completion.get(role, 0) + c

    def total(self) -> int:
        return sum(self.spent.values())

    def summary(self) -> dict:
        """M15.1: per-run token summary written into the report artifacts (plan.json / heal-report.json);
        the control-API prices it into `cost_usd`. `spent` == prompt+completion per role."""
        return {"prompt": sum(self.prompt.values()), "completion": sum(self.completion.values()),
                "total": self.total(),
                "plan": {"prompt": self.prompt.get("plan", 0), "completion": self.completion.get("plan", 0)},
                "heal": {"prompt": self.prompt.get("heal", 0), "completion": self.completion.get("heal", 0)}}

    def _role_limit(self, role: str) -> int:
        return self.plan_limit if role == "plan" else self.heal_limit

    def exceeded(self, role: str) -> bool:
        """True once this role's (or the total) limit is reached — the caller should degrade."""
        if self.total_limit and self.total() >= self.total_limit:
            return True
        lim = self._role_limit(role)
        return bool(lim) and self.spent.get(role, 0) >= lim

    def reset(self) -> None:
        self.spent = {"plan": 0, "heal": 0}
        self.prompt = {"plan": 0, "heal": 0}
        self.completion = {"plan": 0, "heal": 0}


_tracker = BudgetTracker()


def tracker() -> BudgetTracker:
    """The process-wide tracker consulted by the planner / healing engine."""
    return _tracker


def reset(**kwargs) -> None:
    """Start a fresh tracker for a run (or a test); kwargs override the env-derived limits."""
    global _tracker
    _tracker = BudgetTracker(**kwargs)
    log(f"budget: reset (plan={_tracker.plan_limit} heal={_tracker.heal_limit} total={_tracker.total_limit})")
