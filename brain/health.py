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
EXECUTOR = "executor"
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
    """Would make_backend('planner') return a real backend?

    Mirrors brain/llm.py's own branches rather than probing the network: the mandatory tier must cost
    nothing and must work in the air-gapped bundle, where the endpoint is a LAN address with no key.
    Being a mirror is a liability — llm.py can change underneath it — so the gate that covers this
    asserts the two agree rather than trusting the copy.
    """
    def _env(name: str) -> str:
        return os.environ.get(f"LLM_{name}_PLANNER") or os.environ.get(f"LLM_{name}") or ""

    provider = (_env("BACKEND") or "anthropic").lower()
    if provider == "anthropic":
        return bool(_env("API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "openai":
        if not (_env("MODEL")):
            return False
        return bool(_env("API_KEY") or os.environ.get("OPENAI_API_KEY") or _env("BASE_URL"))
    # An unknown provider is not "configured" — make_backend logs backend_unknown and returns None.
    return False


def _executor_runnable() -> "str | None":
    """Can the executor command actually be run? Returns a reason when it cannot.

    Deliberately NOT a spawn: initialising the executor launches a real browser, which is expensive
    and is exactly what happens today — too late, and after the run has already been reported as
    started. A path check catches the case that actually occurs (a wrong PW_EXECUTOR_CMD, a missing
    dist/ because nobody ran the build) at a cost of two stat calls.
    """
    cmd = os.environ.get("PW_EXECUTOR_CMD", "").strip()
    if not cmd:
        return "PW_EXECUTOR_CMD is not set"
    try:
        parts = shlex.split(cmd)
    except ValueError as exc:
        return f"PW_EXECUTOR_CMD is not parseable: {exc}"
    if not parts:
        return "PW_EXECUTOR_CMD is empty"
    interpreter = parts[0]
    if not (shutil.which(interpreter) or pathlib.Path(interpreter).exists()):
        return f"{interpreter!r} is not on PATH and is not a file"
    # The script argument, when there is one. `node dist/server.js` is the shipped shape.
    for arg in parts[1:]:
        if arg.startswith("-"):
            continue
        if not pathlib.Path(arg).exists():
            return f"{arg!r} does not exist — has pw-executor been built?"
        break
    return None


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

    need = {EXECUTOR}
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
    if component == EXECUTOR:
        log("fatal.executor_not_runnable", reason=reason)
    elif component == STORE:
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

        if component == EXECUTOR:
            why = _executor_runnable()
        elif component == LLM:
            why = None if _llm_configured() else (
                "no usable planner backend: set LLM_BACKEND/LLM_MODEL/LLM_BASE_URL, or an API key")
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
