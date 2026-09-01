// End-to-end check of the built extension in a REAL Chromium against a live control-api.
//
// ⚠ THIS HEADER USED TO SAY "Not part of `npm test` / CI … dev-only". That was true, and it was the
// defect: measured 2026-08-30 and again 2026-09-01, this file was run by NOTHING — every mention of
// it in the tree was prose or self-description. It runs in CI now (ADR-143), in the `build` job,
// after the UI smoke. It is still not part of `npm test`, and that is deliberate: `npm test` is the
// jsdom unit pass, which must stay runnable without a browser or a server.
//
//   #42  loads as an unpacked MV3 extension, service worker registers, no console errors.
//   #43  from the SW context, a WebSocket to /v1/stream completes the bearer-subprotocol handshake
//        (ws.protocol === sentinel.recorder.v1 — token never reflected), streams an event, gets an ack,
//        and the event lands in the server's runs/record-<session>/events.ndjson.
//   #44  the recorder, injected into a real page, emits real selector candidates and NEVER records a
//        password value (it carries a secretRef instead).
//
// Run (locally — CI does the same thing, see .github/workflows/ci.yml):
//   cd extension && npm run build
//   npx playwright-core install --with-deps chromium     # playwright-core is a devDependency now
//   CHROME_BIN="$(node -e "console.log(require('playwright-core').chromium.executablePath())")" \
//   CONTROL_API_TOKEN=… SERVER_RUN_DIR=/path/the/server/writes/runs \
//   node test/e2e/recorder.e2e.mjs
// Start the server first, e.g.: (cd $SERVER_RUN_DIR && CONTROL_API_TOKEN=… CONTROL_API_ADDR=127.0.0.1:8099 control-api)
import { chromium } from 'playwright-core';
// ADR-143: the REAL subprotocol builder, not a copy of it. This line is the whole point of the #43
// block: the client half of the bearer handshake is what ships, so the check must exercise the
// shipping code. The pair used to be hand-written here, and a hand-written pair agrees with itself
// no matter what the product does.
import { wsSubprotocols, WS_SUBPROTOCOL } from '../../dist/protocol.mjs';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
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
  const ws = await sw.evaluate(({ url, protocols }) => new Promise((resolve) => {
    const sock = new WebSocket(url, protocols);
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
  }), { url: WS_URL, protocols: wsSubprotocols(TOKEN) });
  check('#43 subprotocol echoed (token not reflected)', ws.protocol === WS_SUBPROTOCOL, 'protocol=' + ws.protocol);
  check('#43 the token was offered, and is NOT what came back',
    wsSubprotocols(TOKEN)[1] === 'bearer.' + TOKEN && ws.protocol !== wsSubprotocols(TOKEN)[1],
    'offered bearer.<token>, server echoed ' + ws.protocol);
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

  // ⚠ THE HARNESS NO LONGER FAKES `change` (ADR-143). It used to call `page.dispatchEvent(…,'change')`
  // after each fill, and measured (capture-phase probe over this same fixture) the document then saw
  // TWO change events per field: ours with `isTrusted:false`, then Chromium's own on commit/blur. The
  // frozen transcript inherited that doubling and read as if the RECORDER emitted it — a property of
  // the test presented as a property of the product. A real user's typing raises the browser's event
  // and only that one, so the harness now moves focus and lets the browser do it.
  await page.fill('#email', 'user@example.test');
  await page.fill('#password', 'hunter2-SECRET');
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

  // ---- the transcript stops being a hand-written literal (ADR-143) ----
  //
  // ⚠ `tests/test_record_bridge_recorder_e2e.py` used to carry the event list inline, under the
  // comment "Verbatim from extension/test/e2e/recorder.e2e.mjs". It was not verbatim and never had
  // been: both files have exactly ONE commit and no diff since, so the literal was hand-authored at
  // birth. It held 5 events with an `input` this recorder does not emit, while the live run gave 6.
  // A hand-written transcript cannot go red for any recorder change — which is the whole defect.
  //
  // The addresses are rewritten to a stable placeholder because the real one is this checkout's
  // absolute file:// path; freezing that would make the artefact machine-specific.
  const transcript = recEvents.map((e) => ({ ...e, url: 'file:///s/login.html' }));
  await writeFile(join(here, 'recorded-transcript.json'),
    JSON.stringify(transcript, null, 2) + '\n', 'utf8');
  console.log(`transcript written: ${transcript.length} events -> test/e2e/recorded-transcript.json`);
} finally {
  await ctx.close();
}

console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : failures + ' CHECK(S) FAILED'}`);
process.exit(failures === 0 ? 0 : 1);
