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
// --headed needs the FULL chromium build. CI (and the default headless path) only need
// chromium-headless-shell, so --headed fails on a CI-provisioned checkout until you run:
//   (cd pw-executor && npx playwright install chromium)
//
// Exit 0 = every check passed. Exit 1 = at least one failed (each failure is printed with its reason).

import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import fs from 'node:fs';
import { tmpdir } from 'node:os';
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
// Uncaught page exceptions are COLLECTED, not thrown from the listener: a throw inside an EventEmitter
// callback never reaches the awaiting check() — it escapes as an unhandled rejection instead.
const pageErrors = [];
async function check(name, fn) {
  pageErrors.length = 0;   // per-check, so one broken page does not poison every later check
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

// A real store-gateway on a unix socket, so PUT /v1/config exercises the config domain end to end
// (and /readyz has a store dependency to probe) rather than short-circuiting to 501.
async function startStoreGateway(dir) {
  const sock = path.join(dir, 'store.sock');
  const proc = spawn(path.join(REPO, 'bin', 'store-gateway'),
    ['--addr', sock, '--db', path.join(dir, 'store.db'), '--no-auth'],
    { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'] });
  proc.stderr.on('data', (b) => { if (process.env.DOM_GATE_VERBOSE) process.stderr.write(`[store] ${b}`); });
  const { access } = await import('node:fs/promises');
  for (let i = 0; i < 100; i++) {                       // wait for the socket to appear
    try { await access(sock); return { proc, addr: `unix:${sock}` }; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 50));
  }
  proc.kill('SIGKILL');
  throw new Error('store-gateway did not create its socket within 5s');
}

// cwd defaults to the repo (where runs/ and state/ already live). The standalone-tier check passes a
// temp dir instead: ADR-075 makes a save land in <cwd>/state/config.json, and a gate must not write the
// developer's — or CI's — repository.
async function startControlAPI(port, corsOrigin, storeAddr, cwd) {
  const bin = path.join(REPO, 'bin', 'control-api');
  const proc = spawn(bin, [], {
    cwd: cwd || REPO,
    env: { ...process.env, CONTROL_API_ADDR: `127.0.0.1:${port}`, CONTROL_API_TOKEN: TOKEN,
           CONTROL_API_CORS_ORIGINS: corsOrigin, CONTROL_API_STORE_ADDR: storeAddr || '',
           LLM_BASE_URL: '' },
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
// ADR-064 mode 3: the SAME binary serving the UI from its own port, with no token supplied and no CORS
// allowlist. It must generate its own token and print a one-time bootstrap nonce — which is the only
// place that nonce ever appears, so we scrape it from stderr exactly like an operator reads their log.
// cwd is the temp dir on purpose: state/control-api.token must not land in the repo during CI.
//
// storeAddr picks the TIER, and the two tiers are genuinely different products of the same binary:
// '' is the standalone tier (configfile.go:13 — "the operator chose the standalone tier and the file
// IS the config"), where local accounts are impossible because session.go:313 answers 503 without a
// store. Anything else is the account-bearing tier. Both are shipped, so both are measured — see the
// pair of mode-3 checks below.
async function startModeThreeAPI(port, cwd, storeAddr = '') {
  const proc = spawn(path.join(REPO, 'bin', 'control-api'), [], {
    cwd,
    env: { ...process.env,
           CONTROL_API_ADDR: `127.0.0.1:${port}`,
           CONTROL_API_TOKEN: '', CONTROL_API_AUTOTOKEN: '', CONTROL_API_TOKEN_FILE: '',
           CONTROL_API_CORS_ORIGINS: '',   // mode 3 is same-origin — no allowlist needed at all
           CONTROL_API_SERVE_UI: '1', CONTROL_API_UI_DIR: '',
           CONTROL_API_STORE_ADDR: storeAddr, LLM_BASE_URL: '' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  proc.stderr.on('data', (b) => { log += b; if (process.env.DOM_GATE_VERBOSE) process.stderr.write(`[capi3] ${b}`); });
  for (let i = 0; i < 100; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/healthz`);
      if (r.ok) {
        const m = /[?&]bootstrap=([0-9a-f]+)/.exec(log);
        if (!m) { proc.kill('SIGKILL'); throw new Error(`no bootstrap nonce in the startup log:\n${log}`); }
        return { proc, nonce: m[1] };
      }
    } catch (e) { if (String(e.message).startsWith('no bootstrap')) throw e; }
    await new Promise((r) => setTimeout(r, 50));
  }
  proc.kill('SIGKILL');
  throw new Error('mode-3 control-api did not become healthy within 5s');
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
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await page.goto(`${base}/setup/`, { waitUntil: 'load' });
  await settled(page);
  return { ctx, page };
}
// boot() calls setSrcBadge() synchronously, so a non-empty #srcbadge does NOT mean the trailing
// fetch('../backend-presets.json') has resolved. `presetsSrc` flipping to 'file' does. Waiting on the
// weaker signal would race every preset assertion against that fetch.
const settled = (page) => page.waitForFunction(() => window.presetsSrc === 'file', null, { timeout: 15000 });

// --headed resolves a different browser build than the headless shell CI installs. Fail with the cure,
// not with Playwright's raw "Executable doesn't exist at .../chromium-1228/..." path dump.
async function launchBrowser() {
  const headed = process.argv.includes('--headed');
  try {
    return await chromium.launch({ headless: !headed });
  } catch (e) {
    if (headed) {
      throw new Error(`--headed needs the full chromium build, which CI does not install.\n` +
        `  fix: (cd pw-executor && npx playwright install chromium)\n  original: ${e.message}`);
    }
    throw e;
  }
}

/* ------------------------------------------------------------------ main */
// Every resource is acquired INSIDE the try. Acquiring bin/control-api before it meant a failing
// chromium.launch() skipped the only capi.kill() in the script, orphaning a live process on a live port.
let staticSrv = null, capi = null, store = null, browser = null, tmp = null, base = '', capiURL = '';
let capi3 = null;
// Второй стенд режима 3 — ярус С хранилищем (проверка 9b). Объявлен здесь, а не внутри проверки, по
// той же причине, что и всё остальное в этом списке: упавшая проверка обязана быть прибрана в
// `finally`, иначе гейт оставляет живой процесс на живом порту.
let capi3b = null, store3b = null, tmp3b = null;
try {
  staticSrv = await startStatic();
  base = `http://127.0.0.1:${staticSrv.port}`;
  tmp = await mkdtemp(path.join(tmpdir(), 'sentinel-domgate-'));
  store = await startStoreGateway(tmp);
  const capiPort = await freePort();
  capi = await startControlAPI(capiPort, base, store.addr);
  capiURL = `http://127.0.0.1:${capiPort}`;
  browser = await launchBrowser();

  console.log(`setup wizard DOM gate — docs at ${base}, control-api at ${capiURL}, store at ${store.addr}\n`);

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
    await settled(page);
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
    await settled(page);
    eq(await step(page), 'review', 'reload restores the step');

    await page.click('#draftclear');
    await settled(page);
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

  /* 10b — ADR-085: operator settings are rendered from schema.settings, explained in the reader's
     language, and only EXPORTED when actually changed. Before this, these knobs existed solely as
     environment variables — findable by reading source, which for their audience is not findable. */
  await check('settings: rendered from the schema, explained bilingually, exported only when changed', async () => {
    const { ctx, page } = await freshPage(browser, base);
    await page.click('button[data-next="model"]');
    await page.click('button[data-next="params"]');

    const rows = await page.$$eval('#settings input', (els) => els.map((e) => e.id));
    ok(rows.length >= 10, `settings rendered (${rows.length} controls)`);
    ok(rows.includes('s-log_ttl_hours'), 'the TTL knob this was asked for is present');

    // The hint is the WHOLE POINT: a number with no explanation is what the env var already was.
    const hintRu = await page.textContent('#settings div:has(> #s-log_ttl_hours) .hint');
    ok(/\u0447\u0430\u0441\u043e\u0432/.test(hintRu), 'RU hint explains the unit, not just the name');
    await page.click('button[data-lang-set="en"]').catch(() => {});
    await page.evaluate(() => { document.documentElement.lang = 'en'; });

    // Bounds come from the descriptor, not from a per-key branch: hours must not inherit step=1000.
    const stepAttr = await page.getAttribute('#s-log_ttl_hours', 'step');
    eq(stepAttr, '1', 'step comes from the schema');
    const confStep = await page.getAttribute('#s-heal_auto', 'step');
    eq(confStep, '0.05', 'a 0..1 confidence gets a 0.05 step, not 1000');
    eq(await page.getAttribute('#s-heal_auto', 'max'), '1', 'and its max');

    await page.fill('#target', 'https://app.example');
    await page.click('button[data-next="review"]');
    eq(await step(page), "review", "settings do not block the step");

    // Untouched settings must NOT appear: every exported line should mean "I decided this".
    let env = await page.textContent('#env');
    ok(!/SENTINEL_LOG_TTL_HOURS/.test(env), 'a default-valued setting is not exported');

    await page.click('button[data-back="params"]');
    await page.fill('#s-log_ttl_hours', '48');
    await page.click('button[data-next="review"]');
    env = await page.textContent('#env');
    ok(/export SENTINEL_LOG_TTL_HOURS=48/.test(env), 'a changed setting reaches the generated env');
    await ctx.close();
  });

  /* 10c — ADR-086: collapsible per-field help (the OPNsense idiom) — folded by default, one switch
     opens them all, and the choice survives a reload. Folded by default is the decision under test:
     a form where every field carries a paragraph is as unreadable as one explaining nothing. */
  await check('help: folded by default, one switch opens all, choice persists, bilingual', async () => {
    const { ctx, page } = await freshPage(browser, base);

    // Static fields carry help now — they explained nothing at all before.
    const markers = await page.$$eval('details.fhelp', (els) => els.length);
    ok(markers >= 10, `help attached to ${markers} fields`);
    const openAtStart = await page.$$eval('details.fhelp[open]', (els) => els.length);
    eq(openAtStart, 0, 'every block starts folded');

    // A folded block still HAS its text — the reader can reach it, the layout is not paying for it.
    // textContent works on any step; clicking needs the field's step to be the visible one.
    const targetHelp = await page.textContent('label[for="target"] details.fhelp .fhelp-body');
    ok(/file:\/\//.test(targetHelp), 'target help explains the consequence, not the label');

    await page.click('button[data-next="model"]');
    await page.click('button[data-next="params"]');
    await page.click('label[for="target"] details.fhelp > summary');
    eq(await page.$$eval('details.fhelp[open]', (e) => e.length), 1, 'clicking one opens exactly one');

    // The switch opens every one of them, and the preference is remembered.
    await page.check('#helpall');
    const allOpen = await page.$$eval('details.fhelp[open]', (e) => e.length);
    eq(allOpen, markers, 'the switch opens all of them');
    await page.reload();
    // `attached`, not the default `visible`: the first help block belongs to a step that is not the
    // one shown after a reload, and waiting for visibility would hang on a page that is perfectly fine.
    await page.waitForSelector('details.fhelp', { state: 'attached' });
    ok(await page.isChecked('#helpall'), 'the preference survives a reload');
    ok((await page.$$eval('details.fhelp[open]', (e) => e.length)) >= 10, 'and re-opens the blocks');

    // Bilingual: switching language switches the help text with everything else.
    await page.click('#lang-en');
    const en = await page.textContent('label[for="pwnotrace"] details.fhelp .fhelp-body');
    ok(/Playwright has no way to mask/.test(en), 'EN help renders under EN');
    await ctx.close();
  });

  /* 10d — ADR-091: a preset says what it takes to make it work. Only ollama and litellm have a real
     compose service; the other seven are placeholders whose address the user must replace, and that
     was stated only in a `_note` inside the JSON nobody opens. A dropdown entry that silently means
     "you have more work to do" is the LiteLLM defect one level down. */
  await check('presets: each says whether it is a real service or a placeholder', async () => {
    const { ctx, page } = await freshPage(browser, base);
    // #preset lives on the step the wizard opens with — the neighbouring preset check selects it
    // straight away, and clicking "next" first moves PAST it.
    await page.selectOption('#preset', 'litellm');
    const lite = await page.textContent('.presetnote');
    ok(/docker compose --profile litellm/.test(lite), 'litellm names the command that starts it');
    ok(/deploy\/litellm\/config\.yaml/.test(lite), 'and where the provider keys go');

    await page.selectOption('#preset', 'ollama');
    ok(/--profile ollama/.test(await page.textContent('.presetnote')), 'ollama names its command');

    await page.selectOption('#preset', 'vllm');
    const vllm = await page.textContent('.presetnote');
    ok(/ЗАГОТОВКА|PLACEHOLDER/.test(vllm), 'a preset with no compose service says so');

    // Bilingual, like every other hint on the page.
    await page.click('#lang-en');
    ok(/PLACEHOLDER/.test(await page.textContent('.presetnote')), 'and says it in EN too');
    await ctx.close();
  });

  /* 11 — M11.5 PR-5: "Save to server" writes the config domain and flips /readyz 503 -> 200 */
  await check('config: "Save to server" persists a secret-free document and readiness flips 503 -> 200', async () => {
    const readyz = async (tok) => {
      const r = await fetch(`${capiURL}/readyz`, tok ? { headers: { Authorization: `Bearer ${tok}` } } : undefined);
      return { code: r.status, body: await r.json() };
    };

    // store is wired but nothing is stored yet -> not ready, and an anonymous caller learns no topology
    const before = await readyz(null);
    eq(before.code, 503, '/readyz before any config');
    eq(before.body.status, 'not_ready', 'overall status');
    eq(before.body.checks.store.status, 'ok', 'store check');
    eq(before.body.checks.config.status, 'error', 'config check');
    ok(before.body.checks.store.detail === undefined, 'anonymous caller must not receive detail strings');
    const beforeAuthed = await readyz(TOKEN);
    ok(typeof beforeAuthed.body.checks.config.detail === 'string', 'an authenticated caller receives detail');

    const { ctx, page } = await freshPage(browser, base);
    await page.selectOption('#preset', 'anthropic');     // no base_url -> the llm probe stays `skipped`
    await page.click('button[data-next="model"]');
    await page.fill('#apikey', 'SEKRIT-NEVER-SENT');     // typed, and must never reach the server
    await page.click('button[data-next="params"]');
    await page.fill('#target', 'https://app.example');
    await page.click('button[data-next="review"]');
    await page.fill('#capi', capiURL);
    await page.fill('#capitok', TOKEN);
    await page.click('#savecfg');
    await page.waitForFunction(() => /сохранён|saved to/.test(document.getElementById('capistatus').textContent), null, { timeout: 10000 });

    const after = await readyz(null);
    eq(after.code, 200, '/readyz after saving the config');
    eq(after.body.status, 'ready', 'overall status');
    eq(after.body.checks.config.status, 'ok', 'config check');
    eq(after.body.checks.llm.status, 'skipped', 'anthropic has no base_url -> llm probe skipped');

    const stored = await (await fetch(`${capiURL}/v1/config`, { headers: { Authorization: `Bearer ${TOKEN}` } })).json();
    const blob = JSON.stringify(stored);
    ok(!blob.includes('SEKRIT'), `the API key reached the server: ${blob}`);
    ok(!/api_key|apikey/i.test(blob), `a secret-shaped field reached the server: ${blob}`);
    eq(stored.config.llm.backend, 'anthropic', 'stored backend');
    eq(stored.config.llm.model.planner, 'claude-opus-4-8', 'stored planner model');
    eq(stored.config.run.target, 'https://app.example', 'stored target');
    ok(typeof stored.updated_at === 'string' && stored.updated_at.length > 0, 'updated_at reported');

    // a configured but unreachable LLM endpoint must FAIL readiness (port 1 = instant refusal)
    await page.click('.subtab-btn[data-subtab="runtime"]');   // #preset lives on step 1
    await page.selectOption('#preset', 'ollama');
    await page.fill('#baseurl', 'http://127.0.0.1:1/v1');
    await page.click('button[data-next="model"]');
    await page.fill('#m-planner', 'qwen3:14b');
    await page.click('button[data-next="params"]');
    await page.click('button[data-next="review"]');
    await page.click('#savecfg');
    await page.waitForFunction(() => /сохранён|saved to/.test(document.getElementById('capistatus').textContent), null, { timeout: 10000 });

    const dead = await readyz(TOKEN);
    eq(dead.code, 503, '/readyz with an unreachable llm base_url');
    eq(dead.body.checks.llm.status, 'error', 'llm check');
    eq(dead.body.checks.config.status, 'ok', 'config is still stored');
    await ctx.close();
  });

  // ADR-075. The defect: with no store-gateway, PUT /v1/config answered 501 whose text claimed "this
  // deployment keeps its config in a file (standalone tier)" while no file was written anywhere, and the
  // wizard relayed that as a soft refusal. The claim now is that the save REALLY LANDS — so the check
  // reads the document back off disk, not just the status line, and then proves the config reaches a run
  // by asking the server for the env layer it feeds (ADR-063 layer 3, which was dead in this tier).
  await check('config: with no store-gateway the wizard still saves — to a file that really exists', async () => {
    const cwd = fs.mkdtempSync(path.join(tmpdir(), 'sentinel-filetier-'));
    const port = 18790 + Math.floor(Math.random() * 60);
    const proc = await startControlAPI(port, base, '', cwd);   // storeAddr '' = the standalone tier
    const url = `http://127.0.0.1:${port}`;
    try {
      const cfgPath = path.join(cwd, 'state', 'config.json');
      ok(!fs.existsSync(cfgPath), 'the temp deployment already had a config before the wizard saved one');

      const { ctx, page } = await freshPage(browser, base);
      try {
        await page.selectOption('#preset', 'ollama');
        await page.fill('#baseurl', 'http://127.0.0.1:1/v1');
        await page.click('button[data-next="model"]');
        await page.fill('#m-planner', 'qwen3:14b');
        await page.click('button[data-next="params"]');
        await page.fill('#target', 'https://app.example');
        await page.click('button[data-next="review"]');
        await page.fill('#capi', url);
        await page.fill('#capitok', TOKEN);
        await page.click('#savecfg');
        await page.waitForFunction(
          () => /сохранён|saved to/.test(document.getElementById('capistatus').textContent),
          null, { timeout: 10000 });

        // The status line must name the MEDIUM. "Saved" alone was the old lie's shape.
        const status = await page.locator('#capistatus').textContent();
        ok(/файл|file/i.test(status), `the status line does not say where it saved: ${status}`);
      } finally { await ctx.close(); }

      // On disk, not merely reported.
      ok(fs.existsSync(cfgPath), `nothing was written to ${cfgPath} — the 501 lie has become a 200 lie`);
      const onDisk = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
      // The file embeds the document as real JSON (the store column escapes it as a string) — a config
      // file is meant to be readable by whoever runs the deployment.
      const doc = onDisk.value_json;
      ok(doc && typeof doc === 'object', `value_json is not an embedded document: ${typeof doc}`);
      eq(doc.llm.backend, 'openai', 'stored backend');
      eq(doc.llm.model.planner, 'qwen3:14b', 'stored planner model');
      ok(!JSON.stringify(doc).match(/api_key|apikey/i), 'a secret-shaped field reached the file');

      // Read back over HTTP, and the tier is named there too.
      const got = await (await fetch(`${url}/v1/config`, { headers: { Authorization: `Bearer ${TOKEN}` } })).json();
      eq(got.tier, 'file', 'GET /v1/config tier');
      eq(got.config.run.target, 'https://app.example', 'round-tripped target');

      // ADR-063 layer 3 in this tier: readiness now probes the llm base_url that only the FILE knows
      // about. Port 1 refuses instantly, so an `error` here proves the server read the saved document —
      // a `skipped` would mean it never saw it.
      const rz = await (await fetch(`${url}/readyz`, { headers: { Authorization: `Bearer ${TOKEN}` } })).json();
      eq(rz.checks.config.status, 'ok', 'config check in the file tier');
      eq(rz.checks.llm.status, 'error', 'the llm probe never used the base_url from the saved file');
    } finally {
      proc.kill('SIGKILL');
      fs.rmSync(cwd, { recursive: true, force: true });
    }
  });

  /* 9a — ADR-064 mode 3 on the STANDALONE tier (no store-gateway), ADR-156 exchange.
   *
   * ⚠ ЭТА ПРОВЕРКА БЫЛА ОДНОЙ И ТРЕБОВАЛА НЕВОЗМОЖНОГО. Она поднимала стенд БЕЗ хранилища
   * (`CONTROL_API_STORE_ADDR: ''`) и ждала, что на нём завершится заведение администратора — а
   * `session.go:313` на таком стенде отвечает 503 `local accounts need a store-gateway`, и это НЕ
   * дефект, а объявленное свойство яруса. Тот же довод записан в самом ADR-156 (право безопасно
   * выдавать там, где аккаунт завести негде, ИМЕННО потому что создание ответит 503) — то есть
   * проверка противоречила тексту, который её же правка и написала. Замер: обмен→200 с `setup`,
   * `POST /v1/users`→503, поле кредентиала пусто, `#fa-status` называет причину; гейт при этом
   * падал по таймауту ожидания поля, ВЫБРАСЫВАЯ сообщение, которым страница объясняет отказ.
   *
   * Поэтому проверок теперь ДВЕ, по одной на ярус, и утверждают они разное. Здесь — что ярус без
   * хранилища НЕ МОЛЧИТ: право выдано, машинный секрет в браузер не попал, а человеку НАЗВАНА
   * причина, по которой дальше хода нет. Молчаливый отказ и есть тот дефект, ради которого
   * заводился [UI-FIRST-RUN-BOOTSTRAP-DEAD-END]. */
  await check('mode 3, standalone tier: the grant is issued, no machine token reaches the browser, and the page NAMES why it can go no further', async () => {
    const port3 = await freePort();
    capi3 = await startModeThreeAPI(port3, tmp);      // storeAddr omitted — standalone on purpose
    const ui = `http://127.0.0.1:${port3}`;

    // The pages come from the binary's embedded FS, not from the static server used above.
    eq((await fetch(`${ui}/setup/`)).status, 200, 'GET /setup/ from the embedded UI');
    eq((await fetch(`${ui}/COMPETITIVE_ANALYSIS.internal.md`)).status, 404, 'internal doc must not be served');
    eq((await fetch(`${ui}/v1/not-an-endpoint`)).status, 404, 'unknown /v1 path must not fall through to the UI');

    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await page.goto(`${ui}/setup/?bootstrap=${capi3.nonce}`, { waitUntil: 'load' });
    await settled(page);

    // ⚠ ПРОВЕРКА ПЕРЕПИСАНА ПОД ADR-156, А НЕ ОСЛАБЛЕНА. Она ждала, что бутстрап ЗАПОЛНИТ поле
    // кредентиала — то есть ровно то поведение, которое и оказалось дефектом: обмен вручал странице
    // МАШИННЫЙ токен (незаскоупленный, постоянный, общий с CI). Теперь обмен отдаёт одноразовое
    // право завести первого администратора; поле обязано остаться ПУСТЫМ, а мастер — предложить
    // создание администратора. Утверждение стало сильнее: не «поле заполнено ожидаемым», а
    // «машинный секрет в браузер не попал вообще».
    await page.waitForSelector('#firstadmin:not([hidden])', { timeout: 10000 });
    eq(await page.inputValue('#capitok'), '', 'the machine token must never reach the browser');
    eq(await page.inputValue('#capi'), ui, 'control-API URL prefilled with the serving origin');
    ok(!page.url().includes('bootstrap='), `the nonce was left in the URL: ${page.url()}`);

    // Попытка довести первый запуск до конца ЗДЕСЬ ОБЯЗАНА УПЕРЕТЬСЯ — и упереться ВСЛУХ.
    // Утверждается не «кнопка не сработала», а три разных свойства отказа: причина НАЗВАНА и
    // называет виновника (store-gateway), кредентиал так и не появился, форма осталась на экране.
    // Проверка «поле пусто» сама по себе прошла бы и над страницей, которая молча ничего не делает.
    await page.fill('#fa-name', 'wizadmin');
    await page.fill('#fa-pass', 'a-long-enough-passphrase');
    await page.click('#fa-create');
    // ⚠ Ждать «непустой статус» НЕЛЬЗЯ: обработчик СНАЧАЛА ставит многоточие «…» как признак работы
    // (docs/setup/index.html, firstAdmin), и такое ожидание выигрывает гонку у самого ответа —
    // проверка читала бы «…» и падала с ним же в сообщении. Ждём статус, ОТЛИЧНЫЙ от многоточия.
    await page.waitForFunction(() => {
      const t = (document.getElementById('fa-status').textContent || '').trim();
      return t.length > 0 && t !== '…';
    }, null, { timeout: 15000 });
    const status = (await page.textContent('#fa-status')).trim();
    ok(/store-gateway/.test(status),
      `the page must NAME the component whose absence blocks the first run, got: ${JSON.stringify(status)}`);
    eq(await page.inputValue('#capitok'), '', 'a credential appeared on a tier that cannot have accounts');
    ok(await page.locator('#firstadmin').isVisible(),
      'the first-run block hid itself even though no administrator was created');

    // Single-use: replaying the nonce an operator may still have in their scrollback buys nothing.
    // 403 and not 409 — this deployment still has no accounts, so it is the NONCE that is spent.
    // The 409 branch belongs to the account-bearing tier and is asserted in the next check.
    eq((await fetch(`${ui}/v1/ui-token?nonce=${capi3.nonce}`)).status, 403, 'replayed nonce');
    await ctx.close();
  });

  /* 9b — the SAME binary and the SAME first run, on the tier that HAS a store-gateway: this is the
   * default deployment (docker-compose.yml:207 sets CONTROL_API_STORE_ADDR), so it is the path an
   * operator actually walks. Here the first run must COMPLETE — grant → administrator → session —
   * and the machine token must still never reach the browser. Measured live before it was written:
   * exchange → 200 with `setup`, POST /v1/users → 201 `is_admin:true`, login → 64-hex session with
   * expires_in_seconds 43200, replayed nonce → 409 (not 403: once an account exists the deployment
   * state is checked BEFORE the nonce, so the answer names signing in instead of the spent nonce). */
  await check('mode 3, account tier: the first run completes — grant becomes an administrator, then a session, and never the machine token', async () => {
    const port3b = await freePort();
    tmp3b = await mkdtemp(path.join(tmpdir(), 'sentinel-domgate-tier2-'));
    store3b = await startStoreGateway(tmp3b);
    capi3b = await startModeThreeAPI(port3b, tmp3b, store3b.addr);
    const ui = `http://127.0.0.1:${port3b}`;

    // ⚠ ВЕСЬ ПЕРВЫЙ ЗАПУСК ГОНЯЕТСЯ ПОД ВРАЖДЕБНЫМ ЧЕРНОВИКОМ, и это не украшение проверки.
    // Замерено на этом самом гейте: обработчик слал первичную настройку по адресу из поля `#capi`,
    // которое несёт `data-draft` и восстанавливается из прошлой сессии, а бутстрап подставляет свой
    // origin ТОЛЬКО в пустое поле — то есть черновик побеждал. С черновиком на чужой хост туда
    // уходили `X-Setup-Grant` и `{"name":…,"password":…}`, а следом тот же пароль ещё раз в
    // `POST /v1/login`. Право признаётся сервером лишь при `sameOriginRequest` (`access.go:247`),
    // поэтому чужой адрес не может СРАБОТАТЬ — он может только утечь.
    //
    // Ловушка слушает по-настоящему: утверждать «поле равно origin» было бы утверждением о ФОРМЕ
    // страницы, а не о том, куда ушёл запрос. Здесь наблюдается сам адресат.
    const trapped = [];
    const trap = createServer((req, res) => {
      let b = ''; req.on('data', (c) => { b += c; });
      req.on('end', () => {
        trapped.push({ method: req.method, url: req.url, grant: req.headers['x-setup-grant'] || null, body: b });
        res.writeHead(200, { 'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' });
        res.end('{"user_id":"trap","name":"trap","is_admin":true,"session":"trap"}');
      });
    });
    await new Promise((r) => trap.listen(0, '127.0.0.1', r));
    const trapURL = `http://127.0.0.1:${trap.address().port}`;

    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    page.on('pageerror', (e) => pageErrors.push(e.message));
    try {
      await page.goto(`${ui}/setup/`, { waitUntil: 'load' });
      await page.evaluate((u) => localStorage.setItem('sentinel_setup_draft', JSON.stringify({ capi: u })), trapURL);
      await page.goto(`${ui}/setup/?bootstrap=${capi3b.nonce}`, { waitUntil: 'load' });
      await settled(page);
      eq(await page.inputValue('#capi'), trapURL, 'the hostile draft did not survive — the trap would measure nothing');

    await page.waitForSelector('#firstadmin:not([hidden])', { timeout: 10000 });
    eq(await page.inputValue('#capitok'), '', 'the machine token must never reach the browser');
    // Утверждения «#capi равен origin» здесь НЕТ намеренно: черновик победил, и именно это условие
    // проверка и создаёт. Прежнее поведение поля живёт в проверке 9a, где черновика нет.
    ok(!page.url().includes('bootstrap='), `the nonce was left in the URL: ${page.url()}`);

    await page.fill('#fa-name', 'wizadmin');
    await page.fill('#fa-pass', 'a-long-enough-passphrase');
    await page.click('#fa-create');
    await page.waitForFunction(() => document.getElementById('capitok').value.length > 0, null, { timeout: 15000 });

    // ⚠ ЛОВУШКА СПРАШИВАЕТСЯ ПЕРВОЙ, И ПОРЯДОК ЗДЕСЬ — ЧАСТЬ ПРОВЕРКИ. Утечка проявляется и ниже
    // (в поле оказывается поддельная сессия чужого хоста, и утверждение о ФОРМЕ краснеет), но
    // сообщение получилось бы «the session has the wrong shape: trap» — то есть про форму строки,
    // а не про то, что пароль администратора уехал на чужой адрес. Замерено мутацией: именно так
    // гейт и падал, пока эти три строки стояли в конце. Диагноз обязан называть дефект.
    const leaked = trapped.filter((t) => /\/v1\/(users|login)/.test(t.url));
    eq(leaked.length, 0, `first-run credentials went to the address from the draft: ${JSON.stringify(leaked)}`);
    ok(!trapped.some((t) => t.grant), `the setup grant was sent off-origin: ${JSON.stringify(trapped)}`);
    ok(!trapped.some((t) => /a-long-enough-passphrase/.test(t.body || '')),
      `the administrator password was sent off-origin: ${JSON.stringify(trapped)}`);

    const tok = await page.inputValue('#capitok');
    ok(/^[0-9a-f]{64}$/.test(tok), `the session has the wrong shape: ${tok}`);
    // Читаем машинный токен со стенда: утверждение «это НЕ он» стоит ровно столько, сколько стоит
    // сравнение с настоящим значением, а не с догадкой о его форме (обе строки — 64 hex-символа).
    const machineTok = fs.readFileSync(path.join(tmp3b, 'state', 'control-api.token'), 'utf8').trim();
    ok(tok !== machineTok, 'the field holds the MACHINE token, not a session');
    ok(await page.locator('#firstadmin').isHidden(), 'the first-run block stayed visible after the admin was created');

    // Заводимый первым запуском аккаунт обязан быть АДМИНИСТРАТОРОМ, хотя форма этого не просила
    // (ADR-156): иначе окно закрылось бы обычным пользователем и развёртывание осталось без админа.
    // Спрашиваем сервер, а не страницу — страница о роли не знает и знать не должна.
    // ⚠ Форма ответа: `{machine, scoped, user:{is_admin,…}}` — признак лежит ПОД `user`, и первая
    // редакция этой строки читала его с верхнего уровня, то есть утверждала бы `undefined === true`
    // над совершенно исправным сервером. Поймано прогоном, а не чтением.
    const me = await (await fetch(`${ui}/v1/me`, { headers: { Authorization: `Bearer ${tok}` } })).json();
    ok(me && me.user && me.user.is_admin === true,
      `the first account is not an administrator: ${JSON.stringify(me)}`);
    // Сессия человека обязана быть ЗАСКОУПЛЕНА — незаскоупленной бывает только машина. Если бы
    // первый запуск выдал машинный кредентиал (дефект, ради которого писался ADR-156), здесь стояло
    // бы `machine:true, scoped:false`, и одна эта строка отличает починку от её видимости.
    eq([me.machine, me.scoped], [false, true], 'the first-run credential is a machine token, not a human session');

    // Same invariant as check 5, now for a credential the page was GIVEN rather than typed.
    const blob = await page.evaluate(() => JSON.stringify(localStorage));
    ok(!blob.includes(tok), `the session leaked into localStorage: ${blob}`);

    // Нонс потрачен И аккаунты уже есть. Ответ обязан быть 409 и НАЗЫВАТЬ путь входа: 403 сказал бы
    // «нонс просрочен», то есть предложил бы перезапустить сервер там, где надо просто войти.
    const replay = await fetch(`${ui}/v1/ui-token?nonce=${capi3b.nonce}`);
    eq(replay.status, 409, 'replayed nonce once accounts exist');
    const replayBody = await replay.json();
    ok(/login/i.test(replayBody.error || ''),
      `the refusal must name the way in, got: ${JSON.stringify(replayBody)}`);

    // …and the token it handed over really is the one that authorises mutations.
    const authed = await fetch(`${ui}/v1/config`, { headers: { Authorization: `Bearer ${tok}` } });
    ok(authed.status !== 403, `the session the wizard obtained was rejected by the API (HTTP ${authed.status})`);
    eq((await fetch(`${ui}/v1/config`)).status, 403, 'unauthenticated /v1/config');

    // Ловушку спрашиваем ЕЩЁ РАЗ, уже в конце: выше она отвечала сразу после первичной настройки, а
    // здесь ловится всё, что страница послала ПОСЛЕ входа — например `checkCapi()`. `/healthz` и
    // `/v1/config-schema` туда ходить ВПРАВЕ: это проба связи с control-API, ради которой поле и
    // существует, и кредентиала она не несёт. Запрещено ровно то, что несёт право или пароль.
    ok(!trapped.some((t) => t.grant), `the setup grant reached the draft address: ${JSON.stringify(trapped)}`);
    ok(!trapped.some((t) => /a-long-enough-passphrase/.test(t.body || '')),
      `the administrator password reached the draft address: ${JSON.stringify(trapped)}`);
    await ctx.close();
    } finally {
      trap.close();
    }
  });
} finally {
  if (browser) await browser.close().catch(() => {});
  if (capi) capi.kill('SIGTERM');
  if (capi3) capi3.proc.kill('SIGTERM');
  if (capi3b) capi3b.proc.kill('SIGTERM');
  if (store) store.proc.kill('SIGTERM');
  if (store3b) store3b.proc.kill('SIGTERM');
  if (staticSrv) staticSrv.srv.close();
  if (tmp) await rm(tmp, { recursive: true, force: true }).catch(() => {});
  if (tmp3b) await rm(tmp3b, { recursive: true, force: true }).catch(() => {});
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log(`\nfailed: ${failed.map((f) => f.name).join(' · ')}`);
  process.exit(1);
}
