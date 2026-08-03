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
const TARGET = arg('target', `file:///app/testdata/site/index.html`);

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
      await page.waitForTimeout(800);
      await shot(page, 'hub-loaded');
    });

    await check('it connects with a credential and unlocks its controls', async () => {
      await page.evaluate(() => { location.hash = '#v=settings'; });
      await page.waitForTimeout(300);
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
      await page.waitForTimeout(400);
      await page.fill('#b-target', TARGET);
      await shot(page, 'run-form');
      await page.click('#b-run');
      // The run id appears in the page once control-api has accepted it.
      await page.waitForFunction(
        () => /[0-9a-f]{8,}/.test(document.body.innerText),
        undefined, { timeout: 30000 });
      await shot(page, 'run-started');
    });

    await check('the run reaches a verdict and the hub shows it', async () => {
      await page.waitForFunction(
        () => /ПРОЙДЕНО|PASSED|ПРОБЛЕМ|FAILED|exit\s*\d/i.test(document.body.innerText),
        undefined, { timeout: 180000 });
      await shot(page, 'verdict');
    });

    await check('the live area offers all three modes and says what each one is', async () => {
      await page.evaluate(() => { location.hash = '#v=chat'; });
      await page.waitForTimeout(500);
      for (const mode of ['frame', 'actions', 'video']) {
        const btn = await page.$(`#lv-mode-${mode}`);
        ok(btn, `the live area has no ${mode} mode button`);
        await btn.click();
        await page.waitForTimeout(1200);
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
        await page.waitForTimeout(1500);
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
