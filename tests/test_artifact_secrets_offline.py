"""Offline gate: a session file is not world-readable, and a green run leaves no trace (ADR-084).

Run:  .venv/bin/python tests/test_artifact_secrets_offline.py

Two holes, both about someone else's data sitting in our files:

  * `browser.saveStorageState` wrote cookies + localStorage — the live session of an authenticated
    user — with the process umask, typically 0644. The run directory is chmod 0700 because it MIGHT
    hold PII (THREAT_MODEL ❹); the file that CERTAINLY holds credentials had nothing, and
    `STORAGE_STATE_SAVE` is an arbitrary path that usually lands beside the project, not inside runs/.
  * `trace.zip` was kept on every run. It carries the tested application's live DOM (`input.value`
    included) and request bodies, and Playwright has no mask API — so a passing CI run left a copy of
    someone's application state on disk for a post-mortem nobody was going to perform.

The trace decision is asserted as a RULE (`_keep_trace`) and through the call path (`_stop_trace`),
because the two can disagree: the rule is trivial to get right and easy to never call.
"""
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.__main__ import _keep_trace, _stop_trace                # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent


class Ex:
    """Records how traceStop was called: with a path (keep) or without one (discard)."""

    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


# --- the rule ---------------------------------------------------------------------------------------
def _outcome(code, degraded=False):
    """ADR-139: решение об уликах принимает ИСХОД, а не голое число — так зелёный, но неполный
    прогон тоже может удержать запись, по явному рычагу. Правило про код при этом не изменилось."""
    from brain.outcome import Outcome, VERDICT_WORD
    return Outcome(exit_code=code, verdict=VERDICT_WORD.get(code, "problem"),
                   degraded=degraded, reason="", failed=0)


def test_a_clean_run_discards_its_trace_and_a_failed_one_keeps_it():
    assert _keep_trace(_outcome(1)) is True, "a failed run is exactly when a post-mortem happens"
    assert _keep_trace(_outcome(2)) is True, "a golden regression exits 2 with no step failing — still worth it"
    assert _keep_trace(_outcome(3)) is True, "a hard abort"
    assert _keep_trace(_outcome(0)) is False, "nothing to diagnose on a green run"
    assert _keep_trace(_outcome(0, degraded=True)) is False, (
        "a green but incomplete run still discards by default — ADR-084 must not be rolled back silently")
    # ⚠ И ОБРАТНАЯ ПОЛОВИНА, без которой предыдущая строка бессодержательна: убери ветку про
    # деградацию целиком — и утверждение «по умолчанию не держим» останется верным, а рычаг молча
    # перестанет существовать. Найдено мутацией.
    os.environ["SENTINEL_TRACE_ON_DEGRADED"] = "1"
    try:
        assert _keep_trace(_outcome(0, degraded=True)) is True, (
            "the lever that keeps a degraded run's trace is gone — the rule ADR-139 added has no effect")
        assert _keep_trace(_outcome(0)) is False, "the lever must not keep a CLEAN run's trace"
    finally:
        os.environ.pop("SENTINEL_TRACE_ON_DEGRADED", None)


def test_the_escape_hatch_restores_the_old_behaviour():
    """Someone debugging a run that PASSES but behaves oddly must be able to get the trace back —
    otherwise the rule trades one diagnosis away for another."""
    old = os.environ.get("SENTINEL_TRACE_ALWAYS")
    try:
        os.environ["SENTINEL_TRACE_ALWAYS"] = "1"
        assert _keep_trace(_outcome(0)) is True
        os.environ["SENTINEL_TRACE_ALWAYS"] = "0"
        assert _keep_trace(_outcome(0)) is False, "only an explicit 1 opts in"
    finally:
        os.environ.pop("SENTINEL_TRACE_ALWAYS", None)
        if old is not None:
            os.environ["SENTINEL_TRACE_ALWAYS"] = old


# --- the call path ----------------------------------------------------------------------------------
def test_the_teardown_passes_a_path_only_when_the_trace_is_kept():
    """The rule being right is not enough — this is the seam where it is applied. `path` present means
    Playwright writes the file; absent means it throws the buffer away, so the bytes never land."""
    keep = Ex()
    _stop_trace(keep, "/tmp/t.zip", _outcome(1))
    assert keep.calls == [("browser.traceStop", {"path": "/tmp/t.zip"})], keep.calls

    drop = Ex()
    _stop_trace(drop, "/tmp/t.zip", _outcome(0))
    assert drop.calls == [("browser.traceStop", {})], drop.calls


def test_a_teardown_failure_does_not_crash_a_finished_run():
    class Boom:
        def call(self, *a, **k):
            raise RuntimeError("executor already gone")

    _stop_trace(Boom(), "/tmp/t.zip", _outcome(1))          # must not raise


# --- the criterion that depended on the by-product --------------------------------------------------
def test_explore_no_longer_calls_a_missing_trace_a_failure():
    """`ok = ... and trace.exists()` asserted a BY-PRODUCT, not the result. After ADR-084 a clean
    explore deliberately leaves no trace, so that clause would have made every SUCCESSFUL explore
    report failure — a self-inflicted red CI.

    ⚠ REWRITTEN BEHAVIOURALLY (ADR-139). It used to assert the TEXT of the assignment line
    (`ok = plan_file.exists() and len(steps) >= 5`), which this repository calls a surrogate for a
    reason: it agreed with any code that merely spelled itself that way, and it could not have
    noticed the by-product coming back through a different statement. It also pinned the very
    threshold ADR-139 removed — the one that called a fully converged two-page site a finding about
    the application. The property it was defending is now driven: a HEALTHY explore that produced no
    trace file at all exits 0. That is strictly stronger, and it survives the next rename."""
    import contextlib
    import io
    import tempfile
    from pathlib import Path
    from brain.__main__ import _run_explore

    url = "file:///x/app.html"

    class _Healthy:
        """One page, one button, nothing else — and no trace file is ever written."""

        def call(self, m, **p):
            if m == "browser.navigate":
                return {"url": url, "title": "t", "status": 200}
            if m == "browser.currentUrl":
                return {"url": url, "title": "t"}
            if m == "browser.snapshot":
                return {"ariaSnapshot": '- button "Go"', "nodeCount": 2}
            if m == "browser.interactives":
                return {"elements": [{"role": "button", "name": "Go", "locator": {"css": "#go"},
                                      "visible": True, "enabled": True, "kind": "button",
                                      "testid": None}]}
            if m == "browser.links":
                return {"links": []}
            if m == "browser.click":
                return {"ok": True, "navigated": False}
            if m == "browser.probe":
                return {"count": 1}
            return {}

        def close(self):
            pass

    out = Path(tempfile.mkdtemp())
    with contextlib.redirect_stdout(io.StringIO()):
        rc = _run_explore(_Healthy(), "r", out, url, 0.85, 5)
    assert not (out / "trace.zip").exists(), "the fake executor is not supposed to write a trace"
    assert rc == 0, f"a healthy explore that left no trace reported failure: rc={rc}"


# --- the session file -------------------------------------------------------------------------------
def test_the_executor_restricts_the_session_file_and_the_test_can_tell():
    """Driven through the REAL executor over stdio, because the property is a filesystem mode — a unit
    test of our own code would assert that we called chmod, not that the file ended up restricted.

    Skipped rather than failed when the executor is not built: this suite must run without a browser
    install, and a skipped check says so out loud instead of passing quietly."""
    dist = REPO / "pw-executor" / "dist" / "server.js"
    if not dist.exists():
        print("     SKIP — pw-executor/dist not built (npm run build)")
        return
    out = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    script = (
        'import sys; sys.path.insert(0, %r)\n'
        'from brain.executor import Executor\n'
        'ex = Executor("node %s")\n'
        'ex.call("browser.navigate", url="about:blank")\n'
        'ex.call("browser.saveStorageState", path=%r)\n'
        'ex.call("shutdown"); ex.close()\n' % (str(REPO), dist, str(out))
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PW_NO_TRACE": "1"}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                       timeout=180)
    if not out.exists():
        print("     SKIP — no browser available:", (r.stderr or "")[-200:].replace("\n", " "))
        return
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o600, f"session file mode is {oct(mode)}, want 0o600"
    assert out.read_text().strip(), "an empty state file would make the mode check vacuous"


def test_the_executor_really_writes_nothing_when_the_path_is_omitted():
    """The brain asking for a discard is only half the contract — the executor has to honour it, and a
    mutation proved the other half was untested: making `traceStop` write regardless of the argument
    broke nothing, which is precisely the failure mode where we believe the trace is gone and the
    application's live DOM is on disk anyway.

    Driven through the real executor with tracing ON, both directions in one run: discard leaves no
    file, and the control (a path IS given) leaves one — otherwise "no file" could just mean tracing
    never started."""
    dist = REPO / "pw-executor" / "dist" / "server.js"
    if not dist.exists():
        print("     SKIP — pw-executor/dist not built (npm run build)")
        return
    d = pathlib.Path(tempfile.mkdtemp())
    dropped, kept = d / "dropped.zip", d / "kept.zip"
    script = (
        'import sys; sys.path.insert(0, %r)\n'
        'from brain.executor import Executor\n'
        'for path, arg in ((%r, None), (%r, %r)):\n'
        '    ex = Executor("node %s")\n'
        '    ex.call("browser.navigate", url="about:blank")\n'
        '    ex.call("browser.traceStop") if arg is None else ex.call("browser.traceStop", path=arg)\n'
        '    ex.call("shutdown"); ex.close()\n'
        % (str(REPO), str(dropped), str(kept), str(kept), dist)
    )
    env = {k: v for k, v in os.environ.items() if k != "PW_NO_TRACE"}
    env.update(PYTHONPATH=str(REPO))
    # Run from an EMPTY cwd, so "wrote nothing" can be checked as "produced no file anywhere it could
    # reasonably put one" rather than only "did not write the name we asked for". The realistic bug is
    # a default path (`path ?? "trace.zip"`), which lands relative to the executor's working directory
    # and would otherwise sail past a check that only looks at the requested name.
    cwd = d / "work"
    cwd.mkdir()
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                       cwd=cwd, timeout=240)
    if not kept.exists():
        print("     SKIP — no browser available:", (r.stderr or "")[-200:].replace("\n", " "))
        return
    assert kept.stat().st_size > 0, "the control produced an empty trace — the check below is vacuous"
    assert not dropped.exists(), "traceStop without a path still wrote a trace to the requested name"
    strays = [p.name for p in cwd.rglob("*") if p.is_file()]
    assert not strays, f"traceStop without a path wrote something anyway: {strays}"


def test_the_executor_declares_the_restriction_at_the_write_site():
    """Belt and braces for the case above being skipped in a browserless environment: the chmod must
    at least exist in the source, right where the file is produced."""
    src = (REPO / "pw-executor" / "src" / "server.ts").read_text()
    i = src.index("case 'browser.saveStorageState'")
    body = src[i:i + 1400]
    assert "chmodSync" in body and "0o600" in body, body[:400]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} artifact-secret tests passed")
