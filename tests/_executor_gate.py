"""One place that decides "there is no executor" versus "the executor ran and failed".

WHY THIS FILE EXISTS. Seven offline suites drive the REAL pw-executor in a child process and read a
success marker off its stdout. Every one of them inferred "no browser available" from the ABSENCE of
that marker and returned `None`, which the caller turned into a silent `return`. The child's
`returncode` was never read — measured: zero occurrences of `returncode` in all seven files. So a
verb raising, the node child dying, a page error, a stalled RPC, or a renamed marker in the harness
itself all produced the same output as a healthy run: `SKIP`, `ok <test>`, a summary line counting
DEFINED functions, and a green CI step. The load-bearing property of ADR-093
(`audit.seen == len(interactives.elements)`) was unfalsifiable precisely when the executor was broken
enough to fail at all, and the CI floor counted FILES executed, never that one drove an executor.

THE RULE THIS FILE IMPLEMENTS. A skip must be an OBSERVATION of the environment, made BEFORE the
child starts and true or false regardless of whether any verb worked. It may never be an inference
from failure. Three outcomes, not two:

  (a) a prerequisite is genuinely absent  -> SKIP-LOUD, and the suite says how many checks are
      UNCHECKED. It does not print "N tests passed".
  (b) prerequisites present, child failed -> FAIL, naming the exit code and the stderr tail.
  (c) marker present                      -> the results.

The precedent is `tests/test_browser_journal_offline.py:80`, which already discriminates correctly by
observing `node --version` — "a check that silently skips is indistinguishable from one that passes,
which is how two mutations survived in PR-B". This module is that doctrine applied to the other
seven, in ONE place: leaving a copy per suite reproduces exactly the divergence mechanism ADR-093
was written about.

⚠ BIAS OF THE PROBE. When a prerequisite cannot be determined, it is reported PRESENT, so an
unexplained failure surfaces as a failure rather than a skip. A false skip is the defect this file
exists to remove; a false failure is visible and gets fixed. The bias only ever points at noise.

The filename starts with `_` so the CI glob `tests/test_*_offline.py` does not pick it up as a suite.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DIST = REPO / "pw-executor" / "dist" / "server.js"
MARKER = "@@RESULT@@"

# Bumped by drive() on every completed call. run_suite() snapshots it around each test to count how
# many checks reached a real executor — a number the suite DERIVES rather than one it declares.
_drives = 0


class ExecutorFailed(AssertionError):
    """Prerequisites were present and the executor still did not deliver a result.

    Deliberately an AssertionError: these suites are plain scripts run by `python <file>`, and a
    failure here has to end the process non-zero exactly the way a failed assert does.
    """


def _browser_missing() -> "str | None":
    """Is a Playwright browser absent? Returns a reason, or None when present OR undeterminable.

    `PLAYWRIGHT_BROWSERS_PATH=0` means the browsers live under the package's own node_modules; any
    other value is an explicit cache root; unset means the machine-global default. CI installs
    chromium-headless-shell into that global cache (ci.yml, "Build + unit-test pw-executor"), so in
    CI this returns None and any executor failure below is a real one.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override == "0":
        roots = [REPO / "pw-executor" / "node_modules"]
    elif override:
        roots = [pathlib.Path(override)]
    else:
        roots = [pathlib.Path.home() / ".cache" / "ms-playwright"]
    for root in roots:
        try:
            if not root.exists():
                continue
            if any(p.name.startswith("chromium") for p in root.iterdir()):
                return None
        except OSError:
            return None  # undeterminable -> report present, so a failure stays a failure
    return f"no chromium in the Playwright cache ({', '.join(str(r) for r in roots)})"


def missing_prerequisite() -> "str | None":
    """The first prerequisite observed ABSENT, or None. Never looks at whether anything worked."""
    if not DIST.exists():
        return f"pw-executor is not built ({DIST} — run `npm run build` in pw-executor/)"
    if shutil.which("node") is None:
        return "node is not on PATH"
    return _browser_missing()


def drive(calls: list, *, timeout: int = 300, env_extra: "dict | None" = None) -> list:
    """Run (method, params) pairs through the REAL executor and return the results.

    Raises ExecutorFailed when the child does not deliver. Callers must NOT guard on a None return —
    that shape is what let every failure read as a skip.
    """
    global _drives
    script = (
        'import sys, json; sys.path.insert(0, %r)\n'
        'from brain.executor import Executor\n'
        'ex = Executor("node %s")\n'
        'out = [ex.call(m, **p) for m, p in json.loads(%r)]\n'
        'ex.call("shutdown"); ex.close()\n'
        'print("%s" + json.dumps(out))\n' % (str(REPO), DIST, json.dumps(calls), MARKER)
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PW_NO_TRACE": "1"}
    env.update(env_extra or {})
    try:
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        # A hung executor is a failed executor. Left to propagate, TimeoutExpired reads as a suite
        # traceback — a different-looking crash for the same defect.
        raise ExecutorFailed(f"the executor did not finish within {timeout}s: {e}") from e
    out = _result_or_none(r.stdout)
    if out is not None:
        _drives += 1
        return out
    raise ExecutorFailed(_diagnosis(r, calls))


def _result_or_none(stdout: str) -> "list | None":
    for line in (stdout or "").splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    return None


def _diagnosis(r: "subprocess.CompletedProcess", calls: list) -> str:
    verbs = ", ".join(m for m, _ in calls)
    tail = ((r.stderr or "") + (r.stdout or ""))[-600:].replace("\n", " ")
    if r.returncode != 0:
        return (f"the executor exited {r.returncode} driving [{verbs}]. Prerequisites were observed "
                f"present, so this is a real failure and NOT a missing browser: {tail}")
    return (f"the executor exited 0 driving [{verbs}] but printed no {MARKER} line. Either a verb "
            f"returned without completing the run, or the marker was renamed in one place and not "
            f"the other: {tail}")


def drives() -> int:
    """How many executor calls have completed. For suites with their own runner."""
    return _drives


def require_executor(label: str) -> "str | None":
    """For a suite that drives an executor from only SOME of its checks.

    Returns the skip reason, or None when the suite may drive. Prints SKIP-LOUD itself so every
    caller words it identically.
    """
    missing = missing_prerequisite()
    if missing:
        print(f"  SKIP-LOUD: {missing} — {label} is UNCHECKED here (not 'checked and fine')")
    return missing


def run_suite(ns: dict, label: str) -> int:
    """Run every test_* in `ns`, and report how many of them reached a real executor.

    The count is DERIVED from drive() calls, not from `len(fns)`. That distinction is the whole
    point: the old summary counted DEFINED functions, so a run in which all eight checks skipped
    printed the identical "OK — 9 perception-engine tests passed".
    """
    global _drives
    fns = [v for k, v in sorted(ns.items()) if k.startswith("test_") and callable(v)]
    missing = missing_prerequisite()
    if missing:
        print(f"  SKIP-LOUD: {missing}")
        print(f"     {len(fns)} {label} checks are UNCHECKED here, not 'checked and fine'")
        print(f"EXECUTOR-DRIVEN 0 {label}")
        return 0
    driven = 0
    for fn in fns:
        before = _drives
        fn()
        if _drives > before:
            driven += 1
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} {label} tests passed")
    # Machine-readable and floored in CI. A suite that stops driving the executor altogether still
    # prints "ok" for every check; this line is the only thing that goes to zero.
    print(f"EXECUTOR-DRIVEN {driven} {label}")
    return 0
