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
 * The name THIS process answers to in the deployment's record — the one place it is decided.
 *
 * WHY IT IS A FUNCTION AND NOT A LITERAL AT EACH USE. Two services run this same binary: `browser`
 * (headless, the default stack) and `browser-vnc` (headed, behind the `vnc` profile, ADR-127). They
 * append to the SAME journal file, so this value is the only thing that tells their lines apart.
 * Until now it was read here for the `svc` FIELD and written as the literal `browser` in the message
 * TEXT — two statements about one fact, and they disagreed the first time anybody measured them on a
 * live stack: `{"code":"service.started","msg":"Service browser started: version dev, …",
 * "svc":"browser-vnc"}` (docker, 2026-08-18). The field was right and the sentence was wrong, which
 * is the worse way round: the hub's "Service journal" view shows people the TEXT, so two services
 * read as one and the difference survives only in the machine representation.
 *
 * ⚠ WHY NO GATE SAW IT, and what that demands of the new ones. The catalogue template is
 * `Service {svc} started: …` and `{svc}` accepts ANY value, so matching a message against its
 * template — the check that exists precisely for this class — matched the WRONG name perfectly.
 * A test that only renders this service's own default name is vacuous for the same reason: the
 * literal and the correct answer are the same string. Hence svcjournal.test.ts drives a name this
 * process is NOT running under, and asserts the sentence and the `svc` field agree.
 */
export function svcName(): string {
  return process.env.SENTINEL_SVC_NAME || 'browser';
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
 *
 * ⚠ `svc` is LAST and defaults to svcName() on purpose. The shipping call site passes nothing, so the
 * name in the sentence and the name in the record's `svc` field come from ONE function call apiece to
 * ONE function — they cannot drift without changing the line they both read, which is the whole point
 * of the parameter. It is a parameter at all, rather than an unconditional svcName() inside, because
 * a test that cannot say a name other than this process's own cannot tell a derived name from the
 * hardcoded one it replaced.
 */
export function startedMsg(version: string, sup: string, pid: number, detail: string,
                           svc: string = svcName()): string {
  return `Service ${svc} started: version ${version}, brought up by ${sup}, pid ${pid}${detail}`;
}

/** The `service.stopped` sentence, same contract as startedMsg — including where `svc` comes from. */
export function stoppedMsg(reason: string, svc: string = svcName()): string {
  return `Service ${svc} stopped: ${reason}`;
}

/**
 * The `service.live_claim_conflict` sentence — two runs declaring ONE page (ADR-128).
 *
 * WHY A RARE EVENT GETS A CODE RATHER THAN A LOG LINE. Until ADR-128 this was the topology: in
 * CDP-attach mode every run adopted `pages()[0]`, so a second concurrent run announced the first
 * one's target by construction (measured live: both `84DC6185`). A run now opens its own page, which
 * makes the collision UNREACHABLE in a deployment whose parts agree — and that is exactly what turns
 * it from noise into a signal. If it fires now, the executor driving this service is older than
 * ADR-128, and the symptom a person will actually meet is "the live view refuses both of my runs"
 * with the cause on stderr of a container nobody is tailing. A code puts it in the deployment's own
 * record, where the hub, the CLI and the API can all read it.
 */
export function claimConflictMsg(runs: string, target: string): string {
  return `Runs ${runs} claimed one browser page ${target} — the live view cannot say which of them it shows`;
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
    // LIVE-VNC: TWO services run this same binary and append to the SAME file, so this field is what
    // answers "which service said this". It reads svcName() rather than the environment directly
    // because the message builders above read the same function: the field and the sentence are one
    // fact, and reading the variable twice is how they came to disagree (see svcName).
    svc: svcName(),
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
