#!/usr/bin/env node
// End-to-end UI smoke against a REAL deployment, with screenshots (HEALTH/LIVE verification).
//
// The DOM gates that already exist check properties — that a control resolves, that a view hides its
// siblings, that nothing calls the API without a backend. They are precise and they are blind to one
// thing: whether the product, assembled and running, looks like something a person can use. This
// script drives the whole stack the way an operator would and leaves PNGs behind, so a change that
// makes the hub technically correct and visually broken is visible rather than argued about.
//
// It is a SMOKE, deliberately: it asserts the few things whose absence means the deployment is not
// working at all (the hub loads, it connects, a run starts and reaches a verdict, the live area
// answers), and it captures the rest as images for a human. A screenshot cannot assert taste; what it
// can do is make a regression in layout or emptiness impossible to miss in review.
//
// Run:  node scripts/ui-smoke.mjs --base http://127.0.0.1:8090 --token <tok> --out <dir>

import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(REPO, 'pw-executor', 'package.json'));
const { chromium } = require('playwright');

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const BASE = (arg('base', 'http://127.0.0.1:8090')).replace(/\/+$/, '');
const TOKEN = arg('token', '');
const OUT = arg('out', path.join(REPO, 'ui-smoke'));
// The bundled fixture BY THE PATH THIS PROCESS CAN SEE. The default used to be the in-container
// path (`file:///app/testdata/…`), which is right for a compose deployment and wrong everywhere
// else: on a runner the run starts, navigates to a path that does not exist, and comes back
// non-zero — the exact ADR-078 trap the run form warns about, in mirror image. Pass --target to
// aim at a deployment whose filesystem is not this one.
const TARGET = arg('target', `file://${path.join(REPO, 'testdata', 'site', 'index.html')}`);

fs.mkdirSync(OUT, { recursive: true });

const results = [];
const pageErrors = [];
let shotN = 0;

async function shot(page, name) {
  const file = path.join(OUT, `${String(++shotN).padStart(2, '0')}-${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  return path.basename(file);
}

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

async function main() {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => pageErrors.push(e.message));

  try {
    await check('the hub loads from the deployment that serves it', async () => {
      const r = await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
      ok(r && r.ok(), `GET / answered ${r && r.status()}`);
      // The rail is the last thing the hub paints, so its presence is "the page is up" — a sleep
      // here is either too long on a fast machine or too short on a loaded one, and the second is
      // how a gate ends up asserting against a half-drawn page.
      await page.waitForSelector('.rail a[data-nav="settings"]', { state: 'visible', timeout: 20000 });
      await shot(page, 'hub-loaded');
    });

    await check('it connects with a credential and unlocks its controls', async () => {
      await page.evaluate(() => { location.hash = '#v=settings'; });
      await page.waitForSelector('#capi', { state: 'visible', timeout: 10000 });
      await page.fill('#capi', BASE);
      if (TOKEN) await page.fill('#capitok', TOKEN);
      await page.click('#cap-check');
      await page.waitForFunction(
        () => (document.getElementById('cap-status') || {}).textContent?.includes('ok'),
        undefined, { timeout: 15000 });
      const stillDisabled = await page.$$eval('[data-needs-api]', (els) =>
        els.filter((e) => e.disabled).map((e) => e.id));
      ok(stillDisabled.length === 0, `connected, yet still disabled: ${stillDisabled.join(', ')}`);
      await shot(page, 'connected');
    });

    await check('the run form accepts a target and starts a run', async () => {
      await page.evaluate(() => { location.hash = '#v=run'; });
      await page.waitForSelector('#b-target', { state: 'visible' });
      // EXPLORE, not the form's default. `goal` needs a model, and since HEALTH-001 a goal run with
      // none is REFUSED rather than quietly downgraded — so the default mode turns this smoke into a
      // test of the refusal against a stack that was never given a model. Explore is the mode that
      // genuinely needs nothing, which is what a smoke should exercise. (The refusal has its own
      // check below; it is a promise worth keeping, not an accident to route around.)
      await page.selectOption('#b-mode', 'explore');
      await page.fill('#b-target', TARGET);
      await shot(page, 'run-form');
      await page.click('#b-run');
      // The run log is written by the submit handler itself, so it is a signal about THIS click.
      // The previous version waited for any 8-hex-digit run of characters anywhere in the body —
      // satisfied by a token, an artifact path, or a golden hash already on the page.
      await page.waitForFunction(
        () => /run_id=|✗/.test((document.getElementById('b-runlog') || {}).innerText || ''),
        undefined, { timeout: 30000 });
      const log = await page.locator('#b-runlog').innerText();
      ok(!/✗/.test(log), `the hub refused to start the run: ${log.split('\n').slice(0, 3).join(' / ')}`);
      await shot(page, 'run-started');
    });

    await check('the run reaches a verdict and the hub shows it', async () => {
      // The verdict BADGE, not a regex over the whole page: «ПРОЙДЕНО» also appears in the prose of
      // other views, so a body-wide match can be satisfied without a run having finished at all.
      await page.waitForFunction(
        () => ((document.getElementById('b-verdict') || {}).innerText || '').trim().length > 0,
        undefined, { timeout: 180000 });
      const v = await page.locator('#b-verdict').innerText();
      ok(/exit\s*0/.test(v), `an explore run against the bundled fixture did not pass: ${v.split('\n')[0]}`);
      await shot(page, 'verdict');
    });

    await check('a goal run with no model is refused rather than quietly downgraded (HEALTH-001)', async () => {
      // The promise HEALTH-001 made: `goal` without a reachable model does not fall back to the
      // heuristic planner and exit 0 — it stops. Asserted as "not green", deliberately: the WORDING
      // of that verdict is what HEALTH-004 is about to change, and pinning today's sentence here
      // would make an improvement look like a regression.
      await page.evaluate(() => { location.hash = '#v=run'; });
      await page.waitForSelector('#b-mode', { state: 'visible' });
      await page.selectOption('#b-mode', 'goal');
      await page.fill('#b-goal', 'open the actions page');
      await page.fill('#b-target', TARGET);
      await page.click('#b-run');
      await page.waitForFunction(
        () => ((document.getElementById('b-verdict') || {}).innerText || '').trim().length > 0,
        undefined, { timeout: 180000 });
      const v = await page.locator('#b-verdict').innerText();
      ok(!/exit\s*0/.test(v),
         `a goal run with no model came back green — the silent downgrade HEALTH-001 removed: ${v.split('\n')[0]}`);
      await shot(page, 'goal-without-a-model');
    });

    await check('the live area offers all three modes and says what each one is', async () => {
      await page.evaluate(() => { location.hash = '#v=chat'; });
      await page.waitForSelector('#lv-mode-frame', { state: 'visible' });
      for (const mode of ['frame', 'actions', 'video']) {
        const btn = await page.$(`#lv-mode-${mode}`);
        ok(btn, `the live area has no ${mode} mode button`);
        await btn.click();
        // Wait for the pane to be SHOWN — the thing the assertions below are about — instead of
        // guessing how long the switch takes. The video mode then goes to the network, so it is
        // given its own wait for content rather than a shared sleep.
        await page.waitForSelector(`#lv-${mode}`, { state: 'visible', timeout: 10000 });
        if (mode === 'video') {
          await page.waitForFunction(
            () => ((document.getElementById('lv-video') || {}).innerText || '').trim().length > 20
                  || !!document.querySelector('#lv-video img'),
            undefined, { timeout: 20000 });
        }
        const pane = await page.$(`#lv-${mode}`);
        ok(pane && !(await pane.evaluate((e) => e.hidden)), `the ${mode} pane did not become visible`);
        const text = (await pane.innerText()).trim();
        const hasImage = await pane.$('img, canvas');
        // Either it shows something, or it SAYS why it does not. An empty pane is the one outcome
        // this whole ADR exists to remove — it reads as broken and is indistinguishable from it.
        ok(hasImage || text.length > 20,
           `the ${mode} pane is empty and silent — a mode that shows nothing must say why`);
        await shot(page, `live-${mode}`);
      }
    });

    await check('the library and results views load without erroring', async () => {
      for (const view of ['library', 'results', 'logs']) {
        await page.evaluate((v) => { location.hash = `#v=${v}`; }, view);
        // The section becoming visible is the state; what it then fetches is captured by the
        // screenshot and by the pageerror listener, which is what this check is actually for.
        await page.waitForSelector(`[data-view="${view}"]`, { state: 'visible', timeout: 15000 });
        await shot(page, `view-${view}`);
      }
    });
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  console.log(`${shotN} screenshots in ${OUT}`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
