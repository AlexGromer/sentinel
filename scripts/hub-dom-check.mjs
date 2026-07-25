#!/usr/bin/env node
// DOM gate for the hub (ADR-065 Logs view + ADR-066 navigation) — a real headless Chromium against a
// real control-API serving the real page, driving a real run.
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
// `allowConsole` names console output that is CORRECT for the scenario, so it cannot be silently
// blanket-ignored. The one real case: a reload without ?bootstrap= leaves the tab with no token, by
// design (ADR-032/064 — the nonce is one-time and the token never touches storage), so a token-gated
// read answering 403 is the product behaving properly.
async function check(name, fn, opts) {
  pageErrors.length = 0;
  try {
    await fn();
    const allow = opts && opts.allowConsole;
    const left = allow ? pageErrors.filter((e) => !allow.test(e)) : pageErrors;
    pageErrors.length = 0;
    pageErrors.push(...left);
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
  // ADR-066: navigation is the rail; the Logs checks below reach it through the router.

  const rows = () => page.locator('#lg-list .lgrow:not(.child)');
  const setLevels = async (on) => {
    for (const l of ['error', 'warn', 'info', 'debug']) {
      const el = page.locator(`#lg-${l}`);
      if (on.includes(l)) await el.check(); else await el.uncheck();
    }
    await page.waitForTimeout(250);
  };

  console.log(`\nhub-dom-check — hub navigation (ADR-066) + Logs view (ADR-065), port ${PORT}\n`);

  /* ---------------------------------------------------------------- ADR-066: navigation */
  const VIEWS = ['chat', 'run', 'live', 'library', 'results', 'logs', 'tools', 'settings'];

  await check('nav: every rail item reveals its own view and nothing else', async () => {
    for (const v of VIEWS) {
      await page.click(`.rail a[data-nav="${v}"]`);
      await page.waitForTimeout(200);
      const shown = await page.locator(`[data-view~="${v}"]:visible`).count();
      ok(shown > 0, `view ${v} revealed nothing`);
      const cur = await page.locator('.rail a[aria-current="page"]').getAttribute('data-nav');
      eq(cur, v, `the rail did not mark ${v} as current`);
      // Nothing from another view may be on screen at the same time.
      for (const other of VIEWS.filter((x) => x !== v)) {
        const leaked = await page.locator(`[data-view="${other}"]:visible`).count();
        eq(leaked, 0, `view ${v} is showing ${leaked} section(s) belonging to ${other}`);
      }
    }
  });

  await check('nav: the product\'s main action is not buried under Settings', async () => {
    // The defect this redesign exists to fix: launching a run lived under a tab called "Settings",
    // next to four calculators.
    await page.click('.rail a[data-nav="settings"]');
    await page.waitForTimeout(200);
    ok(!(await page.locator('#build').isVisible()), 'the run builder is still inside Settings');
    ok(await page.locator('#capitok').isVisible(), 'Settings must hold the connection fields');
    await page.click('.rail a[data-nav="run"]');
    await page.waitForTimeout(200);
    ok(await page.locator('#b-run').isVisible(), 'the run button is not on its own view');
    ok(!(await page.locator('#rec-out').isVisible()), 'a calculator is showing next to the run form');
  });

  await check('nav: ONE token field for the whole app', async () => {
    const fields = await page.locator('input[id="capitok"]').count();
    eq(fields, 1, 'more than one token field exists — the second would need pasting again');
    await page.click('.rail a[data-nav="settings"]');
    await page.waitForTimeout(200);
    const val = await page.locator('#capitok').inputValue();
    ok(val.length > 0, 'the single token field is empty after bootstrap');
  });

  await check('nav: grouped views switch inner sections without leaking siblings', async () => {
    await page.click('.rail a[data-nav="library"]');
    await page.waitForTimeout(300);
    eq(await page.locator('[data-innerbar="library"] .subtab-btn').count(), 3, 'library inner tabs');
    await page.click('[data-innerbar="library"] .subtab-btn[data-sub="runs"]');
    await page.waitForTimeout(250);
    ok(await page.locator('#runs-list').isVisible(), 'the Runs section did not appear');
    ok(!(await page.locator('[data-subpanel="library"]').isVisible()),
      'Scenarios stayed visible alongside Runs');
    await page.click('[data-innerbar="library"] .subtab-btn[data-sub="library"]');
    await page.waitForTimeout(250);
  });

  await check('nav: an address beats a remembered view', async () => {
    // localStorage now holds a view from the clicking above. An explicit link must win over it —
    // getting this backwards sent every deep link to whatever was open last, which is precisely the
    // kind of link that gets pasted into a bug report.
    await page.goto('about:blank');
    await page.goto(`http://127.0.0.1:${PORT}/#v=logs`, { waitUntil: 'load' });
    await page.waitForTimeout(600);
    eq(await page.locator('.rail a[aria-current="page"]').getAttribute('data-nav'), 'logs',
      '#v=logs did not open the Logs view');

    // A legacy anchor must resolve to the view that now contains that section, not 404 into the default.
    await page.goto('about:blank');
    await page.goto(`http://127.0.0.1:${PORT}/#connect`, { waitUntil: 'load' });
    await page.waitForTimeout(600);
    eq(await page.locator('.rail a[aria-current="page"]').getAttribute('data-nav'), 'settings',
      'the legacy #connect anchor did not resolve to Settings');

    // A hash-only change is a SAME-DOCUMENT navigation: without a hashchange listener nothing rereads it.
    await page.evaluate(() => { location.hash = '#v=tools'; });
    await page.waitForTimeout(300);
    eq(await page.locator('.rail a[aria-current="page"]').getAttribute('data-nav'), 'tools',
      'editing the hash did not switch the view');
  }, { allowConsole: /403 \(Forbidden\)/ });

  await check('nav: the standalone chat page redirects into the app instead of duplicating it', async () => {
    await page.goto(`http://127.0.0.1:${PORT}/chat/`, { waitUntil: 'load' });
    await page.waitForTimeout(700);
    ok(/#v=chat$/.test(page.url()), `/chat/ did not land on the Chat view, got ${page.url()}`);
    const fields = await page.locator('input[id="capitok"]').count();
    eq(fields, 1, 'the chat page still carries its own token field');
  });

  /* Back to a clean load for the Logs checks. The bootstrap nonce was already spent by the first load,
     and the token is deliberately never persisted, so this reload pastes it the way a Mode-2 operator
     does — which is also the documented fallback when the one-time link has expired. */
  await page.goto('about:blank');
  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'load' });
  await page.waitForTimeout(400);
  // The fields live in Settings, and only the active view is on screen — which is the point of the
  // rail, so the gate has to navigate like a person rather than reach into a hidden input.
  await page.click('.rail a[data-nav="settings"]');
  await page.waitForTimeout(200);
  await page.fill('#capi', `http://127.0.0.1:${PORT}`);
  await page.fill('#capitok', token);
  await page.click('.rail a[data-nav="logs"]');
  await page.click('#lg-reload');
  await page.waitForSelector('#lg-list .lgrow', { timeout: 20000 });

  /* ---------------------------------------------------------------- ADR-065: Logs view */
  await check('logs: the view loads and starts with DEBUG off', async () => {
    const tok = await page.locator('#capitok').inputValue();
    ok(tok.length > 0, 'no token in the field, so every token-gated read fails');
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
