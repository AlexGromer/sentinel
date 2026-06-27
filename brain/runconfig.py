"""Sentinel brain — minimal RunConfig YAML (M9.2a, ADR-027).

A config surface for richer runs than flags carry (GAP-M9-09). M9.2a scope is deliberately minimal:
`{mode, goal, planner, coverage_target, max_steps, plan_budget, heal_budget, total_budget}`. Loaded by
the brain when `RUN_CONFIG` points at a YAML file (`agentctl --run-config <path>`).

Precedence: an explicit flag/env > the RunConfig file > built-in defaults. agentctl emits its flag
DEFAULTS for every run, so to honour "explicit flag wins even when its value equals the default" it
also emits `SENTINEL_EXPLICIT` (a comma list of the flags the user actually passed, via `fs.Visit`).
A key whose flag is in that list is never overridden by the file; otherwise the file may override a
value that is still blank or at the known agentctl default. Unknown keys are IGNORED (forward-compat
for the M9.2b auth/scenarios surface). Numeric keys are validated at load (a bad scalar is a config
error -> exit 3, not a silent run failure). `mode`/`planner` are aliases for PLANNER and resolve
deterministically (conflict raises). Pure: `load` reads a file -> dict; `apply` merges into an env map.
"""
import os

import yaml

# RunConfig key -> the brain env var the rest of the code already reads.
_KEY_ENV = {
    "goal": "GOAL",
    "describe": "DESCRIBE",       # M9.2b describe-mode
    "planner": "PLANNER",
    "coverage_target": "COVERAGE_TARGET",
    "max_steps": "MAX_STEPS",
    "plan_budget": "PLAN_TOKEN_LIMIT",
    "heal_budget": "HEAL_TOKEN_LIMIT",
    "total_budget": "TOTAL_TOKEN_LIMIT",
}
# M9.2b (ADR-028): structured keys handled specially (not a single env var).
_ALLOWED = set(_KEY_ENV) | {"mode", "auth", "scenarios"}
# RunConfig `auth:` sub-key -> the M9.1 env var it drives (declarative; NO new runtime).
_AUTH_ENV = {"storage_state": "STORAGE_STATE", "storage_state_save": "STORAGE_STATE_SAVE",
             "login_plan": "PLAN_FILE", "pw_no_trace": "PW_NO_TRACE"}
# Numeric keys are validated/coerced at load so a bad scalar fails as a config error (exit 3).
_NUMERIC = {"coverage_target": float, "max_steps": int,
            "plan_budget": int, "heal_budget": int, "total_budget": int}
# agentctl emits these defaults for EVERY run; the file may override a still-default value.
_AGENTCTL_DEFAULTS = {"PLANNER": "heuristic", "COVERAGE_TARGET": "0.85", "MAX_STEPS": "40"}
# brain env var -> the agentctl flag that sets it (for the explicit-flag-wins check).
_EXPLICIT_FLAG = {"GOAL": "goal", "DESCRIBE": "describe", "PLANNER": "planner",
                  "COVERAGE_TARGET": "coverage-target", "MAX_STEPS": "max-steps"}


def load_run_config(path: str) -> dict:
    """Parse a RunConfig YAML file -> a validated dict of allowed keys (unknown keys ignored).

    Returns {} for a missing/empty file. Raises ValueError on non-mapping YAML or a non-numeric value
    for a numeric key, so a malformed config fails loudly (the caller maps it to exit 3).
    """
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"RunConfig {path!r}: top-level YAML must be a mapping, got {type(data).__name__}")
    cfg = {}
    for k, v in data.items():
        if k not in _ALLOWED or v is None:
            continue
        if k in _NUMERIC:
            try:
                v = _NUMERIC[k](v)
            except (TypeError, ValueError):
                raise ValueError(f"RunConfig {path!r}: key {k!r} must be {_NUMERIC[k].__name__}, got {v!r}")
        elif k == "auth":                              # M9.2b: declarative auth -> M9.1 env (validated)
            if not isinstance(v, dict):
                raise ValueError(f"RunConfig {path!r}: 'auth' must be a mapping")
            v = {sk: sv for sk, sv in v.items() if sk in _AUTH_ENV and sv is not None}
        elif k == "scenarios":                         # M9.2b: a list of {name, goal XOR describe}
            if not isinstance(v, list) or not all(
                    isinstance(e, dict) and e.get("name")
                    and (bool(e.get("goal")) != bool(e.get("describe"))) for e in v):
                raise ValueError(f"RunConfig {path!r}: 'scenarios' must be a list of "
                                 f"{{name, goal|describe}} (exactly one of goal/describe per entry)")
        cfg[k] = v
    return cfg


def _explicit_set(env) -> set:
    raw = (env.get("SENTINEL_EXPLICIT") or "").strip()
    return {p for p in raw.split(",") if p} if raw else set()


def _overridable(env, env_key: str) -> bool:
    """RunConfig may set env_key only if the flag was NOT passed explicitly AND the value is unset/blank
    or still at the agentctl default (no explicit non-default flag)."""
    if _EXPLICIT_FLAG.get(env_key) in _explicit_set(env):
        return False                                # user passed the flag -> the file never overrides it
    cur = (env.get(env_key) or "").strip()
    return cur == "" or cur == _AGENTCTL_DEFAULTS.get(env_key)


def _resolve_planner(cfg: dict):
    """Resolve the mode/planner alias -> a single PLANNER value (None = leave default). `planner` is
    canonical; `mode` is a synonym (`explore` == default planner). Conflicting values raise."""
    planner = str(cfg["planner"]).strip().lower() if "planner" in cfg else None
    mode_planner = None
    if "mode" in cfg:
        mode = str(cfg["mode"]).strip().lower()
        mode_planner = None if mode == "explore" else mode
    if planner is not None and mode_planner is not None and planner != mode_planner:
        raise ValueError(f"RunConfig: conflicting mode={cfg['mode']!r} and planner={cfg['planner']!r}")
    return planner if planner is not None else mode_planner


def _apply_auth(auth: dict, env) -> None:
    """M9.2b: declarative auth -> M9.1 env (STORAGE_STATE*/PLAN_FILE/PW_NO_TRACE); a pre-set env wins."""
    for sk, sv in auth.items():
        env_key = _AUTH_ENV.get(sk)
        if not env_key or not _overridable(env, env_key):
            continue
        if sk == "pw_no_trace":                        # normalize common truthy forms -> "1"/"0"
            env[env_key] = "1" if str(sv).strip().lower() in ("1", "true", "yes", "on") else "0"
        else:
            env[env_key] = str(sv)


def _apply_scenarios(scenarios: list, env) -> None:
    """M9.2b: `--scenario <name>` selects ONE entry; an empty selector -> the first (§C: one mode/run).
    A non-empty selector matching no entry is a config error -> raise (caller maps it to exit 3)."""
    if not scenarios:
        return
    selector = (env.get("SCENARIO") or "").strip()
    if selector:
        chosen = next((s for s in scenarios if s.get("name") == selector), None)
        if chosen is None:
            raise ValueError(f"RunConfig: --scenario {selector!r} not found; available "
                             f"{[s.get('name') for s in scenarios]}")
    else:
        chosen = scenarios[0]
    if chosen.get("goal") and _overridable(env, "GOAL"):
        env["GOAL"] = str(chosen["goal"])
    if chosen.get("describe") and _overridable(env, "DESCRIBE"):
        env["DESCRIBE"] = str(chosen["describe"])


def apply_run_config(cfg: dict, env=None) -> dict:
    """Merge `cfg` into `env` (default os.environ). Precedence: explicit flag/env > RunConfig > default.

    `auth:`/`scenarios:` are applied declaratively (M9.2b). Returns the env mapping (mutated in place when
    it is os.environ). Raises ValueError on a mode/planner conflict (caller maps it to exit 3).
    """
    env = os.environ if env is None else env
    planner = _resolve_planner(cfg)                 # raises on mode/planner conflict
    if planner and _overridable(env, "PLANNER"):
        env["PLANNER"] = planner
    for key, value in cfg.items():
        if key in ("mode", "planner", "auth", "scenarios"):
            continue                                # handled by _resolve_planner / _apply_auth / _apply_scenarios
        env_key = _KEY_ENV[key]
        if _overridable(env, env_key):
            env[env_key] = str(value)
    _apply_auth(cfg.get("auth") or {}, env)         # M9.2b declarative auth + scenario selector
    _apply_scenarios(cfg.get("scenarios") or [], env)
    return env
