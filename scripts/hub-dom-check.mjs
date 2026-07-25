#!/usr/bin/env node
// DOM gate for the hub's Logs view (ADR-065) — a real headless Chromium against a real control-API
// serving the real page, driving a real run.
//
// It exists because the claims this feature makes are all about what a person SEES, and none of them
// can be checked from Go or Python: that the run narrative never leaks into the levelled diagnostics,
// that a loop reads as one row with a count, that a level checkbox actually filters, that a regex
// search works and a half-typed regex does not blank the view, that Russian shows real values rather
// than {placeholders}, and that switching language re-renders both the rows and the <option> labels
// (an <option> cannot host <span data-lang>, so those are rebuilt in JS and nothing else would catch
// a regression there).
//
// Run: node scripts/hub-dom-check.mjs

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(REPO, 'pw-executor', 'package.json'));
const { chromium } = require('playwright');

const PORT = Number(process.env.HUB_GATE_PORT || 18744);
const FIXTURE = `file://${REPO}/testdata/fixtures/l2.html`;

/* ------------------------------------------------------------------ tiny harness */
const results = [];
// Page exceptions are COLLECTED, not thrown from the listener: a throw inside an EventEmitter callback
// never reaches the awaiting check() — it escapes as an unhandled rejection instead. (Same lesson the
// wizard gate records.)
const pageErrors = [];
async function check(name, fn) {
  pageErrors.length = 0;
  try {
    await fn();
    if (pageErrors.length) throw new Error(`uncaught page error(s): ${pageErrors.join(' | ')}`);
    results.push({ name, ok: true });
    console.log(`  ok   ${name}`);
  } catch (e) {
    results.push({ name, ok: false, err: e.message });
    console.log(`  FAIL ${name}\n       ${e.message.split('\n').join('\n       ')}`);
  }
}
function ok(cond, what) { if (!cond) throw new Error(what); }
function eq(actual, expected, what) {
  if (String(actual) !== String(expected)) throw new Error(`${what}: expected ${expected}, got ${actual}`);
}

/* ------------------------------------------------------------------ fixtures */
let capi = null, browser = null;

// Every resource is captured INSIDE the try, so a failure to launch cannot leave an orphaned
// control-API holding the port. An orphan is not a hypothetical: it makes the NEXT run talk to the old
// process, whose one-time bootstrap nonce does not match, and the failure then looks like a UI bug.
try {
  const bin = path.join(REPO, 'bin', 'control-api');
  if (!fs.existsSync(bin)) throw new Error(`${bin} not built — run: go build -o bin/control-api ./cmd/control-api`);

  fs.mkdirSync(path.join(REPO, 'state'), { recursive: true });
  capi = spawn(bin, [], {
    cwd: REPO,
    env: {
      ...process.env,
      CONTROL_API_ADDR: `127.0.0.1:${PORT}`,
      CONTROL_API_SERVE_UI: '1',
      CONTROL_API_UI_DIR: path.join(REPO, 'docs'),
      CONTROL_API_AGENTCTL: path.join(REPO, 'bin', 'agentctl'),
      CONTROL_API_CORS_ORIGINS: '',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  capi.stdout.on('data', (b) => { log += b; });
  capi.stderr.on('data', (b) => { log += b; });

  let nonce = '';
  for (let i = 0; i < 100; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/healthz`);
      if (r.ok) { const m = /bootstrap=([0-9a-f]+)/.exec(log); if (m) { nonce = m[1]; break; } }
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  if (/address already in use|bind:/i.test(log)) throw new Error(`port ${PORT} already in use (orphan?)`);
  if (!nonce) throw new Error(`control-API never printed a bootstrap nonce:\n${log.slice(0, 1500)}`);

  // control-api persists its generated token here (ADR-064). On a fresh checkout state/ may not exist
  // yet, and without it the token stays in memory only and this read would fail — so create it before
  // the server starts rather than diagnosing a missing file later.
  const tokenPath = path.join(REPO, 'state', 'control-api.token');
  if (!fs.existsSync(tokenPath)) throw new Error(`control-api did not persist a token at ${tokenPath}`);
  const token = fs.readFileSync(tokenPath, 'utf8').trim();

  // A real run against a fixture that is KNOWN to loop on a disabled control — that loop is the
  // signal the collapsing exists to make visible, so the gate needs it.
  const started = await fetch(`http://127.0.0.1:${PORT}/v1/runs`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: FIXTURE }),
  });
  if (started.status !== 202) throw new Error(`POST /v1/runs -> ${started.status}`);
  // Long enough for the browser to launch, a few steps to run, and the heal stub to repeat.
  await new Promise((r) => setTimeout(r, 25000));

  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1180, height: 1400 } });
  page.on('pageerror', (e) => pageErrors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') pageErrors.push(`console: ${m.text()}`); });

  await page.goto(`http://127.0.0.1:${PORT}/?bootstrap=${nonce}`, { waitUntil: 'load' });
  await page.waitForTimeout(400);
  await page.click('#tab-btn-tests');
  await page.click('.subtab-btn[data-subtab="logs"]');
  // The view fetches the catalogue, the run list and the logs; wait for rows rather than a fixed sleep.
  await page.waitForSelector('#lg-list .lgrow', { timeout: 20000 });

  const rows = () => page.locator('#lg-list .lgrow:not(.child)');
  const setLevels = async (on) => {
    for (const l of ['error', 'warn', 'info', 'debug']) {
      const el = page.locator(`#lg-${l}`);
      if (on.includes(l)) await el.check(); else await el.uncheck();
    }
    await page.waitForTimeout(250);
  };

  console.log(`\nhub-dom-check — Logs view (ADR-065), port ${PORT}\n`);

  await check('bootstrap: the tab is usable without pasting a token', async () => {
    const tok = await page.locator('#capitok').inputValue();
    ok(tok.length > 0, 'the bootstrap exchange left #capitok empty, so every token-gated read fails');
    const err = await page.locator('#lg-err').isVisible();
    ok(!err, `the error box is visible: ${await page.locator('#lg-err').innerText()}`);
    // Asserted HERE, before any later check touches the boxes: a tester must not be handed the
    // noisiest view first, so capture keeps everything while the VIEW starts without debug.
    ok(!(await page.locator('#lg-debug').isChecked()), 'DEBUG must start unchecked');
    for (const l of ['error', 'warn', 'info']) {
      ok(await page.locator(`#lg-${l}`).isChecked(), `${l.toUpperCase()} must start checked`);
    }
  });

  await check('the run narrative never leaks into the levelled diagnostics', async () => {
    const text = await page.locator('#lg-list').innerText();
    ok(!text.includes('@@AGUI'), 'an AG-UI frame reached the Logs view — it belongs to Live');
    ok(!/"type"\s*:\s*"(step\.progress|tool\.call|state\.transition)"/.test(text),
      'a raw narrative event is being rendered as a diagnostic');
  });

  await check('a repeated line reads as ONE row carrying a count', async () => {
    await setLevels(['error', 'warn', 'info', 'debug']);
    const badges = page.locator('#lg-list .lgn');
    ok(await badges.count() > 0,
      'no ×N badge: the loop this fixture produces is being rendered as N separate rows');
    const label = await badges.first().innerText();
    const n = parseInt(label.replace(/[^0-9]/g, ''), 10);
    ok(n >= 2, `a count badge must mean 2 or more, got ${label}`);
    // The collapsed row must still be ONE row, not N.
    const stub = page.locator('#lg-list .lgrow:not(.child)', { hasText: 'разведк' });
    ok(await stub.count() <= 2, `the collapsed record appears ${await stub.count()} times`);
  });

  await check('level checkboxes narrow the view', async () => {
    await setLevels(['error', 'warn', 'info', 'debug']);
    const all = await rows().count();
    await setLevels(['error', 'warn', 'info']);
    const noDebug = await rows().count();
    ok(noDebug < all, `unchecking DEBUG changed nothing (${all} -> ${noDebug})`);
    await setLevels(['error']);
    const onlyErr = await rows().count();
    ok(onlyErr <= noDebug, `narrowing to ERROR did not narrow the view (${noDebug} -> ${onlyErr})`);
    await setLevels(['error', 'warn', 'info', 'debug']);
  });

  await check('search filters, regex mode works, and a half-typed regex does not blank the view', async () => {
    const before = await rows().count();
    await page.uncheck('#lg-re');
    await page.fill('#lg-q', 'zzzz-no-such-text');
    await page.waitForTimeout(250);
    eq(await rows().count(), 0, 'a search with no match should show no rows');

    await page.check('#lg-re');
    await page.fill('#lg-q', 'разведк|browser');
    await page.waitForTimeout(250);
    const re = await rows().count();
    ok(re > 0 && re < before, `regex search matched ${re} of ${before} rows`);

    // A regex is typed one character at a time, so it is INVALID most of the time. Blanking the list
    // on every keystroke would make the field unusable.
    await page.fill('#lg-q', '[unclosed');
    await page.waitForTimeout(250);
    ok(await rows().count() > 0, 'an incomplete regex blanked the view instead of being ignored');

    await page.fill('#lg-q', '');
    await page.waitForTimeout(250);
  });

  await check('Russian shows real values, not {placeholders}', async () => {
    const text = await page.locator('#lg-list').innerText();
    ok(!/\{\w+\}/.test(text),
      `an unsubstituted placeholder is on screen: ${(/\{[^}]*\}/.exec(text) || [])[0]}`);
    ok(/Прогон \w+ начат/.test(text),
      'the run-config message is not rendering with its real values in Russian');
  });

  await check('switching language re-renders rows AND <option> labels', async () => {
    await page.click('#lang-en');
    await page.waitForTimeout(400);
    const en = await page.locator('#lg-list').innerText();
    const enSort = await page.locator('#lg-sort option:checked').innerText();
    ok(/\binfo\b/.test(en), 'level labels did not switch to English');
    ok(!/информация/.test(en), 'Russian level labels survived the switch to English');
    ok(/by time|time/i.test(enSort), `the sort <option> label did not switch: ${enSort}`);

    await page.click('#lang-ru');
    await page.waitForTimeout(400);
    const ru = await page.locator('#lg-list').innerText();
    const ruSort = await page.locator('#lg-sort option:checked').innerText();
    ok(/информация/.test(ru), 'level labels did not switch back to Russian');
    ok(/по времени/.test(ruSort), `the sort <option> label did not switch back: ${ruSort}`);
  });

  await check('sort order changes the first row', async () => {
    await page.selectOption('#lg-sort', 'asc');
    await page.waitForTimeout(250);
    const first = await rows().first().innerText();
    await page.selectOption('#lg-sort', 'desc');
    await page.waitForTimeout(250);
    const firstDesc = await rows().first().innerText();
    ok(first !== firstDesc, 'newest-first produced the same first row as oldest-first');
    await page.selectOption('#lg-sort', 'asc');
    await page.waitForTimeout(250);
  });

  await check('category filter offers only catalogue categories and narrows the view', async () => {
    const opts = await page.locator('#lg-cat option').evaluateAll((els) => els.map((e) => e.value));
    ok(opts.length > 1, 'the category dropdown was never populated from the catalogue');
    ok(opts.includes('heal') && opts.includes('llm'), `catalogue categories missing: ${opts.join(',')}`);
    const all = await rows().count();
    await page.selectOption('#lg-cat', 'browser');
    await page.waitForTimeout(250);
    const some = await rows().count();
    ok(some > 0 && some < all, `filtering by category browser gave ${some} of ${all}`);
    await page.selectOption('#lg-cat', '');
    await page.waitForTimeout(250);
  });

  await check('a run with no logs says so instead of looking empty', async () => {
    // A never-run id: the endpoint answers recorded:false, and the view must explain that rather than
    // render the same emptiness it shows for "nothing matched".
    const msg = await page.evaluate(async () => {
      const r = await fetch('/v1/runs/ffffffffffffffff/logs',
        { headers: { Authorization: 'Bearer ' + document.getElementById('capitok').value } });
      const j = await r.json();
      return { recorded: j.recorded, reason: j.reason || '' };
    });
    eq(msg.recorded, false, 'a run with no log file must report recorded=false');
    ok(msg.reason.length > 0, 'recorded=false must carry a reason a person can read');
  });
} catch (e) {
  results.push({ name: 'harness', ok: false, err: e.message });
  console.log(`  FAIL harness\n       ${e.message}`);
} finally {
  if (browser) await browser.close().catch(() => {});
  if (capi) capi.kill('SIGTERM');
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log(failed.map((f) => `  FAIL ${f.name}: ${f.err}`).join('\n'));
  process.exit(1);
}
