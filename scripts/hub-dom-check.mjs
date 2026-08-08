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
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(REPO, 'pw-executor', 'package.json'));
const { chromium } = require('playwright');
import { hubViews, MIN_VIEWS } from './hub-views.mjs';

const PORT = Number(process.env.HUB_GATE_PORT || 18744);
const FIXTURE = `file://${REPO}/testdata/fixtures/l2.html`;
// The fixture that BREAKS on purpose (ADR-067): JS exceptions, 404s, console errors. Needed because
// every claim about the application channel is unfalsifiable against a fixture that behaves.
const FAULT_FIXTURE = `file://${REPO}/testdata/fixtures/l7-appfaults.html`;

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
let capi = null, capi2 = null, browser = null, gw = null;

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

  // Wait for a run to actually FINISH, rather than sleeping a guessed number of seconds.
  //
  // The fixed 25s/22s sleeps were the gate's own flake: on a slow runner the run had not written its
  // log yet, so eleven Logs checks failed on a run that was merely still going — and the failure looked
  // exactly like a regression in the page. Two consecutive PRs died on it. Waiting on the STATE the
  // checks depend on removes the guess; the cap only bounds a genuinely stuck run, and it says so.
  const waitForRun = async (id, capMs = 120000) => {
    const deadline = Date.now() + capMs;
    for (;;) {
      const r = await fetch(`http://127.0.0.1:${PORT}/v1/runs/${id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (r.ok) {
        const state = (await r.json()).state;
        if (state && state !== 'running') return state;
      }
      if (Date.now() > deadline) {
        throw new Error(`run ${id} was still running after ${capMs}ms — the gate cannot check logs that do not exist yet`);
      }
      await new Promise((res) => setTimeout(res, 500));
    }
  };

  const spawnRun = async (target) => {
    const started = await fetch(`http://127.0.0.1:${PORT}/v1/runs`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ target }),
    });
    if (started.status !== 202) throw new Error(`POST /v1/runs (${target}) -> ${started.status}`);
    return (await started.json()).run_id;
  };

  // A real run against a fixture whose controls refuse to be clicked — several steps, several heal
  // misses, a real narrative to split from real diagnostics. It used to be the source of the repeat
  // the collapsing check needed, because explore retried one control until max_steps; ADR-070 gave
  // that retry a budget, so the repeat now comes from the application instead (see l7 below).
  // Kept, because the Logs checks below are about THIS run specifically — see the explicit selection
  // before that block.
  const baseRun = await spawnRun(FIXTURE);
  await waitForRun(baseRun);

  // A SECOND run, against the fixture that actually misbehaves. The audience check below needs a run
  // carrying application-side records, and l2 has none — it behaves, so `src == application` matches
  // nothing there. Two mutation runs proved that mattered: with the audience expansion removed
  // entirely, `src == business` and `src == application || src == testing` both matched zero rows on
  // l2 and the check passed on 0 == 0. A set identity over two empty sets asserts nothing.
  const faultRun = await spawnRun(FAULT_FIXTURE);
  await waitForRun(faultRun);

  browser = await chromium.launch({ headless: true });
  // An EXPLICIT context, not browser.newPage(): the ADR-074 relay check has to open a second tab beside
  // this one, and Playwright refuses to add pages to the implicit context browser.newPage() creates.
  // Two tabs in one context also share the origin partition a BroadcastChannel is scoped to, which is
  // the very thing under test.
  const context = await browser.newContext({ viewport: { width: 1180, height: 1400 } });
  const page = await context.newPage();
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
  // Two runs exist, and every check other than the two below reads the first one. Restoring by
  // `{index: 0}` would assume an order /v1/runs does not promise, so the previous selection is read
  // back and restored by VALUE — a check must not leave the view pointing somewhere its neighbours
  // did not expect.
  const withRun = async (id, fn) => {
    const before = await page.locator('#lg-run').inputValue();
    await page.selectOption('#lg-run', id);
    await page.waitForTimeout(1500);
    try { await fn(); } finally {
      await page.selectOption('#lg-run', before);
      await page.waitForTimeout(1200);
    }
  };

  console.log(`\nhub-dom-check — hub navigation (ADR-066) + Logs view (ADR-065), port ${PORT}\n`);

  /* ---------------------------------------------------------------- ADR-066: navigation */
  // Derived from the hub, not restated here (docs/DEVELOPMENT.md §0.5). This was the SECOND
  // independent copy of the same list; the third one, in ui-smoke, held seven of nine and nobody saw
  // it. A list that has to be kept in step by hand is a list that eventually is not.
  const VIEWS = hubViews();
  if (VIEWS.length < MIN_VIEWS) {
    throw new Error(`derived only ${VIEWS.length} views, expected >= ${MIN_VIEWS} — the neighbour-leak `
      + 'check iterates this list, so a short one silently narrows every navigation assertion below');
  }

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

  // ADR-074. The wizard is a separate page and the bootstrap nonce can only ever be redeemed once, so a
  // /setup/ opened second had no token and refused to save. Two claims, and the second needs a control
  // or it proves nothing: a tab CAN pick the token up from an open sibling, and it CANNOT get one when
  // no sibling is holding it. Without the negative half, a wizard that somehow acquired a token by any
  // other route would pass this check while the relay did nothing.
  await check('the wizard is reachable from the app and inherits the token from an open tab', async () => {
    await page.click('.rail a[data-nav="settings"]');
    await page.waitForTimeout(200);
    const hubTok = await page.locator('#capitok').inputValue();
    ok(hubTok.length > 0, 'the hub holds no token, so there is nothing to relay');
    ok(await page.locator('#connect a[href="./setup/"]:visible').count() === 1,
      'exactly one visible link to the wizard must sit beside the token it needs');

    // Same browser context = same origin partition, which is what a BroadcastChannel is scoped to.
    const sibling = await context.newPage();
    const siblingErrors = [];
    sibling.on('pageerror', (e) => siblingErrors.push(e.message));
    sibling.on('console', (m) => { if (m.type() === 'error') siblingErrors.push(`console: ${m.text()}`); });
    try {
      await sibling.goto(`http://127.0.0.1:${PORT}/setup/`, { waitUntil: 'load' }); // NO ?bootstrap=
      ok(!/bootstrap=/.test(sibling.url()), 'the control must not carry a nonce, or it proves nothing');
      await sibling.waitForFunction(() => {
        const el = document.getElementById('capitok');
        return el && el.value.length > 0;
      }, null, { timeout: 8000 });
      eq(await sibling.locator('#capitok').inputValue(), hubTok, 'the wizard received a different token');
      // The relay must not have turned the token into stored state (ADR-032/061/064).
      const stored = await sibling.evaluate(() => JSON.stringify(localStorage) + '|' + JSON.stringify(sessionStorage));
      ok(!stored.includes(hubTok), 'the relayed token reached browser storage');
      ok(siblingErrors.length === 0, `wizard page error(s): ${siblingErrors.join(' | ')}`);
    } finally {
      await sibling.close();
    }

    // The control: a wizard with no sibling holding a token stays empty. A separate context has its own
    // origin partition, so no channel connects the two.
    const lone = await browser.newContext();
    try {
      const lonePage = await lone.newPage();
      await lonePage.goto(`http://127.0.0.1:${PORT}/setup/`, { waitUntil: 'load' });
      await lonePage.waitForTimeout(1500);
      eq(await lonePage.locator('#capitok').inputValue(), '',
        'a wizard with no open app tab acquired a token anyway — the positive half above proves nothing');
    } finally {
      await lone.close();
    }
  }, { allowConsole: /403 \(Forbidden\)/ });

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

  /* ------------------------------------------- ADR-066 tail: navigation that reaches a dead name */
  await check('a run in flight can be watched: the button CONNECTS and moves to the live view', async () => {
    // Watch only renders while a run's state is `running`, so this spawns its own instead of reusing
    // the two settled runs above. Asserting against a button that is not in the DOM passes vacuously,
    // which is exactly how a dead control survives a suite.
    const liveRun = await spawnRun(FIXTURE);
    await page.click('.rail a[data-nav="library"]');
    await page.click('[data-innerbar="library"] .subtab-btn[data-sub="runs"]');
    await page.click('#runs-refresh');
    await page.waitForSelector(`#runs-list [data-watch="${liveRun}"]`, { timeout: 20000 });
    await page.click(`#runs-list [data-watch="${liveRun}"]`);
    await page.waitForTimeout(400);
    // BOTH halves are asserted because the defect broke both and either assertion alone would have
    // passed while the other stayed broken: the throw came from the router call, and because it sat
    // ahead of the connect on the same line it swallowed that too.
    eq(await page.inputValue('#live-runid'), liveRun,
      'the connect never ran — connecting to the run is what this button is FOR');
    eq(await page.locator('.rail a[aria-current="page"]').getAttribute('data-nav'), 'live',
      'the router never moved to the live view');

    // Leave no run in flight. The Logs checks below pick the NEWEST run, so an unfinished one becomes
    // the run they read — and its log has only the control-API's own opening lines, no brain output at
    // all. That made `unchecking DEBUG changed nothing (7 -> 7)` and a disabled #lg-debug, two failures
    // whose text points at the Logs view and whose cause is this check. It is a RACE, so it passed here
    // and failed in CI, which is the worst way for a gate to be wrong.
    // Cancelled rather than waited out: it settles in about a second instead of twenty-five, and it
    // exercises POST /v1/runs/{id}/cancel on the way.
    await fetch(`http://127.0.0.1:${PORT}/v1/runs/${liveRun}/cancel`, {
      method: 'POST', headers: { Authorization: `Bearer ${token}` },
    });
    for (let i = 0; i < 60; i++) {
      const r = await fetch(`http://127.0.0.1:${PORT}/v1/runs/${liveRun}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (r.ok && (await r.json()).state !== 'running') break;
      await page.waitForTimeout(500);
    }
  });

  await page.click('.rail a[data-nav="logs"]');
  await page.click('#lg-reload');
  await page.waitForSelector('#lg-list .lgrow', { timeout: 20000 });
  // Pick the run these checks are ABOUT, rather than inheriting whichever is newest.
  //
  // The view defaults to the most recent run, and the in-flight check above deliberately spawns one
  // and cancels it — so the newest run is a CANCELLED one whose log holds two control-API lines and no
  // brain output at all. Every Logs check then measures an empty view and fails with text pointing at
  // the Logs view: "unchecking DEBUG changed nothing (2 -> 2)". Cancelling settles that run quickly but
  // does not stop it being the newest, which is why the previous fix did not hold. Naming the run
  // removes the dependency on ordering entirely.
  await page.selectOption('#lg-run', baseRun);
  await page.waitForTimeout(1500);

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

  // The repeat is sourced from the APPLICATION (l7 emits the same console error 8 times in a row),
  // not from the tool. It used to come from explore retrying one control until max_steps — ADR-070
  // deliberately removed that loop, and with it the only thing this check had to collapse. Sourcing
  // the repeat from the page under test is the honest version anyway: an app stuck in a broken render
  // loop is the real case for collapsing, whereas the tool repeating itself was a defect.
  await check('a repeated line reads as ONE row carrying a count', async () => {
   await withRun(faultRun, async () => {
    await setLevels(['error', 'warn', 'info', 'debug']);
    const badges = page.locator('#lg-list .lgn');
    ok(await badges.count() > 0,
      'no ×N badge: the burst l7 produces is being rendered as N separate rows');
    const label = await badges.first().innerText();
    const n = parseInt(label.replace(/[^0-9]/g, ''), 10);
    ok(n >= 2, `a count badge must mean 2 or more, got ${label}`);

    // The collapsed record must be ONE row, not N. Anchored on the burst's own text so the assertion
    // names what it is about, instead of a phrase that happened to be adjacent to it.
    const burst = page.locator('#lg-list .lgrow:not(.child)', { hasText: 'sku' });
    eq(await burst.count(), 1, 'the 8-line burst must render as exactly one row');

    // And the general invariant behind it, which no fixture change can quietly make vacuous: if
    // consecutive duplicates are collapsed, no two ADJACENT rows can read identically.
    const texts = await page.locator('#lg-list .lgrow:not(.child) .lgmsg').allInnerTexts();
    ok(texts.length > 3, `too few rows to say anything about adjacency: ${texts.length}`);
    for (let i = 1; i < texts.length; i++) {
      ok(texts[i].trim() !== texts[i - 1].trim(),
        `rows ${i - 1} and ${i} read identically — consecutive duplicates were not collapsed: ${texts[i].slice(0, 60)}`);
    }
   });
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

  /* ---------------------------------------------------------------- ADR-067/068: source, register, language */
  await check('filter language: the grammar means what it says', async () => {
    // Evaluated INSIDE the shipped page, so this tests the parser that actually runs — not a copy.
    const bad = await page.evaluate(() => {
      const R = (o) => Object.assign({ lvl:'info', cat:'run', src:'tool', mod:'brain.x', code:'a.b',
        msg:'hello world', step:0, n:1, ts:'2026-07-25T10:00:00Z' }, o);
      const cases = [
        ['', R({}), true],
        ['lvl >= warn', R({lvl:'warn'}), true],
        ['lvl >= warn', R({lvl:'info'}), false],
        ['src == application', R({src:'application'}), true],
        ['src == application', R({src:'tool'}), false],
        ['step == 4', R({step:4}), true],
        ['step > 2', R({step:3}), true],
        ['cat in {llm, plan}', R({cat:'plan'}), true],
        ['cat in {llm, plan}', R({cat:'heal'}), false],
        ['msg ~ /wor.d/', R({}), true],
        ['msg contains WORLD', R({}), true],
        ['!(cat == run)', R({}), false],
        ['lvl >= warn && src == application', R({lvl:'error', src:'tool'}), false],
        ['cat == run || cat == heal', R({}), true],
        ['lvl in {}', R({}), false],
      ];
      const out = [];
      for (const [expr, rec, want] of cases) {
        const c = lgCompile(expr);
        if (!c.ok) { out.push(`${expr} -> parse error: ${c.msg}`); continue; }
        if (c.pred(rec) !== want) out.push(`${expr} -> got ${!want}, want ${want}`);
      }
      // Malformed input must be REFUSED, with a message. Silently accepting it would filter wrongly.
      for (const expr of ['nosuch == 1', 'cat ==', '(cat == run', 'msg ~ /[/', 'lvl >= loud',
                          'step == abc', 'cat == run xyz']) {
        const c = lgCompile(expr);
        if (c.ok) out.push(`${expr} -> accepted, should be refused`);
        else if (!c.msg || !c.en) out.push(`${expr} -> refused without a bilingual message`);
      }
      return out;
    });
    ok(bad.length === 0, `grammar failures:\n       ${bad.join('\n       ')}`);
  });

  await check('filter language: an invalid expression warns without blanking the view', async () => {
    const before = await rows().count();
    await page.fill('#lg-expr', 'lvl >= warn && (');
    await page.waitForTimeout(250);
    ok(await page.locator('#lg-expr.bad').count() === 1, 'an invalid expression must show as invalid');
    ok(await page.locator('#lg-exprmsg.bad').count() === 1, 'and must say what is wrong');
    ok(await rows().count() > 0, 'an invalid expression blanked the view instead of holding the last good filter');
    await page.fill('#lg-expr', '');
    await page.waitForTimeout(250);
    eq(await rows().count(), before, 'clearing the expression did not restore the view');
  });

  await check('controls WRITE the expression rather than filtering separately', async () => {
    // The promise this design makes: whatever a click did is visible as text you can read and paste.
    // Levels are restored first: an earlier check leaves them narrowed, and this fixture emits no
    // warnings at all, so inheriting that state would blank the view for reasons unrelated to the claim.
    await setLevels(['error', 'warn', 'info', 'debug']);
    await page.click('#lg-expr-clear');
    await page.waitForTimeout(200);
    await page.uncheck('#lg-debug');
    await page.uncheck('#lg-info');
    await page.waitForTimeout(250);
    const expr = await page.locator('#lg-expr').inputValue();
    ok(/lvl\s*>=\s*warn/.test(expr),
      `unchecking INFO/DEBUG should read as a threshold, got: ${expr}`);
    await page.selectOption('#lg-src', 'application');
    await page.waitForTimeout(250);
    const expr2 = await page.locator('#lg-expr').inputValue();
    ok(/src\s*==\s*application/.test(expr2), `the source dropdown did not write a clause: ${expr2}`);
    ok(/&&/.test(expr2), `clauses must combine with &&: ${expr2}`);

    // The clause must FILTER, not merely describe. Shown by discrimination rather than by "more rows":
    // this fixture behaves, so it emits nothing from the application, and a level threshold may already
    // have left nothing to count — an earlier version of this check asserted "more" and failed for that
    // reason rather than for a defect.
    await setLevels(['error', 'warn', 'info', 'debug']);
    await page.selectOption('#lg-src', '');
    await page.waitForTimeout(250);
    const all = await rows().count();
    ok(all > 0, 'no rows with every level on — the fixture produced nothing');
    await page.selectOption('#lg-src', 'tool');
    await page.waitForTimeout(250);
    const tool = await rows().count();
    await page.selectOption('#lg-src', 'application');
    await page.waitForTimeout(250);
    const app = await rows().count();
    ok(tool > 0, 'filtering to the tool hid the tool\'s own diagnostics');
    ok(app < tool, `src must discriminate: tool=${tool}, application=${app}, all=${all}`);
    await page.selectOption('#lg-src', '');
    await page.waitForTimeout(250);
    eq(await rows().count(), all, 'clearing the source did not restore the view');
  });

  await check('hand-editing the expression says so instead of letting controls lie', async () => {
    await page.fill('#lg-expr', 'cat == browser');
    await page.waitForTimeout(250);
    ok(await page.locator('#lg-custom').isVisible(),
      'a hand-written expression must be announced — otherwise the dropdowns claim to describe it');
    ok(await page.locator('#lg-src').isDisabled(), 'controls that cannot describe the filter must be disabled');
    await page.click('#lg-expr-clear');
    await page.waitForTimeout(250);
    ok(!(await page.locator('#lg-custom').isVisible()), 'clearing must hand control back');
    ok(!(await page.locator('#lg-src').isDisabled()), 'clearing must re-enable the controls');
  });

  await check('clicking a cell filters by it', async () => {
    await setLevels(['error', 'warn', 'info', 'debug']);
    await page.click('#lg-expr-clear');
    await page.waitForTimeout(200);
    const cell = page.locator('#lg-list .lgclick[data-f="cat"]').first();
    const val = await cell.getAttribute('data-v');
    await cell.click();
    await page.waitForTimeout(250);
    const expr = await page.locator('#lg-expr').inputValue();
    ok(expr.indexOf('cat == ' + val) >= 0, `clicking a category cell should add it: got ${expr}`);
    const shown = await rows().count();
    ok(shown > 0, 'clicking a cell filtered everything away');
    await page.click('#lg-expr-clear');
    await page.waitForTimeout(200);
  });

  // ADR-068 rev.2: «подробно» is a level of DETAIL, not a second register. The old check asserted that
  // the two registers read DIFFERENTLY, which a pair of unrelated wordings satisfies trivially. Detail
  // has to be additive instead: same rows, same order, the plain sentence still there, plus the
  // machine identity. That is a strictly stronger claim and it is what a reader relies on.
  await check('«подробно» adds the machine identity without changing which rows there are', async () => {
    await setLevels(['error', 'warn', 'info', 'debug']);
    await page.click('#lg-expr-clear');
    await page.waitForTimeout(200);
    const n1 = await rows().count();
    const plain = await page.locator('#lg-list .lgrow:not(.child)').first().innerText();
    await page.check('#lg-detail');
    await page.waitForTimeout(300);
    const n2 = await rows().count();
    const detailed = await page.locator('#lg-list .lgrow:not(.child)').first().innerText();
    eq(n2, n1, 'turning on detail changed the number of rows');
    ok(/\d{4}-\d{2}-\d{2}T/.test(detailed), `detail must show the full timestamp: ${detailed}`);
    ok(/[a-z]+\.[a-z_]+/.test(detailed), 'detail must show the event code');
    ok(/\bsrc=/.test(detailed), 'detail must name the source the row came from');
    // Additive, not a swap: the sentence the reader was already reading has to survive. Compared on
    // the message text alone — the timestamp and the level chip legitimately change form.
    const sentence = plain.split('\n').pop().trim().replace(/\s*×\d+\s*$/, '');
    const core = sentence.replace(/^(шаг|step)\s+\d+\s*/, '').slice(0, 40);
    ok(core.length > 3 && detailed.indexOf(core) >= 0,
      `detail dropped the plain sentence: looked for "${core}" in "${detailed}"`);
    await page.uncheck('#lg-detail');
    await page.waitForTimeout(300);
  });

  // The coarse axis a tester picks first. An audience is not a source — it stands for the set of them,
  // so it must filter to the UNION and never to nothing, which is what a plain string compare would do.
  //
  // Run against l7-appfaults, NOT the default l2: l2 behaves, so it emits nothing from the application
  // and every set identity below would hold over empty sets. Mutation-proven — with the expansion torn
  // out, `src == business` matched 0 and `application || testing` matched 0, and 0 == 0 passed.
  await check('an audience filters to the sources it contains, not to its own name', async () => {
   await withRun(faultRun, async () => {
    await setLevels(['error', 'warn', 'info', 'debug']);
    await page.click('#lg-expr-clear');
    await page.waitForTimeout(250);
    const all = await rows().count();
    ok(all > 0, 'no rows with every level on — the misbehaving fixture produced nothing');

    // The dropdown must offer the groups, and the group heading itself must be selectable.
    const groups = await page.locator('#lg-src optgroup').count();
    ok(groups >= 2, `expected the audience optgroups, got ${groups}`);
    await page.selectOption('#lg-src', 'tool');
    await page.waitForTimeout(250);
    ok(await rows().count() > 0, 'the tool audience hid the tool\'s own diagnostics');

    // Set equality, established through the expression rather than through the control: business must
    // select exactly what application ∪ testing does, and `in {business}` must agree with `== business`.
    const countFor = async (expr) => {
      await page.fill('#lg-expr', expr);
      await page.waitForTimeout(250);
      ok(!((await page.locator('#lg-expr').getAttribute('class')) || '').includes('bad'),
        `expression rejected by the parser: ${expr}`);
      return rows().count();
    };
    // NON-VACUITY FIRST. Every identity below is trivially true over empty sets, so the check has to
    // establish that the sets are non-empty before it may claim they are equal.
    const application = await countFor('src == application');
    ok(application > 0,
      'l7-appfaults produced no application-side records — the audience identities below would be ' +
      'vacuous, so this is a gate failure, not a fixture quirk');

    const business = await countFor('src == business');
    const union = await countFor('src == application || src == testing');
    const inForm = await countFor('src in {business}');
    const negated = await countFor('src != business');
    ok(business > 0, `business must be non-empty for the identities to mean anything: ${business}`);
    ok(business >= application, `business must contain application: business=${business}, application=${application}`);
    eq(business, union, 'src == business must equal application ∪ testing');
    eq(inForm, business, '`in {business}` and `== business` must agree');
    eq(business + negated, all, 'an audience and its negation must partition the records');
    ok(business < all, `business must exclude the tool's own logs: business=${business}, all=${all}`);

    await page.click('#lg-expr-clear');
    await page.waitForTimeout(200);
   });
  });

  /* ------------------------------------------------- HEALTH-005 PR-B: the service journal view */
  // The stream that had no reader at all until this PR. What is checked is that it reads a DIFFERENT
  // file from Logs and answers about the tool rather than about a run — a view that silently rendered
  // the run log again would look perfectly correct on screen.
  const svRows = () => page.locator('#sv-list .lgrow:not(.child)');

  await check('journal: the service journal has its own view and shows the tool\'s own events', async () => {
    await page.click('.rail a[data-nav="journal"]');
    await page.waitForTimeout(300);
    ok(await page.locator('#sv-list').isVisible(), 'the journal view did not open');
    ok(!(await page.locator('#sv-err').isVisible()),
      `the error box is visible: ${await page.locator('#sv-err').innerText()}`);
    await page.click('#sv-reload');
    await page.waitForTimeout(800);
    const n = await svRows().count();
    ok(n > 0, 'the journal is empty — this server has been signing requests in and out throughout this gate');

    // It is the SERVICE stream, not the run stream rendered twice. Every row must name a writer, and
    // the codes must be service.* — a view accidentally wired to /v1/runs/{id}/logs would still be
    // full of plausible rows.
    const text = await page.locator('#sv-list').innerText();
    ok(/service\./.test(text), `no service.* code in the journal view: ${text.slice(0, 300)}`);
    ok(!/run\.(started|finished)/.test(text), 'a RUN event is being rendered in the service journal');
  });

  await check('journal: the level control changes what is shown, and does it server-side', async () => {
    await page.click('.rail a[data-nav="journal"]');
    await page.waitForTimeout(200);
    await page.selectOption('#sv-lvl', 'debug');
    await page.waitForTimeout(900);
    const withDebug = await svRows().count();
    await page.selectOption('#sv-lvl', 'error');
    await page.waitForTimeout(900);
    const errorsOnly = await svRows().count();
    ok(withDebug > errorsOnly,
      `the level control changed nothing (${withDebug} -> ${errorsOnly}) — it is decorative`);
    await page.selectOption('#sv-lvl', 'info');
    await page.waitForTimeout(700);
  });

  await check('journal: the writer facet is built from the journal, not from a hand-kept list', async () => {
    // Asserting that "control-api" is offered would not distinguish the two — a hard-coded list would
    // contain it too. So a writer NO list could have been written with is appended to the journal and
    // the facet is required to have learned it. This is the property that makes the browser service
    // (PR-C) appear in the filter without an edit here.
    const journal = path.join(REPO, 'state', 'logs', 'service.jsonl');
    const novel = 'gate-injected-writer';
    fs.appendFileSync(journal, JSON.stringify({
      seq: 0, ts: new Date().toISOString(), lvl: 'info', cat: 'service',
      code: 'service.started', msg: 'a writer this page has never heard of', svc: novel,
    }) + '\n');

    await page.click('.rail a[data-nav="journal"]');
    await page.waitForTimeout(200);
    await page.click('#sv-reload');
    await page.waitForTimeout(900);

    const opts = await page.locator('#sv-svc option').allTextContents();
    ok(opts.some((o) => o.includes('control-api')),
      `control-api wrote this journal and is not in the writer list: ${JSON.stringify(opts)}`);
    ok(opts.some((o) => o.includes(novel)),
      `the writer list did not learn ${novel} from the journal — it is a hand-kept list: ${JSON.stringify(opts)}`);

    // And selecting it must actually narrow, or the facet is decorative.
    await page.selectOption('#sv-svc', novel);
    await page.waitForTimeout(800);
    const only = await svRows().count();
    eq(only, 1, `filtering by ${novel} returned ${only} rows, want exactly the one record it wrote`);
    await page.selectOption('#sv-svc', '');
    await page.waitForTimeout(700);
  });

  // M9-LIVE fix: the silent downgrade. The most consequential field in the form used to hide behind a
  // summary reading "⚙ Budgets, auth, model", and an operator who never expanded it launched a goal-mode
  // run that quietly became a heuristic one. Both halves are checked: the block must OPEN itself, and the
  // warning must appear — telling someone a field is empty while keeping it hidden is worse than silence.
  await check('choosing a planner that needs an LLM warns, and opens the block holding the fields', async () => {
    await page.click('.rail a[data-nav="run"]');
    await page.waitForTimeout(200);
    const warn = page.locator('#b-llmwarn');
    const adv = page.locator('#b-adv');

    // Baseline: heuristic + no model is a perfectly valid combination and must stay quiet, or the warning
    // becomes wallpaper and stops being read.
    await page.selectOption('#b-planner', 'heuristic');
    await page.selectOption('#b-backend', '');
    // Cleared through the DOM, not `fill`: #b-baseurl lives inside #b-urlWrap, which is display:none until
    // the backend is openai-compat, so `fill` would wait forever for a field that is correctly invisible.
    // (The first draft of this check did exactly that and hung the gate.)
    await page.evaluate(() => {
      const el = document.getElementById('b-baseurl');
      if (el) { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }
    });
    await page.waitForTimeout(250);
    ok(!(await warn.isVisible()), 'a heuristic run with no model must not warn');

    await page.selectOption('#b-planner', 'goal');
    await page.waitForTimeout(250);
    ok(await warn.isVisible(), 'goal without a model must warn about the silent fallback to heuristic');
    const text = await warn.innerText();
    ok(/эвристик|heuristic/i.test(text), `the warning must name what will actually happen: ${text}`);
    eq(await adv.evaluate((e) => e.open), true, 'the block holding the model fields must open itself');

    // Configuring a model clears it. `anthropic` needs no base_url — a built-in default model is a real
    // choice, and demanding a URL for it would train the operator to ignore the warning.
    await page.selectOption('#b-backend', 'anthropic');
    await page.waitForTimeout(250);
    ok(!(await warn.isVisible()), `configuring anthropic must clear the warning, got: ${await warn.innerText()}`);

    // openai-compat DOES need a base_url, and the warning must come back until it has one.
    await page.selectOption('#b-backend', 'openai');
    await page.waitForTimeout(250);
    ok(await warn.isVisible(), 'openai-compat without a base_url must warn');
    await page.fill('#b-baseurl', 'http://127.0.0.1:11434/v1');
    await page.waitForTimeout(250);
    ok(!(await warn.isVisible()), 'a base_url must clear it');

    // The mirror mistake is just as quiet: a model configured that the planner will never use.
    await page.selectOption('#b-planner', 'heuristic');
    await page.waitForTimeout(250);
    ok(await warn.isVisible(), 'a configured model with a heuristic planner must say the LLM is unused');
  });

  // M9-LIVE fix: /readyz has probed the LLM since ADR-062 and nothing showed it. The three states must
  // stay distinct — `skipped` (nothing configured, heuristic may be intended) vs `error` (configured and
  // unreachable, almost never intended) are different news, and one red dot for both rebuilds the
  // ambiguity this fixes.
  await check('the rail says whether the LLM is connected, and distinguishes «not configured» from «broken»', async () => {
    const host = page.locator('#rail-llm');
    ok(await host.count() === 1, 'the rail must carry an LLM state indicator');
    // This gate's control-API runs with no LLM configured, so the honest answer is `skipped`.
    await page.waitForTimeout(500);
    const txt = (await host.innerText()).trim();
    ok(/LLM/.test(txt), `the indicator must be labelled: ${txt}`);
    const title = await host.getAttribute('title');
    ok(/не настроена|not configured/i.test(title || ''),
      `with no LLM configured the tooltip must say so rather than claim a failure: ${title}`);
    const cls = await host.locator('.dot').getAttribute('class');
    ok(!/\bno\b/.test(cls || ''),
      `"not configured" must NOT render as the red/error dot: ${cls}`);
    // And the class must be one the stylesheet actually defines — inventing a class name would leave the
    // dot permanently grey, a silent no-op.
    const defined = await page.evaluate(() => {
      const out = [];
      for (const sheet of document.styleSheets) {
        let rules; try { rules = sheet.cssRules; } catch { continue; }
        for (const r of rules) if (r.selectorText && /\.dot\./.test(r.selectorText)) out.push(r.selectorText);
      }
      return out.join(' ');
    });
    ok(/\.dot\.ok/.test(defined) && /\.dot\.no/.test(defined),
      `the dot state classes must exist in the stylesheet: ${defined}`);
  });

  /* ------------------------------------------------- ADR-076: verdict states an exit code cannot carry
     brain distinguishes pass_with_drift / pass_with_app_faults / problem_drift / problem_app_faults
     (ADR-071/072); until now the badge read the exit code alone, so a pass that survived only on repairs
     and a clean pass drew the same green tick.

     Driven through the REAL path — a real POST /v1/runs, a real stream, a real artifact fetch — against
     a second control-API whose `agentctl` is a stub that writes the report we want to render. Driving it
     from a live replay was tried first and is not dependable here: an explore run on the fault fixture
     does not reliably reach plan freeze inside the gate's window, so there is nothing to replay from,
     and a replay of the well-behaved fixture produces none of the states under test. */
  await check('the verdict tells the whole truth: refined state, drift, app faults, degradation, and no replay without a plan', async () => {
    const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-verdict-gate-'));
    const stub = path.join(stubDir, 'agentctl-stub.mjs');
    // Writes the heal-report the hub will fetch, then exits 0 — the shape brain/replay.py produces.
    fs.writeFileSync(stub, [
      "import fs from 'node:fs'; import path from 'node:path';",
      "const a = process.argv; const dir = a[a.indexOf('--artifact-dir') + 1];",
      "fs.mkdirSync(dir, { recursive: true });",
      "fs.writeFileSync(path.join(dir, 'heal-report.json'), JSON.stringify({",
      "  plan_id: 'gate', mode: 'replay', exit_code: 0, verdict: 'pass_with_drift', healed: 3, failed: 0,",
      "  drift: { rebind: 2, reground: 1, elements: [",
      "    { step: 1, name: 'Login', kind: 'rebind', strategy: 'role', confidence: 0.9 },",
      "    { step: 2, name: 'Submit', kind: 'reground', strategy: 'css', confidence: 0.7 }] },",
      "  app_faults: { counts: { 'app.js_error': 9, 'app.http_error': 4 }, total: 13, errors: 12 }",
      "}));",
      // A real degrading line on the run's own stdout: control-api's log sink classifies it against the
      // embedded catalogue exactly as it would from brain, so the badge below is fed by the real path
      // rather than by a fixture the page was handed. Deliberately NOT accompanied by plan.json —
      // this stub is also the "run that died before plan freeze" case the re-run controls must refuse.
      "console.log('[warn|llm] llm.no_anthropic_key: No AI key (planner)');",
      "console.log('stub run complete');",
    ].join('\n'));
    // A shim so the stub runs under node without control-api needing to know that. It lives in the temp
    // dir, not in scripts/ — a gate must leave no files in the repository.
    const shim = path.join(stubDir, 'agentctl');
    fs.writeFileSync(shim, `#!/bin/sh\nexec "${process.execPath}" "${stub}" "$@"\n`, { mode: 0o755 });

    const vPort = PORT + 3;
    const capi2 = spawn(path.join(REPO, 'bin', 'control-api'), [], {
      cwd: stubDir,   // so runs/ and state/ land in the temp dir
      env: { ...process.env,
             CONTROL_API_ADDR: `127.0.0.1:${vPort}`, CONTROL_API_TOKEN: 'verdict-gate-token',
             CONTROL_API_SERVE_UI: '1', CONTROL_API_UI_DIR: path.join(REPO, 'docs'),
             CONTROL_API_AGENTCTL: shim,
             CONTROL_API_CORS_ORIGINS: '', CONTROL_API_STORE_ADDR: '', LLM_BASE_URL: '' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const vPage = await context.newPage();
    const vErrors = [];
    // The hub asks for every artifact a run MIGHT have produced and renders whatever came back; the stub
    // writes only heal-report.json, so the rest answer 404 and the browser logs each one. That is the
    // product behaving correctly, not a defect — but it is only allowed to be a 404 on the artifact
    // endpoint, which the response listener below proves rather than assumes.
    const notFound = [];
    vPage.on('response', (r) => { if (r.status() === 404) notFound.push(r.url()); });
    vPage.on('pageerror', (e) => vErrors.push(e.message));
    vPage.on('console', (m) => {
      if (m.type() !== 'error') return;
      if (/status of 404/.test(m.text())) return;
      vErrors.push(`console: ${m.text()}`);
    });
    try {
      for (let i = 0; i < 100; i++) {
        try { if ((await fetch(`http://127.0.0.1:${vPort}/healthz`)).ok) break; } catch { /* not up */ }
        await new Promise((r) => setTimeout(r, 100));
      }
      await vPage.goto(`http://127.0.0.1:${vPort}/#v=settings`, { waitUntil: 'load' });
      await vPage.waitForTimeout(400);
      await vPage.fill('#capi', `http://127.0.0.1:${vPort}`);
      await vPage.fill('#capitok', 'verdict-gate-token');
      await vPage.click('.rail a[data-nav="run"]');
      await vPage.fill('#b-target', 'file:///app/x.html');
      await vPage.click('#b-run');
      await vPage.waitForFunction(
        () => (document.getElementById('b-verdict').textContent || '').length > 0, null, { timeout: 30000 });

      const head = await vPage.locator('#b-verdict-head').textContent();
      ok(head.length > 0, 'the verdict badge rendered no headline');
      ok(/УЕХАЛ|DRIFTED/.test(head),
        `a pass_with_drift run drew the plain PASSED badge instead: ${head}`);
      ok(/exit 0/.test(head), `the exit code disappeared from the badge: ${head}`);

      // Drift and faults are reported SEPARATELY even though only drift named the verdict.
      const drift = vPage.locator('#b-verdict-drift');
      ok(await drift.count() === 1, 'no drift detail block');
      const dtext = await drift.textContent();
      ok(/3/.test(dtext), `drift block does not carry the total: ${dtext}`);
      ok(/Login/.test(dtext) && /Submit/.test(dtext),
        `drift block does not expand into the elements that moved: ${dtext}`);
      const faults = vPage.locator('#b-verdict-faults');
      ok(await faults.count() === 1, 'application faults were swallowed because drift named the verdict');
      const ftext = await faults.textContent();
      ok(/13/.test(ftext) && /12/.test(ftext), `fault block lost total/errors: ${ftext}`);

      // ADR-077: degradation that ALREADY HAPPENED, on the verdict. The pre-run guard cannot know about
      // it; before this the fact lived only in a log file nobody opens when the build is green.
      const degr = vPage.locator('#b-verdict-degraded');
      ok(await degr.count() === 1, 'a run that logged a degrading event drew no degradation notice');
      const dtxt = await degr.textContent();
      ok(dtxt.length > 0, 'the degradation notice is empty');
      // The catalogue's verdict SENTENCE, not the code: a reader is asking what it means for the result.
      ok(!/llm\.no_anthropic_key/.test(dtxt),
        `the notice shows the raw code instead of the catalogue sentence: ${dtxt}`);
      ok(/ИИ|AI/.test(dtxt), `the notice does not say what was lost: ${dtxt}`);

      // ADR-047 follow-on: this stub froze no plan, so re-run/baseline must refuse BEFORE the click and
      // say why — pressing them used to answer `400 from_run: no replayable plan`.
      ok(await vPage.locator('#b-rerun').isDisabled(), '🔁 is enabled on a run that left no plan');
      ok(await vPage.locator('#b-baseline').isDisabled(), '📌 is enabled on a run that left no plan');
      const why = await vPage.locator('#b-noplan').textContent();
      ok(why && why.trim().length > 0, 'the controls are greyed out with no reason given');
      ok(/плана|plan/.test(why), `the reason does not mention the missing plan: ${why}`);
      // This check passed for months while the note rendered `<span data-lang="ru">прогон не оставил
      // плана…` verbatim on screen: the substring it looks for was present INSIDE the markup. Caught
      // by a screenshot, not by an assertion — so the assertion now covers the form as well.
      ok(!/<span|data-lang=/i.test(why),
        `the no-plan note is printing its markup instead of rendering it: ${why}`);
      const tip = await vPage.locator('#b-rerun').getAttribute('title');
      ok(!/<span|data-lang=/i.test(tip || ''),
        `the 🔁 tooltip carries markup a title attribute cannot render: ${tip}`);

      ok(vErrors.length === 0, `verdict page error(s): ${vErrors.join(' | ')}`);
      const unexpected404 = notFound.filter((u) => !/\/artifact\?name=/.test(u));
      ok(unexpected404.length === 0, `404 outside the artifact endpoint: ${unexpected404.join(' | ')}`);
    } finally {
      await vPage.close();
      capi2.kill('SIGKILL');
      fs.rmSync(stubDir, { recursive: true, force: true });
    }
  });

  /* ------------------------------------------------------- HEALTH-004: "we broke" vs "your app broke"
     The photographed defect. HEALTH-001 gave a refusal-to-start exit 3; exit 3 already meant
     `integrity`; so a run refused because the MODEL ENDPOINT was unreachable rendered as
     «ЦЕЛОСТНОСТЬ / КОНФИГУРАЦИЯ · несовпадение plan_hash/golden — нужен человек» — sending the operator
     to inspect a plan_hash that was never involved, while the honest sentence sat in the log below it.

     Same harness as the ADR-076 check above and deliberately a SEPARATE stub: this run must end the way
     a real refusal ends — the catalogued fatal line on stdout, exit 3, and NO report artifact, because
     a run that refused to start produces none. That absence matters: it proves the badge is fed by the
     run's own log rather than by a report the failing path never writes. */
  await check('a run refused because OUR component is down says the TOOL broke, not that a plan_hash mismatched', async () => {
    const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentinel-fault-gate-'));
    const stub = path.join(stubDir, 'agentctl-stub.mjs');
    fs.writeFileSync(stub, [
      // The literal line brain/health.py emits before returning 3. Rendered by control-api's log sink
      // against the embedded catalogue, which is where the fault comes from — no fixture is handed to
      // the page.
      "console.log('[error|llm] fatal.llm_required_unreachable: This mode needs a model and there is " +
        "none: no LLM backend. Without one the goal would be silently ignored');",
      'process.exit(3);',
    ].join('\n'));
    const shim = path.join(stubDir, 'agentctl');
    fs.writeFileSync(shim, `#!/bin/sh\nexec "${process.execPath}" "${stub}" "$@"\n`, { mode: 0o755 });

    const fPort = PORT + 4;
    const capi3 = spawn(path.join(REPO, 'bin', 'control-api'), [], {
      cwd: stubDir,
      env: { ...process.env,
             CONTROL_API_ADDR: `127.0.0.1:${fPort}`, CONTROL_API_TOKEN: 'fault-gate-token',
             CONTROL_API_SERVE_UI: '1', CONTROL_API_UI_DIR: path.join(REPO, 'docs'),
             CONTROL_API_AGENTCTL: shim,
             CONTROL_API_CORS_ORIGINS: '', CONTROL_API_STORE_ADDR: '', LLM_BASE_URL: '' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const fPage = await context.newPage();
    try {
      for (let i = 0; i < 100; i++) {
        try { if ((await fetch(`http://127.0.0.1:${fPort}/healthz`)).ok) break; } catch { /* not up */ }
        await new Promise((r) => setTimeout(r, 100));
      }
      await fPage.goto(`http://127.0.0.1:${fPort}/#v=settings`, { waitUntil: 'load' });
      await fPage.waitForTimeout(400);
      await fPage.fill('#capi', `http://127.0.0.1:${fPort}`);
      await fPage.fill('#capitok', 'fault-gate-token');
      await fPage.click('.rail a[data-nav="run"]');
      await fPage.fill('#b-target', 'file:///app/x.html');
      await fPage.click('#b-run');
      await fPage.waitForFunction(
        () => (document.getElementById('b-verdict').textContent || '').length > 0, null, { timeout: 30000 });

      // The chip: present, and attributed to us. Read from the attribute rather than the label so the
      // assertion survives translation and cannot be satisfied by a coincidence of wording.
      const chip = fPage.locator('#b-verdict-fault');
      ok(await chip.count() === 1,
        'a run refused because a required component was unreachable drew no fault attribution at all');
      eq(await chip.getAttribute('data-fault'), 'tool',
        'the refusal was attributed to something other than the tool');
      const chipText = (await chip.textContent()).trim();
      ok(chipText.length > 0, 'the fault chip is empty — an attribution nobody can read is not one');
      // `bi()` RETURNS MARKUP, so escaping its output prints the tags at the reader. The first version
      // of this chip did exactly that and the ui-smoke screenshot caught it — the badge read
      // `<SPAN DATA-LANG="RU">ИНСТРУМЕНТ</SPAN>…`. Asserted over the whole badge because the same
      // mistake was ALSO sitting on the no-plan note next to it, unnoticed, in the same picture.
      const badgeText = await fPage.locator('#b-verdict').textContent();
      ok(!/<span|data-lang=|&lt;span/i.test(badgeText),
        `the verdict area is printing raw markup instead of rendering it: ${badgeText.slice(0, 200)}`);

      // The SECOND wrong sentence in the same picture. `llm.no_anthropic_key` fires before health.py
      // refuses, so the degradation notice sat under the verdict claiming «Прогон прошёл с потерей
      // качества» about a run that never began. The facts stay — only the headline's claim of having
      // run goes away.
      const degr = fPage.locator('#b-verdict-degraded');
      if (await degr.count() === 1) {
        const dtxt = await degr.textContent();
        ok(!/Прогон прошёл с потерей|run completed with degraded/i.test(dtxt),
          `a run that was refused at startup still claims it completed: ${dtxt.slice(0, 160)}`);
        ok(dtxt.trim().length > 0, 'the degradation notice went empty instead of changing its headline');
      }

      // The sentence that was wrong. This is the whole point of the change, so it is asserted by its
      // ABSENCE on the rendered badge, not by the presence of a new phrasing we happen to prefer today.
      const badge = await fPage.locator('#b-verdict').textContent();
      ok(!/plan_hash/.test(badge),
        `the badge still blames plan_hash on a run where no plan was involved: ${badge}`);
      ok(!/golden/i.test(badge),
        `the badge still blames the golden baseline on a refusal to start: ${badge}`);
      // And it must say something instead — an empty explanation would also pass the two lines above.
      ok(/инструмент|tool/i.test(badge),
        `the badge does not say the tool is what broke: ${badge}`);
      ok(/exit 3/.test(badge), `the exit code disappeared from the badge: ${badge}`);
    } finally {
      await fPage.close();
      capi3.kill('SIGKILL');
      fs.rmSync(stubDir, { recursive: true, force: true });
    }
  });

  // ADR-078. Live runs went out as file:///D:/Projects/... — the operator's HOST path, invisible to the
  // container — and died with ERR_FILE_NOT_FOUND and exit 1 without saying which problem the test found.
  // Both target fields are checked: there are two run paths, and fixing one would have left the chat
  // path crashing exactly as before.
  await check('a file:// target outside the container warns before the run, on BOTH target fields', async () => {
    await page.click('.rail a[data-nav="run"]');
    await page.waitForTimeout(200);

    const warnFor = async (id, box, value) => {
      await page.fill(id, value);
      await page.waitForTimeout(120);
      const el = page.locator(box);
      return (await el.isVisible()) ? (await el.textContent()) : '';
    };

    ok(await warnFor('#b-target', '#b-targetwarn', 'file:///D:/Projects/sentinel/testdata/l1.html'),
      'a Windows host path produced no warning — this is the exact shape that failed live');
    const win = await page.locator('#b-targetwarn').textContent();
    ok(/\/app\//.test(win), `the warning does not say what a correct path looks like: ${win}`);

    eq(await warnFor('#b-target', '#b-targetwarn', 'file:///app/testdata/fixtures/l2.html'), '',
      'the in-container path was warned about — that is the CORRECT form');
    eq(await warnFor('#b-target', '#b-targetwarn', 'https://app.example'), '',
      'an http target was warned about');
    // A non-Windows host path is just as invisible to the container.
    ok(await warnFor('#b-target', '#b-targetwarn', 'file:///home/alex/site/index.html'),
      'a POSIX host path produced no warning');

    // The bundled fixtures are offered, and the list is the one that EXISTS on disk.
    const opts = await page.locator('#fixtures option').evaluateAll((os_) => os_.map((o) => o.value));
    ok(opts.length > 0, 'no fixture suggestions at all');
    ok(opts.every((v) => v.startsWith('file:///app/testdata/fixtures/')),
      `a suggestion is not an in-container path: ${opts.join(' ')}`);
    ok(opts.some((v) => /l7-appfaults\.html$/.test(v)), 'the fault fixture is missing from the list');
    ok(!opts.some((v) => /\/l6\.html$/.test(v)), 'the list offers l6.html, which does not exist on disk');
    eq(await page.locator('#b-target').getAttribute('list'), 'fixtures', 'the target field is not wired to the list');

    // The chat view has its own target field and its own run path.
    await page.click('.rail a[data-nav="chat"]');
    await page.waitForTimeout(200);
    eq(await page.locator('#ch-target').getAttribute('list'), 'fixtures', 'the chat target field is not wired to the list');
    ok(await warnFor('#ch-target', '#ch-targetwarn', 'file:///C:/Users/alex/app.html'),
      'the chat target field does not warn — fixing only the build form leaves this path crashing');
    eq(await warnFor('#ch-target', '#ch-targetwarn', 'file:///app/testdata/fixtures/l2.html'), '',
      'the chat field warned about the correct in-container path');
    await page.click('.rail a[data-nav="run"]');
    await page.waitForTimeout(150);
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
  await check('perception: three categories, and they add up to what the audit measured', async () => {
    // ADR-097. Exercised through the real `perceptionBlock` in the real page — it is a pure function
    // of plan.json, so it needs no run, and asserting the rendered HTML is what an operator sees.
    const out = await page.evaluate(() => window.__gate.perceptionBlock({ perception: {
      worst_ratio: 0.444,
      pages: { '/p': { seen: 4, total: 9, ratio: 0.444, usable: 2, blocked: 2, no_role: 0,
                       unseen: { outside_selector: 5, iframe: 0 },
                       opaque: { canvas: 1, shadow_roots_closed: 1, frames_nested: 0, frames_unreachable: 0 } } },
    } }));
    ok(out.includes('44%'), 'the worst-page ratio must be shown');
    // Anchored on the SUMMARY. The body carries a glyph per category, so a document-wide search for
    // ⚠ is satisfied by the "cannot act" row and passes even when the headline says everything is
    // fine. Caught by mutation — forcing `partial = false` left this green. Fifth instance in this
    // repository of "assert the cell, not the document".
    const summaryOf = (h) => (h.match(/<summary>([\s\S]*?)<\/summary>/) || [,''])[1];
    ok(/⚠/.test(summaryOf(out)), 'a partly-seen page must SUMMARISE as a warning, not a tick');
    ok(!/✓/.test(summaryOf(out)), 'and it must not also carry a tick in the same summary');
    ok(/<b>2<\/b>\s*(<[^>]*>)*\s*видим и можем|>2</.test(out), 'the usable count must be rendered');
    ok(out.includes('5'), 'the unseen count must be rendered');
    // The opaque zones are named but NOT folded into the three counts — they cannot be counted, and a
    // guess in the denominator is the flattering number wearing a pessimistic coat.
    ok(out.includes('canvas'), 'an opaque zone must be named');
    ok(!/<b>9<\/b>/.test(out) || out.includes('44%'),
       'the breakdown must not silently absorb the opaque zones into a total');
  });

  await check('perception: "not measured" does not read as "all visible"', async () => {
    // ADR-092's own rule, and the defect it closed: an older executor returns ratio null, and a null
    // that renders like 100% re-creates exactly the reassuring number this work removed.
    const nul = await page.evaluate(() => window.__gate.perceptionBlock({ perception: {
      worst_ratio: null,
      pages: { '/p': { ratio: null, reason: 'executor does not support browser.perceptionAudit' } } } }));
    const full = await page.evaluate(() => window.__gate.perceptionBlock({ perception: {
      worst_ratio: 1,
      pages: { '/p': { seen: 9, total: 9, ratio: 1, usable: 9, blocked: 0, no_role: 0,
                       unseen: { outside_selector: 0, iframe: 0 }, opaque: {} } } } }));
    ok(nul.length > 0, 'an unmeasured page must still say something');
    ok(!nul.includes('100%'), 'not measured must never render as a percentage');
    ok(/не измерена|not measured/.test(nul), 'it has to say it was not measured, in words');
    ok(full.includes('100%'), 'a fully seen page shows 100%');
    ok(nul !== full, 'not measured and fully visible must not render identically');
    // Asserted on the SUMMARY, not on the block: the body carries a glyph per category, and a
    // document-wide search finds those too. (Caught by this check failing — the same "assert the
    // cell, not the document" trap this repository keeps stepping into.)
    const sum = (h) => (h.match(/<summary>([\s\S]*?)<\/summary>/) || [,''])[1];
    ok(/✓/.test(sum(full)) && !/⚠/.test(sum(full)), 'a fully seen page summarises as a tick');
    ok(!/⚠/.test(full), 'and with every category at zero, nothing inside wears a warning either');
  });

  await check('perception: absent block renders nothing rather than an empty shell', async () => {
    // A replay carries no perception (the audit runs on the explore path). An empty box with three
    // zeroes would read as "we saw nothing", which is a different and false claim.
    for (const plan of [{}, { perception: {} }, { perception: { pages: {} } }]) {
      const out = await page.evaluate((p) => window.__gate.perceptionBlock(p), plan);
      eq(out, '', `a plan without perception must render nothing, got: ${out.slice(0, 60)}`);
    }
  });

  /* ---------------- ADR-101: per-field help in the hub ----------------
     ADR-086 built this for the wizard and said so in its own scope ("17 hand-written fields of the
     WIZARD"). The hub — which is what GitHub Pages serves at the root — had none, so a reader of the
     published UI saw no help anywhere and concluded the mechanism did not exist. These checks are
     about what a person SEES, which is why they live here and not in Go. */

  await check('help: every run-panel field carries a folded marker, and it is a <details>', async () => {
    await page.goto('about:blank');
    await page.goto(`http://127.0.0.1:${PORT}/#build`, { waitUntil: 'load' });
    const got = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('details.fhelp').forEach((d) => out.push({
        key: d.getAttribute('data-help'),
        tag: d.tagName,
        open: d.open,
        marker: (d.querySelector('summary') || {}).textContent || '',
        body: (d.querySelector('.fhelp-body') || {}).textContent || '',
      }));
      return out;
    });
    // A count, not "at least one": a mechanism that attached to a single field would otherwise pass.
    ok(got.length >= 16, `expected 16+ helped fields, found ${got.length}`);
    for (const h of got) {
      eq(h.tag, 'DETAILS', `${h.key}: help must be <details> so keyboard/AT behaviour comes from the platform`);
      ok(h.open === false, `${h.key}: help must be FOLDED by default — a form where every field carries a paragraph is unreadable`);
      ok(h.marker.trim().startsWith('?'), `${h.key}: the marker must be the "?" affordance, got ${JSON.stringify(h.marker.slice(0, 12))}`);
      ok(h.body.length > 40, `${h.key}: help body is ${h.body.length} chars — too short to be an explanation`);
    }
  });

  await check('help: the text says what CHANGES, not a longer spelling of the label', async () => {
    // The failure this guards is the one ADR-086 names: "a hint that repeats the field name is the
    // same silence, only wordier". Asserted as three SPECIFIC facts, each of which contradicts what
    // the field name suggests — so a hint reduced to a restatement cannot satisfy them.
    const body = (key) => page.evaluate(
      (k) => (document.querySelector(`details.fhelp[data-help="${k}"] .fhelp-body [data-lang="ru"]`) || {}).textContent || '', key);

    const budget = await body('b-planbud');
    ok(/эвристик/i.test(budget),
      'plan_budget help must say the run DEGRADES to the heuristic rather than failing (brain/budget.py:3-11)');
    ok(/НЕ роняет|не роняет/.test(budget), 'plan_budget help must say running out does not fail the run');

    const ss = await body('b-ss');
    ok(/НЕАВТОРИЗОВАН/i.test(ss),
      'storage_state help must say a missing/corrupt file continues UNAUTHENTICATED (pw-executor/src/server.ts:384,387)');

    const steps = await body('b-maxsteps');
    ok(/НЕЗАВЕРШ/i.test(steps),
      'max_steps help must say reaching it ends the run UNFINISHED (brain/graph.py:349-350)');

    // And the generic form: no help may simply echo its own label.
    const echoes = await page.evaluate(() => {
      const bad = [];
      document.querySelectorAll('details.fhelp').forEach((d) => {
        const lab = d.closest('label');
        const labText = (lab ? lab.textContent : '').replace(d.textContent, '').trim();
        const bodyText = (d.querySelector('.fhelp-body') || {}).textContent || '';
        if (labText && bodyText.trim() === labText) bad.push(d.getAttribute('data-help'));
      });
      return bad;
    });
    eq(echoes.length, 0, `help that only restates its label: ${echoes.join(', ')}`);
  });

  await check('help: bilingual in the DOM, so the language toggle carries it', async () => {
    // ADR-086 records falling into this exact trap once: building the string with tr() freezes the
    // language of the moment, and the toggle leaves those blocks behind. Both variants must be
    // present in the DOM at all times, including the marker's accessible name.
    const both = await page.evaluate(() => {
      const d = document.querySelector('details.fhelp[data-help="b-maxsteps"]');
      return {
        ru: !!d.querySelector('.fhelp-body [data-lang="ru"]'),
        en: !!d.querySelector('.fhelp-body [data-lang="en"]'),
        ariaRu: !!d.querySelector('summary [data-lang="ru"]'),
        ariaEn: !!d.querySelector('summary [data-lang="en"]'),
      };
    });
    ok(both.ru && both.en, 'both language variants must be in the DOM, not chosen at creation time');
    ok(both.ariaRu && both.ariaEn, "the marker's accessible name must be bilingual too, not a frozen aria-label");

    // And the visible one actually follows the toggle.
    const visible = async () => page.evaluate(() => {
      const spans = document.querySelectorAll('details.fhelp[data-help="b-maxsteps"] .fhelp-body span');
      return Array.from(spans).filter((s) => s.offsetParent !== null || getComputedStyle(s).display !== 'none')
        .map((s) => s.getAttribute('data-lang'));
    });
    await page.evaluate(() => { document.querySelector('details.fhelp[data-help="b-maxsteps"]').open = true; });
    await page.evaluate(() => setLang('ru'));
    eq((await visible()).join(','), 'ru', 'RU selected → the Russian variant is the visible one');
    await page.evaluate(() => setLang('en'));
    eq((await visible()).join(','), 'en', 'EN selected → the English variant is, without re-rendering');
    await page.evaluate(() => setLang('ru'));
  });

  await check('help: one switch opens every block, and the choice survives a reload', async () => {
    const openCount = () => page.evaluate(
      () => document.querySelectorAll('details.fhelp[open]').length);
    const total = await page.evaluate(() => document.querySelectorAll('details.fhelp').length);

    await page.evaluate(() => { document.getElementById('helpall').checked = true;
                                document.getElementById('helpall').dispatchEvent(new Event('change')); });
    eq(await openCount(), total, 'the switch must open every block, not the first one');

    // Persisted: the reader who wants everything explained should not re-ask on every visit.
    await page.reload({ waitUntil: 'load' });
    eq(await page.evaluate(() => document.getElementById('helpall').checked), true,
      'the switch must come back checked after a reload');
    eq(await openCount(), total, 'and the blocks must come back open');

    await page.evaluate(() => { document.getElementById('helpall').checked = false;
                                document.getElementById('helpall').dispatchEvent(new Event('change')); });
    eq(await openCount(), 0, 'unchecking must fold them all again');
  });

  /* ---------------- PROD-DISCOVERY / PROD-IMPORT: features surfaced IN the UI ---------------- */

  await check('the capabilities catalogue renders IN the hub, not only in the docs', async () => {
    // served same-origin (CONTROL_API_UI_DIR=docs), so ./capabilities.json is reachable and the panel
    // populates. A feature nobody can find is a feature that does not exist — the landing UI is where
    // a new user looks.
    await page.goto('about:blank');
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'load' });
    await page.waitForFunction(() => {
      const el = document.getElementById('cap-list');
      return el && /capabilities|каталог/.test(el.textContent);
    }, { timeout: 5000 });
    const txt = await page.textContent('#cap-list');
    ok(/OpenAI/i.test(txt) && /import/i.test(txt),
      'the capabilities panel does not name real features (OpenAI shim, import)');
  });

  await check('the import panel is present and its button gates on files + a connection', async () => {
    await page.goto('about:blank');
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'load' });
    // reveal the run view the way a user does (the nav button), then the panel exists.
    await page.click('[data-nav="run"]');
    ok(await page.$('#import'), 'the import panel is missing from the run view');
    // the button starts disabled and enables only once a connection AND a file are present — a POST
    // with neither is refused server-side anyway, but the UI should not offer it.
    const before = await page.evaluate(() => document.getElementById('imp-go').disabled);
    ok(before === true, 'import button was enabled with no connection and no file');
  });

  await check('the import report NAMES files the server could not read, it does not just count them', async () => {
    await page.goto('about:blank');
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'load' });
    await page.click('[data-nav="run"]');
    // Drive the real renderer with the real server report shape. The CLI was fixed so a file it
    // cannot read is named and the run goes red; the panel must not undo that by showing only
    // "imported 0 tests" — a UI that stays silent about the skipped file reintroduces the same
    // silent-drop one layer up.
    const txt = await page.evaluate(() => {
      const box = document.createElement('div');
      document.body.appendChild(box);
      window.__gate.renderImportReport(box, {
        engines: [],
        totals: { tests: 0, steps: 0, bound: 0, weak: 0, dropped: 0, skipped: 1 },
        skipped: [{ source: 'cypress/integration/checkout.spec.ts', engine: 'cypress',
                    why: 'engine detected but no parser for this dialect yet' }],
        reports: [],
      }, 0);
      return box.textContent;
    });
    ok(/checkout\.spec\.ts/.test(txt), 'the panel did not name the file the server refused to import');
    ok(/cypress/i.test(txt), 'the panel did not say WHICH engine was detected in the skipped file');
    ok(!/^\s*$/.test(txt) && /(НЕ импортировано|NOT imported)/.test(txt),
      'the panel did not state that the file was not imported');
    // and the header must not claim an engine when nothing was imported.
    ok(!/playwright/i.test(txt), 'the panel named an engine although nothing was imported');
  });
  await check('the catalogue OPENS a tool, and says plainly when it cannot', async () => {
    await page.goto('about:blank');
    await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: 'load' });
    await page.click('[data-nav="tools"]');
    await page.waitForFunction(() => {
      const b = document.getElementById('cap-list');
      return b && /Open|Открыть/.test(b.textContent);
    }, { timeout: 15000 });
    // A UI capability offers a button; a CLI-only one says so instead of offering a dead control.
    const n = await page.evaluate(() => document.querySelectorAll('#cap-list .cap-open').length);
    ok(n >= 5, `only ${n} capabilities offer a way in — the catalogue is inert again`);
    const txt = await page.textContent('#cap-list');
    ok(/CLI only|только CLI/.test(txt),
      'a CLI-only capability offers no button and does not say it is CLI-only — the reader is left hunting');
    // Pressing it must actually navigate. A button that names a view and does not go there is the
    // same broken promise the catalogue exists to prevent, moved from prose into a control.
    const before = await page.evaluate(() => document.body.className);
    await page.click('#cap-list .cap-open[data-goto="logs"]');
    const view = await page.evaluate(() => {
      const el = document.querySelector('[data-view="logs"]');
      return el ? getComputedStyle(el).display : 'missing';
    });
    ok(view !== 'none' && view !== 'missing', `the Open button did not reach the logs view (${view}, was ${before})`);
  });

  /* ------------------------------------------- ADR-107c: the UI as a projection of the schema */

  /* Re-establish the connection fields rather than inheriting them. Several checks above reload the
     page, and the token is deliberately never persisted, so by here #capitok is empty — a check that
     assumed otherwise would fail for a reason that has nothing to do with what it tests. */
  await page.click('.rail a[data-nav="settings"]');
  await page.waitForTimeout(200);
  await page.fill('#capi', `http://127.0.0.1:${PORT}`);
  await page.fill('#capitok', token);

  // A fresh instance has never saved a config, and GET /v1/config answers 404 to say exactly that —
  // a deliberate contract (configfile_test.go) that distinguishes "never saved" from "corrupt file".
  // The browser logs the 404 regardless of how the page handles it, so it is named as output that is
  // CORRECT for this scenario rather than silently tolerated everywhere.
  const freshConfig404 = /404 \(Not Found\)/;

  await check('settings: one control per schema setting, and the schema is what says how many', async () => {
    await page.click('.rail a[data-nav="settings"]');
    await page.waitForSelector('#cfg-groups input', { timeout: 15000 });
    // The SCHEMA is the authority on the expected count. A hard-coded 16 would be satisfied by a page
    // that renders sixteen of something, and would have to be edited by whoever adds the seventeenth.
    const declared = await page.evaluate(async () => {
      const r = await fetch('/v1/config-schema');
      return Object.keys((await r.json()).settings || {}).length;
    });
    ok(declared > 0, 'the schema declares no settings — this check would pass vacuously');
    eq(await page.locator('#cfg-groups input').count(), declared,
      'the settings view does not render one control per schema setting');
    ok(await page.locator('#cfg-groups h3').count() > 1,
      'every setting landed in one group — the schema\'s `group` is not being read');
    // The env name has to be visible: it is the SAME setting by file, by environment and by
    // `agentctl config set`, and a person who cannot see the name cannot use the other two ways.
    eq(await page.locator('#cfg-groups code').count(), declared, 'not every setting shows its env var name');
  }, { allowConsole: freshConfig404 });

  await check('settings: hints survive the language switch instead of freezing at first render', async () => {
    // The page switches language with CSS over data-lang pairs. A hint rendered as one chosen string
    // would freeze in whichever language was active when the view first opened — the defect the Logs
    // view already had.
    const ru = await page.locator('#cfg-groups .hint [data-lang="ru"]').count();
    const en = await page.locator('#cfg-groups .hint [data-lang="en"]').count();
    ok(ru > 0, 'no bilingual hint pairs were rendered at all');
    eq(en, ru, 'hints are not emitted as data-lang PAIRS, so one language will be missing after a switch');
  }, { allowConsole: freshConfig404 });

  await check('every schema run-field has a control, or a recorded reason for living elsewhere', async () => {
    // Walks the schema against the page's own map. This is the gate for the defect ADR-107 exists to
    // fix: nine inputs were rendered whose values the submit handler never read, because the form and
    // the handler were two lists that had to agree and did not.
    const missing = await page.evaluate(async () => {
      const r = await fetch('/v1/config-schema');
      const fields = Object.keys((await r.json()).fields || {});
      const map = window.cfgFieldIds || {};
      const bad = [];
      for (const name of fields) {
        const v = map[name];
        if (typeof v === 'string') {
          if (!document.getElementById(v)) bad.push(`${name} -> #${v} (no such element)`);
        } else if (v && typeof v.elsewhere === 'string' && v.elsewhere.trim()) {
          continue;                        // deliberately not in this form, reason recorded
        } else {
          bad.push(`${name} (absent from cfgFieldIds)`);
        }
      }
      return bad;
    });
    eq(missing.length, 0, `schema fields with no control: ${missing.join(', ')}`);
  }, { allowConsole: freshConfig404 });

  await check('the Run button SENDS the budgets and the auth block, not just renders them', async () => {
    // The behavioural half. Before ADR-107 every one of these values was collected by the form and
    // dropped by the handler, which no assertion about the DOM could have noticed.
    // ▶ Run ships disabled and only capUnlock() enables it, which the connection check calls. Driving
    // the app the way a person does — press Проверить first — rather than reaching in to clear the
    // attribute, so the check also proves the unlock path still works.
    await page.click('.rail a[data-nav="settings"]');
    await page.click('#cap-check');
    await page.waitForTimeout(700);
    await page.click('.rail a[data-nav="run"]');
    await page.waitForTimeout(200);
    ok(!(await page.locator('#b-run').isDisabled()), 'the connection check did not enable ▶ Run');
    await page.fill('#b-target', 'http://127.0.0.1:1/never');
    await page.fill('#b-planbud', '1234');
    await page.fill('#b-healbud', '2345');
    await page.fill('#b-totbud', '3456');
    await page.fill('#b-ss', 'state/auth.json');
    await page.fill('#b-loginplan', 'runs/login/plan.json');
    await page.fill('#b-scenario', 'checkout');
    await page.fill('#b-autversion', 'deadbeef');
    await page.check('#b-healllm');

    let body = null;
    await page.route('**/v1/runs', async (route) => {
      body = route.request().postDataJSON();
      // Answered here rather than let through: the assertion is about what the page SENDS, and a real
      // run would spend 25 seconds and a browser to tell us nothing more.
      await route.fulfill({ status: 202, contentType: 'application/json',
        body: JSON.stringify({ run_id: 'gate', artifact_dir: '/tmp/gate', state: 'running' }) });
    });
    await page.click('#b-run');
    await page.waitForTimeout(600);
    await page.unroute('**/v1/runs');

    ok(body, 'the Run button sent no request at all');
    const want = {
      plan_budget: '1234', heal_budget: '2345', total_budget: '3456',
      storage_state: 'state/auth.json', login_plan: 'runs/login/plan.json',
      scenario: 'checkout', aut_version: 'deadbeef', heal_llm: true,
    };
    for (const [k, v] of Object.entries(want)) {
      eq(JSON.stringify(body[k]), JSON.stringify(v), `POST /v1/runs is missing ${k}`);
    }
  }, { allowConsole: freshConfig404 });

  await check('ci + force_replay is refused beside the checkboxes, not in a run log', async () => {
    // The previous check left ▶ Run disabled: bSubmit disables the controls for the duration of a run,
    // and the run it started was answered by a route interceptor, so the flow never reached its end.
    // Re-checking the connection is how a person would get the button back.
    await page.click('.rail a[data-nav="settings"]');
    await page.click('#cap-check');
    await page.waitForTimeout(700);
    await page.click('.rail a[data-nav="run"]');
    await page.waitForTimeout(200);
    ok(!(await page.locator('#b-run').isDisabled()), 'the connection check did not re-enable ▶ Run');
    await page.check('#b-ci');
    await page.check('#b-forcereplay');
    let sent = false;
    await page.route('**/v1/runs', async (route) => { sent = true; await route.abort(); });
    await page.click('#b-run');
    await page.waitForTimeout(400);
    await page.unroute('**/v1/runs');
    ok(!sent, 'the incompatible pair was sent to the server instead of being refused in the form');
    ok(await page.locator('#b-cifr-warn').isVisible(), 'nothing on screen said why the run did not start');
    await page.uncheck('#b-ci');
    await page.uncheck('#b-forcereplay');
  }, { allowConsole: freshConfig404 });

  /* ================= ADR-108d — the three-pane chat screen ================= */

  await check('three-pane: the layout exists in the CHAT tab and nowhere else', async () => {
    await page.click('.rail a[data-nav="chat"]');
    await page.waitForTimeout(250);
    ok(await page.locator('#chat3').isVisible(), 'the chat tab has no three-pane layout');
    ok(await page.locator('#live-area').isVisible(), 'no live area');
    ok(await page.locator('#run-flow').isVisible(), 'no run-flow pane');
    // BELONGING, not just visibility. A mutation that forced the author subpanel visible everywhere
    // survived the visibility check alone: the router hides the whole view container, so the panes
    // were invisible for a reason that has nothing to do with where they live. What Alex's directive
    // is about is which view OWNS this layout, so that is what gets asserted — the panes must be
    // inside the chat's own subpanel, where the router can only ever reveal them with the chat.
    for (const id of ['chat3', 'live-area', 'run-flow', 'artifacts']) {
      const owned = await page.evaluate((elId) => {
        const el = document.getElementById(elId);
        return !!el && !!el.closest('[data-subpanel="author"][data-view="chat"]');
      }, id);
      ok(owned, `#${id} is not inside the chat subpanel — it would outlive the chat view`);
    }
    // Alex's directive is explicit that this belongs to the chat and NOT to settings, the library,
    // results or the tools. A layout that leaked into them would be a different product decision.
    for (const view of ['settings', 'library', 'results', 'logs']) {
      await page.click(`.rail a[data-nav="${view}"]`);
      await page.waitForTimeout(150);
      ok(!(await page.locator('#chat3').isVisible()),
         `the three-pane layout is visible under ${view} — it belongs to the chat alone`);
    }
    await page.click('.rail a[data-nav="chat"]');
    await page.waitForTimeout(200);
  });

  await check('three-pane: all three live modes switch WITHOUT reloading', async () => {
    const url0 = page.url();
    // The frame pane starts visible; the other two are hidden but PRESENT — a mode that does not exist
    // until clicked cannot be said to be switchable.
    ok(await page.locator('#lv-frame').isVisible(), 'the browser-frame pane is not the default');
    for (const mode of ['actions', 'video', 'frame']) {
      await page.click(`#lv-mode-${mode}`);
      await page.waitForTimeout(150);
      ok(await page.locator(`#lv-${mode}`).isVisible(), `mode ${mode} did not reveal its pane`);
      for (const other of ['frame', 'actions', 'video'].filter((m) => m !== mode)) {
        ok(!(await page.locator(`#lv-${other}`).isVisible()), `mode ${mode} left ${other} on screen`);
      }
      eq(await page.locator(`#lv-mode-${mode}`).getAttribute('aria-selected'), 'true',
         `mode ${mode} is shown but not marked selected`);
    }
    eq(page.url(), url0, 'switching modes navigated — the toggle must not reload (Alex: без перезагрузки)');
  });

  await check('three-pane: the unbuilt mode says so instead of showing an empty box', async () => {
    await page.click('#lv-mode-video');
    await page.waitForTimeout(150);
    const text = (await page.locator('#lv-video').innerText()).trim();
    ok(text.length > 0, 'the video mode is an empty box — indistinguishable from a broken one');
    ok(/screencast|CDP/i.test(text), `the video mode does not say WHY it is unavailable: ${text}`);
    await page.click('#lv-mode-frame');
  });

  await check('three-pane: the run flow explains an empty screen, and the reasons differ', async () => {
    // Run BEFORE the next check ever calls window.lvOnEvent — this is the pristine, nothing-has-ever-
    // streamed state, which is also the state a real run started from this very chat leaves the pane
    // in (bSubmit/chRunFlow hand run_id to tFillLiveRunId, which "never auto-connects" the WS — see
    // that function's own comment). An empty #rf-list here would be indistinguishable from broken.
    const text = (await page.locator('#rf-list').innerText()).trim();
    ok(text.length > 20, 'the run-flow pane is blank — indistinguishable from broken');
    ok(await page.locator('#rf-idle').isVisible(), 'the idle hint is not the visible content of #rf-list');
    // Four DIFFERENT sentences, not one generic "nothing yet" — the most frequent cause (the event
    // stream is not connected) has to be named, because a run in the SAME chat does not connect it.
    ok(/не подключ|not connected/i.test(text), 'does not say the event stream is disconnected (the common case)');
    ok(/не начал|has not started/i.test(text), 'does not mention a run that has not started');
    ok(/шаг/i.test(text) || /step/i.test(text), 'does not mention a run producing no step events');
    ok(/ещё не пришл|has not arrived/i.test(text), 'does not mention data still in flight');
    // Not one of the `.rf-*` classes — those are reserved for real rows: rfApplyFilter and the split
    // count below key off `#rf-list .rf-tool`/`.rf-business`, so a hint tagged that way would be folded
    // into the rows the tool-filter checkbox hides, or double-counted as a row that carries no event.
    const idleClass = await page.getAttribute('#rf-idle', 'class');
    ok(!/(^|\s)rf-/.test(idleClass || ''), `the idle hint carries an rf- class: ${idleClass}`);
    eq(await page.locator('#rf-list .rf-tool').count(), 0, 'the idle hint is counted as a tool row');
    eq(await page.locator('#rf-list .rf-business').count(), 0, 'the idle hint is counted as a business row');
    // The FIRST real row removes it — the same contract #lv-actions-idle makes. `log` is used rather
    // than `tool.call` deliberately: lvKindOf files `tool.call`/`step.progress`/`step.frame` under
    // "business" (what happened to the app), and everything else, `log` included, under "tool" (what
    // Sentinel itself did) — so this also doubles as a live check that the mapping stayed put.
    await page.evaluate(() => window.lvOnEvent({
      type: 'log', run_id: 'r-idle-hint', seq: 1, data: { line: 'x' },
    }));
    await page.waitForTimeout(150);
    ok(!(await page.locator('#rf-idle').count()), 'the idle hint is still in the DOM once a real row exists');
    ok(await page.locator('#rf-list .rf-tool').first().isVisible(), 'the real row the idle hint yielded to is missing');
  });

  await check('three-pane: the run flow separates the tool from the application, visibly', async () => {
    // Driven through the page's own event consumer, so this exercises the shipped code path rather
    // than a copy of it — the same seam the Logs filter checks use.
    await page.evaluate(() => {
      const ev = (type, data) => window.lvOnEvent({ type, run_id: 'r-test', seq: 1, data });
      ev('tool.call', { name: 'click', args_summary: "click button 'Pay'" });
      ev('step.progress', { n: 1, total: 10, desc: 'pay for the order' });
      ev('state.transition', { to: 'perceive' });
    });
    await page.waitForTimeout(200);
    const business = await page.locator('#rf-list .rf-business').count();
    const tool = await page.locator('#rf-list .rf-tool').count();
    ok(business > 0 && tool > 0,
       `the split is not visible: business=${business} tool=${tool} — one side missing means the layout `
       + 'is not showing the distinction it exists for');
    // And it is a LAYOUT, not a filter: both are on screen at once by default.
    ok(await page.locator('#rf-list .rf-business').first().isVisible(), 'business rows are not visible');
    ok(await page.locator('#rf-list .rf-tool').first().isVisible(), 'tool rows are not visible by default');
    // The toggle hides the tool without touching the application's side.
    await page.uncheck('#rf-tool');
    await page.waitForTimeout(150);
    ok(!(await page.locator('#rf-list .rf-tool').first().isVisible()), 'unchecking left the tool rows visible');
    ok(await page.locator('#rf-list .rf-business').first().isVisible(), 'hiding the tool also hid the application');
    await page.check('#rf-tool');
  });

  await check('three-pane: artefacts are reachable FROM THE CHAT and open on a canvas', async () => {
    await page.click('.rail a[data-nav="chat"]');
    await page.waitForTimeout(200);
    ok(await page.locator('#artifacts').isVisible(), 'the chat has no artefacts panel — Alex: качаются из чата');
    // The list is built by ASKING the server which names this run produced, so it is driven at a real
    // run rather than asserted against a hard-coded set. A name the run never wrote must not appear:
    // offering it would be a download that 404s, which is the dead-button defect this milestone keeps
    // finding.
    await page.evaluate((id) => window.lvOnEvent({ type: 'run.started', run_id: id, seq: 1, data: {} }), baseRun);
    await page.click('#art-refresh');
    await page.waitForFunction(() => {
      const t = document.querySelector('#art-list')?.innerText || '';
      return t && !/looking|смотрю/i.test(t);
    }, { timeout: 20000 });
    const names = await page.locator('#art-list .art-open').allInnerTexts();
    ok(names.length > 0, 'no artefacts offered for a finished run');
    ok(names.includes('plan.json') || names.includes('scenario.json'),
       `neither plan.json nor scenario.json offered: ${JSON.stringify(names)}`);
    ok(!names.includes('heal-report.json'),
       'a replay-only artefact was offered for an explore run — the list is guessing, not asking');

    // Opening a JSON artefact shows it as text; the canvas stays for images. Both exist because a
    // canvas cannot show scenario.json and a <pre> cannot show a frame.
    await page.click('#art-list .art-open >> nth=0');
    await page.waitForTimeout(600);
    ok(await page.locator('#art-view').isVisible(), 'opening an artefact revealed nothing');
    const shownText = await page.locator('#art-text').isVisible();
    const shownCanvas = await page.locator('#art-canvas').isVisible();
    ok(shownText || shownCanvas, 'the artefact opened into an empty viewer');
  }, { allowConsole: /404 \(Not Found\)/ });

  /* ================= ADR-109 — local accounts in the hub =================
     These run LAST and against their OWN control-API, deliberately.

     Local accounts need a store-gateway, and attaching one to the server the
     checks above use changes what those checks are looking at: /readyz turns
     503 until a config is saved (the store tier EXPECTS one, the file tier
     does not), and creating the first account tightens the pre-identity open
     reads. Both are correct product behaviour and both would rewrite the
     premises of forty-five checks that are about something else. A second
     process on its own port is cheaper than reasoning about that overlap —
     and it is also how the product is deployed when identity is in use. */

  const gwBin = path.join(REPO, 'bin', 'store-gateway');
  if (!fs.existsSync(gwBin)) {
    // NOT a skip. The identity checks would silently stop measuring, and a gate that cannot fail is
    // indistinguishable from one that passes (ADR-097).
    throw new Error(`${gwBin} not built — run: go build -o bin/store-gateway ./cmd/store-gateway`);
  }
  // SHORT socket path, under /tmp: a unix address is capped at ~108 bytes and a longer one fails with
  // a bare "bind: invalid argument" that reads as a build problem.
  const storeSock = `/tmp/sentinel-hubgate-${process.pid}.sock`;
  const storeToken = 'hub-gate-store-token';
  gw = spawn(gwBin, ['--addr', storeSock, '--db', path.join(REPO, 'state', `hub-gate-${process.pid}.db`)], {
    cwd: REPO,
    env: { ...process.env, STORE_TOKEN: storeToken },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  for (let i = 0; i < 100 && !fs.existsSync(storeSock); i++) await new Promise((r) => setTimeout(r, 50));
  if (!fs.existsSync(storeSock)) throw new Error('store-gateway socket never appeared');

  const PORT2 = PORT + 1;
  capi2 = spawn(bin, [], {
    cwd: REPO,
    env: {
      ...process.env,
      CONTROL_API_ADDR: `127.0.0.1:${PORT2}`,
      CONTROL_API_SERVE_UI: '1',
      CONTROL_API_UI_DIR: path.join(REPO, 'docs'),
      CONTROL_API_AGENTCTL: path.join(REPO, 'bin', 'agentctl'),
      CONTROL_API_CORS_ORIGINS: '',
      CONTROL_API_STORE_ADDR: `unix:${storeSock}`,
      STORE_TOKEN: storeToken,
      CONTROL_API_TOKEN: 'hub-gate-machine-token',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log2 = '';
  capi2.stdout.on('data', (b) => { log2 += b; });
  capi2.stderr.on('data', (b) => { log2 += b; });
  let nonce2 = '';
  for (let i = 0; i < 100; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT2}/healthz`);
      if (r.ok) { const m = /bootstrap=([0-9a-f]+)/.exec(log2); if (m) { nonce2 = m[1]; break; } }
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  if (!nonce2) throw new Error(`second control-API never printed a bootstrap nonce:\n${log2.slice(0, 1200)}`);
  const token2 = 'hub-gate-machine-token';

  // Two accounts, created through the API with the machine token — the same way an operator does it
  // before anyone can sign in.
  const mkUser = async (name, password, isAdmin) => {
    const r = await fetch(`http://127.0.0.1:${PORT2}/v1/users`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token2}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, password, is_admin: isAdmin }),
    });
    if (r.status !== 201) throw new Error(`POST /v1/users ${name} -> ${r.status} ${await r.text()}`);
  };
  await mkUser('gate-admin', 'gate-admin-pass', true);
  await mkUser('gate-user', 'gate-user-pass1', false);
  // A stored document, so the settings view has the tool's sections to show and lock. Written by the
  // machine token, which is exactly how the setup wizard writes it.
  {
    const r = await fetch(`http://127.0.0.1:${PORT2}/v1/config`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${token2}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: { log_keep: 5 }, run: { max_steps: 40 } }),
    });
    if (!r.ok) throw new Error(`PUT /v1/config -> ${r.status} ${await r.text()}`);
  }

  const idPage = await context.newPage();
  idPage.on('pageerror', (e) => pageErrors.push(e.message));
  idPage.on('console', (m) => { if (m.type() === 'error') pageErrors.push(`console: ${m.text()}`); });
  await idPage.goto(`http://127.0.0.1:${PORT2}/?bootstrap=${nonce2}`, { waitUntil: 'load' });
  await idPage.waitForTimeout(400);

  // The sign-in controls live in the Settings view, so every check here starts by revealing it. A
  // locator that is merely present is not a control a person can use.
  const openSettings = async () => {
    await idPage.click('.rail a[data-nav="settings"]');
    await idPage.waitForTimeout(200);
  };
  // WAIT FOR THE STATE THE ASSERTIONS DEPEND ON, not for a guessed interval. This slept 800 ms, and
  // POST /v1/login MEASURES 1.0-1.6 s on this hardware because verifying a password is 600 000
  // PBKDF2 iterations by design (internal/identity/password.go). So the three identity checks were
  // asserting against a page still showing «вхожу…», and they failed for a reason that had nothing
  // to do with what they claim to measure — a slow login is not a broken one.
  //
  // Why nobody saw it: CI's criterion for this gate is a FLOOR on the number of checks that passed
  // (>= 45 of 57), so a check that fails every time still leaves the step green. A gate that cannot
  // fail and a gate whose failures are not read are the same gate.
  const signIn = async (name, password) => {
    await openSettings();
    await idPage.fill('#id-name', name);
    await idPage.fill('#id-pass', password);
    await idPage.click('#id-login');
    // The status line, not the identity line. Waiting for #id-who to CHANGE looks equivalent and is
    // not: the page runs its own idRefresh() on load, so an unrelated repaint satisfies that wait
    // while the login request is still in flight — measured, and it is how the first attempt at this
    // fix still failed. #id-status is written only by the click handler, so ✓/✗ is the one signal
    // that belongs to this action.
    //
    // Both outcomes are accepted deliberately: a wait that only accepts success turns a genuine
    // refusal into a timeout, and a timeout names the harness instead of the product.
    await idPage.waitForFunction(() => {
      const st = document.getElementById('id-status');
      return !!st && /[✓✗]/.test(st.textContent || '');
    }, null, { timeout: 30_000 });
    // ✓ is set BEFORE the handler awaits idRefresh(), so the identity line is repainted a moment
    // later. #id-logout appears only for a signed-in session, so it is the paint's own signal.
    if (/✓/.test(await idPage.locator('#id-status').innerText())) {
      await idPage.locator('#id-logout').waitFor({ state: 'visible', timeout: 15_000 });
    }
  };

  await check('identity: the hub can sign in, and says who is working', async () => {
    await openSettings();
    ok(await idPage.locator('#id-login').isVisible(), 'there is no sign-in control in the hub at all');
    await signIn('gate-user', 'gate-user-pass1');
    const who = await idPage.locator('#id-who').innerText();
    ok(/gate-user/.test(who), `the page does not say who is signed in: ${JSON.stringify(who)}`);
    // The session REPLACED the machine token rather than sitting beside it: two credentials would mean
    // an invisible rule about which one wins.
    const tok = await idPage.locator('#capitok').inputValue();
    ok(tok.length > 0 && tok !== token2, 'the machine token is still in the field after signing in');
    ok(await idPage.locator('#id-logout').isVisible(), 'no way to sign out once signed in');
  }, { allowConsole: freshConfig404 });

  await check('identity: a plain account is not offered the account controls', async () => {
    ok(!(await idPage.locator('#idadmin').isVisible()),
       'the account-management block is visible to a non-admin — a control whose use will be refused');
  }, { allowConsole: freshConfig404 });

  await check('config split: the tool\'s settings are visible but locked, and the reason is on screen', async () => {
    await idPage.click('#cfg-reload');
    await idPage.waitForTimeout(900);
    const first = idPage.locator('#cfg-groups input').first();
    ok(await first.count() > 0, 'the settings view rendered no controls at all');
    ok(await first.isDisabled(), 'a non-admin can edit the tool\'s settings — the split is not reaching the UI');
    // Visible, not hidden: a value nobody can see is one nobody can ask to have changed.
    ok(await first.isVisible(), 'the tool\'s settings were HIDDEN from a non-admin rather than shown read-only');
    const why = await idPage.locator('#cfg-locked').innerText();
    ok(why.trim().length > 0, 'the controls are disabled with nothing saying why');
  }, { allowConsole: freshConfig404 });

  await check('config split: saving as a plain account is not refused', async () => {
    // The document the hub reads back is the MERGED one, so it always contains the tool's sections.
    // Sending them back would earn a 403 and make the settings view unusable for everyone but an admin
    // — the opposite of what the split is for. What leaves the page must be only what is theirs.
    let sentBody = null;
    await idPage.route('**/v1/config', async (route) => {
      if (route.request().method() === 'PUT') { sentBody = route.request().postData(); await route.abort(); return; }
      await route.continue();
    });
    await idPage.click('#cfg-save');
    await idPage.waitForTimeout(400);
    await idPage.unroute('**/v1/config');
    ok(sentBody !== null, 'the save button sent nothing');
    const doc = JSON.parse(sentBody);
    ok(!('settings' in doc), `a non-admin's save carried the tool's settings section: ${sentBody.slice(0, 200)}`);
    ok(!('llm' in doc), `a non-admin's save carried the tool's llm section: ${sentBody.slice(0, 200)}`);
    // The aborted PUT is the technique, not a defect: intercepting the request is how its BODY is read
    // (the assertion is about what leaves the page, not about what the server answers), and an aborted
    // fetch necessarily logs a failed resource. Allowed narrowly, by the abort's own signature.
  }, { allowConsole: /404 \(Not Found\)|net::ERR_FAILED/ });

  await check('identity: an admin gets the account controls and a real list', async () => {
    await idPage.click('#id-logout');
    await idPage.waitForTimeout(400);
    await signIn('gate-admin', 'gate-admin-pass');
    ok(await idPage.locator('#idadmin').isVisible(), 'an admin is not offered the account controls');
    await idPage.click('#ua-reload');
    await idPage.waitForTimeout(600);
    const list = await idPage.locator('#ua-list').innerText();
    ok(/gate-user/.test(list) && /gate-admin/.test(list), `the account list does not show the accounts: ${list}`);
  }, { allowConsole: freshConfig404 });

  await check('config split: an admin may edit the tool', async () => {
    await idPage.click('#cfg-reload');
    await idPage.waitForTimeout(900);
    const first = idPage.locator('#cfg-groups input').first();
    ok(!(await first.isDisabled()),
       'an admin cannot edit the tool\'s settings either — then the lock is not about permission at all');
  }, { allowConsole: freshConfig404 });

  await check('identity: signing out returns the hub to no identity', async () => {
    await idPage.click('#id-logout');
    await idPage.waitForTimeout(500);
    ok(!(await idPage.locator('#idadmin').isVisible()), 'the account controls survived signing out');
    eq(await idPage.locator('#capitok').inputValue(), '', 'the session token stayed in the field after signing out');
  }, { allowConsole: freshConfig404 });

} catch (e) {
  results.push({ name: 'harness', ok: false, err: e.message });
  console.log(`  FAIL harness\n       ${e.message}`);
} finally {
  if (browser) await browser.close().catch(() => {});
  if (capi) capi.kill('SIGTERM');
  if (capi2) capi2.kill('SIGTERM');
  if (gw) gw.kill('SIGTERM');
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log(failed.map((f) => `  FAIL ${f.name}: ${f.err}`).join('\n'));
  process.exit(1);
}
