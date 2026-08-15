/**
 * LIVE-HUMAN (ADR-120) — the decoration layer, gated on BEHAVIOUR over a real page.
 *
 * WHY THIS TEST DRIVES A BROWSER when every other test in this package is pure. What is being
 * claimed here is a claim about PIXELS and about the DOM of somebody else's page: that a cursor
 * appears, that it travels to the control instead of jumping onto it, that it is on the picture a
 * person watches and NOT on the picture a model or a golden reads. Every cheaper form of this test
 * asserts something else instead — that `withCleanFrame` is called, that a string is present in the
 * source — and this repository has already measured what those are worth: a mutation that made
 * `traceStop` write regardless of its argument broke nothing, because the assertion was about our
 * code rather than about the file on disk.
 *
 * The gate therefore speaks to the shipped executor over its own JSON-RPC transport, exactly as the
 * brain does, and reads the result back through verbs that already exist (`browser.probe`,
 * `browser.frame`, `browser.screenshotHash`, `browser.setOfMarks`, `browser.currentUrl`). No verb
 * was added for the test. The one thing the transport cannot reach — where the drawn cursor actually
 * ENDED UP — is measured by the fixture itself and published in `document.title`, which travels back
 * on `browser.currentUrl` and changes no pixels (testdata/fixtures/l11-decorate.html).
 *
 * Each check names the mutation it exists to kill; all five were applied and seen to turn this file
 * red before being reverted.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';
import { resolveLaunchPlan } from './launch.js';
import {
  decorationsEnabled, DECOR_ROOT_ID, DECOR_SLOW_MO_MS, DECOR_STEP_PAUSE_MS,
} from './decorate.js';

const SERVER = path.join(__dirname, 'server.js');
const REPO = path.resolve(__dirname, '..', '..');
const FIXTURE = pathToFileURL(path.join(REPO, 'testdata', 'fixtures', 'l11-decorate.html')).href;

/** The viewport the executor pins for determinism — a control scrolled into view must land inside it. */
const VIEWPORT_H = 720;

/* --------------------------------------------------------------------------------------------
 * A minimal JSON-RPC client: the same newline-delimited protocol brain/executor.py speaks.
 * ------------------------------------------------------------------------------------------ */
class Exec {
  private proc: ChildProcess;
  private buf = '';
  private waiting = new Map<number, { ok: (v: unknown) => void; bad: (e: Error) => void }>();
  private nextId = 1;
  /** Everything the executor logged. Read by the secret check — stderr is a channel too. */
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

  /** How many elements match — `browser.probe` is how this gate looks at the page's DOM. */
  async count(css: string): Promise<number> {
    return (await this.call<{ count: number }>('browser.probe', { locator: { css } })).count;
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

interface FixtureProbe {
  x: number | null; y: number | null; samples: number; clicks: number; input: number; keydown: number;
}

/** What the fixture saw, out of `document.title`. */
function readProbe(title: string): FixtureProbe {
  const m = /^SENTINEL cursor=(\S+) samples=(\d+) clicks=(\d+) input=(\d+) keydown=(\d+)$/
    .exec((title || '').trim());
  assert.ok(m, `the fixture published nothing parseable — is l11-decorate.html loaded? got ${JSON.stringify(title)}`);
  const [, cur, s, c, i, k] = m!;
  const xy = cur === 'none' ? [null, null] : cur.split(',').map(Number);
  return { x: xy[0], y: xy[1], samples: +s, clicks: +c, input: +i, keydown: +k };
}

async function probeOf(ex: Exec): Promise<FixtureProbe> {
  return readProbe((await ex.call<{ title: string }>('browser.currentUrl')).title);
}

interface Mark { testid: string | null; bbox: { x: number; y: number; w: number; h: number } }

/** The centre of a control, taken from the product's OWN inventory — the same box `announce` aims
 *  from. Computing it in the test instead would compare our arithmetic against itself. */
async function centreOf(ex: Exec, testid: string): Promise<{ x: number; y: number; box: Mark['bbox'] }> {
  const marks = (await ex.call<{ marks: Mark[] }>('browser.setOfMarks', {})).marks;
  const m = marks.find((k) => k.testid === testid);
  assert.ok(m, `the fixture control '${testid}' is not in the mark inventory (${marks.length} marks)`);
  return { x: m!.bbox.x + m!.bbox.w / 2, y: m!.bbox.y + m!.bbox.h / 2, box: m!.bbox };
}

const near = (a: number | null, b: number, tol = 2): boolean => a !== null && Math.abs(a - b) <= tol;

/* --------------------------------------------------------------------------------------------
 * 1. The switch and the launch plan — pure, no browser.
 * ------------------------------------------------------------------------------------------ */

test('SENTINEL_DECORATE is read in exactly one shape, and the launch plan carries the pacing', () => {
  assert.equal(decorationsEnabled({}), false);
  assert.equal(decorationsEnabled({ SENTINEL_DECORATE: '0' }), false);
  // Only "1" turns it on — same rule as the frame switches the resolver writes beside it, so a
  // truthy-looking value cannot mean one thing to the brain and another here.
  assert.equal(decorationsEnabled({ SENTINEL_DECORATE: 'true' }), false);
  assert.equal(decorationsEnabled({ SENTINEL_DECORATE: '1' }), true);

  const plain = resolveLaunchPlan({});
  assert.equal(plain.decorate, false);
  assert.equal(plain.slowMo, 0, 'an ordinary run must launch exactly as it did before ADR-120');
  assert.equal(plain.stepPauseMs, 0);

  const human = resolveLaunchPlan({ SENTINEL_DECORATE: '1' });
  assert.equal(human.decorate, true);
  assert.equal(human.slowMo, DECOR_SLOW_MO_MS);
  assert.ok(human.slowMo > 0, 'a "human" run that is not slowed down is not a human run');
  assert.equal(human.slowMoUnavailable, false);
  assert.equal(human.stepPauseMs, DECOR_STEP_PAUSE_MS);
});

test('an adopted browser cannot be slowed at launch — the plan says so and pays for it elsewhere', () => {
  const cdp = resolveLaunchPlan({ SENTINEL_DECORATE: '1', PW_CDP_ENDPOINT: 'http://127.0.0.1:9222' });
  assert.equal(cdp.kind, 'cdp');
  assert.equal(cdp.decorate, true, 'decoration is orthogonal to how the browser was obtained');
  assert.equal(cdp.slowMo, 0, 'slowMo is a launch() option and we did not launch this browser');
  assert.equal(cdp.slowMoUnavailable, true, 'the impossibility has to be NAMED, not silently absent');
  assert.ok(
    cdp.stepPauseMs > resolveLaunchPlan({ SENTINEL_DECORATE: '1' }).stepPauseMs,
    'the pacing slowMo cannot carry has to be paid on our side, not dropped',
  );
});

/* --------------------------------------------------------------------------------------------
 * 2. The decorated run, over a real page.
 * ------------------------------------------------------------------------------------------ */

test('SENTINEL_DECORATE=1 draws a travelling cursor, and keeps it off the frames a machine reads', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-decor-'));
  const ex = new Exec({ SENTINEL_DECORATE: '1', PW_NO_TRACE: '1' });
  try {
    await ex.call('browser.navigate', { url: FIXTURE });

    // Nothing has been acted on yet. The overlay is built on first use, so this is a real "before".
    assert.equal(await ex.count(`#${DECOR_ROOT_ID}`), 0, 'something drew into the page before any action');
    const before = path.join(dir, 'before.png');
    await ex.call('browser.frame', { path: before });
    const goldenBefore = (await ex.call<{ hash: string }>('browser.screenshotHash')).hash;

    await ex.call('browser.click', { locator: { testid: 'target' } });

    // (а) A cursor exists in the page at all — Playwright draws none, so this is entirely ours.
    // KILLS: dropping installDecorations, or drawing nothing on click.
    assert.equal(await ex.count(`#${DECOR_ROOT_ID}`), 1, 'no overlay in the DOM after a decorated click');

    const p = await probeOf(ex);
    // The decoration must not have eaten the action it decorates.
    assert.equal(p.clicks, 1, 'the click did not land — decoration is not allowed to intercept it');

    // (а) …and it TRAVELLED. A teleporting cursor is the failure mode a screenshot cannot show:
    // the still frame looks identical either way, and only a person watching notices there was no
    // movement. KILLS: replacing the interpolation in DECOR_INIT_SCRIPT with a single `place()`.
    assert.ok(p.samples >= 5,
      `the cursor reached the target in ${p.samples} distinct positions — that is a jump, not a move`);

    // (а) …and it arrived AT THE TARGET, measured against the product's own box for that control.
    // KILLS: aiming at the box corner instead of its centre, or at a stale box.
    const target = await centreOf(ex, 'target');
    assert.ok(near(p.x, target.x) && near(p.y, target.y),
      `the cursor stopped at ${p.x},${p.y} while the control is centred on ${target.x},${target.y}`);

    // (г) The frame a PERSON watches carries the cursor…
    // KILLS: wrapping browser.frame in withCleanFrame "for consistency".
    const after = path.join(dir, 'after.png');
    await ex.call('browser.frame', { path: after });
    assert.notEqual(fs.readFileSync(after).toString('base64'), fs.readFileSync(before).toString('base64'),
      'the live frame is byte-identical before and after the cursor existed — nothing was drawn on it');

    // (г) …and the GOLDEN does not. This is the ADR-120 decision, and it is a decision about
    // correctness: a reference with our cursor baked into it is not a degraded reference, it is a
    // wrong one, and it fails on somebody else's replay with nothing on screen to explain why.
    // KILLS: removing withCleanFrame from browser.screenshotHash.
    const goldenAfter = (await ex.call<{ hash: string }>('browser.screenshotHash')).hash;
    assert.equal(goldenAfter, goldenBefore,
      'the golden hash changed once the cursor existed: the reference now describes our overlay');

    // The overlay is taken down AROUND the capture and put back — not cancelled for the run.
    // KILLS: hiding the overlay without restoring it (the person's cursor would vanish for good).
    assert.equal(await ex.count(`#${DECOR_ROOT_ID}`), 1, 'the overlay never came back after a clean capture');
    const after2 = path.join(dir, 'after2.png');
    await ex.call('browser.frame', { path: after2 });
    assert.equal(fs.readFileSync(after2).toString('base64'), fs.readFileSync(after).toString('base64'),
      'the live frame changed across a clean capture — the overlay did not return as it was');

    // (в) Entry is per CHARACTER, not a paste. Invisible in the final value, which is identical
    // either way — so it is counted at the page, in keydowns.
    // KILLS: reverting browser.fill to a plain fill() under decoration.
    const t0 = await probeOf(ex);
    await ex.call('browser.fill', { locator: { testid: 'field' }, value: 'abcde' });
    const t1 = await probeOf(ex);
    assert.ok(t1.keydown - t0.keydown >= 5,
      `5 characters produced ${t1.keydown - t0.keydown} keystrokes — the value was pasted, not typed`);

    // (а) The box is read AFTER scrolling, not before. A control below the fold is where the two
    // orders differ: click() scrolls on its own, so a box read first names coordinates the control
    // is about to leave, and the cursor is then shown pointing confidently at nothing.
    // KILLS: moving scrollIntoViewIfNeeded after boundingBox in `announce`.
    await ex.call('browser.click', { locator: { testid: 'far' } });
    const t2 = await probeOf(ex);
    const far = await centreOf(ex, 'far');
    assert.ok(far.box.y < VIEWPORT_H, 'the fixture control never scrolled into view — the check is vacuous');
    assert.ok(near(t2.x, far.x) && near(t2.y, far.y),
      `the cursor was left at ${t2.x},${t2.y} while the control ended up centred on ${far.x},${far.y}`);

    // (а) The injection SURVIVES navigation. A one-shot evaluate passes every check above and fails
    // this one — and in production it would mean the mode working exactly until the run went
    // somewhere, which a person reads as the tool having stopped rather than the cursor being lost.
    // KILLS: context.addInitScript -> page.evaluate in installDecorations.
    await ex.call('browser.navigate', { url: FIXTURE });
    assert.equal(await ex.count(`#${DECOR_ROOT_ID}`), 1,
      'the overlay did not survive a navigation — the page-side half is not being re-injected per document');
  } finally {
    await ex.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/* --------------------------------------------------------------------------------------------
 * 3. The control: without the switch, nothing changes at all.
 * ------------------------------------------------------------------------------------------ */

test('without the switch nothing is drawn and the pixels are exactly what they were', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-decor-'));
  const ex = new Exec({ SENTINEL_DECORATE: '0', PW_NO_TRACE: '1' });
  try {
    await ex.call('browser.navigate', { url: FIXTURE });
    const before = path.join(dir, 'plain-before.png');
    await ex.call('browser.frame', { path: before });
    const goldenBefore = (await ex.call<{ hash: string }>('browser.screenshotHash')).hash;

    await ex.call('browser.click', { locator: { testid: 'target' } });

    assert.equal(await ex.count(`#${DECOR_ROOT_ID}`), 0, 'an undecorated run drew an overlay anyway');
    assert.equal((await probeOf(ex)).clicks, 1, 'the click did not land — the control run is vacuous');

    // This is what makes the decorated run's "the frame changed" mean "the cursor was drawn": the
    // very same actions on the very same page change nothing when decoration is off.
    const after = path.join(dir, 'plain-after.png');
    await ex.call('browser.frame', { path: after });
    assert.equal(fs.readFileSync(after).toString('base64'), fs.readFileSync(before).toString('base64'),
      'clicking changed the picture without any decoration — the fixture is not pixel-stable');
    assert.equal((await ex.call<{ hash: string }>('browser.screenshotHash')).hash, goldenBefore);

    // And entry stays a paste: the per-character path belongs to the decorated mode only.
    const t0 = await probeOf(ex);
    await ex.call('browser.fill', { locator: { testid: 'field' }, value: 'abcde' });
    const t1 = await probeOf(ex);
    assert.equal(t1.keydown - t0.keydown, 0, 'an undecorated fill went through the keyboard');
    assert.ok(t1.input > t0.input, 'the field was not filled at all — the check above is vacuous');
  } finally {
    await ex.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/* --------------------------------------------------------------------------------------------
 * 4. The secret keeps its own rules under decoration.
 * ------------------------------------------------------------------------------------------ */

test('a secret is still pasted, never typed key by key, and is named on no channel', async () => {
  // Assembled at runtime so the literal is not a string sitting in the repository for a scanner to
  // find; it is a fake value either way, but the habit is the point.
  const secret = ['pw', Date.now().toString(36), 'not-real'].join('-');
  const ex = new Exec({ SENTINEL_DECORATE: '1', PW_NO_TRACE: '1', SENTINEL_TEST_SECRET: secret });
  try {
    await ex.call('browser.navigate', { url: FIXTURE });
    const t0 = await probeOf(ex);
    await ex.call('browser.fill', { locator: { testid: 'pw' }, secretRef: 'SENTINEL_TEST_SECRET' });
    const t1 = await probeOf(ex);

    // Per-character entry turns one value into N keystroke events — N more things a page listener,
    // a screencast frame or a future trace can pick up. The decorated path must not reach here.
    // KILLS: routing the secretRef branch through the decorated per-character entry.
    assert.equal(t1.keydown - t0.keydown, 0, 'the secret was typed character by character');
    assert.ok(t1.input > t0.input, 'the secret was never entered — the check above is vacuous');

    // …while the person still sees WHICH field is being filled. Decoration is not skipped for the
    // secret; only the keystrokes are.
    assert.equal(await ex.count(`#${DECOR_ROOT_ID}`), 1, 'the secret field was filled with no cursor at all');
    const p = await probeOf(ex);
    const pw = await centreOf(ex, 'pw');
    assert.ok(near(p.x, pw.x) && near(p.y, pw.y),
      `the cursor is at ${p.x},${p.y} rather than on the password field at ${pw.x},${pw.y}`);

    assert.ok(!ex.stderr.includes(secret), 'the secret reached the executor log');
  } finally {
    await ex.close();
  }
});

/* --------------------------------------------------------------------------------------------
 * 5. Completeness: the list of captures is DERIVED, and the derivation has a floor.
 * ------------------------------------------------------------------------------------------ */

test('every screenshot the executor takes is either declared human or taken with the overlay down', () => {
  // Belt and braces to the behavioural checks above, and the only one of the five that can see a
  // capture site that does not exist YET. It is a source scan, which this repository treats as a
  // surrogate on its own — so it is not asserting that a call is present, it is ENUMERATING every
  // capture in the file and demanding that each one be accounted for. A fourth screenshot added
  // later without a decision about the overlay turns this red on the day it is written.
  const src = fs.readFileSync(path.join(REPO, 'pw-executor', 'src', 'server.ts'), 'utf8');

  // The one capture that is deliberately DECORATED, with the reason: it is what the hub shows a
  // person, and stripping the cursor from it would remove the only evidence of who is acting.
  const HUMAN_CAPTURES = new Set(['browser.frame']);

  const sites: Array<{ verb: string; clean: boolean }> = [];
  const shot = /page!\.screenshot\(/g;
  for (let m = shot.exec(src); m; m = shot.exec(src)) {
    const before = src.slice(0, m.index);
    const caseAt = before.lastIndexOf("case 'browser.");
    assert.ok(caseAt > 0, 'a screenshot is taken outside any verb — this scan cannot classify it');
    const verb = /case '(browser\.[a-zA-Z]+)'/.exec(src.slice(caseAt))![1];
    sites.push({ verb, clean: before.slice(caseAt).includes('withCleanFrame(') });
  }

  // The floor. Without it the regex could stop matching — a rename, a refactor into a helper — and
  // this test would pass over an empty list while claiming every capture is accounted for.
  assert.ok(sites.length >= 3,
    `only ${sites.length} screenshot site(s) found in server.ts — the scan has stopped seeing them`);

  for (const s of sites) {
    if (HUMAN_CAPTURES.has(s.verb)) {
      assert.equal(s.clean, false,
        `${s.verb} is the frame a PERSON watches and must NOT be stripped of the cursor`);
    } else {
      assert.ok(s.clean,
        `${s.verb} captures for a machine (a golden, or a model asked to pick one of our marks) ` +
        'and is not wrapped in withCleanFrame — a decorated reference is a wrong reference');
    }
  }

  // Both kinds have to be present, or the loop above proves only that one of the two rules holds.
  assert.ok(sites.some((s) => s.clean), 'no clean capture at all — the machine-facing rule is untested');
  assert.ok(sites.some((s) => !s.clean), 'no human capture at all — the person-facing rule is untested');
});
