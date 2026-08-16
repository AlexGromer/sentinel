#!/usr/bin/env python3
"""PROD-FAIL-MEDIA part A (ADR-125) — the picture of a step that FAILED.

Run:  .venv/bin/python tests/test_fail_media_frame_offline.py

THE DEFECT THIS PINS. A replay could fail a step and leave nothing to look at but a stack trace. The
machinery to take a picture already existed — `capture_frame`, ADR-108d — but it lived inside
`brain/graph.py`, which made it explore's private property; the replay path had no way to reach it.
So the fix was a MOVE (`brain/frames.py`), not a second implementation: two functions writing
`frames/frame-NNNN.png` under slightly different rules is how the artifact route and the hub end up
disagreeing about what a frame is.

WHAT IT ASSERTS, each against a way this could rot back:

 1. A failed step carries `frame`, and the FILE the name points at exists. A name with no file behind
    it is worse than no name: the hub draws a control that 404s.
 2. A step that PASSED carries no frame. "Media at the failure" turning into a frame per step would
    change what every existing replay costs and produce hundreds of pictures of things that went right.
 3. The capture honours `SENTINEL_LIVE_FRAMES=0`. `observe=off` means "nothing is captured, by
    request", and quietly making an exception for failures would hand a person who asked for no
    pictures one of their application's error state.
 4. A capture that FAILS does not fail the run. The picture is an observation of the run, never a
    condition of it — and the step's own verdict must be untouched by whether a PNG could be written.
 5. The name matches the published pattern. `frames/frame-NNNN.png` is enforced by
    `frameNamePattern` in cmd/control-api and read by `lvShowFrame` in the hub; a frame named anything
    else is a file the product cannot serve.

Offline, stdlib only — no browser, no network. The executor is a fake that writes the bytes it is
asked to write, which is the whole of what `browser.frame` does.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.replay import run_replay                         # noqa: E402
from brain.state import canonical_plan_hash                 # noqa: E402

PAGE = "file:///s/app.html"
GOOD = {"testid": "pay"}
GONE = {"testid": "vanished"}

# The pattern cmd/control-api/main.go enforces (`frameNamePattern`). Written out here rather than
# imported, because the point is that the two agree while living in different languages.
FRAME_NAME = re.compile(r"^frame-[0-9]{4}\.png$")

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:400])


class Ex:
    """A fake executor. `frame_raises` models a capture that cannot be taken (disk full, dead page)."""

    def __init__(self, frame_raises=False):
        self.url, self.frame_raises, self.frames = PAGE, frame_raises, []

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", self.url); return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- page app", "nodeCount": 2}
        if m in ("browser.interactives", "browser.links"):
            return {"elements": [], "links": []}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        if m == "browser.probe":
            return {"count": 1 if p.get("locator") == GOOD else 0}
        if m == "browser.frame":
            if self.frame_raises:
                raise RuntimeError("no screen")
            self.frames.append(p["path"])
            with open(p["path"], "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n")
            return {"path": p["path"]}
        if m == "browser.click":
            if p.get("locator") == GOOD:
                return {"ok": True}
            raise RuntimeError("locator not found")
        if m == "browser.appFaults":
            return {}
        return {}


class Store:
    def record_step(self, *a, **k): return False
    def get_golden(self, *a, **k): return None
    def save_golden(self, *a, **k): return None
    def audit(self, *a, **k): return None


class Heal:
    """Heals nothing. The point of this file is the step that STAYS broken.

    Answers the real shape rather than `None`: the healer's contract is a dict, and a fake that
    returns nothing would make the run crash on the healing path instead of failing the step — a
    different event, and not the one being measured here.
    """

    def heal(self, _ctx):
        return {"locator": None, "strategy": None, "confidence": 0.0, "outcome": "failed"}


def _plan():
    """One step that works, one that cannot: the report must distinguish them by more than an outcome."""
    steps = [
        {"step_id": 1, "action_type": "click", "intent": "click button 'Pay'",
         "semantic_id": "sid-pay", "is_milestone": False, "locator": GOOD, "alternatives": []},
        {"step_id": 2, "action_type": "click", "intent": "click button 'Gone'",
         "semantic_id": "sid-gone", "is_milestone": False, "locator": GONE, "alternatives": []},
    ]
    return {"plan_id": "p1", "steps": steps, "plan_hash": canonical_plan_hash(steps),
            "target_url": PAGE}


def _run(ex, env=None):
    run_dir = tempfile.mkdtemp(prefix="sentinel-failmedia-")
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k); os.environ[k] = v
    try:
        return run_replay(ex, Store(), Heal(), _plan(), PAGE, run_dir, run_id="fm"), run_dir
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _steps(report):
    return {s.get("step_id"): s for s in report.get("steps", [])}


def test_a_failed_step_carries_a_frame_and_the_file_is_really_there():
    ex = Ex()
    report, run_dir = _run(ex)
    st = _steps(report)
    check("the fixture actually produced a failure — otherwise every check below is vacuous",
          st.get(2, {}).get("outcome") == "failed", st.get(2))
    frame = st.get(2, {}).get("frame")
    check("the failed step names a frame", bool(frame), st.get(2))
    if frame:
        check("...and it matches the pattern cmd/control-api will serve",
              FRAME_NAME.match(frame) is not None,
              f"{frame!r} — a frame the artifact route refuses is a control the hub draws and cannot open")
        check("...and the FILE exists, not merely the name",
              os.path.exists(os.path.join(run_dir, "frames", frame)),
              f"{frame} is named in the report but absent from {run_dir}/frames")


def test_a_passing_step_is_not_photographed():
    ex = Ex()
    report, _ = _run(ex)
    st = _steps(report)
    check("the fixture actually produced a pass", st.get(1, {}).get("outcome") != "failed", st.get(1))
    check("a step that passed carries no frame — this is media AT THE FAILURE, not per step",
          "frame" not in st.get(1, {}),
          f"{st.get(1)} — a frame per step changes what every existing replay costs")
    check("...and exactly one capture was requested for a plan with one failure",
          len(ex.frames) == 1, ex.frames)


def test_observation_off_is_honoured_even_for_a_failure():
    ex = Ex()
    report, _ = _run(ex, env={"SENTINEL_LIVE_FRAMES": "0"})
    st = _steps(report)
    check("the failure still happened", st.get(2, {}).get("outcome") == "failed", st.get(2))
    check("...and NO frame was taken: `observe=off` means nothing is captured, without exceptions",
          "frame" not in st.get(2, {}) and not ex.frames,
          f"frames={ex.frames} step={st.get(2)} — a person who asked for no pictures got one of "
          "their application's error state")


def test_a_capture_that_fails_does_not_fail_the_run():
    ex = Ex(frame_raises=True)
    report, _ = _run(ex)
    st = _steps(report)
    check("the run still produced a report", bool(report.get("steps")), report)
    check("the step's own verdict is untouched by the camera",
          st.get(2, {}).get("outcome") == "failed", st.get(2))
    check("...and no frame is claimed when none was taken — a name with no file is worse than silence",
          "frame" not in st.get(2, {}), st.get(2))
    check("the passing step is still a pass", st.get(1, {}).get("outcome") != "failed", st.get(1))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        print(fn.__name__)
        fn()
    if FAILS:
        print(f"\nFAIL — {len(FAILS)} check(s): " + "; ".join(FAILS[:6]))
        sys.exit(1)
    print(f"\nALL PASS ({len(fns)} fail-media tests)")
