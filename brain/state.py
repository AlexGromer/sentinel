"""Sentinel brain — shared RunState and pure helpers (M1)."""
import hashlib
import json
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit


class RunState(TypedDict, total=False):
    # identity / config
    run_id: str
    run_mode: str
    target_url: str
    base_origin: str
    coverage_target: float
    max_steps: int
    artifact_dir: str
    goal: str                     # M9.2a: NL goal text for goal-mode (GoalPlanner); "" in explore-mode
    describe: str                 # M9.2b: NL flow description for describe-mode (DescribePlanner); "" otherwise
    # M9.2b two-phase authoring (ADR-028): a site-wide element map built during the explore walk, then
    # a one-shot scenario head grounds the goal/describe into replayable steps.
    site_map: dict                # page_path -> [element {semantic_id,role,name,testid,locator,alternatives,page}]
    phase: str                    # "explore" | "scenario"
    scenario_steps: list          # the grounded authored steps (appended to exploration_plan)
    scenario_unmatched: list      # refs/draft-steps that could not be grounded to a real element
    # perception
    current_url: str
    page_model: dict
    # exploration accounting
    exploration_plan: list
    plan_hash: str
    current_step: int
    interactive_seen: list        # semantic_ids (dedup'd, JSON-safe)
    interactive_exercised: list
    visited_paths: list
    nav_frontier: list
    coverage_achieved: float
    exploration_complete: bool
    executed_actions: list
    errors: list
    # transient channels (must be declared so LangGraph keeps them across nodes)
    _pending: dict
    _last_ok: bool
    _verify_ok: bool


def normalize_url(u: str) -> str:
    """Drop query + fragment; keep scheme/host/path. Stable page identity."""
    if not u:
        return ""
    s = urlsplit(u)
    return urlunsplit((s.scheme, s.netloc, s.path, "", ""))


def semantic_id(path: str, role: str, name: str) -> str:
    return hashlib.sha1(f"{path}|{role}|{name}".encode()).hexdigest()[:12]


def canonical_plan_hash(steps: list) -> str:
    """Deterministic SHA-256 over the ENTIRE ordered step dicts — every field is included (`sort_keys`
    only makes key order irrelevant; nothing is excluded). So any field change, including the M9.1 step
    fields (secretRef/value/text/clear/condition/expected/expect_ok/key), is tamper-detectable
    (a plan_hash mismatch hard-aborts replay with exit 3)."""
    payload = json.dumps(steps, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()
