#!/usr/bin/env python3
"""LIVE-HUMAN (ADR-120) — the `human` mode END TO END, through the real process boundary.

WHY THIS FILE EXISTS BESIDE test_observation_modes_offline.py. That one drives the resolver in
process: it can say `apply()` returns a dict with SENTINEL_DECORATE in it. It cannot say the variable
reaches the executor — and that is the whole claim. The half-built state this arc keeps walking into
is exactly the one where each side is right on its own: a resolver that decides, a switch nobody
exports, an executor reading a variable nobody sets. Each unit test passes; the person gets no cursor.

So this runs the SHIPPED entry point (`python -m brain`) with a stub executor that does one thing —
writes its own environment to a file and exits — and then reads what the child actually received.
No browser, no network, no model: the stub dies on the first JSON-RPC call, which is several steps
AFTER the observation plan has been resolved, logged and exported.

WHAT IT PINS, each against a way this could rot back:

 1. `observe=human` is ACCEPTED by the shipped path (exit is not the pre-flight refusal), and the
    child process receives SENTINEL_DECORATE=1. Either half alone is the half-built state above.
 2. Every other mode leaves the child with SENTINEL_DECORATE=0 — including `off`, where "nothing is
    observed" turning drawing ON would be invisible in the headless run where it matters.
 3. `human` + a golden capture is refused BEFORE the executor is spawned: the stub's env dump does
    not exist at all. A refusal that costs a browser is not a refusal at the door.
 4. The run.observation EVENT carries the mode and the decoration, and carries them recoverably: the
    values are pulled back out of the rendered line with the catalogue's own template, which is the
    mechanism the hub uses to mark a decorated run (docs/index.html::lgFields). One event, one source
    — the surfaces do not get a second field to disagree with.
 5. A hand-set switch survives the resolver AND is named in that same line, so a run whose decoration
    was forced from outside cannot read as an ordinary `frames` run.

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


def run_brain(mode, run_mode="explore", env_extra=None):
    """Run the shipped entry point with a stub executor. Returns (exit code, stderr, child env|None)."""
    tmp = tempfile.mkdtemp(prefix="sentinel-livehuman-")
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
        "RUN_ID": "livehuman",
        "TARGET_URL": "file:///dev/null",
        "PLAN_FILE": os.path.join(tmp, "plan.json"),
        "PYTHONPATH": ROOT,
        "CI": "0",
        "HEAL_LLM": "0",
    })
    # The plan a baseline/replay run would read. Never executed — the stub dies first — but its
    # absence would change WHICH refusal we are looking at, and this file is about one of them.
    with io.open(env["PLAN_FILE"], "w", encoding="utf-8") as fh:
        json.dump({"steps": []}, fh)
    for k in ("SENTINEL_OBSERVE", "SENTINEL_DECORATE", "SENTINEL_LIVE_FRAMES", "SENTINEL_TRACE_SCREENSHOTS"):
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


def test_human_reaches_the_executor_as_a_switch_not_as_an_intention():
    rc, err, child = run_brain("human")
    check("observe=human is not refused by the shipped path (exit 3 is the pre-flight refusal)",
          rc != 3, f"rc={rc} stderr={err[-300:]}")
    check("...and the executor process was actually reached", child is not None, err[-300:])
    if child:
        check("the child receives SENTINEL_DECORATE=1 — the ONLY thing that makes the mode real",
              child.get("SENTINEL_DECORATE") == "1",
              f"SENTINEL_DECORATE={child.get('SENTINEL_DECORATE')!r}; a resolver that decides and "
              "exports nothing is the half-built state this mode spent months in")
        check("...and human still captures frames, like stream",
              child.get("SENTINEL_LIVE_FRAMES") == "1" and child.get("SENTINEL_TRACE_SCREENSHOTS") == "1", child)


def test_no_other_mode_decorates_across_the_boundary():
    for mode in (None, "off", "frames", "stream"):
        rc, err, child = run_brain(mode)
        label = mode or "(nothing asked for)"
        check(f"{label}: the executor was reached", child is not None, err[-300:])
        if child:
            check(f"{label}: SENTINEL_DECORATE=0 in the child",
                  child.get("SENTINEL_DECORATE") == "0",
                  f"SENTINEL_DECORATE={child.get('SENTINEL_DECORATE')!r} — a cursor drawn into a run "
                  "nobody asked to watch corrupts the frames a model and a golden read")
    rc, err, child = run_brain("off")
    if child:
        check("off: nothing is captured AND nothing is drawn (two questions, two answers)",
              child.get("SENTINEL_LIVE_FRAMES") == "0" and child.get("SENTINEL_DECORATE") == "0", child)


def test_human_and_a_golden_capture_die_at_the_door_not_in_the_browser():
    rc, err, child = run_brain("human", run_mode="baseline")
    check("human + baseline exits 3", rc == 3, f"rc={rc} stderr={err[-400:]}")
    check("...as the observation refusal, not as some other pre-flight failure",
          "fatal.observe_refused" in err, err[-400:])
    check("...and no executor was spawned at all — the refusal costs nothing",
          child is None,
          "the stub executor ran, which means the contradiction was noticed after a process was started")


def test_the_event_carries_the_decoration_where_a_surface_can_read_it():
    """The mark a finished run wears comes from THIS line and nothing else. So the line has to carry
    the fact, and carry it in a shape the reader can recover — which is what the hub does with the
    catalogue's English template (docs/index.html::lgFields). Recovered here the same way, from the
    same catalogue, so a template edit that breaks the hub's reader breaks this too."""
    with io.open(os.path.join(ROOT, "brain", "events.json"), encoding="utf-8") as fh:
        entry = json.load(fh)["events"]["run.observation"]
    check("the catalogue still has both renderings of the observation event",
          bool(entry.get("en")) and bool(entry.get("ru")), entry)

    for mode, want in (("human", "True"), ("frames", "False")):
        _rc, err, _child = run_brain(mode)
        line = obs_line(err)
        check(f"{mode}: the run reports its observation at all", bool(line), err[-300:])
        rendered = line.split("run.observation:", 1)[-1].strip()
        fields = fields_from_template(entry["en"], rendered)
        check(f"{mode}: the mode is recoverable from the rendered line",
              (fields or {}).get("mode") == mode, f"line={rendered!r} fields={fields}")
        check(f"{mode}: the DECORATION is recoverable from the same line, no second source needed",
              (fields or {}).get("decorations") == want, f"line={rendered!r} fields={fields}")


def test_a_hand_set_switch_wins_and_says_so():
    rc, err, child = run_brain("human", env_extra={"SENTINEL_DECORATE": "0"})
    check("a hand-set SENTINEL_DECORATE survives the resolver",
          child is not None and child.get("SENTINEL_DECORATE") == "0", child)
    line = obs_line(err)
    check("...and the run SAYS the plan was overridden, naming the switch",
          "SENTINEL_DECORATE" in line and "hand" in line.lower(),
          f"{line!r} — an override the log does not mention makes the plan it printed a lie")


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
    print(f"\nALL PASS ({len(fns)} live-human tests)")
