#!/usr/bin/env python3
"""M9.7 (ADR-123) — the pluggable adapter SPI is a SEAM, proved by driving a stranger through it.

WHAT WAS MEASURED before this file existed, and what each measurement demands of this gate:

  * `grep -c 'adapter\\|SPI' internal/ cmd/` (Go) == 0 — the word lived in the roadmap, not in the
    product. So this gate must exercise real code, never assert that a word appears in a file.
  * `brain/llm.py::make_backend` dispatched on two literal provider names and ended in
    `log("llm.backend_unknown"); return None`. ADR-045 made OpenAI-COMPATIBILITY pluggable, not the
    provider slot: a backend with a different SDK needed an edit to the core file.
  * `brain/runconfig.py::_AUTH_ENV` was a four-entry literal dict, and `load_run_config` DROPPED
    every `auth:` sub-key outside it — an out-of-tree auth mechanism could not even RECEIVE its
    configuration. Test 6 is that exact case and would have failed on the pre-M9.7 tree.
  * Deploy had no runtime seam at all; the Helm chart's `extraEnv` is evaluated before the process
    starts and the product cannot be extended through it.

EVERY CHECK HERE IS BEHAVIOURAL. Adapters defined in this file are registered into the real
registry, and the SHIPPED entry points — `brain.llm.make_backend(role)` and
`brain.runconfig.apply_run_config` / `load_run_config` — are then called for real. An assertion
about the SHAPE of the source is a surrogate: mutations pass straight through one, which this
project has measured repeatedly. Nothing here reads a source file.

THE LICENCE BOUNDARY IS CHECKED, NOT WRITTEN DOWN (test 9). ADR-056 §2 row 42 puts enterprise auth —
Keycloak/OIDC/Vault/SSO/RBAC — outside this Apache-2.0 repository, irreversibly. So the gate walks
the adapter names this repository registers and fails if one of them speaks that vocabulary. The
walk is DERIVED from `adapters.names(kind)`; its companion floor (`>= 2` built-ins, `>= 7` declared
sub-keys) is what stops a registry that has stopped registering anything from passing vacuously
(docs/DEVELOPMENT.md §0 principle 5).

Offline: no browser, no network, no model, no live LLM SDK.
Run:  PYTHONPATH="$PWD" .venv/bin/python tests/test_adapter_spi_offline.py
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain import adapters                                              # noqa: E402
from brain import llm                                                   # noqa: E402
from brain.runconfig import load_run_config, apply_run_config           # noqa: E402

# Snapshot the names THIS REPOSITORY ships, taken before any test registers a stranger. The licence
# check below must judge the product, not the fixtures, and doing it by snapshot rather than by
# filtering means a fixture cannot be renamed into invisibility.
SHIPPED = {kind: adapters.names(kind) for kind in adapters.KINDS}

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:400])


def raises(fn, *a, **kw):
    """(did_raise, message) — used instead of assertRaises so the message can be inspected too."""
    try:
        fn(*a, **kw)
    except Exception as e:
        return True, f"{type(e).__name__}: {e}"
    return False, ""


class _RecordingModelAdapter:
    """A provider this repository has never heard of. Records the ModelSpec it was handed."""

    name = "acme_cloud"

    def __init__(self):
        self.seen = None

    def make(self, spec):
        self.seen = spec
        return _FakeBackend(spec.model)


class _FakeBackend:
    supports_vision = False
    supports_structured = False
    name = "acme_cloud"

    def __init__(self, model):
        self.model = model


class _RefAuthAdapter(adapters.EnvBlockAdapter):
    """A stranger auth adapter whose sub-key the pre-M9.7 loader would have deleted."""

    name = "spi_ref_probe"
    _ENV = {"secret_path": "SENTINEL_SPI_PROBE_PATH", "insecure": "SENTINEL_SPI_PROBE_INSECURE"}
    _BOOL = frozenset({"insecure"})


def _yaml(body):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return path


class _Env:
    """Restore os.environ exactly, including keys the body deleted."""

    def __enter__(self):
        self._saved = dict(os.environ)
        return os.environ

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self._saved)
        return False


def _clean_llm_env(env):
    for k in [k for k in env if k.startswith("LLM_")]:
        del env[k]
    env.pop(adapters.ADAPTERS_ENV, None)


# --- 1. the registry refuses what would fail later ------------------------------------------------

def test_registration_refuses_a_broken_adapter_at_registration_time():
    """A plugin that fails on first USE fails inside somebody's CI, with a message about a missing
    attribute rather than about the plugin. So the shape is checked when it is registered."""
    class NoName:
        def make(self, spec):
            return None

    class NoMake:
        name = "no_make"

    class MakeIsData:
        name = "make_is_data"
        make = "not callable"

    for label, obj in (("no name", NoName()), ("no make", NoMake()), ("make not callable", MakeIsData())):
        did, msg = raises(adapters.register, adapters.MODEL, obj)
        check(f"a model adapter with {label} is refused", did, msg)

    ok = _RecordingModelAdapter()
    adapters.register(adapters.MODEL, ok)
    try:
        did, msg = raises(adapters.register, adapters.MODEL, _RecordingModelAdapter())
        check("registering the same name twice is refused unless replace=True", did, msg)
        replacement = _RecordingModelAdapter()
        adapters.register(adapters.MODEL, replacement, replace=True)
        check("...and replace=True actually replaces it",
              adapters.get(adapters.MODEL, "acme_cloud") is replacement)
    finally:
        adapters.unregister(adapters.MODEL, "acme_cloud")
    check("unregister removes it", adapters.get(adapters.MODEL, "acme_cloud") is None)

    did, msg = raises(adapters.register, "nonsense_kind", ok)
    check("an unknown KIND is refused", did, msg)


def test_require_names_what_is_actually_available():
    """The usual cause of a miss is a typo or a plugin that never loaded; both are answered by the
    real names, so they travel in the message."""
    did, msg = raises(adapters.require, adapters.AUTH, "keycloak")
    check("requiring an unregistered adapter raises", did, msg)
    check("...and the message lists what IS registered",
          all(n in msg for n in SHIPPED[adapters.AUTH]), msg)


# --- 2. the model half: a stranger provider really drives make_backend ----------------------------

def test_a_third_party_provider_is_picked_up_by_make_backend():
    """The measured gap: LLM_BACKEND could only name one of two hard-coded providers."""
    probe = _RecordingModelAdapter()
    adapters.register(adapters.MODEL, probe)
    try:
        with _Env() as env:
            _clean_llm_env(env)
            env["LLM_BACKEND"] = "acme_cloud"
            env["LLM_MODEL"] = "global-model"
            env["LLM_MODEL_PLANNER"] = "planner-model"
            env["LLM_BASE_URL"] = "http://acme.invalid/v1"
            env["LLM_API_KEY"] = "configured-key"
            env["LLM_VISION"] = "1"
            env["LLM_STRUCTURED"] = "1"
            backend = llm.make_backend("planner")

        check("make_backend returned the stranger's backend",
              isinstance(backend, _FakeBackend), type(backend).__name__)
        spec = probe.seen
        check("the adapter was handed a ModelSpec", isinstance(spec, adapters.ModelSpec), spec)
        check("role travels", spec.role == "planner", spec)
        check("provider travels lowercased", spec.provider == "acme_cloud", spec)
        # The precedence rule is the product's, not the adapter's: an adapter re-reading LLM_* would
        # be a second implementation of it, free to drift.
        check("role-specific model beats the global one (precedence resolved for the adapter)",
              spec.model == "planner-model", spec)
        check("base_url travels", spec.base_url == "http://acme.invalid/v1", spec)
        check("the explicitly configured key travels", spec.api_key == "configured-key", spec)
        check("vision/structured flags travel",
              spec.supports_vision is True and spec.supports_structured is True, spec)
        check("...and the backend the adapter built is the one returned",
              getattr(backend, "model", None) == "planner-model", backend)
    finally:
        adapters.unregister(adapters.MODEL, "acme_cloud")


def test_the_shipped_providers_travel_the_same_path():
    """A seam only strangers use is a seam nothing in CI exercises. `anthropic` and `openai` are
    registered adapters, and their pre-SPI refusals are byte-for-byte the same behaviour."""
    check("the shipped providers are registered as adapters",
          {"anthropic", "openai"} <= set(SHIPPED[adapters.MODEL]), SHIPPED[adapters.MODEL])

    with _Env() as env:
        _clean_llm_env(env)
        env.pop("ANTHROPIC_API_KEY", None)
        env["LLM_BACKEND"] = "anthropic"
        check("anthropic with no key still degrades to None (unchanged)",
              llm.make_backend("planner") is None)

        _clean_llm_env(env)
        env["LLM_BACKEND"] = "openai"
        env["LLM_MODEL"] = "some-model"
        env.pop("OPENAI_API_KEY", None)
        check("openai with neither key nor base_url still degrades to None (unchanged)",
              llm.make_backend("planner") is None)

        _clean_llm_env(env)
        env["LLM_BACKEND"] = "no_such_provider"
        check("an unregistered provider still degrades to None (unchanged)",
              llm.make_backend("planner") is None)


def test_an_unimportable_plugin_degrades_the_run_but_does_not_crash_it():
    """`make_backend` promises never to raise. A broken SENTINEL_ADAPTERS is a config problem, and
    the contract for those is the same as a missing SDK: no AI, and the run says so."""
    with _Env() as env:
        _clean_llm_env(env)
        env["LLM_BACKEND"] = "anthropic"
        env["ANTHROPIC_API_KEY"] = "x"
        env[adapters.ADAPTERS_ENV] = "sentinel_no_such_adapter_module_xyz"
        did, msg = raises(llm.make_backend, "planner")
        check("make_backend did not raise on an unimportable plugin", not did, msg)
        check("...and returned None so the run continues without AI",
              llm.make_backend("planner") is None)


# --- 3. the auth/deploy half: a stranger block really drives apply_run_config ----------------------

def test_the_reference_auth_adapter_is_the_m9_1_workflow_unchanged():
    """The default adapter must mean what `auth:` meant before the SPI, or every existing RunConfig
    (and control-api, which writes these blocks with no `adapter:` key) changes meaning silently."""
    env = {}
    cfg = load_run_config(_yaml("auth:\n  storage_state: s.json\n  storage_state_save: save.json\n"
                                "  login_plan: l.json\n  pw_no_trace: true\n"))
    apply_run_config(cfg, env)
    check("STORAGE_STATE", env.get("STORAGE_STATE") == "s.json", env)
    check("STORAGE_STATE_SAVE", env.get("STORAGE_STATE_SAVE") == "save.json", env)
    check("login_plan -> PLAN_FILE", env.get("PLAN_FILE") == "l.json", env)
    check("pw_no_trace: true normalizes to '1'", env.get("PW_NO_TRACE") == "1", env)

    off = {}
    apply_run_config(load_run_config(_yaml("auth: {pw_no_trace: no}\n")), off)
    check("...and a falsey spelling normalizes to '0'", off.get("PW_NO_TRACE") == "0", off)

    # Precedence: an explicit env beats the file, and it must keep beating it now that an adapter
    # produces the value instead of a literal dict.
    preset = {"STORAGE_STATE": "explicit.json"}
    apply_run_config(load_run_config(_yaml("auth: {storage_state: file.json}\n")), preset)
    check("a pre-set env still wins over the adapter's value",
          preset["STORAGE_STATE"] == "explicit.json", preset)


def test_a_third_party_auth_adapter_receives_keys_the_old_loader_deleted():
    """THE measurement that defines this milestone. Before M9.7 the loader filtered `auth:` sub-keys
    against a four-entry literal dict, so `secret_path` was deleted in `load_run_config` and no
    adapter could ever be asked about it. This check fails on the pre-M9.7 tree."""
    adapters.register(adapters.AUTH, _RefAuthAdapter())
    try:
        cfg = load_run_config(_yaml("auth:\n  adapter: spi_ref_probe\n"
                                    "  secret_path: kv/data/app\n  insecure: yes\n"))
        check("the stranger's sub-key SURVIVES the loader",
              cfg["auth"].get("secret_path") == "kv/data/app", cfg)
        check("...and the adapter name is normalized onto the block",
              cfg["auth"].get("adapter") == "spi_ref_probe", cfg)

        env = {}
        apply_run_config(cfg, env)
        check("apply_run_config routed the block through the stranger's adapter",
              env.get("SENTINEL_SPI_PROBE_PATH") == "kv/data/app", env)
        check("...including its boolean normalization",
              env.get("SENTINEL_SPI_PROBE_INSECURE") == "1", env)
        check("and the default adapter's variables were NOT written",
              "STORAGE_STATE" not in env, env)

        preset = {"SENTINEL_SPI_PROBE_PATH": "explicit"}
        apply_run_config(cfg, preset)
        check("precedence holds for a third-party adapter too (it cannot outrank an explicit value)",
              preset["SENTINEL_SPI_PROBE_PATH"] == "explicit", preset)
    finally:
        adapters.unregister(adapters.AUTH, "spi_ref_probe")


def test_an_unknown_adapter_name_is_a_config_error_not_a_silent_fallback():
    """Falling back to the default on a typo would run with NO auth and look like a run that worked
    — the exact shape of silence this project keeps paying for."""
    did, msg = raises(load_run_config, _yaml("auth: {adapter: keycloakk, storage_state: s.json}\n"))
    check("an unknown auth adapter raises (caller maps it to exit 3)", did, msg)
    check("...and the message names the available adapters",
          all(n in msg for n in SHIPPED[adapters.AUTH]), msg)

    did, msg = raises(load_run_config, _yaml("deploy: {adapter: nomad}\n"))
    check("an unknown deploy adapter raises too", did, msg)

    did, msg = raises(load_run_config, _yaml("deploy: not_a_mapping\n"))
    check("a non-mapping deploy block raises", did, msg)


def test_an_unimportable_plugin_is_a_hard_config_error_on_the_runconfig_path():
    """The other half of the deliberate split: `make_backend` degrades, RunConfig refuses. A run
    whose config names a module that does not exist has not been configured."""
    with _Env() as env:
        env[adapters.ADAPTERS_ENV] = "sentinel_no_such_adapter_module_zzz"
        did, msg = raises(load_run_config, _yaml("auth: {storage_state: s.json}\n"))
        check("load_run_config raises on an unimportable SENTINEL_ADAPTERS module", did, msg)
        check("...and names the module", "sentinel_no_such_adapter_module_zzz" in msg, msg)


def test_a_real_run_exits_3_on_an_unknown_adapter_end_to_end():
    """The exit code is claimed in three documents, so it is measured rather than read off
    `brain/__main__.py`. A REAL `python -m brain` is launched with `RUN_CONFIG` naming an adapter
    that does not exist; the refusal happens before any executor is spawned, so this stays offline.

    Reading the source instead would be the surrogate this file exists to avoid: the mapping from
    ValueError to 3 lives in a try/except several hundred lines from the parse, and a change to
    either end would leave a source assertion perfectly green."""
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "run.yaml")
    with open(cfg, "w") as fh:
        fh.write("auth: {adapter: keycloakk, storage_state: s.json}\n")
    env = dict(os.environ)
    env.update({"PYTHONPATH": ROOT, "ARTIFACT_DIR": os.path.join(tmp, "art"),
                "RUN_CONFIG": cfg, "RUN_MODE": "replay"})
    env.pop(adapters.ADAPTERS_ENV, None)
    proc = subprocess.run([sys.executable, "-m", "brain"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    check("a run whose RunConfig names an unknown adapter exits 3", proc.returncode == 3,
          f"rc={proc.returncode} {out[-300:]}")
    check("...and says which adapter, and which ones exist",
          "keycloakk" in out and "storage_state" in out, out[-300:])


def test_the_reference_deploy_adapter_wires_a_run_to_its_deployment():
    """The half that had no runtime seam at all. These three are read by the brain and are NOT
    pre-set by agentctl, which is why they are the ones a deployment can actually decide."""
    env = {}
    apply_run_config(load_run_config(_yaml(
        "deploy:\n  store_addr: gateway:50051\n"
        "  otel_endpoint: http://otel:4317\n  checkpoint_dsn: postgres://h/db\n")), env)
    check("store_addr -> STORE_ADDR", env.get("STORE_ADDR") == "gateway:50051", env)
    check("otel_endpoint -> OTEL_EXPORTER_OTLP_ENDPOINT",
          env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://otel:4317", env)
    check("checkpoint_dsn -> CHECKPOINT_DSN", env.get("CHECKPOINT_DSN") == "postgres://h/db", env)

    preset = {"STORE_ADDR": "inherited:1"}
    apply_run_config(load_run_config(_yaml("deploy: {store_addr: file:2}\n")), preset)
    check("an inherited STORE_ADDR still wins (control-api passes its own gateway down)",
          preset["STORE_ADDR"] == "inherited:1", preset)

    both = {}
    apply_run_config(load_run_config(_yaml(
        "auth: {storage_state: s.json}\ndeploy: {store_addr: g:1}\n")), both)
    check("auth and deploy blocks apply in the same run without disturbing each other",
          both.get("STORAGE_STATE") == "s.json" and both.get("STORE_ADDR") == "g:1", both)


# --- 4. derived coverage + floors ------------------------------------------------------------------

def test_every_declared_key_of_every_shipped_env_adapter_actually_reaches_env():
    """DERIVED, not enumerated: the key list comes from each adapter's own `keys`. A key declared and
    then dropped by the loader is exactly the failure M9.7 exists to fix, and a hand-written list
    here could not see a key that was never added to it.

    The floors are the mandatory companion: a walk that has stopped finding anything passes
    perfectly over an empty set, and that is the one thing derivation cannot catch by itself."""
    kinds = [k for k in adapters.KINDS if k in adapters.DEFAULTS]
    check("both env-shaped kinds have a default adapter", sorted(kinds) == ["auth", "deploy"], kinds)

    total_keys = 0
    for kind in kinds:
        for name in SHIPPED[kind]:
            adapter = adapters.require(kind, name)
            for key in sorted(adapter.keys):
                total_keys += 1
                env = {}
                cfg = load_run_config(_yaml(f"{kind}:\n  adapter: {name}\n  {key}: probe-value\n"))
                check(f"{kind}/{name}: {key!r} survives the loader", key in cfg[kind], cfg)
                apply_run_config(cfg, env)
                # `probe-value` is not a truthy spelling, so a boolean key lands as "0" — the point is
                # that SOMETHING was written, i.e. the key is wired end to end rather than declared.
                check(f"{kind}/{name}: {key!r} reaches an environment variable", len(env) == 1, env)

    check("floor: at least two env-shaped adapters ship (auth + deploy)",
          sum(len(SHIPPED[k]) for k in kinds) >= 2, {k: SHIPPED[k] for k in kinds})
    check("floor: at least 7 declared sub-keys were walked (4 auth + 3 deploy)",
          total_keys >= 7, total_keys)


def test_no_enterprise_auth_adapter_ships_in_this_apache_repository():
    """ADR-056 §2 row 42, and Apache-2.0 cannot be taken back. Enterprise auth attaches to this SPI
    from OUTSIDE ([M-COMMERCIAL-auth]); shipping one here would be irreversible. Derived from the
    registered names, so a new adapter is judged by this rule automatically."""
    forbidden = re.compile(r"keycloak|oidc|openid|vault|\bsso\b|rbac|saml|ldap|kerberos", re.I)
    offenders = [(kind, n) for kind in adapters.KINDS for n in SHIPPED[kind] if forbidden.search(n)]
    check("no shipped adapter speaks enterprise-auth vocabulary", not offenders, offenders)
    check("floor: the walk saw a non-empty registry",
          sum(len(SHIPPED[k]) for k in adapters.KINDS) >= 4,
          {k: SHIPPED[k] for k in adapters.KINDS})


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        print(fn.__name__)
        fn()
    if FAILS:
        print(f"\nFAIL — {len(FAILS)} check(s): " + "; ".join(FAILS[:6]))
        sys.exit(1)
    print(f"\nALL PASS ({len(fns)} adapter-SPI tests)")
