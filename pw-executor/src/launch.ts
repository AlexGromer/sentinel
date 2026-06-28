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
