#!/usr/bin/env python3
"""LIVE-MATRIX (ADR-120) — the observation mode is the PERSON's choice, resolved in ONE place.

WHAT WAS MEASURED before this existed: four switches, read in four places across three processes and
two languages, and nothing that decided them together —

    PW_HEADED / PW_HEADLESS      pw-executor/src/launch.ts:26
    SENTINEL_TRACE_SCREENSHOTS   pw-executor/src/server.ts:489
    SENTINEL_LIVE_FRAMES         brain/graph.py:45
    PW_NO_TRACE                  a SECRET guard, not a mode

so nobody could answer "what will I see and what does it cost". Worse: ONE picture — the per-step
frame — is gated by TWO of them in TWO languages, and switching one off yields a half-observed run
that says nothing about the missing half.

WHAT THIS FILE PINS, each against a way the design could quietly rot back:

 1. BOTH frame switches are written TOGETHER, and only by the resolver. Written apart, they produce
    the half-observed run again.
 2. PW_NO_TRACE is never written here. It is fail-closed with two enforcement points; an `off` that
    cleared it would remove a protection under the label "turn video off".
 3. A contradiction is REFUSED before the run starts, not resolved by accident: `human` + a golden
    capture does not degrade the reference, it makes it WRONG, and only on somebody else's replay.
 4. A mode this build cannot perform is REFUSED WITH THE TASK NAMED, never accepted and quietly
    downgraded — that is the class of silence this whole arc exists to remove.
 5. An unknown mode is refused, not silently replaced by the default.
 6. Every mode states its COST in both languages, in the schema the hub renders from — a mode that
    only has a name leaves a person choosing by vibe.
 7. The three surfaces agree: the schema enum, the CLI flag and the resolver's own set.

Offline, stdlib only.
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain import observe  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:400])


def read(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_the_chosen_mode_decides_and_says_why():
    p = observe.resolve("frames")
    check("frames captures", p.frames and p.mode == observe.FRAMES, p.as_dict())
    check("...and carries a reason a log can print", len(p.why or "") > 10, p.why)

    off = observe.resolve("off")
    check("off captures nothing", off.frames is False and off.mode == observe.OFF, off.as_dict())

    stream = observe.resolve("stream")
    check("stream still captures frames (the live screen is not a substitute for them)",
          stream.frames and stream.mode == observe.STREAM, stream.as_dict())

    d = observe.resolve(None)
    check("no choice resolves to the default", d.mode == observe.DEFAULT, d.as_dict())
    check("...and the reason SAYS it was the default, so 'did not choose' stays distinguishable "
          "from 'chose frames'", "default" in d.why, d.why)


def test_a_mode_this_build_cannot_perform_is_refused_with_the_task_named():
    for mode in (observe.HUMAN, observe.RECORD):
        try:
            observe.resolve(mode)
            check(f"{mode} is refused rather than silently downgraded", False, "no Refusal raised")
        except observe.Refusal as e:
            check(f"{mode} is refused rather than silently downgraded", True)
            check(f"...and the refusal names the task that will bring it ({mode})",
                  ("LIVE-HUMAN" in str(e)) or ("LIVE-RECORD" in str(e)), str(e))


def test_an_unknown_mode_is_refused_not_quietly_replaced():
    try:
        observe.resolve("cinema")
        check("an unknown mode is refused", False, "no Refusal raised")
    except observe.Refusal as e:
        check("an unknown mode is refused", True)
        check("...and the refusal lists what IS available", "frames" in str(e) and "off" in str(e), str(e))


def test_human_and_a_golden_capture_are_declined_at_the_door():
    """Alex's decision, and the reason it is a refusal rather than a warning: decoration does not make
    a reference worse, it makes it WRONG — and the wrongness surfaces later, on another person's run."""
    try:
        observe.resolve(observe.HUMAN, baseline=True)
        check("human + baseline is refused", False, "no Refusal raised")
    except observe.Refusal as e:
        check("human + baseline is refused", True)
        check("...naming the golden capture as the reason, not the feature flag",
              "baseline" in str(e).lower() or "golden" in str(e).lower(), str(e))
        # It must be refused for the RIGHT reason: not simply because `human` is unimplemented.
        check("...and it is the CONTRADICTION that is named, not the missing machinery",
              "LIVE-HUMAN" not in str(e), str(e))


def test_both_frame_switches_are_written_together_and_only_here():
    on = observe.apply(observe.resolve("frames"), {})
    off = observe.apply(observe.resolve("off"), {})
    for env, want, label in ((on, "1", "frames on"), (off, "0", "frames off")):
        for var in ("SENTINEL_LIVE_FRAMES", "SENTINEL_TRACE_SCREENSHOTS"):
            check(f"{label}: {var}={want}", env.get(var) == want,
                  f"{var}={env.get(var)!r} — both variables gate the SAME picture in two languages; "
                  "writing one and not the other is how a half-observed run is produced")

    kept = observe.apply(observe.resolve("frames"), {"SENTINEL_LIVE_FRAMES": "0"})
    check("a hand-set switch survives (this resolves a DEFAULT, not a policy)",
          kept.get("SENTINEL_LIVE_FRAMES") == "0", kept)
    check("...and overrides() reports it, so the log cannot disagree with the plan silently",
          observe.overrides({"SENTINEL_LIVE_FRAMES": "0"}) == ["SENTINEL_LIVE_FRAMES"])
    check("...and invents none where there are none", observe.overrides({}) == [])

    # ONE writer. Reading is fine — graph.py reads SENTINEL_LIVE_FRAMES, which is the point of
    # expanding into it — but a second WRITER is how the four switches drifted apart to begin with.
    write_re = re.compile(r"""environ\[\s*["'](SENTINEL_LIVE_FRAMES|SENTINEL_TRACE_SCREENSHOTS)["']\s*\]\s*=""")
    scanned = 0
    for base, _dirs, files in os.walk(os.path.join(ROOT, "brain")):
        for f in files:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(base, f), ROOT)
            scanned += 1
            for m in write_re.finditer(read(rel)):
                if not rel.endswith(os.path.join("brain", "observe.py")):
                    check(f"{rel} does not write {m.group(1)} directly", False,
                          "the decision has a second author, and two authors of one decision is "
                          "exactly how the four switches came apart")
    check("the walk actually reached the package (floor, or this check passes over nothing)",
          scanned >= 10, scanned)


def test_the_secret_guard_is_never_written_here():
    src = read(os.path.join("brain", "observe.py"))
    for m in re.finditer(r"PW_NO_TRACE", src):
        line = src[src.rfind("\n", 0, m.start()) + 1:src.find("\n", m.start())]
        check("brain/observe.py does not WRITE PW_NO_TRACE (mentioning it in prose is the point)",
              not re.search(r"""\[\s*["']PW_NO_TRACE["']\s*\]\s*=|setdefault\(\s*["']PW_NO_TRACE""", line),
              line.strip()[:120])
    env = observe.apply(observe.resolve("off"), {"PW_NO_TRACE": "1"})
    check("...and apply() leaves it untouched even for observe=off",
          env.get("PW_NO_TRACE") == "1", env.get("PW_NO_TRACE"))
    # And the guard it defers to still exists where this file says it does. A comment pointing at a
    # vanished enforcement point is worse than no comment.
    check("the executor still refuses to fill a secret while tracing is active",
          "refusing to enter a secret while tracing is active" in read(os.path.join("pw-executor", "src", "server.ts")))
    check("the brain still exits 3 rather than let a secret reach a trace",
          "fatal.secret_would_leak_to_trace" in read(os.path.join("brain", "__main__.py")))


def test_the_vlm_layer_says_when_it_will_get_nothing():
    """The VLM layer is not a mode: it cannot turn capture ON, because the person decides that. But a
    heal that will never receive a frame must SAY so — a heal that silently never ran is
    indistinguishable from one that had nothing to heal."""
    quiet = observe.resolve("off", heal_llm=True, vision_configured=True)
    check("observe=off with a vision heal configured says the heal will see nothing",
          "no frame" in quiet.why or "cannot look" in quiet.why, quiet.why)
    fine = observe.resolve("frames", heal_llm=True, vision_configured=True)
    check("...and says nothing of the sort when frames are on", "no frame" not in fine.why, fine.why)
    novision = observe.resolve("off", heal_llm=True, vision_configured=False)
    check("...and does not warn about a vision model nobody configured",
          "no frame" not in novision.why, novision.why)


def test_every_mode_states_its_cost_in_both_languages():
    for m in observe.MODES:
        c = observe.COST.get(m) or {}
        check(f"{m} states its cost in Russian", len(c.get("ru", "")) > 20, c.get("ru"))
        check(f"{m} states its cost in English", len(c.get("en", "")) > 20, c.get("en"))
    human = observe.COST[observe.HUMAN]
    check("human's cost NAMES the timing change — the one thing that makes it unusable for races",
          "тайминг" in human["ru"].lower() and "timing" in human["en"].lower(), human)


def test_the_three_surfaces_agree_on_the_set():
    """A mode the schema offers and the resolver refuses to name, or a CLI flag listing something
    neither knows, is a capability that exists in one place and not another — the gap ADR-107 exists
    to close. Derived from the files, not restated here."""
    go = read(os.path.join("cmd", "control-api", "main.go"))
    m = re.search(r'"enum":\s*\[\]string\{([^}]*)\}', go[go.index('"observe"'):go.index('"observe"') + 2000])
    check("the schema declares an enum for observe", m is not None)
    if m:
        schema_modes = tuple(x.strip().strip('"') for x in m.group(1).split(",") if x.strip())
        check("the schema enum equals the resolver's set", schema_modes == observe.MODES,
              f"schema={schema_modes} resolver={observe.MODES}")

    cli = read(os.path.join("cmd", "agentctl", "main.go"))
    flag = re.search(r'fs\.String\("observe",\s*"",\s*"([^"]+)"', cli)
    check("the CLI offers the same choice under the same name", flag is not None)
    if flag:
        for mode in observe.MODES:
            check(f"...and its help names {mode}", mode in flag.group(1), flag.group(1))

    # The hub renders the form from the schema, so the COST strings have to be IN the schema rather
    # than hard-coded in the page — otherwise they are a second copy, and second copies drift.
    check("the schema carries the cost of each mode, so the hub need not hard-code it",
          '"cost"' in go[go.index('"observe"'):go.index('"observe"') + 2500], "no cost map in the schema")


def test_the_refusal_is_taken_before_the_run_starts():
    """A refusal that happens mid-run has already cost the browser, the model and the person's time.

    Asserted on the SHIPPED wiring: the resolver is called before make_executor, and its Refusal
    becomes exit 3 — the same shape as force_replay-in-ci and the secret-would-leak guard."""
    src = read(os.path.join("brain", "__main__.py"))
    idx_resolve = src.find("_obs_from_env(")
    # Anchored on the RUN dispatch, not on the first `make_executor(` in the file: the chat branch
    # spawns one hundreds of lines earlier, and anchoring there made this check compare the wrong two
    # positions and fail on correct code. The anchor is the line that begins a run.
    idx_spawn = src.find('log("run.config"')
    check("the observation plan is resolved BEFORE the run is dispatched",
          0 < idx_resolve < idx_spawn, f"resolve@{idx_resolve} dispatch@{idx_spawn}")
    seg = src[max(idx_resolve - 400, 0):idx_spawn]
    check("...and a Refusal becomes exit 3, like the other pre-flight contradictions",
          "return 3" in seg and "fatal.observe_refused" in seg, seg[-300:])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    if FAILS:
        print(f"\nFAIL — {len(FAILS)} check(s): " + "; ".join(FAILS[:6]))
        sys.exit(1)
    print(f"\nALL PASS ({len(fns)} observation-mode tests)")
