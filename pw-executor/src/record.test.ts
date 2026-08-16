/**
 * LIVE-RECORD (ADR-125) — the video artifact, gated on the FILE, not on the call.
 *
 * WHY THIS DRIVES A BROWSER. The claim is that a playable video of the run exists on disk under the
 * name we promised, and every cheaper form of that claim asserts something else instead: that
 * `recordVideo` appears in the source, that `saveAs` was called, that a boolean is true. This
 * repository has already paid for that lesson twice — a mutation making `traceStop` write regardless
 * of its argument broke nothing, because the assertion was about our code rather than about the file.
 * So this speaks to the shipped executor over its own JSON-RPC transport, exactly as the brain does,
 * and then looks at the filesystem.
 *
 * Each behavioural check names the mutation it exists to kill. All of them were applied, compiled,
 * seen to turn this file red, and reverted.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';
import { resolveLaunchPlan } from './launch.js';
import { recordEnabled, RECORD_ENV, makeVideoDir, dropVideoDir } from './record.js';

const SERVER = path.join(__dirname, 'server.js');
const REPO = path.resolve(__dirname, '..', '..');
const FIXTURE = pathToFileURL(path.join(REPO, 'testdata', 'fixtures', 'l11-decorate.html')).href;

/** The same newline-delimited JSON-RPC client brain/executor.py speaks. */
class Exec {
  private proc: ChildProcess;
  private buf = '';
  private waiting = new Map<number, { ok: (v: unknown) => void; bad: (e: Error) => void }>();
  private nextId = 1;
  stderr = '';

  constructor(env: Record<string, string>) {
    this.proc = spawn(process.execPath, [SERVER], {
      env: { ...process.env, ...env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.proc.stdout!.setEncoding('utf8');
    this.proc.stdout!.on('data', (chunk: string) => {
      this.buf += chunk;
      for (let i = this.buf.indexOf('\n'); i >= 0; i = this.buf.indexOf('\n')) {
        const line = this.buf.slice(0, i).trim();
        this.buf = this.buf.slice(i + 1);
        if (!line) continue;
        const msg = JSON.parse(line) as { id: number; result?: unknown; error?: { message: string } };
        const w = this.waiting.get(msg.id);
        if (!w) continue;
        this.waiting.delete(msg.id);
        if (msg.error) w.bad(new Error(msg.error.message));
        else w.ok(msg.result);
      }
    });
    this.proc.stderr!.setEncoding('utf8');
    this.proc.stderr!.on('data', (chunk: string) => { this.stderr += chunk; });
  }

  call<T = Record<string, unknown>>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.waiting.set(id, { ok: (v) => resolve(v as T), bad: reject });
      this.proc.stdin!.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  async close(): Promise<void> {
    try { await this.call('shutdown'); } catch { /* already down */ }
    await new Promise<void>((done) => {
      const t = setTimeout(done, 5000);
      t.unref();
      this.proc.once('exit', () => { clearTimeout(t); done(); });
    });
    this.proc.kill('SIGKILL');
  }
}

/* --------------------------------------------------------------------------------------------
 * 1. The switch, and the impossibility that has no substitute.
 * ------------------------------------------------------------------------------------------ */

test('SENTINEL_RECORD is read in exactly one shape, and the launch plan carries the recording', () => {
  assert.equal(recordEnabled({}), false);
  assert.equal(recordEnabled({ [RECORD_ENV]: '0' }), false);
  // Only "1" turns it on — the same rule as the decoration and frame switches written beside it, so a
  // truthy-looking value cannot mean one thing to the brain and another here.
  assert.equal(recordEnabled({ [RECORD_ENV]: 'true' }), false);
  assert.equal(recordEnabled({ [RECORD_ENV]: '1' }), true);

  const plain = resolveLaunchPlan({});
  assert.equal(plain.video, false, 'an ordinary run must launch exactly as it did before ADR-125');
  assert.equal(plain.videoUnavailable, false);

  const rec = resolveLaunchPlan({ [RECORD_ENV]: '1' });
  assert.equal(rec.kind, 'launch');
  assert.equal(rec.video, true, 'a run told to record must carry the decision into context creation');
  assert.equal(rec.videoUnavailable, false, 'nothing is unavailable on a browser we launch ourselves');
});

test('an adopted browser cannot be recorded — and unlike slowMo there is nothing to pay instead', () => {
  const cdp = resolveLaunchPlan({ [RECORD_ENV]: '1', PW_CDP_ENDPOINT: 'http://127.0.0.1:9222' });
  assert.equal(cdp.kind, 'cdp');
  assert.equal(cdp.video, false,
    'recordVideo is a newContext option and we did not create this context — claiming video:true here ' +
    'would be the plan lying about an option it never set');
  assert.equal(cdp.videoUnavailable, true, 'the impossibility has to be NAMED, not silently absent');

  // ⚠ THE ASYMMETRY WITH slowMo IS THE POINT, and it is asserted rather than described. A decorated
  // CDP run is SLOWED anyway, by our own per-step pause — the plan degrades. A recording CDP run gets
  // nothing, because there is no substitute for a file. If a later change ever "helpfully" makes this
  // path degrade quietly, this is the line that goes red.
  const decorCdp = resolveLaunchPlan({ SENTINEL_DECORATE: '1', PW_CDP_ENDPOINT: 'http://127.0.0.1:9222' });
  assert.ok(decorCdp.stepPauseMs > 0, 'decoration under CDP compensates; the comparison below is vacuous otherwise');
  assert.equal(cdp.video, false, 'and recording under CDP does not compensate — it does not happen');
});

test('the scratch dir is ours, is outside the artifact dir, and is removable', () => {
  const dir = makeVideoDir();
  try {
    assert.ok(fs.existsSync(dir), 'no scratch dir was created');
    // ⚠ Not the run's artifact dir. Playwright names video files itself, and the artifact route in
    // cmd/control-api serves an enumerated whitelist — letting unnamed files land beside plan.json
    // would mean either a wildcard on that route or files nothing can fetch.
    assert.ok(dir.startsWith(os.tmpdir()), `scratch dir ${dir} is not under the OS temp dir`);
  } finally {
    dropVideoDir(dir);
  }
  assert.equal(fs.existsSync(dir), false, 'the scratch dir survived dropVideoDir');
  // Teardown runs on paths that may already be gone; a second drop must not throw and kill a run
  // whose result is already decided. KILLS: dropping `force: true`.
  dropVideoDir(dir);
  dropVideoDir(null);
});

/* --------------------------------------------------------------------------------------------
 * 2. Behaviour: a real recording, on disk, under the promised name.
 * ------------------------------------------------------------------------------------------ */

test('a recorded run leaves a playable file at the path it was given', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-rec-'));
  const ex = new Exec({ [RECORD_ENV]: '1', PW_NO_TRACE: '1' });
  try {
    await ex.call('browser.navigate', { url: FIXTURE });
    await ex.call('browser.click', { locator: { testid: 'target' } });

    const out = path.join(dir, 'video.webm');
    const r = await ex.call<{ path: string | null; kept: boolean }>('browser.videoStop', { path: out });

    // KILLS: returning the requested path without saving; returning kept:true unconditionally.
    assert.equal(r.kept, true, 'the executor reported no recording for a run that was told to record');
    assert.equal(r.path, out);
    assert.ok(fs.existsSync(out), 'browser.videoStop answered with a path that has no file behind it');

    // Not "a file exists" — an EMPTY file exists too, and would satisfy every cheaper assertion here.
    // KILLS: creating the artifact by touch/copy of a zero-length placeholder.
    const size = fs.statSync(out).size;
    assert.ok(size > 1024, `the video is ${size} bytes — that is not a recording of a run`);

    // …and it is a WebM container, read from the bytes rather than trusted from the extension.
    // A .webm that is actually a PNG or an HTML error page passes every check above.
    // KILLS: saving the wrong stream, or writing the response body of a failed fetch.
    const head = fs.readFileSync(out).subarray(0, 4);
    assert.deepEqual([...head], [0x1a, 0x45, 0xdf, 0xa3],
      'the saved file does not start with the EBML magic — it is not a WebM video');
  } finally {
    await ex.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('omitting the path DISCARDS the recording, and says so where a person reads it', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-rec-'));
  const ex = new Exec({ [RECORD_ENV]: '1', PW_NO_TRACE: '1' });
  try {
    await ex.call('browser.navigate', { url: FIXTURE });
    const r = await ex.call<{ path: string | null; kept: boolean }>('browser.videoStop', {});
    // KILLS: writing the file regardless of the argument — the exact mutation that survived on
    // traceStop, which is why this assertion is about the answer AND the directory, not the call.
    assert.equal(r.kept, false, 'a discard reported itself as a keep');
    assert.equal(r.path, null);
    assert.equal(fs.readdirSync(dir).length, 0, 'something was written into the run dir on a discard');

    // ⚠ The discard is ANNOUNCED, with the lever that reverses it. A person who asked for a recording
    // on a run that then passed gets no file, and without this line that reads as the mode having
    // failed. KILLS: deleting the log call, or dropping the lever name from it.
    assert.match(ex.stderr, /video discarded/i, 'a video was thrown away with nothing said about it');
    assert.match(ex.stderr, /SENTINEL_VIDEO_ALWAYS/,
      'the discard message does not name the switch that would have kept the file');
  } finally {
    await ex.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('without the switch nothing is recorded and videoStop is a no-op', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-rec-'));
  const ex = new Exec({ [RECORD_ENV]: '0', PW_NO_TRACE: '1' });
  try {
    await ex.call('browser.navigate', { url: FIXTURE });
    const out = path.join(dir, 'video.webm');
    const r = await ex.call<{ path: string | null; kept: boolean }>('browser.videoStop', { path: out });
    // The brain calls this on EVERY run, recording or not — so the quiet answer is part of the
    // contract. KILLS: making videoStop throw, or recording unconditionally.
    assert.equal(r.kept, false);
    assert.equal(r.path, null);
    assert.equal(fs.existsSync(out), false, 'a run that was not recording produced a video anyway');
  } finally {
    await ex.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
