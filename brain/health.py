"""HEALTH-001 — refuse to start a run whose mode genuinely needs a component that is not there.

Every component in this system degrades independently and quietly. The control channel swallows its
errors on purpose ("telemetry must not fail a run"), the store-gateway is fail-open by design, and an
absent LLM falls back to the heuristic planner. Each of those choices is defensible alone. Together
they mean a run can start, look healthy, and finish green with half the system dead.

The case that made this urgent: `goal` mode with no model reachable. `make_backend` returns None, the
planner falls back to the heuristic, the goal is IGNORED, and the run exits 0. Not a crash — a
success that answered a question nobody asked.

TWO RULES SHAPE EVERY DECISION HERE.

  1. Requirements belong to the MODE, not to the product. `replay` genuinely does not need an LLM
     (L1-L6 healing works offline and that is a shipped path); `explore` genuinely does not need a
     store. A gate that demanded everything would break CI and the air-gapped bundle on the day it
     landed, which is how gates get switched off.

  2. Refuse when something DECLARED is unreachable; do not refuse because something was never
     configured — when "never configured" is itself an intentional, shipped degradation. A
     STORE_ADDR that was never set means LocalStore, which is the documented default. A STORE_ADDR
     that IS set and does not answer means the operator believes they have persistence and does not.

     The LLM for goal/describe is the ONE deliberate exception: it refuses even when nothing was
     configured at all, because there is no legitimate reason to ask for a goal-directed run without
     a model, the way there is a legitimate reason to replay without a shared store.

WHERE THIS IS CALLED FROM, and why it is not called from Go: `--run-config` can set GOAL/DESCRIBE
from a YAML file, and that merge happens INSIDE the brain (runconfig.apply_run_config). A check in
agentctl inspecting `--goal` would therefore be wrong for exactly the runs most worth checking. The
brain, after the merge, is the only place that knows what is actually about to execute — and it is
also the one point all four launch paths converge on (agentctl, control-api → agentctl, the
standalone orchestrator, and `python -m brain` directly).
"""
import os
import pathlib
import shlex
import shutil

from .eventlog import log

# Components, named once so the skip list, the probes and the messages cannot drift apart.
STORE = "store"
LLM = "llm"
ORCHESTRATOR = "orchestrator"

# One fatal code per component: the remedy differs per component, and a single "something is
# unreachable" code would make the message do work the code should.
#
# The codes are emitted as LITERALS in `_report` below rather than looked up from a table. A table
# reads better and the catalogue gate cannot see through it — it scans for a literal code inside a
# log call, and
# called every one of these a PHANTOM ("catalogued, emitted by nothing"). The gate is right: a code
# reachable only through an index is a code nobody can grep for either.


def _skipped() -> "set[str]":
    """Components an operator has explicitly chosen to stop checking.

    Per-component rather than one kill switch: bypassing a flaky store probe must not also blind the
    goal-mode LLM gate. Every hard gate in this codebase ships with a named, loud override
    (--force-replay, SENTINEL_ENV_ALLOWLIST=0, SENTINEL_MAP_GATE=0) and this follows that shape —
    without one, a false positive blocks all work with no recourse short of editing code, which is
    worse than the bug being fixed.
    """
    raw = os.environ.get("SENTINEL_HEALTH_SKIP", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def _llm_configured() -> bool:
    """Would this run get a real planner backend? ASKS make_backend rather than mirroring it.

    The first version copied make_backend's branches to avoid constructing anything. It was wrong
    within the hour, and the way it was wrong is the argument against ever doing that: a test (and
    the MCP sampling path) can inject a backend WITHOUT env vars, so the copy read "no model" while
    the product had one, and a chat turn stating a goal after small talk was refused. The mirror
    was checking the configuration; the question is whether a backend exists.

    Costs nothing a run does not already pay: make_backend reads config and constructs a client
    object — no network, no request. It logs its own reason when it returns None (no key, no model,
    unknown provider), so the operator gets the specific cause without this file restating it.
    """
    try:
        from .llm import make_backend
        return make_backend("planner") is not None
    except Exception:
        # A backend that cannot even be constructed is not a usable one. Reported by the caller with
        # the component and the run's mode, which is more useful than an SDK import error here.
        return False


# LIVE_PROBE_TIMEOUT is the budget for the OPTIONAL live model probe, and it is the same number Go
# spends on the same question (cmd/control-api/readyz.go::readyProbeTimeout). Two statements of one
# budget is the drift class this repo keeps meeting, so tests/test_run_health_offline.py reads BOTH
# and refuses a mismatch — the number may move, but only in both places at once.
LIVE_PROBE_TIMEOUT = 2.0

# The env var that turns the probe on. It is OFF by default and that is not caution, it is the rule
# this module is built on (see the header): refuse when something DECLARED is unreachable, never
# because something was never configured. A probe on by default would also add a network round trip
# to every run — including the air-gapped demo, which runs with `network_mode: none` — and would turn
# a blinking endpoint into a refusal. That is precisely the class of gate operators switch off.
LIVE_PROBE_ENV = "SENTINEL_LLM_LIVE_PROBE"


def _live_probe_enabled() -> bool:
    return os.environ.get(LIVE_PROBE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _llm_answers(timeout: float = LIVE_PROBE_TIMEOUT) -> "str | None":
    """Does the configured endpoint actually ANSWER? Returns a reason when it does not, else None.

    The second tier of the LLM check, and deliberately a different question from the first.
    `_llm_configured()` asks whether a backend would be constructed — cheap, offline, and mandatory.
    This asks whether the thing at the other end is alive, which costs a network round trip and is
    therefore optional. Measured 2026-08-03: ollama stopped answering, a goal run started anyway,
    authoring came back with an empty scenario and exit 1. The product NAMED the cause — the
    degradation was declared, not silent — but the operator learned it after the run instead of
    before it.

    ⚠ A PLAIN HTTP GET, never the SDK. Going through the OpenAI client would hand the real budget to
    the SDK's own retry and timeout defaults, so the number declared above would describe nothing.
    The same reason `_llm_configured` refuses to mirror make_backend's branches, one level down.

    ⚠ Returns None (i.e. "no objection") when there is no base_url to ask. An Anthropic-native
    deployment has no `/models` surface of ours to probe, and a probe that failed for the absence of
    an endpoint it was never given would be refusing a legitimate configuration.
    """
    base = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    if not base:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(base + "/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-supplied URL
            if resp.getcode() >= 400:
                return f"{base}/models answered HTTP {resp.getcode()}"
        return None
    except Exception as exc:  # noqa: BLE001 — any failure to answer is the answer
        return f"{base}/models did not answer: {type(exc).__name__}: {exc}"


def _grpc_answers(target: str, timeout: float = 2.0) -> "str | None":
    """Does something answer gRPC at `target`? Returns a reason when it does not.

    Uses channel readiness rather than a specific RPC so it works for both the store gateway and the
    orchestrator without knowing either service's methods. The address goes through grpcaddr.target()
    for the reason that module exists: a bare socket path is read by gRPC as a DNS name and resolves
    to nothing — silently, which is the failure this whole file is about.
    """
    try:
        import grpc

        from .grpcaddr import target as normalise
        channel = grpc.insecure_channel(normalise(target))
        try:
            grpc.channel_ready_future(channel).result(timeout=timeout)
            return None
        finally:
            channel.close()
    except Exception as exc:
        return f"{target}: {exc}"


def requirements(run_mode: str, has_objective: bool) -> "set[str]":
    """What THIS run genuinely needs.

    `has_objective` is passed in rather than read from the environment because a warm chat turn can
    carry a pinned objective that lives in checkpointer state — invisible to any env check, and the
    single case where a gate at the entry point would let exactly the wrong run through.
    """
    # Modes that never open a browser and never plan: reporting, export, maintenance.
    if run_mode in {"clear-quarantine", "export-spec", "report", "calibrate", "import", "revisions"}:
        return set()

    # The executor is deliberately NOT required here. Its check was a path test on the executor
    # command — a SURROGATE for "will this run get an executor", and wrong in the one place the two
    # differ: a caller that substitutes make_executor (the tests do; an injected executor would)
    # never runs that command at all. The validation moved INTO make_executor, where it sees the
    # command actually being used, and `fatal.executor_cmd_unset` already covers the unset case.
    need = set()
    if has_objective:
        need.add(LLM)
        # The map gate asks a human through the orchestrator. Only required when it is switched on
        # AND an address was declared — otherwise the gate skips itself by design, which is what
        # keeps CI, cron and the air-gapped bundle working.
        if os.environ.get("SENTINEL_MAP_GATE", "") != "0" and os.environ.get("ORCH_ADDR", "").strip():
            need.add(ORCHESTRATOR)
    # A declared store must answer. An undeclared one means LocalStore, the documented default.
    if os.environ.get("STORE_ADDR", "").strip():
        need.add(STORE)
    return need


def _report(component: str, reason: str) -> None:
    """Emit the fatal code for a component, as a literal the catalogue can vouch for."""
    if component == STORE:
        log("fatal.store_unreachable", reason=reason)
    elif component == LLM:
        log("fatal.llm_required_unreachable", reason=reason)
    elif component == ORCHESTRATOR:
        log("fatal.orchestrator_unreachable", reason=reason)


def check(run_mode: str, has_objective: bool) -> "list[tuple[str, str]]":
    """Probe what this run needs, REPORT every failure, and return them.

    Reporting here rather than at the call site keeps the component-to-code mapping in one place —
    two call sites looking it up would be two places to forget. The list is still returned so the
    caller owns the exit code, and so this module stays testable without a subprocess.
    """
    need = requirements(run_mode, has_objective)
    skip = _skipped()
    failures = []

    for component in sorted(need):
        if component in skip:
            # Loud on every use. An override that goes quiet is indistinguishable from a gate that
            # was never there, which is the state this file exists to leave behind.
            log("system.health_check_skipped", component=component)
            continue

        if component == LLM:
            why = None if _llm_configured() else (
                "no usable planner backend: set LLM_BACKEND/LLM_MODEL/LLM_BASE_URL, or an API key")
            # HEALTH-003 — the OPTIONAL second tier, and only ever after the first one passed. A
            # configured-but-dead endpoint is exactly the case the first tier cannot see: make_backend
            # constructs a client without speaking to anything, so "a backend exists" and "the model
            # answers" are different facts and this is the one that costs a round trip.
            if why is None and _live_probe_enabled():
                why = _llm_answers()
        elif component == STORE:
            why = _grpc_answers(os.environ["STORE_ADDR"])
        elif component == ORCHESTRATOR:
            why = _grpc_answers(os.environ["ORCH_ADDR"])
        else:  # pragma: no cover — requirements() is the only producer of these names
            why = None

        if why:
            _report(component, why)
            failures.append((component, why))
    return failures
