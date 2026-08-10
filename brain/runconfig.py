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

M9.7 (ADR-123): `auth:` and `deploy:` are ADAPTER blocks resolved through `brain/adapters.py`. Each
may carry `adapter: <name>`; omitting it selects the adapter that shipped, so every RunConfig written
before this means exactly what it meant before. What changed is which sub-keys survive the loader:
they are now the CHOSEN adapter's `keys`, not a literal dict in this file — an out-of-tree auth or
deploy mechanism used to have its configuration deleted here before it could be asked about it. An
unknown `adapter:` name is a config error (exit 3), never a silent fallback to no auth.
"""
import os

import yaml

from . import adapters

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
    # LIVE-MATRIX (ADR-120). Without this key the export path lied by omission: the hub's run form
    # offers "Наблюдение", prints each mode's cost, and then hands the person a `run.yaml` + a
    # command that carry no trace of the choice — so "repeat this run in CI or from a terminal"
    # repeated it with a DIFFERENT observation, silently. Measured on the rendered form: the auth
    # guard `pw_no_trace` DOES reach the exported file, so the export was selective in a way nobody
    # had written down.
    "observe": "SENTINEL_OBSERVE",
}
# M9.2b (ADR-028): structured keys handled specially (not a single env var).
# M9.7 (ADR-123): the declarative ADAPTER blocks (`auth:`, `deploy:`) are DERIVED from the SPI's
# kinds rather than spelled again here. Before this, `auth` was a literal in this set and its
# sub-keys were a literal dict below it — which is precisely why an out-of-tree auth mechanism could
# not be configured: the loader deleted its keys before any adapter could see them.
_ALLOWED = set(_KEY_ENV) | {"mode", "scenarios"} | set(adapters.DEFAULTS)
# Numeric keys are validated/coerced at load so a bad scalar fails as a config error (exit 3).
_NUMERIC = {"coverage_target": float, "max_steps": int,
            "plan_budget": int, "heal_budget": int, "total_budget": int}
# agentctl emits these defaults for EVERY run; the file may override a still-default value.
_AGENTCTL_DEFAULTS = {"PLANNER": "heuristic", "COVERAGE_TARGET": "0.85", "MAX_STEPS": "40"}
# brain env var -> the agentctl flag that sets it (for the explicit-flag-wins check).
_EXPLICIT_FLAG = {"GOAL": "goal", "DESCRIBE": "describe", "PLANNER": "planner",
                  "COVERAGE_TARGET": "coverage-target", "MAX_STEPS": "max-steps"}
# ⚠ `SENTINEL_OBSERVE` НЕТ в таблице выше НАМЕРЕННО, и это замер, а не забывчивость. Строка для него
# была написана и УБРАНА, когда мутация её выживания ничего не покрасила: `agentctl` не выдаёт для
# наблюдения ДЕФОЛТНОГО значения — только пустую строку либо то, что дал флаг, — поэтому явный выбор
# уже защищён проверкой «текущее значение непусто и не равно дефолту» ниже. Запись в таблице была бы
# недостижимым кодом, а недостижимый код читается как работающая защита.


def load_run_config(path: str) -> dict:
    """Parse a RunConfig YAML file -> a validated dict of allowed keys (unknown keys ignored).

    Returns {} for a missing/empty file. Raises ValueError on non-mapping YAML or a non-numeric value
    for a numeric key, so a malformed config fails loudly (the caller maps it to exit 3).
    """
    if not path or not os.path.exists(path):
        return {}
    adapters.load_from_env()                           # M9.7: `adapter:` may name an out-of-tree module
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"RunConfig {path!r}: top-level YAML must be a mapping, got {type(data).__name__}")
    cfg = {}
    for k, v in data.items():
        if k not in _ALLOWED or v is None:
            continue
        if k == "observe":
            # ⚠ YAML 1.1 — which pyyaml implements — reads a bare `off` as the BOOLEAN False (same
            # for `on`/`yes`/`no`). `observe: off` is the single most likely thing a person writes,
            # and without this it arrives as False, becomes the string "False" in the environment,
            # and the resolver refuses it as an unknown mode. Found by the behavioural gate on the
            # first run; a source-shaped assertion would have passed over it.
            if isinstance(v, bool):
                v = "off" if v is False else "on"
            v = str(v)
        elif k in _NUMERIC:
            try:
                v = _NUMERIC[k](v)
            except (TypeError, ValueError):
                raise ValueError(f"RunConfig {path!r}: key {k!r} must be {_NUMERIC[k].__name__}, got {v!r}")
        elif k in adapters.DEFAULTS:                   # M9.2b auth: / M9.7 deploy: -> an SPI adapter
            v = _validate_block(k, v, path)
        elif k == "scenarios":                         # M9.2b: a list of {name, goal XOR describe}
            if not isinstance(v, list) or not all(
                    isinstance(e, dict) and e.get("name")
                    and (bool(e.get("goal")) != bool(e.get("describe"))) for e in v):
                raise ValueError(f"RunConfig {path!r}: 'scenarios' must be a list of "
                                 f"{{name, goal|describe}} (exactly one of goal/describe per entry)")
        cfg[k] = v
    return cfg


def _validate_block(kind: str, block, path: str) -> dict:
    """Validate a declarative adapter block (`auth:` / `deploy:`) and normalize it (M9.7, ADR-123).

    `adapter:` names the adapter; omitting it means the one that shipped (`adapters.DEFAULTS`), so a
    RunConfig written before this SPI existed means exactly what it always meant. Sub-keys are kept
    or dropped according to the CHOSEN adapter's own `keys` — that is the whole change: the previous
    filter was a fixed four-entry dict, so a third-party adapter's configuration was deleted here
    before the adapter could ever be asked about it.

    An unknown adapter NAME raises: the caller maps a config error to exit 3. Falling back to the
    default would be worse than useless — a typo in `adapter:` would silently run with no auth at
    all, which looks exactly like a run that worked.
    """
    if not isinstance(block, dict):
        raise ValueError(f"RunConfig {path!r}: {kind!r} must be a mapping")
    requested = block.get("adapter")
    requested = str(requested).strip() if requested is not None else adapters.DEFAULTS[kind]
    try:
        adapter = adapters.require(kind, requested)
    except ValueError as e:
        raise ValueError(f"RunConfig {path!r}: {e}") from e
    out = {sk: sv for sk, sv in block.items() if sk in adapter.keys and sv is not None}
    out["adapter"] = adapter.name                      # normalized, so apply need not resolve twice
    return out


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


def _apply_block(kind: str, block: dict, env) -> None:
    """Run a declarative adapter block through its adapter and merge the result into `env`.

    Precedence stays HERE, not in the adapter: `EnvAdapter.env()` is a pure block->mapping function
    and `_overridable` decides what actually lands, so an explicit flag beats the file no matter who
    wrote the adapter. An adapter able to write env directly would be an adapter able to outrank the
    operator's own flag without saying so.
    """
    if not block:
        return
    adapter = adapters.require(kind, str(block.get("adapter") or adapters.DEFAULTS[kind]))
    spec = {k: v for k, v in block.items() if k != "adapter"}
    for env_key, value in adapter.env(spec).items():
        if _overridable(env, env_key):
            env[env_key] = value


def _apply_auth(auth: dict, env) -> None:
    """M9.2b: declarative auth -> env; M9.7: through the AUTH adapter the block names (default
    `storage_state` = the M9.1 STORAGE_STATE*/PLAN_FILE/PW_NO_TRACE workflow). A pre-set env wins."""
    _apply_block(adapters.AUTH, auth, env)


def _apply_deploy(deploy: dict, env) -> None:
    """M9.7: declarative deploy -> env through the DEPLOY adapter (default `local`: STORE_ADDR /
    OTEL_EXPORTER_OTLP_ENDPOINT / CHECKPOINT_DSN). A pre-set env wins."""
    _apply_block(adapters.DEPLOY, deploy, env)


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
    adapters.load_from_env(env)                     # M9.7: `adapter:` may name an out-of-tree module
    planner = _resolve_planner(cfg)                 # raises on mode/planner conflict
    if planner and _overridable(env, "PLANNER"):
        env["PLANNER"] = planner
    for key, value in cfg.items():
        if key in ("mode", "planner", "scenarios") or key in adapters.DEFAULTS:
            continue                                # handled by _resolve_planner / _apply_block / _apply_scenarios
        env_key = _KEY_ENV[key]
        if _overridable(env, env_key):
            env[env_key] = str(value)
    _apply_auth(cfg.get("auth") or {}, env)         # M9.2b declarative auth
    _apply_deploy(cfg.get("deploy") or {}, env)     # M9.7 declarative deploy wiring
    _apply_scenarios(cfg.get("scenarios") or [], env)
    return env
