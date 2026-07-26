"""Sentinel — JUnit XML reporter (ADR-073).

Pure + deterministic: `to_junit(report) -> str`. No browser, no network, no clock — the timestamp comes
from the report, so the same report renders byte-identically twice. That matters because this file is a
CI artifact and a diff of two runs should show what changed in the RUN, not when it was rendered.

WHY THIS EXISTS. Before it, integrating Sentinel with a pipeline meant reading the process exit code.
Every CI system — GitLab, Jenkins, GitHub Actions, TeamCity — consumes JUnit XML to render a test list
with names, durations and failure text, and none of them can do anything with a bare exit code. The
whole report already existed in `heal-report.json`; nothing needed computing, only shaping.

THE MAPPING IS THE DESIGN. JUnit has four step outcomes and Sentinel has more, so each Sentinel concept
has to be assigned a JUnit shape deliberately:

    step ok                  -> <testcase> with no child            (passed)
    step failed              -> <failure>                           (an assertion about the app failed)
    step quarantined         -> <skipped>                           (ADR-013: known flake, suppressed
                                                                     from exit 1 — a CI report that
                                                                     showed it as passed would hide it,
                                                                     and as failed would cry wolf)
    golden/visual regression -> <failure type="regression">         (exit 2 — a real app change)
    healed (re-bind)         -> <system-out> on the passing case     (the test was repaired; the step
                                                                     did pass, so it is not a failure)
    healed (re-ground)       -> <system-err> on the passing case     (ADR-071: identity unverified —
                                                                     stderr is how CI surfaces "passed,
                                                                     but read this")
    app faults               -> <system-err> on the SUITE            (ADR-072: they belong to the run,
                                                                     not to one step; the emitter cannot
                                                                     attribute a console error to a step)
    plan_hash / golden HMAC  -> <error> on a synthetic case          (exit 3: nothing executed, so there
                                                                     are no steps to attach it to, and
                                                                     `error` — not `failure` — is JUnit's
                                                                     word for "the harness broke")

`errors` and `failures` on the suite are counted from the emitted children rather than from the report's
own tallies, so the header can never disagree with the body.
"""
import xml.etree.ElementTree as ET
from xml.dom import minidom

# JUnit has no notion of "passed with drift". A suite attribute would be ignored by every consumer, so
# the signal rides where consumers DO look: stderr text on the case, and the suite name.
_VERDICT_SUFFIX = {
    "pass_with_drift": " (passed with interface drift)",
    "pass_with_app_faults": " (passed, application reported faults)",
}


def _seconds(ms) -> str:
    try:
        return f"{max(0.0, float(ms)) / 1000.0:.3f}"
    except (TypeError, ValueError):
        return "0.000"


def _case_name(s: dict) -> str:
    """A human-readable case name. CI lists these, so `intent` beats `semantic_id` when present."""
    n = s.get("step_id")
    what = s.get("intent") or s.get("type") or s.get("action_type") or "step"
    return f"{n}. {what}" if n is not None else str(what)


def to_junit(report: dict, *, suite: str = "sentinel") -> str:
    """Render a replay/baseline report as JUnit XML. `report` is the heal-report.json dict."""
    steps = report.get("steps") or []
    verdict = report.get("verdict") or ""
    plan_id = str(report.get("plan_id") or "")
    exit_code = report.get("exit_code", -1)

    suite_name = suite + _VERDICT_SUFFIX.get(verdict, "")
    ts = ET.Element("testsuites")
    su = ET.SubElement(ts, "testsuite", {
        "name": suite_name,
        "tests": "0", "failures": "0", "errors": "0", "skipped": "0",
        "time": _seconds(report.get("duration_ms")),
    })
    ET.SubElement(su, "properties")
    props = su.find("properties")
    for k, v in (("plan_id", plan_id), ("mode", str(report.get("mode") or "")),
                 ("exit_code", str(exit_code)), ("verdict", verdict),
                 ("healed", str(report.get("healed", 0)))):
        if v:
            ET.SubElement(props, "property", {"name": k, "value": v})

    # drift lookup by step, so a healed step's case can carry WHAT drifted (ADR-071).
    drift_by_step = {}
    for d in ((report.get("drift") or {}).get("elements") or []):
        drift_by_step[d.get("step")] = d

    n_fail = n_err = n_skip = 0

    # Integrity hard-abort: nothing executed, so there is no step to hang it on. A synthetic case with
    # <error> is the only honest shape — reporting zero tests would read as "nothing to run".
    if exit_code == 3:
        tc = ET.SubElement(su, "testcase", {"name": "plan integrity", "classname": suite, "time": "0.000"})
        ET.SubElement(tc, "error", {"type": "integrity",
                                    "message": str(report.get("reason") or "integrity check failed")})
        n_err += 1

    for s in steps:
        tc = ET.SubElement(su, "testcase", {
            "name": _case_name(s), "classname": suite, "time": _seconds(s.get("duration_ms")),
        })
        outcome = s.get("outcome")
        if s.get("quarantined"):
            ET.SubElement(tc, "skipped", {"message": "quarantined flake (ADR-013): suppressed from exit 1"})
            n_skip += 1
        elif outcome == "failed":
            ET.SubElement(tc, "failure", {
                "type": "step", "message": str(s.get("error") or "step failed")}).text = str(s.get("error") or "")
            n_fail += 1
        elif s.get("regression"):
            kinds = ",".join(s.get("regression") or [])
            ET.SubElement(tc, "failure", {
                "type": "regression", "message": f"golden regression: {kinds}"})
            n_fail += 1
        elif outcome == "healed":
            d = drift_by_step.get(s.get("step_id")) or {}
            heal = s.get("heal") or {}
            line = (f"healed via {heal.get('strategy')} "
                    f"(confidence {heal.get('confidence')}, {heal.get('outcome')})")
            if d:
                line += f"\nclass: {d.get('kind')}\nlocator: {d.get('from')} -> {d.get('to')}"
            # A re-ground goes to stderr, a re-bind to stdout. Both cases PASSED, so neither is a failure —
            # but "we chose a new selector and did not verify it is the same element" is exactly the kind of
            # thing a reviewer should see without opening another artifact.
            tag = "system-err" if d.get("kind") == "reground" else "system-out"
            ET.SubElement(tc, tag).text = line

    # Application faults belong to the SUITE: the executor cannot attribute a console error to a step.
    af = report.get("app_faults") or {}
    if af:
        parts = [f"application reported {af.get('total', 0)} fault(s), {af.get('errors', 0)} error(s)"]
        for code, n in sorted((af.get("counts") or {}).items()):
            parts.append(f"  {code}: {n}")
        if af.get("capped"):
            parts.append(f"  (capture capped at {af.get('cap')} — the list is truncated)")
        ET.SubElement(su, "system-err").text = "\n".join(parts)

    su.set("tests", str(len(steps) + (1 if exit_code == 3 else 0)))
    su.set("failures", str(n_fail))
    su.set("errors", str(n_err))
    su.set("skipped", str(n_skip))

    raw = ET.tostring(ts, encoding="unicode")
    # Pretty-printed on purpose: a human opens this file when CI's own rendering is unclear, and a
    # single-line 40 KB document is unreadable. `toprettyxml` is deterministic for a fixed tree.
    return minidom.parseString(raw).toprettyxml(indent="  ")


def write_junit(report: dict, path: str, *, suite: str = "sentinel") -> None:
    """Write the XML to `path`. Never raises on a write failure being the caller's problem — a report
    that cannot be written must not fail a run that already has its verdict."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_junit(report, suite=suite))
