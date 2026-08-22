import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { journal, startedMsg, stoppedMsg, claimConflictMsg, svcName } from './svcjournal.js';

/**
 * [JOURNAL-SVC-NAME-HARDCODED] — the sentence and the `svc` field must name the SAME service.
 *
 * WHAT SHIPPED AND WHY NOTHING WENT RED. `startedMsg`/`stoppedMsg` returned the literal
 * `Service browser started: …` while the record carried `svc: "browser-vnc"`; measured on a live
 * stack (docker, 2026-08-18):
 *   {"code":"service.started","msg":"Service browser started: version dev, …","svc":"browser-vnc"}
 * The gate built for exactly this class — match the message against its catalogue template — was
 * green over it, because the template is `Service {svc} started: …` and `{svc}` accepts ANY value.
 * A message naming the wrong service matches as well as one naming the right service.
 *
 * ⚠ THE RULE THIS FILE IS BUILT ON: every assertion below drives a service name this process is NOT
 * running under (`browser-vnc`, `headed-probe`). Asserting on the default name would be vacuous —
 * the removed literal and the correct answer are the same string `browser`, so the old code would
 * pass a test written that way, which is precisely how the defect reached a live stack.
 */

/** The group the hub's template matcher captures for `{svc}`, or null when the shape drifted. */
const startedSvc = (msg: string): string | null =>
  /^Service (.*?) started: version /.exec(msg)?.[1] ?? null;

const stoppedSvc = (msg: string): string | null =>
  /^Service (.*?) stopped: /.exec(msg)?.[1] ?? null;

/** Run `fn` with env vars set, then restore — a leaked var would silently rename another test's service. */
function withEnv(vars: Record<string, string | undefined>, fn: () => void): void {
  const saved = new Map<string, string | undefined>();
  for (const [k, v] of Object.entries(vars)) {
    saved.set(k, process.env[k]);
    if (v === undefined) delete process.env[k]; else process.env[k] = v;
  }
  try { fn(); } finally {
    for (const [k, v] of saved) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  }
}

// --- the sentence names the service it was given ----------------------------------------------

test('startedMsg names the service it was handed, not a literal', () => {
  const msg = startedMsg('v9.9.9', 'container', 4242, ' — CDP 0.0.0.0:9223', 'browser-vnc');
  assert.equal(startedSvc(msg), 'browser-vnc');
  // Said twice on purpose: the extraction above would also survive a message that merely CONTAINS
  // the name somewhere, and this one dies on the exact string the defect emitted for a year.
  assert.ok(!msg.includes('Service browser started'), `still emits the literal: ${msg}`);
});

test('stoppedMsg names the service it was handed, not a literal', () => {
  const msg = stoppedMsg('signal SIGTERM', 'browser-vnc');
  assert.equal(stoppedSvc(msg), 'browser-vnc');
  assert.ok(!msg.includes('Service browser stopped'), `still emits the literal: ${msg}`);
});

test('a name with nothing in common with `browser` comes through whole', () => {
  // `browser-vnc` alone would let a half-fix pass — one that appends a suffix rather than
  // interpolating the value. A name that shares no prefix with the old literal cannot be faked.
  assert.equal(startedSvc(startedMsg('dev', 'manual', 1, '', 'headed-probe')), 'headed-probe');
  assert.equal(stoppedSvc(stoppedMsg('signal SIGINT', 'headed-probe')), 'headed-probe');
});

// --- the default is the SAME source the `svc` field reads --------------------------------------

test('svcName is the environment, defaulting to the single-service deployment', () => {
  withEnv({ SENTINEL_SVC_NAME: 'browser-vnc' }, () => assert.equal(svcName(), 'browser-vnc'));
  withEnv({ SENTINEL_SVC_NAME: undefined }, () => assert.equal(svcName(), 'browser'));
  // Empty is treated as unset: an env var declared but not filled in compose must not produce a
  // service whose name is the empty string in every row of the journal.
  withEnv({ SENTINEL_SVC_NAME: '' }, () => assert.equal(svcName(), 'browser'));
});

test('the call site passes no name and still gets this process\'s own', () => {
  // cdp-service.ts calls these with four arguments and one argument respectively. That path is the
  // one that shipped wrong, so it is asserted directly rather than inferred from the parameter.
  withEnv({ SENTINEL_SVC_NAME: 'browser-vnc' }, () => {
    assert.equal(startedSvc(startedMsg('dev', 'container', 7, ' — CDP')), 'browser-vnc');
    assert.equal(stoppedSvc(stoppedMsg('signal SIGTERM')), 'browser-vnc');
  });
});

// --- the property the live stack broke: TEXT and FIELD agree on disk ---------------------------

test('on disk, the message and the `svc` field name one service', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'svcjournal-agree-'));
  try {
    withEnv({ SENTINEL_STATE_DIR: dir, SENTINEL_SVC_NAME: 'browser-vnc' }, () => {
      journal('service.started', 'info', startedMsg('dev', 'container', 4242, ' — CDP'));
      journal('service.stopped', 'info', stoppedMsg('signal SIGTERM'));
    });
    const lines = fs.readFileSync(path.join(dir, 'logs', 'service.jsonl'), 'utf-8')
      .split('\n').filter((l) => l.trim());
    assert.equal(lines.length, 2);
    const [started, stopped] = lines.map((l) => JSON.parse(l) as { msg: string; svc: string });

    // This is the assertion the defect fails. Both halves come from the record itself, so it holds
    // for ANY service name — it cannot be satisfied by hardcoding either side.
    assert.equal(started.svc, 'browser-vnc');
    assert.equal(startedSvc(started.msg), started.svc,
      `the journal says svc=${started.svc} and the text a reader sees says ` +
      `${startedSvc(started.msg)}: ${started.msg}`);
    assert.equal(stoppedSvc(stopped.msg), stopped.svc,
      `the journal says svc=${stopped.svc} and the text a reader sees says ` +
      `${stoppedSvc(stopped.msg)}: ${stopped.msg}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the default deployment is unchanged — `browser` in both halves', () => {
  // The fix must not rename the single-service stack: every golden, every doc example and the
  // offline gate's `svc == "browser"` assertion depend on this staying exactly as it was.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'svcjournal-default-'));
  try {
    withEnv({ SENTINEL_STATE_DIR: dir, SENTINEL_SVC_NAME: undefined }, () => {
      journal('service.started', 'info', startedMsg('dev', 'manual', 1, ''));
    });
    const rec = JSON.parse(fs.readFileSync(path.join(dir, 'logs', 'service.jsonl'), 'utf-8')
      .split('\n')[0]) as { msg: string; svc: string };
    assert.equal(rec.svc, 'browser');
    assert.equal(startedSvc(rec.msg), 'browser');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// --- the word that must NOT be substituted -----------------------------------------------------

test('`browser page` in the claim-conflict sentence is a noun, not a service name', () => {
  // A guard against the over-eager version of this fix. The catalogue template for
  // service.live_claim_conflict reads "…claimed one browser page {target}…" — the word is English
  // prose about what a page is, and interpolating the service name there would break the template
  // match and drop the row back to raw English for a Russian reader.
  withEnv({ SENTINEL_SVC_NAME: 'browser-vnc' }, () => {
    const msg = claimConflictMsg('run-alpha, run-beta', '84DC6185CAFEBABE');
    assert.ok(msg.includes('claimed one browser page 84DC6185CAFEBABE'), msg);
    assert.ok(!msg.includes('browser-vnc'), `the service name leaked into a noun: ${msg}`);
  });
});
