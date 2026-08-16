/**
 * LIVE-RECORD (ADR-125) — the video artifact of a run.
 *
 * WHAT THIS IS NOT. `cdp-service.ts` also says "video", and it means something else entirely: the
 * live screencast, a stream of the latest frame held in MEMORY and never written to disk (ADR-108d/
 * ADR-111). This file is the other thing — a FILE, produced by Playwright's `recordVideo`, that
 * exists after the run is over. They are orthogonal by design, which is why `record` in
 * `brain/observe.py` is not "stream plus a file": asking for one does not start the other.
 *
 * ⚠ THE DECISION IS TAKEN AT THE WRONG END OF THE RUN, and that is the property that shapes
 * everything here. `recordVideo` is a `newContext` option, so whether to record is settled BEFORE the
 * first step — when nobody knows yet whether the run will fail. The trace has the opposite shape:
 * ADR-084 buffers it and decides at the END, so a green run's bytes never reach the disk at all. We
 * cannot have that here. The video is written either way and the choice we DO have is only whether to
 * keep the file, which is a deletion after the fact, not an avoided write. Saying so is the honest
 * half: on a green `record` run there IS a window in which the video sits on disk.
 *
 * ⚠ AND IT CANNOT BE DONE AT ALL OVER CDP. A context we adopted was created by somebody else, so
 * there is no `newContext` of ours to attach the option to. `slowMo` has the same shape of problem
 * and is DEGRADED — launch.ts names it (`slowMoUnavailable`) and the executor pays the pacing with
 * its own per-step pause. Video has nothing to pay with, so it is REFUSED, in `brain/observe.py`
 * before the run starts. What lives here is the second guard: if the environment reaches this process
 * by some other route, the plan still carries `videoUnavailable` and the executor says so out loud
 * rather than finishing quietly without the file that was the entire point.
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

/** The one environment switch this module reads (`brain/observe.py::apply` is its one writer). */
export const RECORD_ENV = 'SENTINEL_RECORD';

/**
 * Only the literal "1" turns recording on — the same rule as `SENTINEL_DECORATE` and the two frame
 * switches written beside it. A truthy-looking value must not mean one thing to the brain and
 * another here, which is exactly how the four original observation switches came apart.
 */
export function recordEnabled(env: NodeJS.ProcessEnv): boolean {
  return env[RECORD_ENV] === '1';
}

/**
 * Where Playwright drops the raw videos while the context is alive.
 *
 * A TEMPORARY directory of our own, deliberately, rather than the run's artifact directory. Two
 * reasons, both measured rather than aesthetic:
 *
 *   1. Playwright names the files itself (a random hash per page) and offers no way to choose. The
 *      artifact directory is a PUBLISHED surface — `artifactWhitelist` in cmd/control-api and
 *      `ART_NAMES` in the hub both enumerate exact names — so letting unnamed files land there would
 *      mean either widening the whitelist to a wildcard or shipping files nothing can fetch.
 *   2. A run with popups produces SEVERAL videos. Only the main page's recording is the artifact; the
 *      rest are dropped with the directory instead of accumulating beside the plan.
 *
 * The final name (`video.webm`) is settled by `video.saveAs()` at the end, which is also the only
 * moment Playwright guarantees the bytes are complete.
 */
export function makeVideoDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-video-'));
}

/**
 * Remove the scratch directory and everything Playwright left in it.
 *
 * Best-effort on purpose: this runs during teardown, after the run's verdict is already decided, and
 * a failure to clean up scratch space must never turn a finished run into a crash. It is not silent
 * either — the caller logs what happened, because a temp directory that quietly accumulates videos of
 * somebody's application is a disclosure problem wearing a housekeeping costume.
 */
export function dropVideoDir(dir: string | null): void {
  if (!dir) return;
  fs.rmSync(dir, { recursive: true, force: true });
}
