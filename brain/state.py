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
    """Deterministic hash of the ordered steps (excludes volatile fields)."""
    payload = json.dumps(steps, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()
