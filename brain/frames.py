"""Sentinel — one frame of the run, as a FILE in the run's artifact dir.

WHY THIS FILE EXISTS AT ALL. The function below lived in `brain/graph.py` from ADR-108d, and that
made it explore's private property: `brain/replay.py` had no way to take a picture, so a replay could
fail a step and leave nothing to look at but a stack trace. PROD-FAIL-MEDIA part A needs exactly this
picture at exactly that moment, and the honest fix was to move the one implementation rather than
write a second — two functions producing `frames/frame-NNNN.png` under slightly different rules is how
the artifact route and the hub end up disagreeing about what a frame is.

⚠ THE NAME IS A PUBLISHED CONTRACT, not a local choice. `frames/frame-NNNN.png` is matched by
`frameNamePattern` in `cmd/control-api/main.go` and fetched by `lvShowFrame` in `docs/index.html`.
The failure frame deliberately reuses it — a new name (`fail-*.png`) would have meant widening the
artifact whitelist for a picture the existing reader already knows how to show.
"""
import os

from .eventlog import log


def capture_frame(ex, artifact_dir, step_id) -> str:
    """ADR-108d: take one frame into `artifact_dir/frames/`. Returns its name, or "" if none was taken.

    The name — not the bytes — is what travels: AG-UI envelopes are stdout lines, and a base64 PNG in
    one would bloat the run log past reading and break the very stream the UI follows. The hub fetches
    the file through the artifact route that already exists.

    Best-effort by construction. A frame is an OBSERVATION of the run, so failing to take one must
    never fail the run — but it is not silent either: the failure is logged, because "the live view
    stopped updating" with no reason is the shape of defect ADR-108d exists to remove.

    ⚠ `SENTINEL_LIVE_FRAMES` GATES THIS, INCLUDING THE FAILURE FRAME, and that is deliberate.
    `observe=off` means "nothing is captured, by request" — quietly making an exception for failures
    would mean a person who asked for no pictures gets one of their application's error state anyway,
    which is precisely the promise the observation resolver exists to keep.
    """
    frames_on = os.environ.get("SENTINEL_LIVE_FRAMES", "1") != "0"
    art = artifact_dir or ""
    if not frames_on or not art:
        return ""
    name = f"frame-{int(step_id or 0):04d}.png"
    try:
        os.makedirs(os.path.join(art, "frames"), exist_ok=True)
        ex.call("browser.frame", path=os.path.join(art, "frames", name))
        return name
    except Exception as e:
        log("live.frame_failed", step=step_id, error=e)
        return ""
