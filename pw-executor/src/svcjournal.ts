/**
 * The SERVICE journal, written from the browser service (HEALTH-005 PR-C, ADR-116).
 *
 * WHY A THIRD WRITER. The journal answers "what did the tool itself do" — sign-ins, configuration
 * changes, services starting and stopping — and until now the browser service was absent from it
 * entirely. It logged with `console.error`, which means the container's stdio: not a file, not
 * catalogued, not filterable, not in the UI, and destroyed by `docker compose down`. A service that
 * is part of the default stack and leaves no trace in the deployment's own record is a hole in
 * exactly the thing the record exists for.
 *
 * SAME FILE, NOT A FOURTH ONE. `state/logs/service.jsonl`, the same one control-api and agentctl
 * write, distinguished by `svc`. Four files would mean answering "what happened at 14:32" by merging
 * them by hand — the reason ADR-116 chose one file in the first place.
 *
 * THREE PROPERTIES THIS DELIBERATELY DOES NOT HAVE, each for a stated reason:
 *
 *   No rotation. Two rotators racing on one file is worse than one: whoever renames second discards
 *   the generation the first had just created. The Go writer sizes the file by SEEKING it rather
 *   than by a counter (svclog.Log, PR-B), so it rotates on the real size no matter who wrote the
 *   bytes — and this service writes about two records per lifetime, so it contributes almost none.
 *
 *   No lock. `flock` needs a native module and this project takes no new runtime dependencies for
 *   it. The consequence is real and is NOT hidden: a record appended here while `agentctl
 *   purge-service` rewrites the file could be lost. That window is what the purge's carry-over pass
 *   exists to close (internal/svclog/purge.go), and the residual gap is declared in
 *   docs/OBSERVABILITY.md rather than implied away.
 *
 *   No throwing. A service must not fail to start over its own log file — the same rule that makes
 *   `svclog.Open` return nil instead of an error. Every failure here is swallowed after one line on
 *   stderr, because a logging problem must not become an outage.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

/** One line of the journal. The field names are the wire format — `svclog.Record`'s JSON tags. */
export interface JournalRecord {
  seq: number;
  ts: string;
  lvl: string;
  cat: string;
  code: string;
  msg: string;
  svc: string;
}

/** Where the journal lives. The compose services mount ./state at /app/state. */
export const stateDir = (): string => process.env.SENTINEL_STATE_DIR ?? '/app/state';

let seq = 0;
let reported = false;

/**
 * Which supervisor brought this process up.
 *
 * ⚠ This mirrors `svclog.Supervisor()` (Go) and is therefore a SECOND implementation of one rule —
 * the shape this milestone spent a PR removing elsewhere. It is here because the two live on
 * opposite sides of a language boundary with no shared runtime, and the alternative is worse: either
 * omit the field the catalogue's template names, or fill it with a guess. Kept to the same signals
 * as the Go original, deliberately in the same order, so a reader comparing them can see they agree.
 * Recorded in the backlog under [DEBT-GO-EVENTLOG].
 */
export function supervisor(): string {
  if (process.env.INVOCATION_ID) return 'systemd';
  try {
    if (fs.existsSync('/.dockerenv')) return 'container';
  } catch { /* an unreadable filesystem is not a reason to guess */ }
  return 'manual';
}

/**
 * The `service.started` sentence, built to match its catalogue template exactly.
 *
 * Exported, and built here rather than at the call site, for the reason PR-B measured the hard way:
 * the hub renders a record in the reader's language by matching the catalogue's English template
 * against the message the service actually sent, so a message that drifts from its template falls
 * back to raw English — silently, one row at a time. Six Go codes were doing that. A message
 * assembled inline cannot be reached by a test; this one can, and tests/test_browser_journal_offline.py
 * runs THIS function and matches its output against brain/events.json.
 */
export function startedMsg(version: string, sup: string, pid: number, detail: string): string {
  return `Service browser started: version ${version}, brought up by ${sup}, pid ${pid}${detail}`;
}

/** The `service.stopped` sentence, same contract as startedMsg. */
export function stoppedMsg(reason: string): string {
  return `Service browser stopped: ${reason}`;
}

/** Append one record. Never throws; never blocks on anything but the write itself. */
export function journal(code: string, lvl: string, msg: string): void {
  const dir = path.join(stateDir(), 'logs');
  const line = JSON.stringify({
    seq: ++seq,
    ts: new Date().toISOString(),
    lvl,
    cat: 'service',
    code,
    msg,
    // LIVE-VNC: TWO services now run this same binary — `browser` (headless, the default stack) and
    // `browser-vnc` (headed, behind the `vnc` profile) — and they append to the SAME journal file.
    // Without this they would both write svc:"browser", and the field that exists to answer "which
    // service said this" would stop answering it. The default is unchanged, so the single-service
    // deployment and every existing test see exactly what they saw before.
    svc: process.env.SENTINEL_SVC_NAME || 'browser',
  } satisfies JournalRecord) + '\n';
  try {
    // 0750/0640 match what the Go writer creates, so the file's permissions do not depend on which
    // service happened to create it first.
    fs.mkdirSync(dir, { recursive: true, mode: 0o750 });
    // ONE write of a complete line: below PIPE_BUF, so it cannot interleave with another appender's
    // line and produce a torn record. Synchronous on purpose — the stop record is written while the
    // process is on its way out, and an async write would not survive the exit.
    fs.appendFileSync(path.join(dir, 'service.jsonl'), line, { mode: 0o640 });
  } catch (e) {
    if (!reported) {
      reported = true;
      process.stderr.write(
        `[cdp-service] service journal write failed: ${(e as Error).message} (further failures are silent)\n`,
      );
    }
  }
}
