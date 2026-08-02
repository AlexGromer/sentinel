#!/usr/bin/env node
// DOM gate for the STATIC showcase (ADR-110) — the hub as it is served by GitHub Pages, with no
// control-API behind it at all.
//
// https://alexgromer.github.io/sentinel/ serves the same docs/index.html that control-api serves in
// mode 3. On Pages nothing is behind it, yet 72 of its 74 buttons looked live: `capUnlock()` gated
// exactly two ids (#b-run and #ch-send) and everything else — save config, promote a test, delete a
// chat, connect the live stream — was clickable and silently hit a URL that answers 404. A page that
// looks operational and is not is worse than one that says so.
//
// The gate does not carry a list of controls that need a backend; it DERIVES the set by clicking and
// watching. So a control added later is covered without anyone remembering this file — and the
// failure message names the control, because "something leaked" is not actionable.
//
// Both directions are asserted, and the second is the one that keeps the first honest: disabling
// everything forever would satisfy "nothing calls the API", so the gate also stands up a fake
// control-API, connects to it, and requires the same controls to come back.
//
// Run: node scripts/pages-static-check.mjs

import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(REPO, 'pw-executor', 'package.json'));
const { chromium } = require('playwright');

const PAGES_PORT = Number(process.env.PAGES_GATE_PORT || 18755);
const FAKE_API_PORT = Number(process.env.PAGES_GATE_API_PORT || 18756);

/* A request is an API attempt if its PATH is one this product serves. With no base configured,
 * `capiBase()` returns '' and every call becomes a same-origin relative URL — so these land on the
 * static server, which is exactly how they are caught. */
const API_PATH = /^\/(v1\/|healthz|readyz)/;

/* ------------------------------------------------------------------ tiny harness */
const results = [];
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

/* ------------------------------------------------------------------ static server for docs/ */
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
               '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };
function staticServer(port, onRequest) {
  const srv = http.createServer((req, res) => {
    const url = new URL(req.url, `http://127.0.0.1:${port}`);
    if (onRequest) onRequest(url.pathname);
    // Deliberately answer 404 to API paths rather than refusing the connection: Pages does exactly
    // that, and a page that behaves differently against a closed port would be tested in a shape
    // nobody deploys.
    if (API_PATH.test(url.pathname)) { res.writeHead(404); return res.end('not found'); }
    const rel = url.pathname === '/' ? '/index.html' : url.pathname;
    const file = path.join(REPO, 'docs', rel);
    if (!file.startsWith(path.join(REPO, 'docs')) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404); return res.end('not found');
    }
    res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(fs.readFileSync(file));
  });
  return new Promise((resolve) => srv.listen(port, '127.0.0.1', () => resolve(srv)));
}

/* A control-API that answers only the handshake — enough for the page to unlock, and no more. */
function fakeApi(port) {
  const srv = http.createServer((req, res) => {
    const p = new URL(req.url, `http://127.0.0.1:${port}`).pathname;
    res.setHeader('access-control-allow-origin', '*');
    res.setHeader('access-control-allow-headers', '*');
    if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }
    if (p === '/healthz') { res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ status: 'ok', version: '0.0.0-fake', runs: 0 })); }
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{}');
  });
  return new Promise((resolve) => srv.listen(port, '127.0.0.1', () => resolve(srv)));
}

/* Interactive controls a person can actually reach: visible, enabled, and not the connection form
 * itself (typing a URL and pressing Check is the ONE thing that must work with no backend). */
const CONNECT_FORM = new Set(['cap-check', 'capi', 'capitok']);

async function clickableControls(page) {
  return page.$$eval('button, [role="button"]', (els) =>
    els.filter((e) => !e.disabled && e.offsetParent !== null)
       .map((e) => e.id || (e.textContent || '').trim().slice(0, 30)));
}

async function main() {
  const seen = [];
  const pagesSrv = await staticServer(PAGES_PORT, (p) => { if (API_PATH.test(p)) seen.push(p); });
  const apiSrv = await fakeApi(FAKE_API_PORT);
  const browser = await chromium.launch();
  try {
    // Clipboard permission is granted because the sweep clicks the copy buttons, and a headless
    // context refuses writeText by default. Without it the gate would report the harness's own
    // permission error as a page defect — the copy buttons work for a person.
    const ctx = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => pageErrors.push(e.message));
    // Cross-origin XHR to the fake API is blocked by CORS unless allowed; we set the header above.
    await page.goto(`http://127.0.0.1:${PAGES_PORT}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(600);

    await check('the static showcase SAYS it is one', async () => {
      const banner = await page.$('#static-showcase');
      ok(banner, 'no #static-showcase banner in the page');
      ok(await banner.isVisible(), 'the showcase banner exists but is not visible with no backend');
      const text = (await banner.textContent()).trim();
      ok(text.length > 40, `the banner says almost nothing: ${JSON.stringify(text.slice(0, 60))}`);
    });

    await check('nothing that needs a backend is offered as if it worked', async () => {
      const disabled = await page.$$eval('[data-needs-api]', (els) => els.filter((e) => e.disabled).length);
      const total = await page.$$eval('[data-needs-api]', (els) => els.length);
      // A floor. Equal-but-empty sets agree perfectly, and a selector typo would otherwise read as
      // a clean pass over zero controls.
      ok(total >= 15, `only ${total} controls are marked data-needs-api — the marking, not the page, is what regressed`);
      ok(disabled === total, `${total - disabled} of ${total} backend-dependent controls are still enabled`);
    });

    await check('clicking every live control, in EVERY view, reaches no API', async () => {
      // THE behavioural half: whatever is still clickable must be genuinely local. This is what
      // catches a NEW control that forgets the marker — no list to keep in step.
      //
      // Every view, not just the one that happens to open: controls in a hidden view have no
      // offsetParent, so a single-view sweep silently covered ~a tenth of the page and reported a
      // clean pass. The view names are read from the DOM (`data-view`) rather than listed here, so
      // a new view is swept the day it appears.
      const views = await page.$$eval('[data-view]', (els) =>
        [...new Set(els.map((e) => e.getAttribute('data-view')))].filter((v) => v && !v.startsWith('__')));
      ok(views.length >= 5, `only ${views.length} views discovered — the sweep would cover almost nothing`);

      const leaked = [];
      let clicked = 0;
      for (const view of views) {
        await page.evaluate((v) => { location.hash = `#v=${v}`; }, view);
        await page.waitForTimeout(250);
        for (const id of await clickableControls(page)) {
          if (CONNECT_FORM.has(id)) continue;
          seen.length = 0;
          try {
            // `[id="…"]`, not `#…`: CSS.escape does not exist in Node, and several ids here would
            // need escaping anyway. The attribute form sidesteps both.
            await page.click(`[id="${id.replace(/"/g, '\\"')}"]`, { timeout: 1200, noWaitAfter: true });
            clicked += 1;
          } catch { continue; }      // not clickable / covered — nothing to assert
          await page.waitForTimeout(120);
          if (seen.length) leaked.push(`[${view}] ${id} -> ${seen.join(',')}`);
        }
      }
      ok(clicked >= 20, `only ${clicked} controls were actually clicked — the sweep is not sweeping`);
      ok(leaked.length === 0,
         `these controls are enabled with no backend and call the API anyway:\n       ${leaked.join('\n       ')}`);
    });

    // ---- the other direction ------------------------------------------------------------------
    await check('connecting to a control-API brings the same controls back', async () => {
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(400);
      const before = await page.$$eval('[data-needs-api]', (els) => els.filter((e) => e.disabled).length);
      ok(before > 0, 'nothing was disabled before connecting — the first half proves nothing');

      // The connection form lives in the settings view; the sweep above left the page elsewhere.
      await page.evaluate(() => { location.hash = '#v=settings'; });
      await page.waitForTimeout(300);
      await page.fill('#capi', `http://127.0.0.1:${FAKE_API_PORT}`);
      await page.click('#cap-check');
      await page.waitForFunction(
        () => (document.getElementById('cap-status') || {}).textContent?.includes('ok'),
        undefined, { timeout: 10000 });

      const stillDisabled = await page.$$eval('[data-needs-api]', (els) =>
        els.filter((e) => e.disabled).map((e) => e.id || e.className));
      ok(stillDisabled.length === 0,
         `connected, yet still disabled: ${stillDisabled.join(', ')} — a permanently dead control is ` +
         `not honesty, it is a different lie`);
    });

    await check('the showcase banner goes away once there IS a backend', async () => {
      const banner = await page.$('#static-showcase');
      ok(banner && !(await banner.isVisible()),
         'the "this is a static showcase" banner is still shown while connected to a control-API');
    });
  } finally {
    await browser.close();
    pagesSrv.close();
    apiSrv.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
