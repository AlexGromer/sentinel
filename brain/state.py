"""Sentinel brain — shared RunState and pure helpers (M1)."""
import hashlib
import json
from typing import Annotated, TypedDict
from urllib.parse import urlsplit, urlunsplit

from langgraph.graph.message import add_messages


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
    # M9.10 (ADR-048): multi-turn conversation accumulator (chat mode). The brain feeds plain dicts
    # {role, content}; LangGraph's add_messages reducer coerces them to BaseMessage and APPENDS across
    # turns, persisted by the shared checkpointer (thread_id=conversation_id). Empty/absent for one-shot
    # explore/goal/describe runs — so their behavior (and plan_hash) is unchanged.
    messages: Annotated[list, add_messages]
    # ADR-108a: the conversation's OBJECTIVE, pinned on the first turn and never rewritten —
    # {"kind": "goal"|"describe", "text": str}.
    #
    # It exists because `goal` was doing two jobs at once. control-api sent each turn's text AS the
    # goal, so "what this conversation is for" and "what the person just typed" were the same field,
    # and nothing could tell a refinement from a new objective. That made the rule "a conversation has
    # one goal; for a new goal start a new chat" unstateable, let alone enforceable.
    #
    # Lives in the checkpointer (thread_id=conversation_id), which is the conversation's real state —
    # NOT in the `chats` SQL row, which ADR-050 defines as a browsable projection and whose `last_goal`
    # column keeps meaning exactly what its name says: the most recent turn.
    chat_intent: dict
    # M9.2b two-phase authoring (ADR-028): a site-wide element map built during the explore walk, then
    # a one-shot scenario head grounds the goal/describe into replayable steps.
    site_map: dict                # page_path -> [element {semantic_id,role,name,testid,locator,alternatives,page}]
    perception: dict              # ADR-092: page_path -> {seen,total,ratio,unseen{...},opaque{...}} — how much of the page perception can SEE, as opposed to how much of what it saw was exercised (coverage)
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
    # M9-LIVE: semantic_id -> how many times acting on it RAISED. `act` marks an element exercised
    # only on success, so before this existed a permanently unactionable control (a disabled button)
    # stayed a candidate forever and the planner proposed it every step until max_steps — the ×34
    # repeat live logs made visible. A dict, not a list, because the retry budget is per element.
    interactive_failed: dict
    visited_paths: list
    nav_frontier: list
    coverage_achieved: float
    exploration_complete: bool
    executed_actions: list
    errors: list
    # M9.8 F4 (ADR-054): operator-takeover resume payloads, appended each time the checkpoint node
    # resumes from an interrupt() (one entry per takeover→return cycle). Observability only — not in
    # plan.json/scenario.json, so plan_hash is unaffected.
    takeover_returns: list
    # M14 (ADR-055): auto-HITL counters (docs/M14_CONTRACT.md §4). consecutive_heal_failures counts
    # heal-node misses in a row (the explore graph's heal node is a stub — see graph.py — so every
    # entry is a miss); reset to 0 on any successful verify. Drives the checkpoint node's auto-arm of
    # _takeover_armed past SENTINEL_AUTO_HITL_THRESHOLD. failed_steps is a running total of verify
    # failures (observability + M15-metrics substrate). Both default absent -> 0 (state.get(..., 0)),
    # so existing runs/tests with no M14 wiring are unaffected.
    consecutive_heal_failures: int
    failed_steps: int
    # transient channels (must be declared so LangGraph keeps them across nodes)
    _pending: dict
    _last_ok: bool
    _verify_ok: bool
    _takeover_armed: bool         # M9.8 F4 (ADR-054): latched by checkpoint when a takeover is pending; drives the pause node


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
