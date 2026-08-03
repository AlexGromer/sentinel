/**
 * Sentinel browser service — a Chromium other containers can attach to (ADR-110).
 *
 * WHY THIS EXISTS AT ALL. `PW_CDP_ENDPOINT` has let the executor attach to somebody else's browser
 * since M9.6 (ADR-037), but there was never a browser to attach TO in a deployment: the executor
 * launches its own, inside its own process. The live-view work needs the opposite shape — a browser
 * that outlives one run and that BOTH the executor and control-api can reach — so the browser
 * becomes a service. This file is that service.
 *
 * WHY IT IS NOT JUST `chromium --remote-debugging-port=9222`. Three measurements against Chrome 150,
 * each fatal on its own:
 *
 *   1. Chromium binds the debugging port to 127.0.0.1 and IGNORES --remote-debugging-address=0.0.0.0.
 *      It does so SILENTLY — the log still reads "DevTools listening on ws://127.0.0.1:9222". A
 *      sibling container simply cannot connect. Hence the forwarder below.
 *   2. The DevTools HTTP endpoint validates the Host header and answers HTTP 500 to a DNS name
 *      ("Host header is specified and is not an IP address or localhost"). Clients must therefore
 *      address this service NUMERICALLY; the executor rewrites a name to an address before
 *      connecting (see resolveCdpEndpoint in server.ts) and says so when it does.
 *   3. Chrome echoes the Host it was addressed by into `webSocketDebuggerUrl`, so addressing it
 *      numerically is also what makes the websocket URL point back through this forwarder rather
 *      than at the client's own loopback.
 *
 * ⚠ SECURITY — the CDP port is UNAUTHENTICATED BY CONSTRUCTION. Anything that reaches it can drive
 * the browser, read any page it has open and its cookies. There is no token to add: the protocol has
 * none. The only control is reachability, so the compose services that carry this NEVER publish the
 * port to the host (no `ports:` key) and keep it on the internal network. Do not "just expose it for
 * debugging" — that is a remote-code-execution surface on whatever the browser can reach.
 *
 * Chromium is launched THROUGH Playwright rather than by path so the service uses the same browser
 * build, and the same container-safe flags, that a normal run would use.
 */
import * as http from 'node:http';
import * as net from 'node:net';
import { chromium, Browser, CDPSession, Page } from 'playwright';

const log = (...a: unknown[]): void => console.error('[cdp-service]', ...a);

/** Internal port Chromium listens on — loopback only, never reachable from outside the container. */
const INTERNAL_PORT = Number(process.env.CDP_INTERNAL_PORT ?? 9222);
/** Port the forwarder publishes on the container's network interfaces. */
const LISTEN_PORT = Number(process.env.CDP_LISTEN_PORT ?? 9223);
const LISTEN_ADDR = process.env.CDP_LISTEN_ADDR ?? '0.0.0.0';
/** Port serving the live screencast (see the LIVE VIEW section below). */
const LIVE_PORT = Number(process.env.CDP_LIVE_PORT ?? 9224);

/* ================================================================== LIVE VIEW (ADR-111)
 * The video mode of the live area, served from HERE rather than from the executor.
 *
 * The executor already carries screencast tools, and they stay — they are the answer when the
 * browser is INTERNAL to a run. But they cannot serve the live view of a deployment: the executor
 * lives inside the brain's process on stdio, so control-api has no address for it. That is the
 * whole reason the video mode has been showing a placeholder.
 *
 * When the browser is a service, the shape inverts and gets simpler: the process holding the browser
 * is long-lived and already listening on the network, so it can serve the frames itself. control-api
 * only proxies, and does so with the credential it already enforces. The alternative — a CDP client
 * inside control-api — would mean hand-writing a WebSocket CLIENT in Go (its ws.go is a hand-rolled
 * SERVER; there is no websocket library in go.mod, and adding one has broken the air-gapped build
 * before), to reach a browser that a Node process is already holding a session to.
 *
 * FRAMES NEVER TOUCH DISK. Only the most recent one is kept: a screencast delivers tens of frames a
 * second, and a live view is worth watching while it happens, not afterwards. Bounded by
 * construction rather than by a cleanup somebody has to remember.
 *
 * The screencast starts on the FIRST request and stops when nobody has asked for IDLE_STOP_MS. It is
 * a real cost — Chromium encodes and ships every frame — and paying it while no one is watching is
 * how a feature becomes a tax on every run.
 */
const IDLE_STOP_MS = Number(process.env.CDP_LIVE_IDLE_MS ?? 15_000);
const FRAME_QUALITY = Number(process.env.CDP_LIVE_QUALITY ?? 55);
const FRAME_MAX_W = Number(process.env.CDP_LIVE_MAX_WIDTH ?? 960);
const FRAME_MAX_H = Number(process.env.CDP_LIVE_MAX_HEIGHT ?? 720);
const FRAME_EVERY_NTH = Number(process.env.CDP_LIVE_EVERY_NTH ?? 2);

/** Chrome opens the HTTP endpoint a moment after Playwright's own transport is up. */
async function waitForCdp(port: number, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return;
    } catch { /* not up yet */ }
    if (Date.now() > deadline) throw new Error(`Chromium never opened its CDP port ${port}`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

/**
 * A byte-for-byte TCP relay. Deliberately NOT an HTTP proxy: rewriting the Host header here would
 * make Chrome echo a webSocketDebuggerUrl the client cannot reach (measurement 3 above), so the
 * client's own address must survive the hop untouched.
 */
function startForwarder(): Promise<net.Server> {
  return new Promise((resolve, reject) => {
    const server = net.createServer((client) => {
      const upstream = net.connect(INTERNAL_PORT, '127.0.0.1');
      // Both halves must be torn down together; a half-open pair leaks a socket per aborted
      // connection, and a browser service is long-lived by definition.
      const bothWays = (a: net.Socket, b: net.Socket) => {
        a.pipe(b);
        a.on('error', () => b.destroy());
        a.on('close', () => b.destroy());
      };
      bothWays(client, upstream);
      bothWays(upstream, client);
    });
    server.on('error', reject);
    server.listen(LISTEN_PORT, LISTEN_ADDR, () => resolve(server));
  });
}

/* --------------------------------------------------------------- screencast state (in memory) */
let liveFrame: { data: Buffer; ts: number } | null = null;
let liveSession: CDPSession | null = null;
let livePage: Page | null = null;
let liveLastAsk = 0;
let liveIdleTimer: NodeJS.Timeout | null = null;
/** Waiters woken by each new frame — this is what makes the MJPEG endpoint a stream, not a poll. */
let liveWaiters: Array<() => void> = [];

/**
 * A SECOND Playwright client, attached over CDP to the very Chromium this process launched.
 *
 * Measured, and it is the whole reason this exists: the `Browser` handle returned by
 * `chromium.launch()` only tracks contexts created through ITS OWN connection. A run attaches over
 * CDP as a separate client and makes its page there, so the launched handle reports `contexts() ==
 * []` for the entire run — `has_page:false` while a browser was visibly driving a page.
 * `connectOverCDP` is the operation that ADOPTS whatever already exists, which is precisely what an
 * observer needs. It is created lazily, so a deployment that never opens the live view never pays
 * for a second connection.
 */
let observer: Browser | null = null;

async function observerBrowser(): Promise<Browser> {
  if (observer && observer.isConnected()) return observer;
  observer = await chromium.connectOverCDP(`http://127.0.0.1:${INTERNAL_PORT}`);
  return observer;
}

/**
 * Find the page to watch.
 *
 * This function used to force a RECONNECT when the cached observer saw no page, on the theory that
 * `connectOverCDP` only ever adopts what exists at connect time. A mutation proved that reconnect
 * unkillable — the gate passed with it removed — so it was measured directly, and the theory was
 * wrong: an observer connected to an EMPTY browser does see a page another client creates a second
 * later. What actually caused "no page during a run" was a race in the reconnect path itself,
 * awaiting `close()` on the live handle while a concurrent request was still using it. Removing the
 * reconnect removes the race and the code that existed to work around it.
 *
 * Kept as a named function rather than inlined because the two callers must not drift: status and
 * the frame path have to be looking at the same page, or they answer about different things.
 */
async function findPage(): Promise<Page | null> {
  return currentPage(await observerBrowser());
}

/**
 * The page to watch: the newest one across the adopted contexts. Resolved per start rather than
 * pinned for the service's lifetime — a second run gets a new page, and a live view frozen on the
 * previous run's page would be showing something true and irrelevant.
 */
function currentPage(b: Browser): Page | null {
  const pages = b.contexts().flatMap((c) => c.pages()).filter((p) => !p.isClosed());
  return pages.length ? pages[pages.length - 1] : null;
}

async function liveStart(): Promise<string | null> {
  liveLastAsk = Date.now();
  if (liveSession) return null;
  const page = await findPage();
  if (!page) return 'the browser has no page yet — start a run first';
  const cdp = await page.context().newCDPSession(page);
  cdp.on('Page.screencastFrame', async (f: { data: string; sessionId: number }) => {
    liveFrame = { data: Buffer.from(f.data, 'base64'), ts: Date.now() };
    const woken = liveWaiters;
    liveWaiters = [];
    for (const w of woken) w();
    // The ack is what keeps frames coming: without it Chromium sends exactly one and stops. So a
    // failure here is not cosmetic — it is the feature ending silently.
    try { await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }); } catch { /* page gone */ }
  });
  await cdp.send('Page.startScreencast', {
    format: 'jpeg', quality: FRAME_QUALITY,
    maxWidth: FRAME_MAX_W, maxHeight: FRAME_MAX_H, everyNthFrame: FRAME_EVERY_NTH,
  });
  liveSession = cdp;
  livePage = page;

  // SEED THE FIRST FRAME EXPLICITLY, and note WHICH PART of this does the work.
  //
  // A screencast emits on REPAINT, not on a timer. Measured on a genuinely idle page (navigated,
  // then left alone for five seconds): `Page.startScreencast` produced NO frame at all in three
  // consecutive four-second rounds — where an active page produced 17. So opening the live view on a
  // run that is between steps, paused, or simply looking at a static page would show an empty box
  // until something moved, and "empty" is indistinguishable from "broken".
  //
  // The load-bearing part is the CAPTURE CALL, not the assignment below it: taking a screenshot
  // forces a paint, the screencast reacts to that paint, and the frame arrives through the normal
  // event handler. That was learned from a mutation — inverting the assignment's guard left the gate
  // green, while removing this whole block turned an idle page's first request into a 503. Both
  // halves are kept: the call for the paint it provokes, the assignment for the case where the event
  // does not arrive. Failure here is not fatal — the stream still works — but it is said out loud
  // rather than left as an unexplained blank.
  try {
    const shot = await cdp.send('Page.captureScreenshot', { format: 'jpeg', quality: FRAME_QUALITY });
    if (shot && typeof shot.data === 'string' && !liveFrame) {
      liveFrame = { data: Buffer.from(shot.data, 'base64'), ts: Date.now() };
    }
  } catch (e) {
    log('could not seed the first frame:', (e as Error).message);
  }

  log(`screencast started on ${page.url().slice(0, 80)}`);
  return null;
}

async function liveStop(reason: string): Promise<void> {
  if (!liveSession) return;
  const s = liveSession;
  liveSession = null; livePage = null; liveFrame = null;
  try { await s.send('Page.stopScreencast'); } catch { /* page gone */ }
  try { await s.detach(); } catch { /* already detached */ }
  log(`screencast stopped (${reason})`);
}

/** Serve the live view. Kept OFF the CDP relay port on purpose — that port speaks CDP and nothing else. */
function startLiveServer(): Promise<http.Server> {
  liveIdleTimer = setInterval(() => {
    if (liveSession && Date.now() - liveLastAsk > IDLE_STOP_MS) void liveStop('nobody watching');
    // A run that finished takes its page with it; keeping a session on a closed page would go quiet
    // without saying why.
    if (liveSession && livePage && livePage.isClosed()) void liveStop('the page closed');
  }, 2_000);

  const srv = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', `http://127.0.0.1:${LIVE_PORT}`);
    if (url.pathname === '/live/status') {
      // Connect the observer here too, so status answers about the BROWSER rather than about
      // whether anyone happened to ask for a frame first. A status that reports has_page:false
      // while a page is plainly open is the kind of answer that sends a reader down the wrong path.
      //
      // The failure is REPORTED, not swallowed. It was swallowed for one commit, and that single
      // `catch {}` produced exactly the wrong answer it was placed next to a warning about: status
      // said has_page:false during a run in which the very next request returned a frame. "Could not
      // look" and "looked and found nothing" are different facts and must not share a field.
      let p: Page | null = null;
      let lookupError: string | null = null;
      try { p = await findPage(); } catch (e) { lookupError = (e as Error).message; }
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({
        streaming: !!liveSession,
        has_page: !!p,
        url: p ? p.url() : null,
        last_frame_ts: liveFrame ? liveFrame.ts : null,
        error: lookupError,
      }));
    }

    if (url.pathname === '/live/frame.jpg') {
      const why = await liveStart().catch((e) => (e as Error).message);
      if (why) { res.writeHead(503, { 'content-type': 'text/plain' }); return res.end(why); }
      // Wait briefly for the FIRST frame rather than answering 503 on a cold start: the screencast
      // was only just asked for, and "not ready" a millisecond after starting it is not information.
      if (!liveFrame) await new Promise<void>((resolve) => {
        const t = setTimeout(resolve, 3_000);
        liveWaiters.push(() => { clearTimeout(t); resolve(); });
      });
      if (!liveFrame) { res.writeHead(503, { 'content-type': 'text/plain' }); return res.end('no frame yet'); }
      res.writeHead(200, { 'content-type': 'image/jpeg', 'cache-control': 'no-store' });
      return res.end(liveFrame.data);
    }

    if (url.pathname === '/live/mjpeg') {
      const why = await liveStart().catch((e) => (e as Error).message);
      if (why) { res.writeHead(503, { 'content-type': 'text/plain' }); return res.end(why); }
      res.writeHead(200, {
        'content-type': 'multipart/x-mixed-replace; boundary=sentinelframe',
        'cache-control': 'no-store',
        connection: 'close',
      });
      let open = true;
      req.on('close', () => { open = false; });
      let sent = -1;
      while (open) {
        liveLastAsk = Date.now();
        if (liveFrame && liveFrame.ts !== sent) {
          sent = liveFrame.ts;
          res.write(`--sentinelframe\r\nContent-Type: image/jpeg\r\nContent-Length: ${liveFrame.data.length}\r\n\r\n`);
          res.write(liveFrame.data);
          res.write('\r\n');
        }
        // Woken BY a frame, with a ceiling so a stalled screencast cannot wedge the connection open
        // forever with nothing said.
        await new Promise<void>((resolve) => {
          const t = setTimeout(resolve, 2_000);
          liveWaiters.push(() => { clearTimeout(t); resolve(); });
        });
      }
      return res.end();
    }

    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found');
  });
  return new Promise((resolve) => srv.listen(LIVE_PORT, LISTEN_ADDR, () => resolve(srv)));
}

async function main(): Promise<void> {
  let browser: Browser | undefined;
  const shutdown = async (sig: string): Promise<void> => {
    log(`${sig} — closing browser`);
    try { await browser?.close(); } catch { /* already gone */ }
    process.exit(0);
  };
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('SIGINT', () => void shutdown('SIGINT'));

  browser = await chromium.launch({
    args: [`--remote-debugging-port=${INTERNAL_PORT}`],
  });
  await waitForCdp(INTERNAL_PORT);
  await startForwarder();
  await startLiveServer();

  log(`browser up: CDP on ${LISTEN_ADDR}:${LISTEN_PORT} -> 127.0.0.1:${INTERNAL_PORT}`);
  log(`live view on ${LISTEN_ADDR}:${LIVE_PORT} (/live/status, /live/frame.jpg, /live/mjpeg)`);
  log('the CDP port is UNAUTHENTICATED — keep it on an internal network, never publish it');
  // A readiness line on stdout: stderr carries the log, and something waiting on this service needs
  // one unambiguous token rather than having to parse prose.
  process.stdout.write('CDP_SERVICE_READY\n');
}

main().catch((e) => {
  log('failed to start:', (e as Error).message);
  process.exit(1);
});
