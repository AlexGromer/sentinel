#!/usr/bin/env python3
"""LIVE-RECORD (ADR-125) — the `record` mode END TO END, through the real process boundary.

WHY THIS FILE EXISTS BESIDE test_observation_modes_offline.py, and why it mirrors
test_live_human_mode_offline.py rather than extending it. The resolver test can say `apply()` returns
a dict containing SENTINEL_RECORD. It cannot say the variable REACHES the executor — and that is the
whole claim. The half-built state this arc keeps walking into is the one where each side is right on
its own: a resolver that decides, a switch nobody exports, an executor reading a variable nobody sets.
Every unit test passes; the person gets no video.

It is a separate file from the `human` one because the two modes now share a mechanism (both decorate)
and differ in exactly the places that matter — one produces a file, one is impossible over CDP — and a
single file mixing them would let a change satisfy the shared half while quietly breaking the other.

So this runs the SHIPPED entry point (`python -m brain`) with a stub executor that does one thing —
writes its own environment to a file and exits — and then reads what the child actually received.
No browser, no network, no model: the stub dies on the first JSON-RPC call, which is several steps
AFTER the observation plan has been resolved, logged and exported. The video FILE itself is proved
elsewhere, where a real browser exists: pw-executor/src/record.test.ts asserts EBML magic bytes on
disk. Neither half proves the mode alone.

WHAT IT PINS, each against a way this could rot back:

 1. `observe=record` is ACCEPTED by the shipped path, and the child receives SENTINEL_RECORD=1.
 2. Every other mode leaves the child with SENTINEL_RECORD=0 — including `human`, which shares the
    decoration and must NOT acquire the recording with it.
 3. `record` carries the CURSOR (SENTINEL_DECORATE=1), which is Alex's requirement of 2026-08-02 and
    the reason the mode inherits `human`'s constraints rather than being a quiet exception to them.
 4. `record` + a golden capture is refused BEFORE the executor is spawned — inherited from the
    decoration, and asserted separately because inheriting it by accident is exactly how it would be
    lost when someone later rewrites the refusal to name one mode.
 5. `record` + CDP-attach is refused at the door too, and the refusal SAYS why. This is the ADR-125
    decision: unlike slowMo there is no substitute, so a run that could not record must not start.
 6. The run.observation EVENT carries the recording where a surface can read it — recovered with the
    catalogue's own template, the same mechanism the hub uses (docs/index.html::lgFields).

Offline, stdlib only.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:400])


# The stub executor. It answers nothing: the brain's first `initialize` call finds the pipe closed and
# reports a transport error, which is fine — everything this file asks about happened before that.
STUB = """#!/bin/sh
env > "$SENTINEL_ENV_DUMP"
exit 0
"""

SWITCHES = ("SENTINEL_OBSERVE", "SENTINEL_DECORATE", "SENTINEL_RECORD",
            "SENTINEL_LIVE_FRAMES", "SENTINEL_TRACE_SCREENSHOTS", "PW_CDP_ENDPOINT")


def run_brain(mode, run_mode="explore", env_extra=None):
    """Run the shipped entry point with a stub executor. Returns (exit code, stderr, child env|None)."""
    tmp = tempfile.mkdtemp(prefix="sentinel-liverecord-")
    stub = os.path.join(tmp, "stub-executor.sh")
    with io.open(stub, "w", encoding="utf-8") as fh:
        fh.write(STUB)
    os.chmod(stub, 0o755)
    dump = os.path.join(tmp, "child.env")

    env = dict(os.environ)
    env.update({
        "PW_EXECUTOR_CMD": stub,
        "SENTINEL_ENV_DUMP": dump,
        "ARTIFACT_DIR": os.path.join(tmp, "artifacts"),
        "RUN_MODE": run_mode,
        "RUN_ID": "liverecord",
        "TARGET_URL": "file:///dev/null",
        "PLAN_FILE": os.path.join(tmp, "plan.json"),
        "PYTHONPATH": ROOT,
        "CI": "0",
        "HEAL_LLM": "0",
    })
    with io.open(env["PLAN_FILE"], "w", encoding="utf-8") as fh:
        json.dump({"steps": []}, fh)
    for k in SWITCHES:
        env.pop(k, None)
    if mode is not None:
        env["SENTINEL_OBSERVE"] = mode
    env.update(env_extra or {})

    p = subprocess.run([sys.executable, "-m", "brain"], cwd=ROOT, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    child = None
    if os.path.exists(dump):
        child = {}
        with io.open(dump, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    child[k] = v
    # Everything this run produced lived under `tmp`, including its artifact dir — so the suite leaves
    # no /tmp/sentinel-* behind. Read first, then remove: the dump is the answer, not a side effect.
    shutil.rmtree(tmp, ignore_errors=True)
    return p.returncode, p.stderr, child


def obs_line(stderr):
    for line in stderr.splitlines():
        if "run.observation:" in line:
            return line
    return ""


def test_record_reaches_the_executor_as_a_switch_not_as_an_intention():
    rc, err, child = run_brain("record")
    check("observe=record is no longer refused by the shipped path (exit 3 is the pre-flight refusal)",
          rc != 3, f"rc={rc} stderr={err[-400:]}")
    check("...and the executor process was actually reached", child is not None, err[-400:])
    if child:
        check("the child receives SENTINEL_RECORD=1 — the ONLY thing that makes the mode real",
              child.get("SENTINEL_RECORD") == "1",
              f"SENTINEL_RECORD={child.get('SENTINEL_RECORD')!r}; a resolver that decides and exports "
              "nothing is the half-built state `record` spent months in, refused with the task named")
        check("...and record still captures frames, like every mode that is not off",
              child.get("SENTINEL_LIVE_FRAMES") == "1", child)


def test_the_recording_carries_the_cursor_because_a_bare_one_is_unreadable():
    """Alex's requirement of 2026-08-02, and the reason `record` inherits `human`'s constraints.

    Asserted at the BOUNDARY rather than on the plan: the decoration is real only if the executor is
    told, and `record` acquiring the cursor in the resolver while failing to export it would produce
    exactly the recording the requirement exists to prevent — a bare screencast nobody can follow."""
    _rc, err, child = run_brain("record")
    check("record exports the decoration too", child is not None and child.get("SENTINEL_DECORATE") == "1",
          f"SENTINEL_DECORATE={(child or {}).get('SENTINEL_DECORATE')!r} — a recording without a cursor "
          "is as unreadable as the bare screencast this mode exists to improve on")


def test_human_did_not_acquire_the_recording_along_with_the_decoration():
    """The mirror of the check above, and the one that would have caught the obvious mistake: the two
    modes now SHARE the decoration, so deriving the recording from the same tuple would have handed a
    video to every `human` run — a file nobody asked for, of somebody's application, on their disk."""
    for mode in (None, "off", "frames", "stream", "human"):
        _rc, err, child = run_brain(mode)
        label = mode or "(nothing asked for)"
        check(f"{label}: the executor was reached", child is not None, err[-300:])
        if child:
            check(f"{label}: SENTINEL_RECORD=0 in the child",
                  child.get("SENTINEL_RECORD") == "0",
                  f"SENTINEL_RECORD={child.get('SENTINEL_RECORD')!r} — recording a run nobody asked to "
                  "record writes a film of their application to disk without being asked")


def test_record_and_a_golden_capture_die_at_the_door_not_in_the_browser():
    rc, err, child = run_brain("record", run_mode="baseline")
    check("record + baseline exits 3", rc == 3, f"rc={rc} stderr={err[-400:]}")
    check("...as the observation refusal, not as some other pre-flight failure",
          "fatal.observe_refused" in err, err[-400:])
    check("...and no executor was spawned at all — the refusal costs nothing", child is None,
          "the stub executor ran, which means the contradiction was noticed after a process was started")


def test_record_over_cdp_is_refused_because_there_is_nothing_to_degrade_to():
    """ADR-125, and the check that separates an impossibility from a preference.

    `slowMo` has the same shape of problem under CDP-attach and is DEGRADED — the executor pays the
    pacing with its own pause. Video has no substitute: the run would simply end with no file. So the
    combination is declined before anything starts, and the refusal has to SAY that, because a person
    told only "refused" will try again with the same flags."""
    rc, err, child = run_brain("record", env_extra={"PW_CDP_ENDPOINT": "http://127.0.0.1:9222"})
    check("record + CDP-attach exits 3", rc == 3, f"rc={rc} stderr={err[-400:]}")
    check("...as the observation refusal", "fatal.observe_refused" in err, err[-400:])
    check("...and nothing was spawned", child is None, "a browser was started for a run that cannot record")
    check("...and the reason names CDP, so the person knows WHICH combination to change",
          "CDP" in err, err[-500:])

    # The control: the same endpoint with a mode that CAN run must not be refused. Without this, a
    # blanket "PW_CDP_ENDPOINT refuses everything" bug would pass every assertion above.
    rc2, err2, child2 = run_brain("human", env_extra={"PW_CDP_ENDPOINT": "http://127.0.0.1:9222"})
    check("a CDP run in a mode that IS possible still starts", rc2 != 3 and child2 is not None,
          f"rc={rc2} stderr={err2[-300:]}")


def test_a_blank_endpoint_is_not_an_attachment():
    """`PW_CDP_ENDPOINT=` (set but empty) is how a compose file spells "not configured". launch.ts
    trims before deciding; if this side did not, `record` would be refused on every deployment that
    declares the variable without a value — a refusal nobody could act on, because nothing is set."""
    rc, err, child = run_brain("record", env_extra={"PW_CDP_ENDPOINT": "   "})
    check("a blank CDP endpoint does not refuse the recording", rc != 3, f"rc={rc} stderr={err[-300:]}")
    check("...and the run still exports the switch", child is not None and child.get("SENTINEL_RECORD") == "1",
          (child or {}).get("SENTINEL_RECORD"))


def test_the_event_carries_the_recording_where_a_surface_can_read_it():
    """The mark a finished run wears comes from THIS line and nothing else, so it has to carry the
    fact in a shape the reader can recover — the hub's own algorithm over the catalogue's English
    template. A template edit that breaks the hub's reader breaks this too."""
    with io.open(os.path.join(ROOT, "brain", "events.json"), encoding="utf-8") as fh:
        entry = json.load(fh)["events"]["run.observation"]
    check("the catalogue still has both renderings of the observation event",
          bool(entry.get("en")) and bool(entry.get("ru")), entry)

    for mode, want in (("record", "True"), ("frames", "False")):
        _rc, err, _child = run_brain(mode)
        line = obs_line(err)
        check(f"{mode}: the run reports its observation at all", bool(line), err[-300:])
        rendered = line.split("run.observation:", 1)[-1].strip()
        fields = fields_from_template(entry["en"], rendered)
        check(f"{mode}: the VIDEO is recoverable from the rendered line, no second source needed",
              (fields or {}).get("video") == want, f"line={rendered!r} fields={fields}")


def test_the_keep_rule_is_the_trace_rule_and_the_lever_that_reverses_it_exists():
    """ADR-125 §3. Alex's rule — write always, delete on green, explicit switch — asserted directly on
    the decision function, because it is the one place a "helpful" change would flip the meaning of
    every existing `record` run at once.

    The lever matters as much as the rule: a person who asked to record a run that then PASSED gets no
    file, and without a documented way to keep it that reads as the mode being broken rather than as
    the policy working. The executor announces the discard by name; here we pin that the name is real."""
    # ADR-139: the argument is the run's OUTCOME, not a bare number — the decision moved off the
    # integer so that a green-but-incomplete run can keep its evidence too, behind an explicit lever.
    # The rule under test did not change: a clean run discards, a non-zero one keeps.
    from brain.__main__ import _keep_video
    from brain.outcome import Outcome, VERDICT_WORD

    def _o(code, degraded=False):
        return Outcome(exit_code=code, verdict=VERDICT_WORD.get(code, "problem"),
                       degraded=degraded, reason="", failed=0)

    os.environ.pop("SENTINEL_VIDEO_ALWAYS", None)
    os.environ.pop("SENTINEL_VIDEO_ON_DEGRADED", None)
    check("a clean run does NOT keep the video (the trace rule, ADR-084)", _keep_video(_o(0)) is False)
    check("a failed run keeps it — that is the run somebody will want to watch", _keep_video(_o(1)) is True)
    check("...and so does a golden regression (exit 2), where no step failed at all",
          _keep_video(_o(2)) is True,
          "exit_code != 0 rather than 'a step failed': a regression is exactly a case a human wants "
          "to look at frame by frame")
    check("a GREEN but incomplete run still discards by default — ADR-084 is not rolled back silently",
          _keep_video(_o(0, degraded=True)) is False,
          "ADR-139 widened WHAT can keep evidence, not WHEN it happens without being asked")
    os.environ["SENTINEL_VIDEO_ON_DEGRADED"] = "1"
    try:
        check("...and keeps it once the lever is thrown", _keep_video(_o(0, degraded=True)) is True)
    finally:
        os.environ.pop("SENTINEL_VIDEO_ON_DEGRADED", None)
    os.environ["SENTINEL_VIDEO_ALWAYS"] = "1"
    try:
        check("SENTINEL_VIDEO_ALWAYS=1 keeps a clean run's video — the lever the discard message names",
              _keep_video(_o(0)) is True)
    finally:
        os.environ.pop("SENTINEL_VIDEO_ALWAYS", None)


def fields_from_template(tpl, rendered):
    """Recover `{name}` values out of a rendered message — the hub's algorithm (lgFields), in Python.

    Deliberately the same shape: it is the only reason a Russian log row can show real values, and the
    only reason a surface can read the mode out of this event without a second field being invented
    for it. Returns None when the rendered text does not match the template at all.
    """
    names, pattern, i = [], "", 0
    while i < len(tpl):
        open_at = tpl.find("{", i)
        if open_at < 0:
            pattern += re.escape(tpl[i:])
            break
        close_at = tpl.find("}", open_at)
        if close_at < 0:
            pattern += re.escape(tpl[i:])
            break
        pattern += re.escape(tpl[i:open_at])
        names.append(tpl[open_at + 1:close_at])
        pattern += "([\\s\\S]*?)"
        i = close_at + 1
    if not names:
        return {}
    m = re.match("^" + pattern + "$", rendered)
    if not m:
        return None
    return dict(zip(names, m.groups()))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    if FAILS:
        print(f"\nFAIL — {len(FAILS)} check(s): " + "; ".join(FAILS[:6]))
        sys.exit(1)
    print(f"\nALL PASS ({len(fns)} live-record tests)")
