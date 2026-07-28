"""Offline gate: a kept trace is a redacted trace, or it is no trace at all.

Run:  .venv/bin/python tests/test_trace_redaction_offline.py

ADR-084 narrowed the WINDOW — `trace.zip` survives only a run that did not finish clean. ADR-098
closes the CONTENT: the archive carries what the tool typed, in three places, and none of them is
reached by the named-secret scanner the backlog item proposed.

The redaction itself lives in Go (`internal/redact`, with its own tests against a real archive). What
this file pins is the WIRING and the policy around it, which is where a security control usually
fails:

  * it runs where the trace is KEPT, not where the report is built — a replay started directly never
    reaches report generation, and a redaction that depends on how a run was launched is the worst
    property such a control can have;
  * it FAILS CLOSED. A trace that could not be redacted is deleted. It is not a degraded artifact, it
    is a leak, and keeping it because the cleanup failed would invert the point;
  * the raw escape hatch announces itself every time — a mode that silently keeps credentials is
    exactly the thing being prevented;
  * every outcome reaches the verdict as a degradation, because a post-mortem the operator expects and
    does not get is a fact about the run.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TYPED = "s3cr3t-PASSWORD-VALUE"


def _sample_trace(dst: pathlib.Path) -> pathlib.Path:
    """A minimal archive carrying the typed value in the shape the real trace uses."""
    with zipfile.ZipFile(dst, "w") as z:
        z.writestr("trace.trace", "\n".join([
            json.dumps({"type": "before", "callId": "call@1", "method": "fill",
                        "params": {"selector": "internal:role=textbox", "value": TYPED}}),
            json.dumps({"type": "log", "callId": "call@1", "message": f'  fill("{TYPED}")'}),
        ]) + "\n")
        z.writestr("resources/a.jpeg", b"\xff\xd8\xff\xe0pixels")
    return dst


def _has_secret(path: pathlib.Path) -> bool:
    with zipfile.ZipFile(path) as z:
        return any(TYPED.encode() in z.read(n) for n in z.namelist() if not n.endswith(".jpeg"))


def _run_redact(trace: pathlib.Path, tool: str) -> None:
    """Call the real `_redact_trace` with SENTINEL_AGENTCTL pointed wherever the case needs."""
    from brain.__main__ import _redact_trace
    old = os.environ.get("SENTINEL_AGENTCTL")
    os.environ["SENTINEL_AGENTCTL"] = tool
    try:
        _redact_trace(str(trace))
    finally:
        if old is None:
            os.environ.pop("SENTINEL_AGENTCTL", None)
        else:
            os.environ["SENTINEL_AGENTCTL"] = old


# --- the load-bearing pair --------------------------------------------------------------------------
def test_a_kept_trace_is_redacted_and_an_unredactable_one_is_deleted():
    """Both halves in one check, because either alone is satisfiable by a broken implementation.

    Delete-always passes the negative half. Keep-always passes the positive half. Only the pair says
    the branch is a branch."""
    agentctl = REPO / "bin" / "agentctl"
    if not agentctl.exists():
        print("     SKIP — bin/agentctl not built (go build -o bin/agentctl ./cmd/agentctl)")
        return
    d = pathlib.Path(tempfile.mkdtemp())

    good = _sample_trace(d / "good.zip")
    assert _has_secret(good), "the fixture carries no secret, so the positive half proves nothing"
    _run_redact(good, str(agentctl))
    assert good.exists(), "a redactable trace must be KEPT — the post-mortem is the reason it exists"
    assert not _has_secret(good), "the trace was kept with the typed value still in it"

    bad = _sample_trace(d / "bad.zip")
    _run_redact(bad, "/nonexistent/agentctl")
    assert not bad.exists(), (
        "a trace that could not be redacted was left on disk. It is not a degraded artifact, it is a "
        "leak, and keeping it because the cleanup failed inverts the point of the cleanup.")


def test_every_outcome_reaches_the_verdict_as_a_degradation():
    """A post-mortem the operator expects and does not get is a fact about the run.

    Checked against the catalogue rather than the code: `degrades` is what carries a code to the
    verdict banner and the report, and a warn-level event without it is a line in a log nobody opens."""
    cat = json.loads((REPO / "brain" / "events.json").read_text(encoding="utf-8"))["events"]
    for code in ("system.trace_raw_kept", "system.trace_discarded_unredacted", "system.trace_leak"):
        assert code in cat, f"{code} is emitted but not catalogued"
        assert cat[code].get("degrades") is True, (
            f"{code} does not degrade the verdict, so a run that lost or leaked its trace looks "
            "exactly like one that did neither")
        for k in ("ru_verdict", "en_verdict"):
            assert cat[code].get(k), f"{code} has no {k}: the banner would fall back to the log wording"
    # ...and the success case must NOT degrade: a control that warns on every run is a control nobody
    # reads (the same reason ADR-092 made the audit speak once per page).
    assert cat["system.trace_redacted"].get("degrades") is not True, \
        "a successful redaction must be quiet, or the warning stops meaning anything"


def test_the_raw_escape_hatch_announces_itself():
    """`SENTINEL_TRACE_RAW=1` exists for diagnosing the tool itself. It must be loud every time: a
    mode that silently keeps credentials is the thing this whole change prevents."""
    d = pathlib.Path(tempfile.mkdtemp())
    t = _sample_trace(d / "raw.zip")
    from brain import eventlog
    from brain.__main__ import _redact_trace
    eventlog.reset_degradations()
    os.environ["SENTINEL_TRACE_RAW"] = "1"
    try:
        _redact_trace(str(t))
    finally:
        os.environ.pop("SENTINEL_TRACE_RAW", None)
    assert t.exists() and _has_secret(t), "raw mode must actually keep the trace as it is"
    assert "system.trace_raw_kept" in eventlog.degradations(), (
        "raw mode did not reach the verdict; a run keeping credentials on disk would look clean")


def test_redaction_runs_where_the_trace_is_kept():
    """The call site, not the function.

    `_stop_trace` is the one place that decides to keep an archive. Folding redaction into report
    generation instead would leave the raw file on disk for the whole gap AND skip it entirely for a
    replay driven directly — the trace would be clean or not depending on how the run was started.

    Anchored on the branch that keeps, because that is the branch that must redact; the discard branch
    must NOT call it (there is no file)."""
    src = (REPO / "brain" / "__main__.py").read_text()
    i = src.index("def _stop_trace(")
    body = src[i:src.index("\ndef ", i + 10)]
    keep = body[body.index("if _keep_trace(exit_code):"):body.index("else:")]
    assert "_redact_trace(" in keep, f"the keep branch does not redact:\n{keep}"
    discard = body[body.index("else:"):]
    assert "_redact_trace(" not in discard, "the discard branch redacts a file that was never written"


def test_the_executor_can_stop_recording_screenshots():
    """Pixels are not redactable, so the lever is not recording them.

    Asserted on the source of the executor: the alternative is a browser run per assertion, and what
    is being pinned is that the option is WIRED to Playwright's own switch rather than invented."""
    src = (REPO / "pw-executor" / "src" / "server.ts").read_text()
    i = src.index("context.tracing.start(")
    call = src[max(0, i - 400):src.index(")", i)]
    assert "SENTINEL_TRACE_SCREENSHOTS" in call, "the toggle is not read where tracing starts"
    assert "screenshots: wantShots" in call, (
        f"the toggle is read but not passed to Playwright:\n{call[-200:]}")
    # Default ON: the trace exists to explain a failure, and failures are usually visible.
    assert "?? '1'" in call, "the default changed; a trace without frames explains much less"


def test_the_redactor_is_one_implementation():
    """One vocabulary for "what is a secret". The scanner moved to `internal/redact` precisely because
    a second consumer appeared, and a copy would be the drift a package boundary was chosen to
    prevent — the same defect ADR-093 removed from the perception selector and ADR-094 from roles."""
    assert (REPO / "internal" / "redact" / "redact.go").exists()
    assert not (REPO / "cmd" / "control-api" / "redact.go").exists(), \
        "the old copy is back; two definitions of a credential is how they drift"
    sink = (REPO / "cmd" / "control-api" / "logsink.go").read_text()
    assert "redact.Line(" in sink, "the log sink stopped using the shared scanner"
    agentctl = (REPO / "cmd" / "agentctl" / "main.go").read_text()
    assert "redact.TraceFile(" in agentctl, "the trace subcommand does not use the shared package"


def test_the_subcommand_is_in_usage():
    """A subcommand absent from usage exists only for whoever reads the switch — and this repository
    has shipped a usage listing four of seven before."""
    src = (REPO / "cmd" / "agentctl" / "main.go").read_text()
    sw = src[src.index("switch os.Args[1] {"):src.index("default:", src.index("switch os.Args[1] {"))]
    import re
    cases = set(re.findall(r'case "([a-z-]+)":', sw))
    usage = src[src.index("func usage()"):src.index("\n}", src.index("func usage()"))]
    missing = sorted(c for c in cases if f"agentctl {c}" not in usage)
    assert not missing, f"subcommands missing from usage: {missing}"
    assert "redact-trace" in cases, "the subcommand vanished"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} trace-redaction tests passed")
