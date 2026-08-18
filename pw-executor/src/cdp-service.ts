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
import { journal, startedMsg, stoppedMsg, supervisor } from './svcjournal.js';

const log = (...a: unknown[]): void => console.error('[cdp-service]', ...a);

/** Internal port Chromium listens on — loopback only, never reachable from outside the container. */
const INTERNAL_PORT = Number(process.env.CDP_INTERNAL_PORT ?? 9222);
/** Port the forwarder publishes on the container's network interfaces. */
const LISTEN_PORT = Number(process.env.CDP_LISTEN_PORT ?? 9223);
const LISTEN_ADDR = process.env.CDP_LISTEN_ADDR ?? '0.0.0.0';
/** Port serving the live screencast (see the LIVE VIEW section below). */
const LIVE_PORT = Number(process.env.CDP_LIVE_PORT ?? 9224);

/**
 * Chromium window geometry for the HEADED case — FOUND BY LOOKING AT THE FIRST VNC FRAME, not by any
 * gate (LIVE-VNC, 2026-08-17).
 *
 * The first screenshot taken over RFB showed a real browser window sitting on a 1280x800 virtual
 * display at roughly 1060x790, with black bands down the right side and along the bottom — about 17%
 * of the screen. Every check was green: the container was healthy, the service answered, the frame
 * had content. It simply looked broken to a person, which is exactly the class of defect the "open
 * the pictures" rule exists for.
 *
 * The cause is that the vnc container has NO WINDOW MANAGER, deliberately (one more package, one more
 * process, and nothing for it to manage). Without a WM nothing maximises a window and nothing places
 * it, so Chromium keeps its built-in default size wherever it opened. `--start-maximized` needs a WM
 * and would do nothing here; the size has to be stated.
 *
 * The geometry comes from the same variable the entrypoint hands to Xvfb, so the window and the
 * screen cannot drift apart — a second number would be a second source of truth for one fact.
 * Returns NOTHING in the headless case: a headless Chromium has no window, and passing a size there
 * would change the viewport the goldens were captured at.
 */
function windowArgs(): string[] {
  if (!(process.env.PW_HEADED === '1' || process.env.PW_HEADLESS === '0')) return [];
  const m = /^(\d+)x(\d+)/.exec(process.env.SENTINEL_VNC_GEOMETRY ?? '');
  if (!m) return [];
  return [`--window-position=0,0`, `--window-size=${m[1]},${m[2]}`];
}

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
/**
 * How long a run's claim outlives its last sign of life. Longer than any run this product supports,
 * because the cost of keeping one is a map entry, and the cost of dropping one early is answering
 * "that run never started" about a run that plainly did.
 */
const CLAIM_TTL_MS = Number(process.env.CDP_LIVE_CLAIM_TTL_MS ?? 12 * 60 * 60 * 1000);

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

/* --------------------------------------------------------- screencast state (in memory) */
/*
 * LIVE-PER-RUN. This state used to be SIX module-level singletons — one frame, one session, one
 * page, one idle clock for the whole process. That made the live view a fact about the SERVICE
 * rather than about a run: with two runs in flight the picture showed whichever page was created
 * last, and the hub said so in words because the topology could not say it in data.
 *
 * Now a session is keyed by the Chromium TARGET it watches, and a run declares which target is its
 * own. The identifier is not invented here: `Target.getTargetInfo` returns the same id to every CDP
 * client, including one that ADOPTED a page it did not create — measured directly, because the
 * design turns on it. That is what makes this work in CDP-attach mode (ADR-037), where the executor
 * owns nothing and could not label a page any other way.
 */
type LiveSession = {
  cdp: CDPSession;
  page: Page;
  targetId: string;
  frame: { data: Buffer; ts: number } | null;
  /** Waiters woken by each new frame — this is what makes the MJPEG endpoint a stream, not a poll. */
  waiters: Array<() => void>;
  /** Acks that failed. Non-zero means the stream has stopped or is about to. */
  ackErrors: number;
  lastAsk: number;
};

/** targetId -> the screencast watching it. One per PAGE, so two viewers of two runs do not fight. */
const sessions = new Map<string, LiveSession>();

/**
 * run_id -> targetId, as declared by the executor driving that run (POST /live/claim).
 *
 * Deliberately NOT derived from page order, URL or creation time. All three were available and all
 * three are wrong the moment two runs overlap, which is exactly the case this exists for.
 */
const claims = new Map<string, { targetId: string; at: number }>();

/** Cache: a page's target id never changes, and asking costs a CDP round trip. */
const targetIds = new WeakMap<Page, string>();

let liveIdleTimer: NodeJS.Timeout | null = null;

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

/** The target id of a page, as every CDP client sees it. Cached; null when the page is gone. */
async function targetIdOf(page: Page): Promise<string | null> {
  const known = targetIds.get(page);
  if (known) return known;
  try {
    const cdp = await page.context().newCDPSession(page);
    const info = (await cdp.send('Target.getTargetInfo')) as { targetInfo?: { targetId?: string } };
    await cdp.detach().catch(() => {});
    const id = info?.targetInfo?.targetId ?? null;
    if (id) targetIds.set(page, id);
    return id;
  } catch {
    return null;
  }
}

function openPages(b: Browser): Page[] {
  return b.contexts().flatMap((c) => c.pages()).filter((p) => !p.isClosed());
}

/**
 * The page to watch when NO run was named: the newest one across the adopted contexts.
 *
 * Kept, and kept UNSCOPED on purpose. An unnamed request is what `agentctl live frame` and every
 * pre-existing caller send, and answering them with a refusal would break a working surface to
 * enforce a rule they never agreed to. What changed is that the answer now SAYS it is unscoped
 * (`scoped:false` in status), so "this is the newest page" is never mistaken for "this is your run".
 */
function currentPage(b: Browser): Page | null {
  const pages = openPages(b);
  return pages.length ? pages[pages.length - 1] : null;
}

type Resolution = {
  page: Page | null;
  targetId: string | null;
  scoped: boolean;
  /** Why there is no page. A refusal that does not say which of the three cases it is, is a guess. */
  why: string | null;
};

/**
 * Which page a request is about.
 *
 * ⚠ A NAMED run that cannot be resolved is REFUSED, never silently answered with somebody else's
 * picture. That is the decision this task exists for: showing the wrong run is worse than showing
 * nothing, because nothing is visibly nothing and a wrong picture is indistinguishable from a right
 * one. The three failures are told apart rather than collapsed — unclaimed, claimed-but-gone, and
 * no-page-at-all lead a reader to three different places.
 */
async function resolve(runId: string): Promise<Resolution> {
  const b = await observerBrowser();
  if (!runId) {
    const p = currentPage(b);
    return { page: p, targetId: p ? await targetIdOf(p) : null, scoped: false,
             why: p ? null : 'the browser has no page yet — start a run first' };
  }
  const claim = claims.get(runId);
  if (!claim) {
    return { page: null, targetId: null, scoped: true,
             why: `run ${runId} has not claimed a page — either it has not started its browser yet, ` +
                  'or this deployment runs the executor without a browser service to announce to' };
  }
  // A page two runs share cannot be attributed to either. Saying so is the whole point: a picture
  // that is true for one run and false for the other, with nothing on screen to tell which, is
  // exactly what this task exists to stop.
  const sharers = [...claims.entries()].filter(([, c]) => c.targetId === claim.targetId).map(([r]) => r);
  if (sharers.length > 1) {
    return { page: null, targetId: claim.targetId, scoped: true,
             why: `run ${runId} shares one browser page with ${sharers.filter((r) => r !== runId).join(', ')} ` +
                  '— in CDP-attach mode runs adopt the same tab, so this picture cannot be attributed to one of them' };
  }
  for (const p of openPages(b)) {
    if ((await targetIdOf(p)) === claim.targetId) {
      return { page: p, targetId: claim.targetId, scoped: true, why: null };
    }
  }
  return { page: null, targetId: claim.targetId, scoped: true,
           why: `run ${runId} claimed a page that is no longer open — the run has finished or its tab was closed` };
}

async function liveStart(runId: string): Promise<{ session: LiveSession | null; why: string | null; scoped: boolean }> {
  const r = await resolve(runId);
  if (!r.page || !r.targetId) return { session: null, why: r.why, scoped: r.scoped };

  const existing = sessions.get(r.targetId);
  if (existing) {
    existing.lastAsk = Date.now();
    return { session: existing, why: null, scoped: r.scoped };
  }

  const page = r.page;
  const targetId = r.targetId;
  const cdp = await page.context().newCDPSession(page);
  const sess: LiveSession = { cdp, page, targetId, frame: null, waiters: [], ackErrors: 0, lastAsk: Date.now() };
  cdp.on('Page.screencastFrame', async (f: { data: string; sessionId: number }) => {
    sess.frame = { data: Buffer.from(f.data, 'base64'), ts: Date.now() };
    const woken = sess.waiters;
    sess.waiters = [];
    for (const w of woken) w();
    // The ack is what keeps frames coming: without it Chromium sends exactly one and stops. The
    // comment here used to say exactly that and then swallow the failure anyway, which left the
    // live view able to go dark mid-run with the operator's only clue being "the picture stopped".
    // It is now COUNTED and SAID: /live/status carries ack_errors, so a stalled stream has a
    // number behind it rather than a guess.
    try {
      await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId });
    } catch (e) {
      sess.ackErrors += 1;
      if (sess.ackErrors === 1) log('screencast ack failed — frames will stop:', (e as Error).message);
    }
  });
  await cdp.send('Page.startScreencast', {
    format: 'jpeg', quality: FRAME_QUALITY,
    maxWidth: FRAME_MAX_W, maxHeight: FRAME_MAX_H, everyNthFrame: FRAME_EVERY_NTH,
  });
  sessions.set(targetId, sess);

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
    if (shot && typeof shot.data === 'string' && !sess.frame) {
      sess.frame = { data: Buffer.from(shot.data, 'base64'), ts: Date.now() };
    }
  } catch (e) {
    log('could not seed the first frame:', (e as Error).message);
  }

  log(`screencast started on ${page.url().slice(0, 80)} (target ${targetId.slice(0, 8)})`);
  return { session: sess, why: null, scoped: r.scoped };
}

async function liveStop(targetId: string, reason: string): Promise<void> {
  const sess = sessions.get(targetId);
  if (!sess) return;
  sessions.delete(targetId);
  try { await sess.cdp.send('Page.stopScreencast'); } catch { /* page gone */ }
  try { await sess.cdp.detach(); } catch { /* already detached */ }
  log(`screencast stopped on target ${targetId.slice(0, 8)} (${reason})`);
}

/** Serve the live view. Kept OFF the CDP relay port on purpose — that port speaks CDP and nothing else. */
function startLiveServer(): Promise<http.Server> {
  liveIdleTimer = setInterval(() => {
    // Per SESSION now, not per process: one viewer leaving must not stop another viewer's stream,
    // and one run's page closing must not take down the picture of a run still going.
    for (const [targetId, sess] of sessions) {
      if (Date.now() - sess.lastAsk > IDLE_STOP_MS) { void liveStop(targetId, 'nobody watching'); continue; }
      // A run that finished takes its page with it; keeping a session on a closed page would go quiet
      // without saying why.
      if (sess.page.isClosed()) void liveStop(targetId, 'the page closed');
    }
    // A claim outlives the page it named — that is what makes "the run has finished" a different
    // answer from "the run never started". Dropped only when it is older than any run could be.
    for (const [runId, c] of claims) {
      if (Date.now() - c.at > CLAIM_TTL_MS) claims.delete(runId);
    }
  }, 2_000);

  const srv = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', `http://127.0.0.1:${LIVE_PORT}`);
    // LIVE-PER-RUN. One place parses it, so status and the frame paths cannot end up answering about
    // different runs — the same reason findPage was a named function rather than two inline lookups.
    const runId = (url.searchParams.get('run_id') ?? '').trim();

    // The executor announces which Chromium target its run is driving. POST, because it writes; and
    // it is the ONLY writer on this surface, which is why the method is checked rather than assumed.
    if (url.pathname === '/live/claim') {
      if (req.method !== 'POST') {
        res.writeHead(405, { 'content-type': 'text/plain' });
        return res.end('POST only');
      }
      let body = '';
      for await (const chunk of req) {
        body += chunk;
        if (body.length > 4096) { res.writeHead(413); return res.end(); }
      }
      let claim: { run_id?: string; target_id?: string };
      try { claim = JSON.parse(body || '{}'); } catch { claim = {}; }
      const rid = (claim.run_id ?? '').trim();
      const tid = (claim.target_id ?? '').trim();
      if (!rid || !tid) {
        res.writeHead(400, { 'content-type': 'text/plain' });
        return res.end('run_id and target_id are both required');
      }
      // ⚠ TWO RUNS CAN CLAIM ONE PAGE, and it is not a bug in the claim — it is the topology.
      // MEASURED with two concurrent runs against one browser service: both announced target
      // 84DC6185, because in CDP-attach mode the executor adopts `contexts()[0]` and then
      // `pages()[0]` — the SECOND run drives the SAME TAB as the first. A label cannot separate what
      // the browser did not separate.
      //
      // So the collision is DETECTED and SAID rather than papered over. Answering either run with
      // that shared page would be the original defect wearing a run id: the picture would be true
      // for one of them and a lie for the other, and nothing on screen would tell them apart.
      // Whether a run should instead create its OWN page in the adopted context is a change to
      // ADR-037's promise (reuse the user's session AND their open tab), so it is a decision to be
      // taken deliberately — not one this endpoint makes by itself while nobody is looking.
      const conflict = [...claims.entries()].find(([r, c]) => r !== rid && c.targetId === tid);
      claims.set(rid, { targetId: tid, at: Date.now() });
      if (conflict) {
        log(`run ${rid} claimed target ${tid.slice(0, 8)} — ALSO claimed by ${conflict[0]}; ` +
            'the live view cannot attribute a page two runs share');
      } else {
        log(`run ${rid} claimed target ${tid.slice(0, 8)}`);
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ ok: true, claims: claims.size, shared_with: conflict ? conflict[0] : null }));
    }

    if (url.pathname === '/live/status') {
      // Connect the observer here too, so status answers about the BROWSER rather than about
      // whether anyone happened to ask for a frame first. A status that reports has_page:false
      // while a page is plainly open is the kind of answer that sends a reader down the wrong path.
      //
      // The failure is REPORTED, not swallowed. It was swallowed for one commit, and that single
      // `catch {}` produced exactly the wrong answer it was placed next to a warning about: status
      // said has_page:false during a run in which the very next request returned a frame. "Could not
      // look" and "looked and found nothing" are different facts and must not share a field.
      let r: Resolution | null = null;
      let lookupError: string | null = null;
      try { r = await resolve(runId); } catch (e) { lookupError = (e as Error).message; }
      const sess = r && r.targetId ? sessions.get(r.targetId) ?? null : null;
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({
        streaming: !!sess,
        has_page: !!(r && r.page),
        url: r && r.page ? r.page.url() : null,
        last_frame_ts: sess && sess.frame ? sess.frame.ts : null,
        ack_errors: sess ? sess.ackErrors : 0,
        // LIVE-PER-RUN. `scoped:false` is the load-bearing field: it is how a caller learns the
        // picture is "the newest page" rather than "your run". Without it an unnamed request and a
        // resolved one are indistinguishable, which is the confusion this whole task removes.
        run_id: runId || null,
        scoped: r ? r.scoped : false,
        reason: r ? r.why : null,
        error: lookupError,
      }));
    }

    if (url.pathname === '/live/frame.jpg') {
      const started = await liveStart(runId).catch((e) => ({ session: null, why: (e as Error).message, scoped: !!runId }));
      if (!started.session) {
        res.writeHead(503, { 'content-type': 'text/plain' });
        return res.end(started.why ?? 'no page');
      }
      const sess = started.session;
      // Wait briefly for the FIRST frame rather than answering 503 on a cold start: the screencast
      // was only just asked for, and "not ready" a millisecond after starting it is not information.
      if (!sess.frame) await new Promise<void>((resolve) => {
        const t = setTimeout(resolve, 3_000);
        sess.waiters.push(() => { clearTimeout(t); resolve(); });
      });
      if (!sess.frame) { res.writeHead(503, { 'content-type': 'text/plain' }); return res.end('no frame yet'); }
      res.writeHead(200, {
        'content-type': 'image/jpeg', 'cache-control': 'no-store',
        // The picture says WHOSE it is, in a header a proxy carries and a human can read. A JPEG
        // cannot carry that in its body, and a caller that asked for a run deserves to be able to
        // check it got that run rather than trust the routing.
        'x-sentinel-run': runId || '',
        'x-sentinel-scoped': String(started.scoped),
      });
      return res.end(sess.frame.data);
    }

    if (url.pathname === '/live/mjpeg') {
      const startedM = await liveStart(runId).catch((e) => ({ session: null, why: (e as Error).message, scoped: !!runId }));
      if (!startedM.session) {
        res.writeHead(503, { 'content-type': 'text/plain' });
        return res.end(startedM.why ?? 'no page');
      }
      const msess = startedM.session;
      res.writeHead(200, {
        'content-type': 'multipart/x-mixed-replace; boundary=sentinelframe',
        'cache-control': 'no-store',
        connection: 'close',
        'x-sentinel-run': runId || '',
        'x-sentinel-scoped': String(startedM.scoped),
      });
      let open = true;
      req.on('close', () => { open = false; });
      let sent = -1;
      while (open) {
        msess.lastAsk = Date.now();
        if (msess.frame && msess.frame.ts !== sent) {
          sent = msess.frame.ts;
          res.write(`--sentinelframe\r\nContent-Type: image/jpeg\r\nContent-Length: ${msess.frame.data.length}\r\n\r\n`);
          res.write(msess.frame.data);
          res.write('\r\n');
        }
        // Woken BY a frame, with a ceiling so a stalled screencast cannot wedge the connection open
        // forever with nothing said.
        await new Promise<void>((resolve) => {
          const t = setTimeout(resolve, 2_000);
          msess.waiters.push(() => { clearTimeout(t); resolve(); });
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
    // Journalled BEFORE the close, not after: `docker compose down` sends SIGTERM and then kills, so
    // a record written after the browser teardown is a record that may never be written at all. The
    // control-api learned the same thing in PR-A, where every shutdown looked like a crash.
    journal('service.stopped', 'info', stoppedMsg(`signal ${sig}`));
    try { await browser?.close(); } catch { /* already gone */ }
    process.exit(0);
  };
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('SIGINT', () => void shutdown('SIGINT'));

  browser = await chromium.launch({
    // LIVE-VNC. Until this line the call passed NO headless option at all, so Playwright's default
    // (headless: true) won unconditionally — and the whole `vnc` profile would have been an X server
    // faithfully exporting an EMPTY desktop while /live/status answered 200 and the container
    // reported healthy. The predicate is the SAME one resolveLaunchPlan already uses (launch.ts), not
    // a second dialect: a service that decided headedness its own way would be a third answer to a
    // question the executor already answers.
    //
    // ⚠ The DEFAULT stays headless, deliberately and by construction — `browser` sets neither
    // variable. `screenshot_hash` is byte-stable ONLY in headless (docs/DETERMINISM.md), so flipping
    // this default would invalidate every golden ever taken, silently, on the next replay.
    headless: !(process.env.PW_HEADED === '1' || process.env.PW_HEADLESS === '0'),
    args: [`--remote-debugging-port=${INTERNAL_PORT}`, ...windowArgs()],
  });
  await waitForCdp(INTERNAL_PORT);
  await startForwarder();
  await startLiveServer();

  // The service plane's own record (HEALTH-005 PR-C). stderr still carries the human lines below —
  // they are what an operator reads while watching `docker compose up` — but the JOURNAL is what
  // survives the container and what the hub, the CLI and the API can read.
  journal('service.started', 'info',
    startedMsg(process.env.SENTINEL_VERSION ?? 'dev', supervisor(), process.pid,
      ` — CDP ${LISTEN_ADDR}:${LISTEN_PORT}, live ${LISTEN_ADDR}:${LIVE_PORT}`));

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
