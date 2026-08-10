"""Sentinel — the pluggable adapter SPI (M9.7, ADR-123).

WHAT WAS MEASURED BEFORE THIS FILE EXISTED. ADR-045 shipped `docs/ADAPTERS.md` and called the
model/backend half "pluggable", and the roadmap has carried "stable auth/deploy adapter SPI" ever
since. Four measurements say what was actually there:

  1. `grep -c 'adapter\\|SPI' internal/ cmd/` (Go) == **0**. The word existed in the roadmap and in
     one document; nothing in the product was named after it.
  2. `brain/llm.py::make_backend` dispatched on two literal provider names and ended in
     `log("llm.backend_unknown"); return None`. What ADR-045 made pluggable was OpenAI-COMPATIBILITY
     (any endpoint that speaks that wire), not the provider slot: a backend with a different SDK
     — Bedrock, Vertex native, an in-house gateway — could not be added without editing the core
     file. "Optional router behind LLM_BASE_URL" is a configuration, not a seam.
  3. `brain/runconfig.py::_AUTH_ENV` was a four-entry literal dict, and `load_run_config` DROPPED
     every `auth:` sub-key outside it. An out-of-tree auth mechanism could not even RECEIVE its
     configuration — the loader deleted it before any code could look.
  4. Deploy had no runtime seam at all. `deploy/sentinel/values.yaml` `extraEnv` + the
     `sentinel.envAllow` helper is the only way a deployment speaks to a run, and that is Helm text
     evaluated before the process starts, not a contract the product can be extended through.

WHAT THIS FILE IS. One registry, three kinds, and the built-ins registered INTO it so the SPI sits
on the hot path rather than beside it. `make_backend` resolves every provider — including the two
that shipped — through `get(MODEL, …)`; `apply_run_config` resolves every `auth:`/`deploy:` block
through `get(AUTH|DEPLOY, …)`. A seam only the third party uses is a seam nobody tests.

WHERE THE LICENCE BOUNDARY RUNS (ADR-056 §2 row 42, and it is irreversible). This repository is
Apache-2.0, and Apache cannot be taken back. The SPI and its reference adapters are the open-core
framework. Enterprise auth — Keycloak/OIDC/Vault/SSO/RBAC/multi-user — attaches to this SPI from
OUTSIDE and must never be committed here; it is tracked as `[M-COMMERCIAL-auth]`. That boundary is
CHECKED, not merely written down: `tests/test_adapter_spi_offline.py` walks the names registered by
this repository and fails if one of them speaks enterprise-auth vocabulary.

DISCOVERY. `SENTINEL_ADAPTERS` names importable modules (comma- or os.pathsep-separated); importing
one is what registers its adapters. Entry-point metadata was rejected: it requires the plugin to be
pip-installed into the same environment, and this product routinely runs from a source tree with a
`--frozen` lockfile, so half the deployments could not use it.

FAILURE IS SPLIT DELIBERATELY, because the two halves have different consequences:
  * importing a module named in `SENTINEL_ADAPTERS` fails -> `load_from_env` RAISES. `make_backend`
    already wraps provider construction and turns it into `llm.provider_unavailable` + heuristic
    fallback (a run without AI, announced); `load_run_config`/`apply_run_config` let it out as a
    config error, which the CLI maps to exit 3. Neither is silent.
  * asking for an adapter BY NAME that is not registered raises, always, in both paths. A typo in
    `auth: {adapter: …}` must not fall back to "no auth" — that is the shape of failure this
    project keeps paying for (a run that looks fine and did not do the thing).
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only; importing llm here would be circular
    from .llm import LLMBackend

#: The three seams. `MODEL` generalizes `brain/llm.py::make_backend`; `AUTH` generalizes the M9.1
#: storageState / login-as-test workflow that `brain/runconfig.py::_apply_auth` drove through a
#: literal dict; `DEPLOY` names the wiring a deployment hands a run, which until now existed only as
#: Helm `extraEnv`.
MODEL = "model"
AUTH = "auth"
DEPLOY = "deploy"
KINDS = (MODEL, AUTH, DEPLOY)

#: Environment variable naming importable adapter modules (see DISCOVERY above).
ADAPTERS_ENV = "SENTINEL_ADAPTERS"

#: Adapter used when a declarative block does not name one. These ARE the pre-SPI behaviours, so an
#: existing RunConfig means after this change exactly what it meant before it.
#:
#: `MODEL` is deliberately absent: its default is not a property of the block but of the env
#: precedence rule in `brain/llm.py` (`LLM_BACKEND_<ROLE>` > `LLM_BACKEND` > "anthropic"), and
#: duplicating it here would create a second place for it to drift.
DEFAULTS = {AUTH: "storage_state", DEPLOY: "local"}


@dataclass(frozen=True)
class ModelSpec:
    """Everything `make_backend` resolved from the environment, handed to a model adapter.

    Frozen and pre-resolved on purpose. A model adapter must NOT re-read `LLM_*` itself: the
    role-then-global precedence (`LLM_<KEY>_<ROLE>` > `LLM_<KEY>`) is a documented product rule, and
    a third-party adapter re-implementing it would drift from it. `api_key` here is only the
    explicitly configured `LLM_API_KEY[_ROLE]`; a provider-specific fallback (ANTHROPIC_API_KEY,
    OPENAI_API_KEY) belongs to the adapter that knows the provider.
    """
    role: str                      # "planner" | "heal" | "chat"
    provider: str                  # the resolved LLM_BACKEND value, lowercased
    model: Optional[str]           # LLM_MODEL[_ROLE], or the per-role default
    base_url: Optional[str]
    api_key: Optional[str]
    supports_vision: bool
    supports_structured: bool


class ModelAdapter(Protocol):
    """Builds an `LLMBackend` for one provider. Returning None means "not configured" — the run
    continues without AI (heuristic planner / L1-L6 healing), which every caller already handles.
    Raising is also allowed: `make_backend` converts it into `llm.provider_unavailable`."""
    name: str

    def make(self, spec: ModelSpec) -> Optional["LLMBackend"]: ...


class EnvAdapter(Protocol):
    """An `auth:` or `deploy:` adapter: a declarative RunConfig block -> environment variables.

    `env()` is PURE — it returns a mapping and does not touch the process environment. That is what
    keeps precedence in one place: `brain/runconfig.py` applies the result through `_overridable`,
    so an explicit flag still beats the file no matter who wrote the adapter. An adapter that could
    write env directly would be an adapter that could quietly outrank the operator's own flag.

    `keys` declares the sub-keys the adapter accepts; the loader keeps exactly those and drops the
    rest (forward-compat, as RunConfig has always done with unknown keys).
    """
    name: str
    keys: frozenset

    def env(self, spec: Mapping[str, Any]) -> Mapping[str, str]: ...


# --- registry -------------------------------------------------------------------------------------

_REGISTRY: dict = {kind: {} for kind in KINDS}

#: What each kind must be able to do. Used by `register` to refuse a broken plugin at registration
#: time rather than at the moment a run needs it — a plugin that fails on first use fails inside
#: someone's CI at 3am, and the message is about a missing attribute rather than about the plugin.
_REQUIRED = {MODEL: ("make",), AUTH: ("env", "keys"), DEPLOY: ("env", "keys")}
#: Of the required attributes, the ones that must be methods rather than data.
_CALLABLE = frozenset({"make", "env"})


def _check_kind(kind: str) -> None:
    if kind not in _REGISTRY:
        raise ValueError(f"unknown adapter kind {kind!r}; expected one of {', '.join(KINDS)}")


def register(kind: str, adapter: Any, *, replace: bool = False) -> Any:
    """Register `adapter` under `kind`. Returns the adapter, so a module can register inline.

    Refuses a duplicate name unless `replace=True`. Overriding a built-in is a legitimate thing for a
    deployment to want (a house `storage_state` that reads from a mounted secret), but it must be
    said out loud — a plugin that silently displaced a built-in would make the run's behaviour depend
    on import order.
    """
    _check_kind(kind)
    name = getattr(adapter, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{kind} adapter must carry a non-empty string `name`, got {name!r}")
    name = name.strip()
    for attr in _REQUIRED[kind]:
        if not hasattr(adapter, attr):
            raise ValueError(f"{kind} adapter {name!r} is missing required attribute {attr!r}")
        if attr in _CALLABLE and not callable(getattr(adapter, attr)):
            raise ValueError(f"{kind} adapter {name!r}: {attr!r} must be callable")
    if name in _REGISTRY[kind] and not replace:
        raise ValueError(f"{kind} adapter {name!r} is already registered; pass replace=True to override")
    _REGISTRY[kind][name] = adapter
    return adapter


def unregister(kind: str, name: str) -> bool:
    """Remove an adapter. True if something was removed."""
    _check_kind(kind)
    return _REGISTRY[kind].pop(name, None) is not None


def get(kind: str, name: str) -> Optional[Any]:
    """The adapter, or None when nothing is registered under that name."""
    _check_kind(kind)
    return _REGISTRY[kind].get((name or "").strip())


def require(kind: str, name: str) -> Any:
    """The adapter, or ValueError naming what IS available.

    The available list is in the message on purpose: the usual cause is a typo or a plugin that was
    never loaded, and both are answered by seeing the real names.
    """
    found = get(kind, name)
    if found is None:
        avail = ", ".join(names(kind)) or "(none registered)"
        raise ValueError(f"unknown {kind} adapter {name!r}; available: {avail}")
    return found


def names(kind: str) -> tuple:
    """Registered names for `kind`, sorted. This is the list every gate walks — there is no second
    copy of it anywhere, which is the point (docs/DEVELOPMENT.md §0 principle 5)."""
    _check_kind(kind)
    return tuple(sorted(_REGISTRY[kind]))


# --- discovery ------------------------------------------------------------------------------------

#: module name -> None when it imported cleanly, or the exception that stopped it. A failure is
#: REMEMBERED and re-raised rather than retried: the second call would fail identically, and a
#: partially-imported module can leave a half-registered adapter behind.
_LOADED: dict = {}


def load_from_env(env=None) -> tuple:
    """Import every module named by `SENTINEL_ADAPTERS`; returns the module names that are loaded.

    Idempotent. Raises ValueError when a named module cannot be imported — see the FAILURE note in
    the module docstring for why this is loud rather than best-effort.
    """
    env = os.environ if env is None else env
    raw = (env.get(ADAPTERS_ENV) or "").strip()
    if not raw:
        return ()
    loaded = []
    for mod in [m.strip() for m in raw.replace(os.pathsep, ",").split(",") if m.strip()]:
        if mod in _LOADED:
            failure = _LOADED[mod]
            if failure is not None:
                raise ValueError(f"{ADAPTERS_ENV}: cannot import adapter module {mod!r}: "
                                 f"{type(failure).__name__}: {failure}") from failure
            loaded.append(mod)
            continue
        try:
            importlib.import_module(mod)
        except Exception as exc:
            _LOADED[mod] = exc
            raise ValueError(f"{ADAPTERS_ENV}: cannot import adapter module {mod!r}: "
                             f"{type(exc).__name__}: {exc}") from exc
        _LOADED[mod] = None
        loaded.append(mod)
    return tuple(loaded)


# --- reference auth / deploy adapters ---------------------------------------------------------------

_TRUE = ("1", "true", "yes", "on")


def _truthy(value: Any) -> str:
    """Normalize the YAML spellings of a boolean into the "1"/"0" the rest of the stack reads."""
    return "1" if str(value).strip().lower() in _TRUE else "0"


class EnvBlockAdapter:
    """Base for an auth/deploy adapter that is a translation table: sub-key -> environment variable.

    Both reference adapters are exactly that, and a third party writing one usually is too, so the
    table is the whole implementation. `keys` is DERIVED from `_ENV` in `__init_subclass__` rather
    than written beside it: a hand-kept companion list fails in the direction you cannot see — an
    entry for a removed key breaks loudly, a MISSING entry is silence, and the key is then dropped by
    the loader before any code can notice (docs/DEVELOPMENT.md §0 principle 5).
    """

    name = ""
    #: sub-key -> env var.
    _ENV: Mapping[str, str] = {}
    #: sub-keys whose value is a boolean and must be normalized to "1"/"0".
    _BOOL: frozenset = frozenset()
    keys: frozenset = frozenset()

    def __init_subclass__(cls, **kw) -> None:
        super().__init_subclass__(**kw)
        cls.keys = frozenset(cls._ENV)

    def env(self, spec: Mapping[str, Any]) -> Mapping[str, str]:
        out = {}
        for key, value in spec.items():
            env_key = self._ENV.get(key)
            if env_key is None or value is None:
                continue
            out[env_key] = _truthy(value) if key in self._BOOL else str(value)
        return out


class StorageStateAuth(EnvBlockAdapter):
    """The reference auth adapter — M9.1 storageState + login-as-test, unchanged (ADR-026).

    It is deliberately the workflow that already shipped rather than something new: the point of this
    milestone is a STABLE seam, and a seam is only shown to be stable by carrying the thing that
    already works. A login run sets `pw_no_trace` and `storage_state_save`; production runs set
    `storage_state` and never type the password at all.

    ⚠ `pw_no_trace` is a fail-closed SECRET guard with two independent enforcement points
    (`pw-executor/src/server.ts` `browser.fill` throws while tracing is active; a replay carrying a
    `secretRef` exits 3 before starting). It is carried here because M9.1 defined it as part of the
    login-as-test workflow — but an adapter that sets it to "0" is removing a guard, not configuring
    a mode. `brain/observe.py` refuses to touch this variable for the same reason.
    """

    name = "storage_state"
    _ENV = {"storage_state": "STORAGE_STATE", "storage_state_save": "STORAGE_STATE_SAVE",
            "login_plan": "PLAN_FILE", "pw_no_trace": "PW_NO_TRACE"}
    _BOOL = frozenset({"pw_no_trace"})


class LocalDeploy(EnvBlockAdapter):
    """The reference deploy adapter — where this run keeps its state and sends its telemetry.

    WHY THESE THREE KEYS AND NOT THE OBVIOUS ONES. `ARTIFACT_DIR` and `PW_EXECUTOR_CMD` look like
    deployment wiring and are not available as such: `cmd/agentctl/main.go` emits BOTH for every run
    it spawns (`ARTIFACT_DIR=`+dir appears on nine spawn paths, `PW_EXECUTOR_CMD=`+pwExec on the
    executor path), so a RunConfig value for either would lose to the explicit value on every real
    invocation and the key would be decoration. The three below are read by the brain and are NOT
    pre-set by agentctl, so a deployment can genuinely decide them:

      store_addr      -> STORE_ADDR                    brain/store.py::make_store (gRPC gateway vs
                                                       LocalStore); agentctl sets it only when it
                                                       starts or inherits a gateway
      otel_endpoint   -> OTEL_EXPORTER_OTLP_ENDPOINT   brain/otel.py — unset means a no-op tracer
      checkpoint_dsn  -> CHECKPOINT_DSN                brain/__main__.py::_checkpointer (Postgres
                                                       instead of SQLite, for multi-runner K3s)

    A `kubernetes` or `compose` adapter derives the same three from a chart or the downward API and
    replaces this one — that substitution is the whole reason the seam exists. Today the Helm chart
    passes them by hand through `extraEnv` + `sentinel.envAllow` (deploy/sentinel/templates/), which
    works and is invisible to the product; naming it makes it a contract.
    """

    name = "local"
    _ENV = {"store_addr": "STORE_ADDR", "otel_endpoint": "OTEL_EXPORTER_OTLP_ENDPOINT",
            "checkpoint_dsn": "CHECKPOINT_DSN"}


register(AUTH, StorageStateAuth())
register(DEPLOY, LocalDeploy())
