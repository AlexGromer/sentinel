"""HEALTH-002 — a swallowed error is DECLARED, or it is listed as safe with a reason.

The rule exists because of a specific, expensive failure. The gRPC channel between the brain and the
orchestrator was dead in every deployment for months. Nobody noticed, because both Python call sites
catch and ignore on purpose — "telemetry must not fail a run" — so budget reconciliation, the abort
signal and operator takeover all degraded to "continue" without a word. The channel was not broken
loudly; it was quiet by construction.

Nothing stops that happening again. A new `except: pass` costs one line and is invisible to review.
This gate makes it cost a decision instead: either the handler emits a catalogued event code
(brain/events.json, which is also what makes a degradation reach the verdict), or it re-raises, or
its site is listed below with a sentence saying why it cannot hide a real failure.

WHY AN AST AND NOT A GREP. A grep for `log(` after `except` would mark the runcontrol handlers
DECLARED — they do log — and miss the actual bug, which is one level up: the logged degradation is
not read by two of its three callers. A gate cannot see that, and this one does not pretend to; what
it CAN do is refuse to let a handler be silent at the point where the failure is still knowable. The
control-flow half is named in the backlog as its own item rather than implied here.

SITES KEYED BY FUNCTION, NOT BY LINE. A line number changes when anything above it is edited, so a
line-keyed allow-list rots into noise within a week and gets bulk-updated without reading. The key
is `file::qualified.function::ordinal` — stable unless the function itself is restructured, which is
exactly when the exemption deserves re-reading.

KNOWN LIMIT, stated rather than discovered: a handler whose diagnostic is produced by a DIFFERENT
function further down the call chain reads as silent here. Those sites are in the list below with
that as their reason — five of them in replay.py, where the except records the step's own outcome
and `__main__.py` logs it afterwards. They are not exemptions from the rule; they are the rule being
satisfied somewhere this gate cannot see.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BRAIN = REPO / "brain"

# Generated protobuf stubs: `except ImportError:` version shims that protoc writes and nobody should
# annotate. Excluded by directory rather than by pattern — the whole tree is generated.
EXCLUDED_DIRS = {"pb"}

# --------------------------------------------------------------------------------------------------
# Sites that swallow deliberately. The reason is the point: an entry with no reason is an omission
# that learned to pass a test, so an empty string fails the gate as loudly as a missing entry.
#
# Grouped by why they are safe, because the groups are what a reader needs to judge new additions.
# --------------------------------------------------------------------------------------------------
ALLOWED: "dict[str, str]" = {}


def _allow(key: str, reason: str) -> None:
    ALLOWED[key] = reason


# 1. Configuration parsing. A bad env value falls back to a documented default; there is no operation
#    being guarded, only a value that was never going to be anything but its own default.
for _k in [
    "planner.py::_tok_budget::0",
    "budget.py::_limit::0",
    "healing.py::_env_conf::0",
    "llm.py::_int_env::0",
    "junit.py::_seconds::0",
    "__main__.py::_resume_through_takeovers::0",
]:
    _allow(_k, "env/format parse falling back to a documented default; no operation is guarded")

# 2. Teardown and cleanup. The object is going away and nothing downstream reads the outcome.
_allow("runcontrol.py::_GrpcRunControl.close::0",
       "channel teardown; the caller is discarding the client")
_allow("healing.py::HealingEngine._visual_reground::1",
       "temp screenshot cleanup; the only consequence is a leaked file in a temp dir")
_allow("__main__.py::_discard_checkpoint::0",
       "removing an optional -wal/-shm sidecar that is commonly absent")

# 3. The except body IS the recovery. It acts on the failure rather than hiding it.
_allow("store.py::_load_or_create_key::0",
       "falls through to an exclusive create, which fails loudly if the file is unusable")
_allow("store.py::_load_or_create_key::1",
       "FileExistsError is the concurrency case: it reads the winner's key, which is correct handling")
_allow("executor.py::McpExecutor._run::0",
       "stores the exception in self._err; __init__ re-raises it two lines later")

# 4. Observability OF observability. A span attribute cannot affect a run's result, and making it
#    fatal would let a tracing problem stop testing. (setup_tracing itself is NOT here — it now
#    declares, because an operator who configured an endpoint must learn that nothing was collected.)
_allow("otel.py::span::0", "one span attribute; the span and the run are unaffected")
_allow("otel.py::set_llm_tokens::0", "token attributes are cosmetic here — budget.py is authoritative")

# 5. THE GROUP WORTH RE-READING WHEN IT GROWS. Each of these records the failure into a structure
#    that a later, named site logs. They satisfy the rule somewhere this gate cannot follow, which is
#    a real limit of an AST walker, not a licence to be quiet.
for _k in ["replay.py::run_replay::1", "replay.py::run_replay::2", "replay.py::run_replay::3",
           "replay.py::run_replay::4", "replay.py::run_replay::5"]:
    _allow(_k, "becomes the step's own outcome='failed'; __main__.py logs test.step_failed from it")
_allow("replay.py::run_replay::6",
       "reason lands in report['reason']; __main__.py logs test.aborted from it")
_allow("__main__.py::_redact_trace::0",
       "reason is captured and logged three lines later as trace_discarded_unredacted / trace_leak")

# 6. Salvage paths whose caller already treats the empty result as the failure and says so.
_allow("llm.py::OpenAICompatBackend.complete_json::0",
       "the caller salvages via extract_json and logs when that fails too")
_allow("llm.py::_one_structured::0", "callers treat data=None as unparseable and log it")
_allow("llm.py::_learned_budgets::0", "performance cache; a miss costs a recomputation, nothing more")
_allow("llm.py::_remember_budget::0", "performance cache; a miss costs a recomputation, nothing more")
_allow("report.py::generate::0", "plan.json is legitimately absent for an imported plan")
_allow("strategies.py::prior_for::0", "falls back to the strategy table")
_allow("replay.py::_env_int::0", "env parse falling back to a documented default; no operation is guarded")
_allow("graph.py::_map_gate_timeout::0", "env parse falling back to a documented default; no operation is guarded")
_allow("executor.py::McpExecutor.close::0", "teardown; the caller is discarding this object")
_allow("store.py::ChatProjector.close::0", "teardown; the caller is discarding this object")
_allow("otel.py::inject_context::0",
       "trace-context injection; a miss costs cross-process correlation, not the run")
_allow("otel.py::extract_context::0",
       "trace-context extraction; a miss costs cross-process correlation, not the run")
_allow("executor.py::Executor.close::0",
       "the except body IS the recovery: it falls through to proc.kill()")
# 7. brain/health.py — caught by this gate the hour after it was written, which is the gate working.
#    Both handlers RETURN the reason as a string; the caller reports it with the component and the
#    run's mode, which is more useful than this file restating it one frame earlier.
_allow("executor.py::make_executor::0",
       "records the parse failure as `why` and raises it four lines later — the same "
       "diagnostic-one-level-up shape as the replay.py group above")
_allow("health.py::_llm_configured::0",
       "a backend that cannot even be constructed is not a usable one; check() reports the component")
_allow("health.py::_grpc_answers::0",
       "returns the connection failure as the reason; check() reports it with the component")
_allow("eventlog.py::_render::0",
       "falls back to the unformatted template — the message is still emitted, only unfilled")


def _is_declared(handler: ast.ExceptHandler) -> bool:
    """A handler declares its failure if it emits a catalogue code, re-raises, or logs at all.

    `log("code", ...)` is the strong form — it reaches the verdict when the code carries
    `degrades: true`. A bare `raise` is equally acceptable: an exception that keeps travelling has
    not been swallowed. `print(..., file=sys.stderr)` counts as the weak form, because several
    modules predate the catalogue and shouting is still better than silence.
    """
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in {"log", "print", "warn", "error", "exception"}:
                return True
    return False


def _sites() -> "list[tuple[str, ast.ExceptHandler, str]]":
    """Every except handler in brain/, keyed by file::qualified-function::ordinal."""
    out = []
    for path in sorted(BRAIN.rglob("*.py")):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(BRAIN).parts[:-1]):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        stack: "list[str]" = []
        counts: "dict[str, int]" = {}

        def visit(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    stack.append(child.name)
                    visit(child)
                    stack.pop()
                    continue
                if isinstance(child, ast.ExceptHandler):
                    qual = ".".join(stack) or "<module>"
                    key = f"{path.name}::{qual}"
                    n = counts.get(key, 0)
                    counts[key] = n + 1
                    out.append((f"{key}::{n}", child, str(path.relative_to(REPO))))
                visit(child)

        visit(tree)
    return out


def test_every_swallowed_error_is_declared_or_listed_with_a_reason():
    sites = _sites()
    # A floor. If the walker silently stops finding handlers — a parse change, a moved directory —
    # an empty set satisfies every assertion below perfectly.
    assert len(sites) >= 60, (
        f"only {len(sites)} except handlers found in brain/ — the walker, not the code, is what "
        f"changed. This gate is worthless over an empty set.")

    silent = []
    for key, handler, relpath in sites:
        if _is_declared(handler):
            continue
        if key in ALLOWED:
            assert ALLOWED[key].strip(), (
                f"{key} is listed with an EMPTY reason — an exemption with no reason is an omission "
                f"that learned to pass a test")
            continue
        silent.append(f"{relpath}:{handler.lineno}  ({key})")

    assert not silent, (
        "these handlers swallow an error without saying anything, and are not listed as safe:\n    "
        + "\n    ".join(silent)
        + "\n\n  Either emit a catalogued code from brain/events.json (which is what makes a "
          "degradation reach the verdict), or re-raise, or add the site to ALLOWED in this file "
          "with a sentence saying why it cannot hide a real failure.")


def test_no_stale_entries_in_the_allow_list():
    """An exemption for a site that no longer exists is a claim about code that is gone.

    Worse than useless: if a handler later reappears under the same function and ordinal, the stale
    entry silences it before anyone has looked at it.
    """
    live = {key for key, _, _ in _sites()}
    stale = sorted(k for k in ALLOWED if k not in live)
    assert not stale, (
        "these sites are listed as deliberately safe but no longer exist:\n    "
        + "\n    ".join(stale)
        + "\n\n  Remove them. A stale exemption silences whatever takes that slot next.")


def test_the_allow_list_does_not_cover_a_handler_that_already_declares():
    """A site that both declares AND is listed means one of the two is stale reasoning.

    Usually it means a handler was fixed to log and nobody removed its exemption — leaving a list
    entry that would silence the site again if the logging were ever removed.
    """
    redundant = []
    for key, handler, relpath in _sites():
        if key in ALLOWED and _is_declared(handler):
            redundant.append(f"{relpath}:{handler.lineno}  ({key})")
    assert not redundant, (
        "these handlers report their failure AND are listed as exempt — the exemption is now "
        "reasoning about code that changed, and would re-silence the site if the report were "
        "removed:\n    " + "\n    ".join(redundant))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} swallowed-error tests passed")
