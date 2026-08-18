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

import { hubViews, MIN_VIEWS } from './hub-views.mjs';
import { liveModes, MIN_LIVE_MODES } from './live-modes.mjs';

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

// Everything the page complained about over the WHOLE session, not per-check. `pageErrors` above is
// cleared by check() so a failure names its own cause; these three accumulate, because a console
// error raised while photographing the tools view is still a defect when it surfaces nowhere else.
// Collected rather than ignored on purpose: a smoke that drives every panel and throws the browser's
// own complaints away is measuring less than the browser already measured for it.
const consoleErrors = [];
const failedRequests = [];
const badResponses = [];

// `full` is not a flourish. Measured: the per-view sweep shot the viewport only, and the tools view
// is a calculator ABOVE the capability catalogue — so "look at the screenshot of the tools view"
// showed the calculator and nothing of the thing that had just been rewritten. A screenshot exists to
// make a regression impossible to miss; one that stops at the fold makes half of them easy to miss.
// The flow steps stay viewport-sized on purpose: there the top of the page IS the subject.
async function shot(page, name, full = false) {
  const file = path.join(OUT, `${String(++shotN).padStart(2, '0')}-${name}.png`);
  await page.screenshot({ path: file, fullPage: full });
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
  page.on('pageerror', (e) => { pageErrors.push(e.message); consoleErrors.push(`pageerror: ${e.message}`); });
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(`console: ${m.text()}`); });
  page.on('requestfailed', (r) => {
    // A favicon the deployment does not ship is not a defect of the product; anything else is.
    if (!/favicon/.test(r.url())) failedRequests.push(`${r.method()} ${r.url()} — ${r.failure()?.errorText}`);
  });
  // A 404 is a SUCCESSFUL response, so `requestfailed` never sees it — the first version of this
  // sweep reported "20 console errors" with no URL because of exactly that. Every non-2xx is
  // recorded WITH its URL; which of them count as defects is decided in the check, not here.
  page.on('response', (r) => { if (r.status() >= 400) badResponses.push(`${r.status()} ${r.url()}`); });

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

    await check('the live area offers every mode it declares, and says what each one is', async () => {
      await page.evaluate(() => { location.hash = '#v=chat'; });
      await page.waitForSelector('#lv-mode-frame', { state: 'visible' });
      // Derived, not listed — this was the fourth hand-kept copy of the mode list.
      const modes = liveModes();
      ok(modes.length >= MIN_LIVE_MODES,
        `derived only ${modes.length} live modes — a walk over a short list covers nothing`);
      for (const mode of modes) {
        const btn = await page.$(`#lv-mode-${mode}`);
        ok(btn, `the live area has no ${mode} mode button`);
        await btn.click();
        // Wait for the pane to be SHOWN — the thing the assertions below are about — instead of
        // guessing how long the switch takes. (This comment used to end "the video mode then goes to
        // the network, so it is given its own wait": that sentence outlived the code it described by
        // one commit, which is its own small version of the drift this file gates against.)
        await page.waitForSelector(`#lv-${mode}`, { state: 'visible', timeout: 10000 });
        // Every mode, not just the one that happens to go to the network. `if (mode === 'video')` was
        // a special case keyed on a NAME, which is the shape this wave exists to remove — and it made
        // the check race the `screen` mode, whose pane is replaced asynchronously exactly like the
        // video one. The wait is the SAME condition the assertion below makes, so a pane that answers
        // instantly satisfies it instantly and nothing is slowed down.
        await page.waitForFunction(
          (m) => ((document.getElementById('lv-' + m) || {}).innerText || '').trim().length > 20
                 || !!document.querySelector(`#lv-${m} img, #lv-${m} canvas`),
          mode, { timeout: 20000 });
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

    await check('the run flow explains an empty screen instead of leaving it blank', async () => {
      // No connection has EVER been made to the WS the run-flow rows are drawn from — two runs
      // already finished in this very session (the explore run and the refused goal run, above) and
      // #rf-list stays the honest idle state, because bSubmit/chRunFlow hand the fresh run_id to
      // tFillLiveRunId, which never auto-connects it (M14 keeps the SSE and WS paths separate on
      // purpose — ADR-108d: "a SECOND consumer of the same stream, never a second stream"). This is
      // the same claim the check above makes for the three live modes, made for the pane beneath them.
      await page.evaluate(() => { location.hash = '#v=chat'; });
      await page.waitForSelector('#rf-list', { state: 'visible' });
      const text = (await page.locator('#rf-list').innerText()).trim();
      // Four DIFFERENT sentences, not one generic "nothing yet" — a blank #rf-list here reads as
      // broken, and the most likely real cause (the stream is not connected) must be named rather
      // than buried under a placeholder that could just as well mean the run never started.
      ok(text.length > 20, 'the run-flow pane is empty and silent — indistinguishable from broken');
      ok(/не подключ|not connected/i.test(text), 'does not say the event stream is disconnected (the common case)');
      ok(/не начал|has not started/i.test(text), 'does not mention a run that has not started');
      ok(/шаг/i.test(text) || /step/i.test(text), 'does not mention a run producing no step events');
      ok(/ещё не пришл|has not arrived/i.test(text), 'does not mention data still in flight');
      await shot(page, 'run-flow-idle', true);
    });

    await check('the library and results views load without erroring', async () => {
      // EVERY view, derived from the hub itself (scripts/hub-views.mjs). This list used to be written
      // out here and held seven of nine: `tools` and `settings` were never screenshotted, ever, and
      // the gap survived a deliberate edit — `journal` was appended and the two absent ones were not
      // noticed. A hand-kept list does not show what is missing from it.
      //
      // Screenshotting chat/run/live again is harmless duplication; the alternative — subtracting the
      // ones already covered — would be a second hand-kept list with the same failure mode.
      const views = hubViews();
      ok(views.length >= MIN_VIEWS,
        `derived only ${views.length} views — a walk over a short list passes without covering anything`);
      for (const view of views) {
        await page.evaluate((v) => { location.hash = `#v=${v}`; }, view);
        // The section becoming visible is the state; what it then fetches is captured by the
        // screenshot and by the pageerror listener, which is what this check is actually for.
        await page.waitForSelector(`[data-view="${view}"]`, { state: 'visible', timeout: 15000 });
        await shot(page, `view-${view}`, true);   // whole view, not the fold — see shot()
      }
    });

    // ---------------------------------------------------------------------------------------------
    // EVERY panel, EVERY control, EVERY field — photographed, and the browser's own complaints kept.
    //
    // WHY THIS EXISTS BESIDE THE PER-VIEW SWEEP ABOVE. A full-page shot of a view proves the view
    // rendered; it does not prove that the panel a change touched is legible, because a person
    // reviewing nine tall screenshots reads the top of each. Measured this session: the run-flow pane
    // shipped empty and silent for weeks INSIDE a view that was screenshotted on every CI run.
    //
    // The list of details is DERIVED from the markup, never written here (docs/DEVELOPMENT.md §0.5).
    // A hand-kept list of panels would fail exactly the way the smoke's own view list failed — it
    // held seven of nine and the omission survived a deliberate edit. Floors are the mandatory
    // companion: a selector that stops matching yields an EMPTY inventory, and every assertion over
    // it passes perfectly.
    // Measured, not guessed: the hub marks a view with `data-view` on EVERY section belonging to it
    // (tools carries nine), and those sections ARE the panels — `.card` sits on the section itself in
    // 10 of 17 cases and nests inside one only once. A sweep over `[data-view] .card` therefore found
    // three panels out of two dozen and its floor caught that, which is what the floor is for.
    const FLOORS = { panels: 20, controls: 60, fields: 100 };
    const panelSel = '[data-view], [data-view] .card';

    await check('every panel and control is inventoried from the markup, and the floors are met', async () => {
      const inv = await page.evaluate(() => {
        const name = (el) => {
          const lab = el.labels && el.labels[0];
          return (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder')
                  || (lab && lab.textContent) || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
        };
        const byView = new Map();
        // Every section the router owns, plus any card nested in one. Deduped by ELEMENT: a section
        // that is itself a `.card` matches both halves of the selector and must be counted once.
        for (const el of document.querySelectorAll('[data-view], [data-view] .card')) {
          const host = el.closest('[data-view]');
          const view = host && host.getAttribute('data-view');
          if (!view) continue;
          if (!byView.has(view)) byView.set(view, { view, panels: [], controls: [], fields: [] });
          const v = byView.get(view);
          if (v.panels.some((p) => p.el === el)) continue;
          v.panels.push({
            el,
            key: el.id || `${view}-panel-${v.panels.length}`,
            heading: ((el.querySelector('h1,h2,h3,h4,legend,summary') || {}).textContent || '').trim().slice(0, 60),
          });
        }
        for (const v of byView.values()) {
          const secs = [...document.querySelectorAll(`[data-view="${v.view}"]`)];
          const seen = new Set();
          for (const sec of secs) {
            for (const b of sec.querySelectorAll('button, summary, [role="button"]')) {
              if (seen.has(b)) continue; seen.add(b);
              v.controls.push({ key: b.id || name(b) || '(без имени)', name: name(b) });
            }
            for (const f of sec.querySelectorAll('input, select, textarea')) {
              if (seen.has(f)) continue; seen.add(f);
              v.fields.push({ key: f.id || name(f) || '(без имени)', name: name(f), type: f.tagName.toLowerCase() });
            }
          }
          v.panels = v.panels.map(({ el, ...rest }) => rest);   // elements do not survive serialisation
        }
        return [...byView.values()];
      });
      const tot = (k) => inv.reduce((n, v) => n + v[k].length, 0);
      const counts = { views: inv.length, panels: tot('panels'), controls: tot('controls'), fields: tot('fields') };
      fs.writeFileSync(path.join(OUT, 'inventory.json'), JSON.stringify({ counts, views: inv }, null, 2));
      console.log(`  inventory: ${counts.views} views · ${counts.panels} panels · ${counts.controls} controls · ${counts.fields} fields`);
      ok(counts.views >= MIN_VIEWS, `derived ${counts.views} views, floor is ${MIN_VIEWS} — the walk regressed, not the hub`);
      ok(counts.panels >= FLOORS.panels, `derived ${counts.panels} panels, floor is ${FLOORS.panels}`);
      ok(counts.controls >= FLOORS.controls, `derived ${counts.controls} controls, floor is ${FLOORS.controls}`);
      ok(counts.fields >= FLOORS.fields, `derived ${counts.fields} fields, floor is ${FLOORS.fields}`);
      // A control nobody can name is a control nobody can describe in a bug report.
      const nameless = inv.flatMap((v) => [...v.controls, ...v.fields].filter((c) => !c.name).map((c) => `${v.view}:${c.key || '?'}`));
      ok(nameless.length === 0, `controls with no accessible name: ${nameless.slice(0, 8).join(', ')}`);
    });

    await check('no panel is both empty and silent, and each one is photographed on its own', async () => {
      // The generalisation of UX-PR-8: a pane that shows nothing must SAY why. Asserted for every
      // panel the markup declares, not only the one that was just fixed — otherwise the next silent
      // pane is found the same way this one was, by somebody looking at a screenshot months later.
      const views = hubViews();
      const silent = [];
      let panelShots = 0;
      for (const view of views) {
        await page.evaluate((v) => { location.hash = `#v=${v}`; }, view);
        await page.waitForSelector(`[data-view="${view}"]`, { state: 'visible', timeout: 15000 });
        const cards = await page.$$(`[data-view="${view}"], [data-view="${view}"] .card`);
        for (let i = 0; i < cards.length; i++) {
          const c = cards[i];
          if (!(await c.isVisible())) continue;               // a hidden pane makes no promise
          const id = (await c.getAttribute('id')) || `${view}-panel-${i}`;
          const text = ((await c.innerText()) || '').trim();
          const rich = await c.$('img, canvas, svg, input, select, textarea, table');
          if (text.length < 12 && !rich) silent.push(`${view}/${id}`);
          try {
            await c.screenshot({ path: path.join(OUT, `panel-${view}-${id}.png`) });
            panelShots++;
          } catch { /* a zero-height pane cannot be photographed; the silence check above still judged it */ }
        }
      }
      console.log(`  ${panelShots} per-panel screenshots`);
      ok(panelShots >= FLOORS.panels,
        `photographed ${panelShots} panels, floor is ${FLOORS.panels} — a sweep over nothing passes`);
      ok(silent.length === 0,
        `panels that are empty AND say nothing (indistinguishable from broken): ${silent.join(', ')}`);
    });

    await check('the browser reported no errors while every panel was driven', async () => {
      // Collected across the WHOLE session (see the listeners in main()). These are the product's own
      // complaints; a smoke that discards them measures less than the browser already measured.
      // Not every non-2xx is a defect, and saying so is the difference between a check people keep
      // and one they switch off. Two answers are CONTRACTS, exactly as routeSpec.probe records them
      // server-side (ADR-116): /readyz answers 503 forever in a deployment with no model, and the
      // API answers 403 to a hub that has not signed in yet — that is the product working. Anything
      // else the browser complained about is kept and fails the check.
      // The declared contracts, each with the reason it is one — an allowance without a written
      // reason is how a gate becomes a list of excuses:
      //   503 /readyz          — permanent and CORRECT in a deployment with no model (readyz.go:10-12)
      //   401/403              — the API refusing a hub that has not signed in yet; the product working
      //   404 …/artifact?name= — the hub ASKS whether an optional artifact exists; 404 means "no"
      //   404 /v1/config       — the standalone tier before the wizard has written anything
      // ⚠ The artifact probes are a contract but not a virtue: the hub asks one question per name
      //   from a list it keeps itself (ART_NAMES), so an explore run leaves eleven 404s in the
      //   console, and a real error is easy to miss among them. Recorded as [UI-ARTIFACT-PROBE-STORM].
      const contract = (s) => /^503 .*\/readyz/.test(s)
        || /^40[13] /.test(s)
        || /^404 .*\/artifact\?name=/.test(s)
        || /^404 .*\/v1\/config$/.test(s);
      const noise = /Failed to load resource/;   // the console echo of a response already recorded, with no URL
      const defects = [
        ...consoleErrors.filter((e) => !noise.test(e)),
        ...failedRequests.map((r) => `request failed: ${r}`),
        ...badResponses.filter((r) => !contract(r)).map((r) => `non-2xx: ${r}`),
      ];
      const seen = [...new Set([...consoleErrors, ...failedRequests, ...badResponses])];
      if (seen.length) fs.writeFileSync(path.join(OUT, 'browser-errors.txt'), seen.join('\n') + '\n');
      console.log(`  browser reported ${seen.length} line(s); ${defects.length} of them are defects (the rest are declared contracts)`);
      ok(defects.length === 0, `${defects.length} browser-side defect(s):\n       ${[...new Set(defects)].slice(0, 8).join('\n       ')}`);
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
