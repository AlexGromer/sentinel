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
 *
 * LIVE-HUMAN (ADR-120) added the pacing of a decorated run HERE, and not by accident: `slowMo` is a
 * `launch()` option, so by the time a step runs the browser exists and the value can no longer be
 * applied. That also makes one case impossible rather than merely awkward — in CDP-attach the
 * browser was launched by somebody else, so `slowMo` CANNOT be set at all. The plan says so
 * (`slowMoUnavailable`) and compensates with a longer per-step pause on our own side, because a mode
 * that silently ran at full speed after being asked to slow down is the class of silence ADR-120
 * exists to remove.
 */
import { decorationsEnabled, DECOR_SLOW_MO_MS, DECOR_STEP_PAUSE_MS, DECOR_STEP_PAUSE_CDP_MS } from './decorate.js';

export interface LaunchPlan {
  /** 'launch' = we spawn Chromium; 'cdp' = we attach to an existing Chromium over CDP. */
  kind: 'launch' | 'cdp';
  /** Meaningful for kind === 'launch'. */
  headless: boolean;
  /** Set for kind === 'cdp' — the CDP endpoint, e.g. http://localhost:9222. */
  cdpEndpoint?: string;
  /** LIVE-HUMAN: this run draws for a person (cursor, highlight, echo, per-character entry). */
  decorate: boolean;
  /** ms for `chromium.launch({slowMo})`; 0 when the run is not slowed. Only kind === 'launch'. */
  slowMo: number;
  /** True when decoration asked for `slowMo` and this launch path cannot carry it (CDP-attach). */
  slowMoUnavailable: boolean;
  /** Our own pause before each acting verb; carries the whole pacing where `slowMo` cannot. */
  stepPauseMs: number;
}

export function resolveLaunchPlan(env: NodeJS.ProcessEnv): LaunchPlan {
  // The ONE reader of SENTINEL_DECORATE in this process (brain/observe.py::apply is its one writer).
  const decorate = decorationsEnabled(env);
  const cdp = (env.PW_CDP_ENDPOINT ?? '').trim();
  if (cdp)
    return {
      kind: 'cdp', headless: false, cdpEndpoint: cdp, decorate,
      slowMo: 0,
      slowMoUnavailable: decorate,
      stepPauseMs: decorate ? DECOR_STEP_PAUSE_CDP_MS : 0,
    };
  // headed only on the explicit opt-in; any other value (unset/"1"/"true"/…) stays headless.
  const headed = env.PW_HEADLESS === '0' || env.PW_HEADED === '1';
  return {
    kind: 'launch', headless: !headed, decorate,
    slowMo: decorate ? DECOR_SLOW_MO_MS : 0,
    slowMoUnavailable: false,
    stepPauseMs: decorate ? DECOR_STEP_PAUSE_MS : 0,
  };
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

/**
 * Choose which resolved address to connect to. IPv4 wins when present.
 *
 * Not a detail: `cdp-service.ts` binds its relay to `0.0.0.0`, the IPv4 wildcard, so an AAAA answer
 * names an address nothing is listening on. A plain `dns.lookup()` returns whatever the resolver
 * happens to order first — on a GitHub runner `localhost` comes back as `::1`, and the connection
 * was refused against a browser that was up and healthy. Selecting by FAMILY rather than by position
 * makes the outcome independent of resolver order, which is why this is a pure function with its own
 * test instead of an argument to `dns.lookup`.
 *
 * The fallback is the first answer of any family, so an IPv6-only deployment (CDP_LISTEN_ADDR=::)
 * still resolves — only the default is opinionated.
 */
export function pickCdpAddress(answers: Array<{ address: string; family: number }>): string | null {
  if (!answers || answers.length === 0) return null;
  const v4 = answers.find((a) => a.family === 4);
  return (v4 ?? answers[0]).address;
}

/** Replace the host of `endpoint` with `addr`, preserving scheme, port and path. */
export function withCdpHost(endpoint: string, addr: string): string {
  const u = new URL(endpoint);
  u.hostname = addr.includes(':') && !addr.startsWith('[') ? `[${addr}]` : addr;
  return u.toString().replace(/\/$/, '');
}
