"""Offline gates for ADR-108d — the live view's frames.

Run:  .venv/bin/python tests/test_live_view_offline.py

The design decision these pin: a frame is a FILE, and what travels in the event is its NAME.

AG-UI envelopes are stdout lines (`@@AGUI {...}`). A base64 PNG in one would bloat the run log past
reading and break the very stream the UI follows to watch a run — so the frame goes to the run's
artifact directory and the hub fetches it through the artifact route that already exists.

And a frame is an OBSERVATION of a run, never a part of it: failing to take one must not fail the
run — but it must not be silent either, which is why the failure is a catalogued degradation
(`live.frame_failed`) whose verdict sentence says the live view was incomplete.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.frames import capture_frame  # noqa: E402  (moved out of graph.py — PROD-FAIL-MEDIA A)


class Ex:
    """Stands in for the Playwright executor; records what it was asked for."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def call(self, method, **kw):
        self.calls.append((method, kw))
        if self.fail:
            raise RuntimeError("executor gone")
        pathlib.Path(kw["path"]).write_bytes(b"PNG")
        return {"path": kw["path"]}


def test_frame_is_a_file_and_the_event_carries_its_name():
    art = tempfile.mkdtemp(); ex = Ex()
    name = capture_frame(ex, art, 7)
    assert name == "frame-0007.png", name
    assert os.path.exists(os.path.join(art, "frames", name)), "the frame was not written"
    # The bytes must NOT be what travels: the tool is asked for a PATH.
    assert ex.calls[0][0] == "browser.frame" and "path" in ex.calls[0][1], ex.calls

def test_a_failed_frame_never_fails_the_run():
    art = tempfile.mkdtemp()
    assert capture_frame(Ex(fail=True), art, 3) == "", "a failure must yield no name"

def test_frames_can_be_switched_off():
    art = tempfile.mkdtemp(); ex = Ex()
    os.environ["SENTINEL_LIVE_FRAMES"] = "0"
    try:
        assert capture_frame(ex, art, 1) == ""
        assert not ex.calls, "it asked for a frame that was switched off"
    finally:
        os.environ.pop("SENTINEL_LIVE_FRAMES", None)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
