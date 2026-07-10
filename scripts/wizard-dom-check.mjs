#!/usr/bin/env node
// Sentinel setup-wizard DOM gate (M11.5; closes the "interactive browser run is manual" boundary of ADR-061).
//
// Drives docs/setup/index.html in a real headless Chromium and asserts the behaviours ADR-061 claims:
// step gating, preset prefill, the `sampling` filter, target validation, draft persistence WITHOUT
// secrets, the RU/EN toggle (including <option> labels, which cannot host <span data-lang>), and the
// live-schema override on "Check".
//
// Zero new dependencies: Playwright is resolved out of pw-executor/node_modules (the executor already
// depends on it) and the static server is node:http. CI installs chromium-headless-shell for the
// executor's own tests, so this gate rides on infrastructure that already exists.
//
//   node scripts/wizard-dom-check.mjs [--headed]
//
// Exit 0 = every check passed. Exit 1 = at least one failed (each failure is printed with its reason).

import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const require = createRequire(path.join(REPO, 'pw-executor', 'package.json'));
const { chromium } = require('playwright');

const TOKEN = 'dom-gate-token';
const MIME = {
  '.html': 'text/html; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.md': 'text/plain; charset=utf-8', '.svg': 'image/svg+xml',
};

/* ------------------------------------------------------------------ tiny harness */
const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`  ok   ${name}`);
  } catch (e) {
    results.push({ name, ok: false, err: e.message });
    console.log(`  FAIL ${name}\n       ${e.message.split('\n').join('\n       ')}`);
  }
}
function eq(actual, expected, what) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${what}: expected ${b}, got ${a}`);
}
function ok(cond, what) { if (!cond) throw new Error(what); }

/* ------------------------------------------------------------------ servers */
// Static server over docs/ — mirrors the `webui` compose service (python -m http.server --directory docs).
function startStatic() {
  const root = path.join(REPO, 'docs');
  const srv = createServer(async (req, res) => {
    let p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
    if (p.endsWith('/')) p += 'index.html';
    const abs = path.join(root, p);
    if (!abs.startsWith(root)) { res.writeHead(403).end(); return; }   // traversal guard
    try {
      const body = await readFile(abs);
      res.writeHead(200, { 'Content-Type': MIME[path.extname(abs)] || 'application/octet-stream' });
      res.end(body);
    } catch { res.writeHead(404).end('not found'); }
  });
  return new Promise((resolve) => srv.listen(0, '127.0.0.1', () => resolve({ srv, port: srv.address().port })));
}

async function startControlAPI(port, corsOrigin) {
  const bin = path.join(REPO, 'bin', 'control-api');
  const proc = spawn(bin, [], {
    cwd: REPO,
    env: { ...process.env, CONTROL_API_ADDR: `127.0.0.1:${port}`, CONTROL_API_TOKEN: TOKEN,
           CONTROL_API_CORS_ORIGINS: corsOrigin, CONTROL_API_STORE_ADDR: '' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', (b) => { if (process.env.DOM_GATE_VERBOSE) process.stderr.write(`[capi] ${b}`); });
  for (let i = 0; i < 100; i++) {                       // ~5s budget for the listener to come up
    try {
      const r = await fetch(`http://127.0.0.1:${port}/healthz`);
      if (r.ok) return proc;
    } catch { /* not listening yet */ }
    await new Promise((r) => setTimeout(r, 50));
  }
  proc.kill('SIGKILL');
  throw new Error('control-api did not become healthy within 5s');
}
function freePort() {
  return new Promise((resolve) => {
    const s = createServer();
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

// A control-API OLDER than ADR-060: it answers /healthz, but its /v1/config-schema lacks
// backends/roles/llm. The wizard must REFUSE that payload and keep its snapshot — accepting it
// emptied the backend select and emitted `export LLM_BACKEND=` under a green "valid" banner.
function startStubAPI(corsOrigin) {
  const srv = createServer((req, res) => {
    const h = { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': corsOrigin, 'Vary': 'Origin' };
    if (req.url === '/healthz') { res.writeHead(200, h); res.end(JSON.stringify({ status: 'ok', version: 'stub', runs: 0 })); return; }
    if (req.url === '/v1/config-schema') {
      res.writeHead(200, h);
      res.end(JSON.stringify({ modes: ['explore', 'goal'], planner: ['heuristic'], fields: { target: { type: 'string', required: true } }, note: 'pre-ADR-060 shape' }));
      return;
    }
    res.writeHead(404, h); res.end('{}');
  });
  return new Promise((resolve) => srv.listen(0, '127.0.0.1', () => resolve({ srv, url: `http://127.0.0.1:${srv.address().port}` })));
}

/* ------------------------------------------------------------------ page helpers */
const step = (page) => page.$eval('.subpanel.on', (el) => el.getAttribute('data-subpanel'));
const draft = (page) => page.evaluate(() => localStorage.getItem('sentinel_setup_draft'));
const allStorage = (page) => page.evaluate(() =>
  Object.fromEntries(Object.keys(localStorage).map((k) => [k, localStorage.getItem(k)])));

// A fresh context per check group: localStorage must not leak between checks.
async function freshPage(browser, base) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  page.on('pageerror', (e) => { throw new Error(`uncaught page error: ${e.message}`); });
  await page.goto(`${base}/setup/`, { waitUntil: 'load' });
  // the wizard's boot() ends with a best-effort fetch('../backend-presets.json'); wait for it to land
  await page.waitForFunction(() => document.getElementById('srcbadge').textContent.length > 0);
  return { ctx, page };
}

/* ------------------------------------------------------------------ main */
const staticSrv = await startStatic();
const base = `http://127.0.0.1:${staticSrv.port}`;
const capiPort = await freePort();
const capi = await startControlAPI(capiPort, base);
const capiURL = `http://127.0.0.1:${capiPort}`;
const browser = await chromium.launch({ headless: !process.argv.includes('--headed') });

console.log(`setup wizard DOM gate — docs at ${base}, control-api at ${capiURL}\n`);

try {
  /* 1 — four steps, forward navigation validates every earlier step (ADR-059 "re-ask" loop) */
  await check('steps: 4 panels, Next/Back gating, forward nav stops at the first broken step', async () => {
    const { ctx, page } = await freshPage(browser, base);
    eq(await step(page), 'runtime', 'initial step');
    eq(await page.$$eval('section[data-subpanel]', (n) => n.length), 4, 'panel count');

    await page.click('button[data-next="model"]');
    eq(await step(page), 'model', 'runtime -> model');
    await page.click('button[data-next="params"]');
    eq(await step(page), 'params', 'model -> params');

    // target is required and empty => Next must refuse and keep us on params
    await page.click('button[data-next="review"]');
    eq(await step(page), 'params', 'params -> review blocked while target empty');
    ok((await page.textContent('#e-target')).trim().length > 0, 'target error message rendered');

    await page.fill('#target', 'https://app.example');
    await page.click('button[data-next="review"]');
    eq(await step(page), 'review', 'params -> review with a valid target');

    await page.click('button[data-back="params"]');
    eq(await step(page), 'params', 'Back returns to params');

    // jumping to a later step from the tab bar re-validates: break target, click the review tab
    await page.fill('#target', '');
    await page.click('.subtab-btn[data-subtab="review"]');
    eq(await step(page), 'params', 'tab jump to review re-validates and lands on the broken step');
    await ctx.close();
  });

  /* 2 — preset prefill, and role models are ALWAYS rewritten (the ADR-061 boundary) */
  await check('presets: prefill backend/base_url/vision/structured + ALWAYS rewrite role models', async () => {
    const { ctx, page } = await freshPage(browser, base);
    await page.selectOption('#preset', 'anthropic');
    eq(await page.inputValue('#backend'), 'anthropic', 'anthropic backend');
    eq(await page.inputValue('#baseurl'), '', 'anthropic base_url empty');
    eq(await page.isChecked('#vision'), true, 'anthropic vision');
    eq(await page.isChecked('#structured'), true, 'anthropic structured');
    eq(await page.inputValue('#m-planner'), 'claude-opus-4-8', 'anthropic planner model');
    eq(await page.inputValue('#m-heal'), 'claude-sonnet-4-6', 'anthropic heal model');

    await page.selectOption('#preset', 'ollama');
    eq(await page.inputValue('#backend'), 'openai', 'ollama backend');
    eq(await page.inputValue('#baseurl'), 'http://ollama:11434/v1', 'ollama base_url');
    eq(await page.isChecked('#vision'), false, 'ollama vision off');
    eq(await page.isChecked('#structured'), false, 'ollama structured off');
    // the regression ADR-061 calls out by name: claude-opus-4-8 must NOT survive into LLM_MODEL_PLANNER
    eq(await page.inputValue('#m-planner'), '', 'ollama planner model cleared');
    eq(await page.inputValue('#m-heal'), '', 'ollama heal model cleared');
    await ctx.close();
  });

  /* 3 — `sampling` is filtered out of the backend dropdown */
  await check('backends: `sampling` is never offered', async () => {
    const { ctx, page } = await freshPage(browser, base);
    const opts = await page.$$eval('#backend option', (n) => n.map((o) => o.value));
    ok(!opts.includes('sampling'), `backend options must not contain sampling; got ${JSON.stringify(opts)}`);
    ok(opts.includes('openai') && opts.includes('anthropic'), `expected anthropic+openai; got ${JSON.stringify(opts)}`);
    await ctx.close();
  });

  /* 4 — a bad target blocks (mirrors validTarget() in cmd/control-api/main.go) */
  await check('validation: a non-http/https/file target blocks the step', async () => {
    const { ctx, page } = await freshPage(browser, base);
    await page.click('button[data-next="model"]');
    await page.click('button[data-next="params"]');
    await page.fill('#target', 'javascript:alert(1)');
    await page.click('button[data-next="review"]');
    eq(await step(page), 'params', 'javascript: target blocked');
    ok((await page.textContent('#e-target')).trim().length > 0, 'target error rendered');
    ok(await page.$eval('#target', (el) => el.classList.contains('bad')), '#target marked .bad');

    await page.fill('#target', 'file:///app/testdata/fixtures/l2.html');
    await page.click('button[data-next="review"]');
    eq(await step(page), 'review', 'file:// target accepted');
    await ctx.close();
  });

  /* 5 — the draft survives a reload; LLM_API_KEY and the bearer token never touch localStorage */
  await check('draft: survives reload; #apikey and #capitok are never persisted', async () => {
    const { ctx, page } = await freshPage(browser, base);
    await page.selectOption('#preset', 'ollama');
    await page.click('button[data-next="model"]');          // #m-planner / #apikey live on step 2
    await page.fill('#m-planner', 'qwen3:14b');
    await page.fill('#apikey', 'SEKRIT-LLM-KEY');
    await page.click('button[data-next="params"]');
    await page.fill('#target', 'https://app.example');
    await page.fill('#f-max_steps', '17');
    await page.click('button[data-next="review"]');
    await page.fill('#capi', capiURL);
    await page.fill('#capitok', 'SEKRIT-BEARER');
    await page.waitForFunction(() => (localStorage.getItem('sentinel_setup_draft') || '').includes('17'));

    const store = await allStorage(page);
    const blob = JSON.stringify(store);
    ok(!blob.includes('SEKRIT-LLM-KEY'), `LLM_API_KEY leaked into localStorage: ${blob}`);
    ok(!blob.includes('SEKRIT-BEARER'), `bearer token leaked into localStorage: ${blob}`);
    const d = JSON.parse(store['sentinel_setup_draft']);
    ok(!('apikey' in d), 'draft must not carry an apikey field');
    ok(!('capitok' in d), 'draft must not carry a capitok field');

    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => document.getElementById('srcbadge').textContent.length > 0);
    eq(await page.inputValue('#target'), 'https://app.example', 'target restored');
    eq(await page.inputValue('#m-planner'), 'qwen3:14b', 'role model restored');
    eq(await page.inputValue('#f-max_steps'), '17', 'generated numeric field restored');
    eq(await page.inputValue('#capi'), capiURL, 'control-API URL restored');
    eq(await page.inputValue('#apikey'), '', 'apikey NOT restored');
    eq(await page.inputValue('#capitok'), '', 'bearer token NOT restored');
    ok(!(await page.$eval('#draftnote', (el) => el.classList.contains('hide'))), 'draft-restored note shown');
    await ctx.close();
  });

  /* 5b — "Reset" must return the wizard to step 1, not to the step the broken draft was on */
  await check('draft: Reset clears the draft AND returns to step 1', async () => {
    const { ctx, page } = await freshPage(browser, base);
    await page.click('button[data-next="model"]');
    await page.click('button[data-next="params"]');
    await page.fill('#target', 'https://app.example');
    await page.click('button[data-next="review"]');
    eq(await step(page), 'review', 'parked on review');
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => document.getElementById('srcbadge').textContent.length > 0);
    eq(await step(page), 'review', 'reload restores the step');

    await page.click('#draftclear');
    await page.waitForFunction(() => document.getElementById('srcbadge').textContent.length > 0);
    eq(await draft(page), null, 'draft removed');
    eq(await step(page), 'runtime', 'Reset lands on step 1');
    await ctx.close();
  });

  /* 6 — RU/EN toggles chrome, <option> labels (which cannot host <span data-lang>) and #srcbadge */
  await check('i18n: RU/EN switches <option> labels and #srcbadge', async () => {
    const { ctx, page } = await freshPage(browser, base);
    await page.click('#lang-ru');
    const modeRu = await page.$eval('#mode option[value="explore"]', (o) => o.textContent);
    const plannerRu = await page.$eval('#planner option[value="heuristic"]', (o) => o.textContent);
    const badgeRu = await page.textContent('#srcbadge');
    ok(/авто-исследование/.test(modeRu), `RU mode label: ${modeRu}`);
    ok(/детерминированный/.test(plannerRu), `RU planner label: ${plannerRu}`);
    ok(/схема/.test(badgeRu), `RU badge: ${badgeRu}`);

    await page.click('#lang-en');
    const modeEn = await page.$eval('#mode option[value="explore"]', (o) => o.textContent);
    const plannerEn = await page.$eval('#planner option[value="heuristic"]', (o) => o.textContent);
    const badgeEn = await page.textContent('#srcbadge');
    ok(/autonomous crawl/.test(modeEn), `EN mode label: ${modeEn}`);
    ok(/deterministic/.test(plannerEn), `EN planner label: ${plannerEn}`);
    ok(/schema/.test(badgeEn), `EN badge: ${badgeEn}`);
    eq(await page.getAttribute('#lang-en', 'aria-pressed'), 'true', 'EN button pressed');
    eq(await page.getAttribute('html', 'lang'), 'en', 'document lang');
    await ctx.close();
  });

  /* 7 — "Check" pulls the LIVE schema over CORS and re-renders; the badge flips to live */
  await check('live: "Check" pulls /v1/config-schema and the badge flips to live', async () => {
    const { ctx, page } = await freshPage(browser, base);
    ok(/снимок|snapshot/.test(await page.textContent('#srcbadge')), 'badge starts on the snapshot');
    await page.click('button[data-next="model"]');
    await page.click('button[data-next="params"]');
    await page.fill('#target', 'https://app.example');
    await page.click('button[data-next="review"]');

    await page.fill('#capi', capiURL);
    await page.click('#check');
    await page.waitForFunction(() => /живая|live/.test(document.getElementById('srcbadge').textContent), null, { timeout: 10000 });
    ok(await page.$eval('#srcbadge', (el) => el.classList.contains('live')), '#srcbadge carries the .live class');
    ok(/ok v/.test(await page.textContent('#capistatus')), `capistatus shows healthz: ${await page.textContent('#capistatus')}`);
    eq(await page.$eval('#run', (b) => b.disabled), false, '"Run" unlocked after a successful check');

    // the form must survive the re-render driven by the live schema
    const opts = await page.$$eval('#backend option', (n) => n.map((o) => o.value));
    ok(!opts.includes('sampling') && opts.length > 0, `backend select after live re-render: ${JSON.stringify(opts)}`);
    eq(await page.inputValue('#target'), 'https://app.example', 'target survives the re-render');
    await ctx.close();
  });

  /* 8 — a PARTIAL live schema (pre-ADR-060 control-API) is refused; the snapshot stays */
  await check('live: an incomplete server schema is refused and the snapshot is kept', async () => {
    const stub = await startStubAPI(base);
    try {
      const { ctx, page } = await freshPage(browser, base);
      await page.click('button[data-next="model"]');
      await page.click('button[data-next="params"]');
      await page.fill('#target', 'https://app.example');
      await page.click('button[data-next="review"]');
      await page.fill('#capi', stub.url);
      await page.click('#check');
      await page.waitForFunction(() => /неполная|incomplete/.test(document.getElementById('capistatus').textContent), null, { timeout: 10000 });

      ok(/снимок|snapshot/.test(await page.textContent('#srcbadge')), 'badge must stay on the snapshot');
      ok(!(await page.$eval('#srcbadge', (el) => el.classList.contains('live'))), '#srcbadge must not be .live');
      const opts = await page.$$eval('#backend option', (n) => n.map((o) => o.value));
      eq(opts, ['anthropic', 'openai'], 'backend select must not be emptied by the partial schema');
      ok(/LLM_BACKEND=openai|LLM_BACKEND=anthropic/.test(await page.textContent('#env')), 'env block still carries a backend');
      await ctx.close();
    } finally { stub.srv.close(); }
  });

  /* 9 — the step-2 openai rules mirror make_backend() in brain/llm.py */
  await check('validation: backend=openai needs a model AND (a key or a base_url)', async () => {
    const { ctx, page } = await freshPage(browser, base);
    await page.selectOption('#preset', 'openai');   // "Cloud — OpenAI-compatible": base_url null, real key
    eq(await page.inputValue('#backend'), 'openai', 'openai backend');
    eq(await page.inputValue('#baseurl'), '', 'no default base_url for the generic openai preset');
    await page.click('button[data-next="model"]');
    eq(await step(page), 'model', 'runtime passes with an empty base_url');

    await page.click('button[data-next="params"]');
    eq(await step(page), 'model', 'blocked: no model, no key, no base_url');
    ok((await page.textContent('#e-m-planner')).trim().length > 0, 'missing-model error');
    ok((await page.textContent('#e-apikey')).trim().length > 0, 'missing key/base_url error');

    await page.fill('#m-planner', 'deepseek-chat');
    await page.click('button[data-next="params"]');
    eq(await step(page), 'model', 'still blocked: model present but no key and no base_url');

    await page.fill('#apikey', 'sk-test');
    await page.click('button[data-next="params"]');
    eq(await step(page), 'params', 'passes once a key is supplied');
    ok(/ANTHROPIC_API_KEY/.test(await page.textContent('#env')) === false, 'openai must not emit ANTHROPIC_API_KEY');
    await ctx.close();
  });

  /* 10 — generated numeric fields carry their schema range */
  await check('validation: generated numeric fields enforce their range', async () => {
    const { ctx, page } = await freshPage(browser, base);
    await page.click('button[data-next="model"]');
    await page.click('button[data-next="params"]');
    await page.fill('#target', 'https://app.example');
    await page.fill('#f-coverage_target', '1.5');
    await page.click('button[data-next="review"]');
    eq(await step(page), 'params', 'coverage_target > 1 blocks');
    ok((await page.textContent('#e-f-coverage_target')).trim().length > 0, 'range error rendered');

    await page.fill('#f-coverage_target', '0.85');
    await page.fill('#f-max_steps', '0');
    await page.click('button[data-next="review"]');
    eq(await step(page), 'params', 'max_steps < 1 blocks');

    await page.fill('#f-max_steps', '40');
    await page.click('button[data-next="review"]');
    eq(await step(page), 'review', 'valid budgets pass');
    await ctx.close();
  });
} finally {
  await browser.close();
  capi.kill('SIGTERM');
  staticSrv.srv.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log(`\nfailed: ${failed.map((f) => f.name).join(' · ')}`);
  process.exit(1);
}
