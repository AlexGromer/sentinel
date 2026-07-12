// Manual end-to-end check of the built extension in a REAL Chromium against a live control-api.
// Not part of `npm test` / CI (needs a full Chromium + a running server) — it's the dev-only live proof
// that mirrors `frontend/`'s posture.
//
//   #42  loads as an unpacked MV3 extension, service worker registers, no console errors.
//   #43  from the SW context, a WebSocket to /v1/stream completes the bearer-subprotocol handshake
//        (ws.protocol === sentinel.recorder.v1 — token never reflected), streams an event, gets an ack,
//        and the event lands in the server's runs/record-<session>/events.ndjson.
//   #44  the recorder, injected into a real page, emits real selector candidates and NEVER records a
//        password value (it carries a secretRef instead).
//
// Run:
//   cd extension && npm run build
//   npm i -D playwright-core            # or reuse pw-executor's playwright
//   CHROME_BIN=/path/to/full/chromium \
//   CONTROL_API_TOKEN=… SERVER_RUN_DIR=/path/the/server/writes/runs \
//   node test/e2e/recorder.e2e.mjs
// Start the server first, e.g.: (cd $SERVER_RUN_DIR && CONTROL_API_TOKEN=… CONTROL_API_ADDR=127.0.0.1:8099 control-api)
import { chromium } from 'playwright-core';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const EXT = join(here, '..', '..', 'dist');
const FIXTURE = 'file://' + join(here, 'login-fixture.html');
const CHROME = process.env.CHROME_BIN;
const WS_URL = process.env.CONTROL_API_WS || 'ws://127.0.0.1:8099/v1/stream';
const TOKEN = process.env.CONTROL_API_TOKEN || '';
const SERVER_RUN_DIR = process.env.SERVER_RUN_DIR;

if (!CHROME) {
  console.error('CHROME_BIN must point at a FULL Chromium (the headless-shell can’t load extensions).');
  process.exit(2);
}

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
  if (!ok) failures++;
};

const userDataDir = await mkdtemp(join(tmpdir(), 'sentinel-ext-'));
const ctx = await chromium.launchPersistentContext(userDataDir, {
  executablePath: CHROME,
  headless: false,
  args: ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
    `--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
});

const consoleErrors = [];
ctx.on('weberror', (e) => consoleErrors.push('weberror: ' + e.error()));

try {
  // ---- #42: service worker registers ----
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent('serviceworker', { timeout: 15000 });
  check('#42 service worker registered', !!sw && sw.url().endsWith('background.js'), 'id=' + new URL(sw.url()).host);

  // ---- #43: bearer-subprotocol handshake + ingest, from the SW context ----
  const ws = await sw.evaluate(({ url, token }) => new Promise((resolve) => {
    const sock = new WebSocket(url, ['sentinel.recorder.v1', 'bearer.' + token]);
    let session = null, ack = null;
    const finish = () => resolve({ protocol: sock.protocol, session, ack });
    sock.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.type === 'session') {
        session = m.session;
        sock.send(JSON.stringify({ type: 'click', url: 'https://x.test/',
          selectorCandidates: [{ strategy: 'css', locator: { css: '#go' } }] }));
      } else if (m.type === 'ack') { ack = m.n; sock.close(); }
    };
    sock.onclose = finish;
    sock.onerror = () => resolve({ protocol: sock.protocol, error: 'ws error' });
    setTimeout(() => resolve({ protocol: sock.protocol, session, ack, timeout: true }), 8000);
  }), { url: WS_URL, token: TOKEN });
  check('#43 subprotocol echoed (token not reflected)', ws.protocol === 'sentinel.recorder.v1', 'protocol=' + ws.protocol);
  check('#43 server greeting carried a session id', !!ws.session, 'session=' + ws.session);
  check('#43 event acked (n=1)', ws.ack === 1, 'ack=' + JSON.stringify(ws.ack) + (ws.error ? ' err=' + ws.error : ''));
  if (SERVER_RUN_DIR && ws.session) {
    const ndjson = await readFile(join(SERVER_RUN_DIR, 'runs', 'record-' + ws.session, 'events.ndjson'), 'utf8').catch(() => '');
    check('#43 event persisted to events.ndjson', ndjson.includes('"type":"click"') && ndjson.includes('#go'));
  }

  // ---- #44: recorder runtime + mandatory redaction, on a real page ----
  const page = await ctx.newPage();
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  await page.addInitScript(() => {
    window.__events = [];
    window.__listeners = [];
    window.chrome = {
      runtime: { sendMessage: (m) => window.__events.push(m), onMessage: { addListener: (f) => window.__listeners.push(f) } },
    };
  });
  await page.goto(FIXTURE);
  const contentSrc = await readFile(join(EXT, 'content.js'), 'utf8');
  await page.evaluate((src) => (0, eval)(src), contentSrc); // run the recorder IIFE in the page's main world
  await page.evaluate(() => window.__listeners.forEach((f) => f({ kind: 'record-control', recording: true })));

  await page.fill('#email', 'user@example.test');
  await page.dispatchEvent('#email', 'change');
  await page.fill('#password', 'hunter2-SECRET');
  await page.dispatchEvent('#password', 'change');
  await page.click('#go');
  await page.waitForTimeout(500);

  const events = await page.evaluate(() => window.__events);
  const recEvents = events.filter((e) => e.kind === 'recorder-event').map((e) => e.event);
  const blob = JSON.stringify(recEvents);
  const pwEvent = recEvents.find((e) => e.selectorCandidates.some((c) => c.locator.css === '#password'));
  const emailEvent = recEvents.find((e) => e.value === 'user@example.test');
  const clickEvent = recEvents.find((e) => e.type === 'click');

  check('#44 recorder emitted events', recEvents.length >= 2, recEvents.length + ' events');
  check('#44 password value NEVER recorded', !blob.includes('hunter2'), 'searched the full event blob');
  check('#44 password field carries a secretRef instead', !!pwEvent && !!pwEvent.secretRef && pwEvent.value === undefined,
    pwEvent ? 'secretRef=' + pwEvent.secretRef : 'no password event');
  check('#44 non-secret value kept (email)', !!emailEvent);
  check('#44 click captured with selector candidates', !!clickEvent && clickEvent.selectorCandidates.length >= 1);
  check('#44 testid candidate ranked first', !!clickEvent && clickEvent.selectorCandidates[0]?.strategy === 'testid');
  check('#44 no page errors from the recorder', pageErrors.length === 0, pageErrors.join('; '));
  check('#42 no web/console errors', consoleErrors.length === 0, consoleErrors.join('; '));
} finally {
  await ctx.close();
}

console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : failures + ' CHECK(S) FAILED'}`);
process.exit(failures === 0 ? 0 : 1);
