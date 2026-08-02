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
import * as net from 'node:net';
import { chromium, Browser } from 'playwright';

const log = (...a: unknown[]): void => console.error('[cdp-service]', ...a);

/** Internal port Chromium listens on — loopback only, never reachable from outside the container. */
const INTERNAL_PORT = Number(process.env.CDP_INTERNAL_PORT ?? 9222);
/** Port the forwarder publishes on the container's network interfaces. */
const LISTEN_PORT = Number(process.env.CDP_LISTEN_PORT ?? 9223);
const LISTEN_ADDR = process.env.CDP_LISTEN_ADDR ?? '0.0.0.0';

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

  log(`browser up: CDP on ${LISTEN_ADDR}:${LISTEN_PORT} -> 127.0.0.1:${INTERNAL_PORT}`);
  log('the CDP port is UNAUTHENTICATED — keep it on an internal network, never publish it');
  // A readiness line on stdout: stderr carries the log, and something waiting on this service needs
  // one unambiguous token rather than having to parse prose.
  process.stdout.write('CDP_SERVICE_READY\n');
}

main().catch((e) => {
  log('failed to start:', (e as Error).message);
  process.exit(1);
});
