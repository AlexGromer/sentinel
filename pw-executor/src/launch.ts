/**
 * M9.6 (ADR-037): resolve the browser launch mode from the environment.
 *
 * PURE function — no I/O, no browser — so the mode decision is unit-testable offline (the offline
 * suite cannot launch a browser). `ensureBrowser()` in server.ts consumes the returned plan.
 *
 * Precedence: PW_CDP_ENDPOINT (attach to a user's Chrome over CDP) > headed (PW_HEADLESS=0 or
 * PW_HEADED=1) > headless (default). headless is the deterministic golden-replay mode; headed and
 * CDP-attach are OBSERVATION / live-drive modes whose screenshot bytes are NOT guaranteed stable
 * (see docs/DETERMINISM.md). Engine is Chromium-only by design (ADR-036) — connectOverCDP is
 * Chromium-only and golden hashes differ per rendering engine.
 */
export interface LaunchPlan {
  /** 'launch' = we spawn Chromium; 'cdp' = we attach to an existing Chromium over CDP. */
  kind: 'launch' | 'cdp';
  /** Meaningful for kind === 'launch'. */
  headless: boolean;
  /** Set for kind === 'cdp' — the CDP endpoint, e.g. http://localhost:9222. */
  cdpEndpoint?: string;
}

export function resolveLaunchPlan(env: NodeJS.ProcessEnv): LaunchPlan {
  const cdp = (env.PW_CDP_ENDPOINT ?? '').trim();
  if (cdp) return { kind: 'cdp', headless: false, cdpEndpoint: cdp };
  // headed only on the explicit opt-in; any other value (unset/"1"/"true"/…) stays headless.
  const headed = env.PW_HEADLESS === '0' || env.PW_HEADED === '1';
  return { kind: 'launch', headless: !headed };
}

/* ---------------------------------------------------------------------------------------------
 * ADR-110 — reaching a CDP endpoint that lives in ANOTHER container.
 *
 * Measured on Chrome 150, not assumed (three separate barriers, each one fatal on its own):
 *
 *   1. Chromium binds the debugging port to 127.0.0.1 and IGNORES
 *      --remote-debugging-address=0.0.0.0 — silently, the log still says
 *      "DevTools listening on ws://127.0.0.1:9222". A sibling container therefore cannot
 *      reach it at all; the browser service publishes the port with a TCP forwarder instead.
 *   2. The DevTools HTTP endpoint validates the Host header:
 *         Host: browser:9223  ->  HTTP 500 "Host header is specified and is not an IP
 *                                 address or localhost."
 *      This is Chrome's DNS-rebinding guard. So `PW_CDP_ENDPOINT=http://browser:9223` — the
 *      obvious compose spelling — fails outright, and the failure names the header rather
 *      than the cause.
 *   3. Chrome echoes the Host it was addressed by into `webSocketDebuggerUrl`. Addressing it
 *      by numeric address is therefore not a workaround for step 2 alone: it is also what
 *      makes the websocket URL Playwright follows point back through the forwarder.
 *
 * Hence: substitute a numeric address for a DNS name before connecting. It is a REWRITE, so
 * ensureBrowser() logs it — an address silently different from the configured one is exactly
 * the kind of thing that makes a later failure unreadable.
 *
 * Both helpers are pure so the offline suite covers them; the DNS lookup itself lives at the
 * one call site that already does I/O.
 * ------------------------------------------------------------------------------------------- */

/**
 * True when `endpoint`'s host is a DNS name that Chrome's Host-header guard will reject.
 * Numeric addresses (v4, bracketed v6) and `localhost` pass through untouched — `localhost`
 * is explicitly allowed by the guard, and rewriting it would only add a lookup that can fail.
 * An unparseable endpoint returns false: it is not our business to reject it here, and
 * connectOverCDP reports a bad URL far better than a helper guessing at intent.
 */
export function cdpHostNeedsNumericAddress(endpoint: string): boolean {
  let u: URL;
  try { u = new URL(endpoint); } catch { return false; }
  const host = u.hostname;
  if (!host) return false;
  if (host === 'localhost') return false;
  if (host.startsWith('[')) return false;          // URL keeps v6 literals bracketed
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return false;
  // A hostname URL never keeps brackets, but an IPv6 literal reaches us unbracketed when the
  // caller built the string by hand; ':' cannot appear in a DNS name, so this is unambiguous.
  if (host.includes(':')) return false;
  return true;
}

/** Replace the host of `endpoint` with `addr`, preserving scheme, port and path. */
export function withCdpHost(endpoint: string, addr: string): string {
  const u = new URL(endpoint);
  u.hostname = addr.includes(':') && !addr.startsWith('[') ? `[${addr}]` : addr;
  return u.toString().replace(/\/$/, '');
}
