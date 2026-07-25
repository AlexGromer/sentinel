/**
 * Sentinel pw-executor — our own Playwright execution server (ADR-001, build-only).
 *
 * DUAL TRANSPORT (M2b-2, ADR-016):
 *  - default: newline-delimited JSON-RPC 2.0 over stdio (M0; proven).
 *  - MCP_TRANSPORT=mcp: the same tools served via the MCP SDK (StdioServerTransport).
 * Both call the SAME `dispatch(method, params)` — identical behavior either way.
 *
 * CRITICAL: stdout carries ONLY protocol frames. All logs MUST go to stderr.
 */
import { chromium, Browser, BrowserContext, Page, Locator } from 'playwright';
import * as readline from 'node:readline';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { setupTracing, spanForTool, currentTraceparent } from './otel.js';
import { resolveLaunchPlan } from './launch.js';
import {
  DETERMINISM_VIEWPORT,
  DETERMINISM_DEVICE_SCALE_FACTOR,
  SCREENSHOT_DETERMINISM_OPTS,
} from './determinism.js';

const log = (...a: unknown[]): void => console.error('[pw-executor]', ...a);

/* ------------------------------------------------------------------ ADR-067: application log channel
 * The tested SITE's own console, exceptions and failed requests. Until now nobody collected them, so a
 * tester watching a step fail could not tell whether the tool mis-clicked or the application threw —
 * which is the first question they actually have.
 *
 * These are emitted in the SAME wire format brain uses (`[lvl|cat] code: message`), so the boundary
 * parses them with no new code and the event catalogue governs their level, category and Russian text.
 *
 * The English strings below must stay identical to the catalogue's `en` templates: the UI recovers the
 * placeholder values by matching that template against this rendered text. A drift would silently
 * degrade the UI to English, so tests/test_event_catalog_offline.py compares the two.
 */
const APP_MESSAGES: Record<string, string> = {
  'app.js_error': 'The page under test threw an error: {msg}',
  'app.console_error': 'Site console: {msg}',
  'app.console_warn': 'Site console warning: {msg}',
  'app.request_failed': 'A site request failed: {method} {url} — {reason}',
  'app.http_error': 'The site answered {status} to {method} {url}',
  'app.dialog': 'The site opened a dialog ({kind}): {msg}',
  'app.log_capped': 'Too many messages from the site — capturing stops here (cap {cap})',
};

/* A hostile or merely chatty page can emit thousands of console lines. The cap bounds the artifact
 * without hiding that it was reached — app.log_capped says so once, so a truncated capture can never be
 * mistaken for a quiet application. */
const APP_LOG_CAP = Number(process.env.PW_APP_LOG_CAP ?? 500);
let appLogCount = 0;

function appLog(lvl: 'debug' | 'info' | 'warn' | 'error', code: string, fields: Record<string, unknown>): void {
  if (appLogCount > APP_LOG_CAP) return;
  appLogCount += 1;
  if (appLogCount > APP_LOG_CAP) {
    console.error(`[warn|app] app.log_capped: ${render(APP_MESSAGES['app.log_capped'], { cap: APP_LOG_CAP })}`);
    return;
  }
  const tpl = APP_MESSAGES[code];
  if (tpl === undefined) return; // an uncatalogued code would render as a bare code in the UI
  console.error(`[${lvl}|app] ${code}: ${render(tpl, fields)}`);
}

/** Fills {placeholders} and flattens newlines — the protocol is line-oriented, and a stack trace in a
 *  console message would otherwise split one event into unparseable fragments. */
function render(tpl: string, fields: Record<string, unknown>): string {
  const filled = tpl.replace(/\{(\w+)\}/g, (whole, k: string) =>
    k in fields ? String(fields[k] ?? '') : whole);
  return filled.split(/\s*\r?\n\s*/).join(' ⏎ ').trim();
}

/** Attaches the application-log capture to one page. Called for the initial page AND every popup/new
 *  tab, because a failure that only happens in a second tab is exactly the kind that goes unnoticed. */
function attachAppCapture(p: Page): void {
  p.on('pageerror', (err) => appLog('error', 'app.js_error', { msg: err.message }));
  p.on('console', (m) => {
    const t = m.type();
    if (t === 'error') appLog('error', 'app.console_error', { msg: m.text() });
    else if (t === 'warning') appLog('warn', 'app.console_warn', { msg: m.text() });
    // info/log/debug from a page are ignored on purpose: they are the application's own chatter, not a
    // signal, and they would bury the two levels that matter.
  });
  p.on('requestfailed', (r) => appLog('warn', 'app.request_failed', {
    method: r.method(), url: r.url(), reason: r.failure()?.errorText ?? 'unknown',
  }));
  p.on('response', (r) => {
    const st = r.status();
    // 4xx/5xx only. A redirect is not a fault, and 3xx is how normal navigation works.
    if (st >= 400) appLog('warn', 'app.http_error', { status: st, method: r.request().method(), url: r.url() });
  });
  p.on('dialog', async (d) => {
    appLog('info', 'app.dialog', { kind: d.type(), msg: d.message() });
    // Playwright auto-dismisses when no handler is attached; having attached one, we must dismiss
    // ourselves or the page hangs forever on an alert.
    try { await d.dismiss(); } catch { /* already handled elsewhere */ }
  });
}

interface RpcRequest {
  jsonrpc: string;
  id: number | string;
  method: string;
  params?: Record<string, unknown>;
}
interface RpcResponse {
  jsonrpc: '2.0';
  id: number | string;
  result?: unknown;
  error?: { code: number; message: string };
}

/** A locator is a dict with EXACTLY ONE of these shapes (M2 locator model). */
interface LocatorSpec {
  testid?: string;
  role?: string;
  name?: string;
  label?: string;
  text?: string;
  css?: string;
  xpath?: string;
}

/** Shared locator builder used by BOTH browser.click and browser.probe. */
function buildLocator(page: Page, locator: LocatorSpec): Locator {
  if (locator.testid !== undefined) return page.getByTestId(locator.testid);
  if (locator.role !== undefined)
    return page.getByRole(locator.role as Parameters<Page['getByRole']>[0], { name: locator.name });
  if (locator.label !== undefined) return page.getByLabel(locator.label);
  if (locator.text !== undefined) return page.getByText(locator.text);
  if (locator.css !== undefined) return page.locator(locator.css);
  if (locator.xpath !== undefined) return page.locator('xpath=' + locator.xpath);
  throw new Error(
    'buildLocator: locator must provide one of {testid}, {role,name}, {label}, {text}, {css}, {xpath}',
  );
}

/** M9.1 (browser.expect): poll an async predicate until true or the deadline — auto-retry that
 * tolerates post-submit navigation/XHR without depending on @playwright/test (GAP-ARCH-001: thin). */
async function pollUntil(fn: () => Promise<boolean>, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      if (await fn()) return true;
    } catch {
      /* transient (detached node mid-nav) — keep polling until the deadline */
    }
    if (Date.now() >= deadline) return false;
    await new Promise((r) => setTimeout(r, 100));
  }
}

let browser: Browser | null = null;
let context: BrowserContext | null = null;
let page: Page | null = null;
// M9.4 (A6): every page in the context (initial + popups/new tabs). `page` is the ACTIVE one;
// browser.switchTab just re-points `page`, so all existing tools operate on the active tab unchanged.
let pages: Page[] = [];
let tracingStarted = false;
let tracingStopped = false;
// M9.6/ADR-037: true when we attached to the user's browser over CDP — teardown must NOT close it.
let attachedOverCDP = false;

async function ensureBrowser(): Promise<void> {
  if (browser) return;
  // M9.6/ADR-037: resolve launch mode (headless default / headed / CDP-attach) from env (pure, tested).
  const plan = resolveLaunchPlan(process.env);
  // M9.1/ADR-026: pre-authenticated context from a saved storageState (produced by login-as-test).
  // Parse the file HERE so a missing OR corrupt/empty state.json both fall back to a no-state context
  // (don't crash the run) — passing a string path would make newContext throw on bad JSON, killing the
  // whole run. Log only the PATH on failure, never the bytes (the file holds session tokens — §3).
  const statePath = process.env.STORAGE_STATE;
  let storageState: Awaited<ReturnType<BrowserContext['storageState']>> | undefined;
  if (statePath && fs.existsSync(statePath)) {
    try {
      storageState = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    } catch {
      log('STORAGE_STATE present but corrupt/unreadable; continuing no-state:', statePath);
    }
  } else if (statePath) {
    log('STORAGE_STATE set but missing; continuing no-state:', statePath);
  }

  if (plan.kind === 'cdp') {
    // M9.6/ADR-037: attach to the user's EXISTING Chromium over CDP (`--remote-debugging-port`) and
    // reuse THEIR context+session. We do not own this browser, so teardown must never close it (see the
    // `attachedOverCDP` guards). Our viewport/DSR/ignoreHTTPSErrors/storageState overrides do NOT apply
    // to an adopted context — screenshots are NOT byte-stable here (observation mode, docs/DETERMINISM.md).
    // NOTE: the shared setup below STILL applies to the adopted context when env-enabled — traceparent
    // route-injection (OTEL_*) and tracing (unless PW_NO_TRACE=1) touch the user's LIVE session; set
    // PW_NO_TRACE=1 to avoid recording their session into trace.zip.
    attachedOverCDP = true;
    browser = await chromium.connectOverCDP(plan.cdpEndpoint!);
    context = browser.contexts()[0] ?? (await browser.newContext());
    log('attached over CDP:', plan.cdpEndpoint);
    if (storageState) log('STORAGE_STATE ignored in CDP-attach mode (reusing the user session)');
  } else {
    // M8/GAP-RISK-009: fixed viewport + DSR=1 so screenshot bytes are stable across browser processes.
    // The anchors live in determinism.ts (single source of truth, asserted by determinism.test.ts).
    browser = await chromium.launch({ headless: plan.headless });
    context = await browser.newContext({
      viewport: DETERMINISM_VIEWPORT,
      deviceScaleFactor: DETERMINISM_DEVICE_SCALE_FACTOR,
      // GAP-OPS-002: AUT TLS handling. Strict by DEFAULT (cert errors surface). Opt-in bypass only for
      // testing a self-signed/expired AUT cert — NEVER for prod auth runs. When strict, browser.navigate
      // re-throws cert failures as a classified, actionable diagnostic instead of an opaque error.
      ...(process.env.PW_IGNORE_HTTPS_ERRORS === '1' ? { ignoreHTTPSErrors: true } : {}),
      ...(storageState ? { storageState } : {}),
    });
    log(plan.headless ? 'browser launched (headless)' : 'browser launched (headed)');
    if (storageState) log('storageState loaded from', statePath);
  }

  // M9.5 / §I: inject the active span's W3C traceparent into EVERY browser request so each UI action
  // maps onto the AUT's end-to-end backend trace (when its services are OTel-instrumented). Gated on a
  // configured collector — no route-interception overhead otherwise.
  if (process.env.OTEL_EXPORTER_OTLP_ENDPOINT) {
    await context.route('**/*', async (route) => {
      const tp = currentTraceparent();
      await route.continue(tp ? { headers: { ...route.request().headers(), traceparent: tp } } : {});
    });
  }
  // M9.1/ADR-026 + GAP-RISK-010: an auth run sets PW_NO_TRACE=1 so a typed password never lands in
  // trace.zip (the trace captures DOM input.value AND the submit POST body — Playwright has no mask API).
  if (process.env.PW_NO_TRACE !== '1') {
    await context.tracing.start({ screenshots: true, snapshots: true });
    tracingStarted = true;
    log('tracing started');
  } else {
    log('tracing DISABLED (PW_NO_TRACE=1)');
  }
  // M9.4 (A6): the active page is the first EXISTING page (CDP: the user's open tab; launch: a fresh
  // page we create), and we track every page in the context (initial + popups/new tabs).
  const existing = context.pages();
  page = existing.length ? existing[0] : await context.newPage();
  page.setDefaultTimeout(5000); // bound browser.expect's pollUntil inner waits to the intended 5s budget
  pages = existing.length ? [...existing] : [page];
  pages.forEach(attachAppCapture); // ADR-067: the site's own console/errors/failed requests
  // The 'page' event fires only for pages created AFTER this handler is attached.
  context.on('page', (p) => {
    if (!pages.includes(p)) {
      p.setDefaultTimeout(5000);
      pages.push(p);
      attachAppCapture(p); // ADR-067
      log('new browser tab/page tracked: index', pages.length - 1);
    }
  });
}

/** Transport-agnostic tool dispatch. `method` is the dotted name (e.g. "browser.navigate"). */
async function dispatch(method: string, params: Record<string, unknown>): Promise<unknown> {
  // M8: continue the brain's trace (W3C `traceparent` in params._meta) with a per-tool child span.
  const meta = params._meta as Record<string, string> | undefined;
  return spanForTool(method, meta, () => dispatchInner(method, params));
}

async function dispatchInner(method: string, params: Record<string, unknown>): Promise<unknown> {
  switch (method) {
    case 'initialize':
      await ensureBrowser();
      return { name: 'pw-executor', version: '0.0.0', capabilities: TOOL_METHODS };
    case 'browser.navigate': {
      await ensureBrowser();
      const url = params?.url as string | undefined;
      if (!url) throw new Error('navigate: missing params.url');
      let resp;
      try {
        resp = await page!.goto(url, { waitUntil: 'domcontentloaded' });
      } catch (e) {
        // GAP-OPS-002: classify TLS cert failures into an actionable message (default-strict path).
        const msg = e instanceof Error ? e.message : String(e);
        const cert = msg.match(/ERR_CERT[_A-Z]*|ERR_SSL[_A-Z]*|SSL_ERROR[_A-Z]*/);
        if (cert) {
          throw new Error(
            `navigate: TLS certificate error for ${url} (${cert[0]}). ` +
              `Set PW_IGNORE_HTTPS_ERRORS=1 to bypass for testing a self-signed/expired cert (never for prod auth).`,
          );
        }
        throw e;
      }
      return { url: page!.url(), title: await page!.title(), status: resp?.status() ?? null };
    }
    case 'browser.snapshot': {
      await ensureBrowser();
      const ariaSnapshot = await page!.locator('body').ariaSnapshot();
      const nodeCount = ariaSnapshot.split('\n').filter((l) => l.trim().startsWith('-')).length;
      return { ariaSnapshot, nodeCount };
    }
    case 'browser.currentUrl':
      await ensureBrowser();
      return { url: page!.url(), title: await page!.title() };
    case 'browser.links': {
      await ensureBrowser();
      const links = await page!.$$eval('a[href]', (els) =>
        els.map((a) => ({ href: (a as HTMLAnchorElement).href, text: (a.textContent || '').trim() })),
      );
      return { links };
    }
    case 'browser.click': {
      await ensureBrowser();
      const loc = buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).first();
      await loc.click({ timeout: 5000 });
      return { clicked: true, url: page!.url() };
    }
    // --- M9.1 (ADR-026): form/login interaction verbs + assert + auth-state ------
    case 'browser.fill': {
      // Text entry. A SECRET is referenced by env-var NAME (secretRef), resolved here ONLY; the value
      // is never returned/logged, and a failure is re-thrown sanitized so it can't leak via the message.
      await ensureBrowser();
      const loc = buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).first();
      const secretRef = params?.secretRef as string | undefined;
      if (secretRef !== undefined) {
        // Fail-closed (GAP-RISK-010): never enter a credential while tracing is active — the trace
        // would capture it (DOM snapshot + submit POST). Guard BEFORE reading the env so the secret is
        // never even loaded. No-op in login-as-test (PW_NO_TRACE=1) and prod (storageState, no secret step).
        if (tracingStarted)
          throw new Error('browser.fill: refusing to enter a secret while tracing is active (set PW_NO_TRACE=1)');
        const v = process.env[secretRef];
        if (v === undefined) throw new Error(`secret '${secretRef}' not set`);
        log('fill', params?.locator, '= <redacted>');
        try {
          await loc.fill(v, { timeout: 5000 });
        } catch {
          throw new Error('browser.fill failed (secret redacted)');
        }
      } else {
        await loc.fill((params?.value as string) ?? '', { timeout: 5000 });
      }
      return { filled: true };
    }
    case 'browser.type': {
      // Keystroke-by-keystroke entry (pressSequentially; locator.type() is deprecated since PW 1.38).
      // Does NOT clear by default (append); pass clear:true to fill('') first.
      await ensureBrowser();
      const loc = buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).first();
      if (params?.clear) await loc.fill('', { timeout: 5000 });
      await loc.pressSequentially((params?.text as string) ?? '', { timeout: 5000 });
      return { typed: true };
    }
    case 'browser.press': {
      await ensureBrowser();
      const key = params?.key as string | undefined;
      if (!key) throw new Error('press: missing params.key');
      if (params?.locator) {
        await buildLocator(page!, params.locator as LocatorSpec).first().press(key, { timeout: 5000 });
      } else {
        await page!.keyboard.press(key); // page-level key needs prior focus
      }
      return { pressed: key };
    }
    case 'browser.select': {
      await ensureBrowser();
      const loc = buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).first();
      const selected = await loc.selectOption(
        params?.value as Parameters<Locator['selectOption']>[0], { timeout: 5000 });
      return { selected };
    }
    case 'browser.expect': {
      // Non-throwing assert primitive for (negative) validation testing: the BRAIN decides pass/fail
      // (step passes iff result.ok == expect_ok). Auto-waits so it doesn't race a post-submit nav.
      // `actual` is restricted to counts/url/booleans — NEVER inputValue() (would echo a secret).
      await ensureBrowser();
      const condition = (params?.condition as string) ?? '';
      const timeout = 5000;
      const locSpec = params?.locator as LocatorSpec | undefined;
      try {
        switch (condition) {
          case 'visible':
            await buildLocator(page!, locSpec!).first().waitFor({ state: 'visible', timeout });
            return { ok: true };
          case 'hidden':
            await buildLocator(page!, locSpec!).first().waitFor({ state: 'hidden', timeout });
            return { ok: true };
          case 'enabled': {
            const loc = buildLocator(page!, locSpec!).first();
            const ok = await pollUntil(() => loc.isEnabled(), timeout);
            return { ok, actual: ok };
          }
          case 'disabled': {
            const loc = buildLocator(page!, locSpec!).first();
            const ok = await pollUntil(async () => !(await loc.isEnabled()), timeout);
            return { ok };
          }
          case 'value_equals': {
            const loc = buildLocator(page!, locSpec!).first();
            const want = String(params?.expected ?? '');
            const ok = await pollUntil(async () => (await loc.inputValue()) === want, timeout);
            return { ok }; // deliberately no `actual` (never echo a field value)
          }
          case 'text_contains': {
            const loc = buildLocator(page!, locSpec!).first();
            const want = String(params?.expected ?? '');
            const ok = await pollUntil(async () => ((await loc.textContent()) ?? '').includes(want), timeout);
            return { ok };
          }
          case 'count_equals': {
            const countLoc = buildLocator(page!, locSpec!);
            const want = Number(params?.expected ?? 0);
            const ok = await pollUntil(async () => (await countLoc.count()) === want, timeout);
            return { ok, actual: await countLoc.count() };
          }
          case 'url_contains': {
            const want = String(params?.expected ?? '');
            try {
              await page!.waitForURL((u) => u.href.includes(want), { timeout });
              return { ok: true, actual: page!.url() };
            } catch {
              return { ok: false, actual: page!.url() };
            }
          }
          default:
            throw new Error(`expect: unknown condition '${condition}'`);
        }
      } catch (e) {
        if (e instanceof Error && e.message.startsWith('expect:')) throw e; // malformed plan -> real error
        return { ok: false }; // assertion simply did not hold within the timeout
      }
    }
    case 'browser.saveStorageState': {
      // M9.1/ADR-026: persist cookies/localStorage after a successful login-as-test run.
      await ensureBrowser();
      const path = params?.path as string | undefined;
      if (!path) throw new Error('saveStorageState: missing params.path');
      await context!.storageState({ path });
      return { path };
    }
    case 'browser.probe':
      await ensureBrowser();
      return { count: await buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).count() };
    case 'browser.interactives': {
      await ensureBrowser();
      const elements = await page!.$$eval(
        'button, a[href], input, select, textarea, [role=button], [role=tab]',
        (els) =>
          els.map((e) => ({
            role: e.getAttribute('role') || e.tagName.toLowerCase(),
            name: (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 200),
            testid: e.getAttribute('data-testid'),
            text: (e.textContent || '').trim().slice(0, 200),
            tag: e.tagName.toLowerCase(),
          })),
      );
      return { elements };
    }
    case 'browser.screenshotHash': {
      await ensureBrowser();
      // GAP-RISK-009: disable animations + hide the caret + CSS-scale so the hash is byte-stable
      // (anchors in determinism.ts, asserted by determinism.test.ts).
      const buf = await page!.screenshot(SCREENSHOT_DETERMINISM_OPTS);
      return { hash: crypto.createHash('sha256').update(buf).digest('hex') };
    }
    case 'browser.setOfMarks': {
      // M5-2 visual heal: number every interactive element + (optionally) write an overlay
      // screenshot, returning the mark->element map so the vision LLM picks a mark, not a pixel.
      await ensureBrowser();
      const outPath = params?.path as string | undefined;
      const marks = await page!.$$eval(
        'button, a[href], input, select, textarea, [role=button], [role=tab]',
        (els) =>
          els
            .map((e, i) => {
              const r = e.getBoundingClientRect();
              return {
                mark: i,
                role: e.getAttribute('role') || e.tagName.toLowerCase(),
                name: (e.getAttribute('aria-label') || (e as HTMLElement).innerText || e.textContent || '')
                  .trim()
                  .slice(0, 120),
                testid: e.getAttribute('data-testid'),
                bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
              };
            })
            .filter((m) => m.bbox.w > 0 && m.bbox.h > 0),
      );
      if (outPath) {
        await page!.evaluate((ms) => {
          const o = document.createElement('div');
          o.id = '__som__';
          for (const m of ms) {
            const box = document.createElement('div');
            box.style.cssText = `position:fixed;left:${m.bbox.x}px;top:${m.bbox.y}px;width:${m.bbox.w}px;height:${m.bbox.h}px;border:2px solid red;z-index:2147483647;pointer-events:none`;
            const lbl = document.createElement('div');
            lbl.textContent = String(m.mark);
            lbl.style.cssText = `position:fixed;left:${m.bbox.x}px;top:${Math.max(0, m.bbox.y - 14)}px;background:red;color:#fff;font:10px monospace;z-index:2147483647;padding:0 2px`;
            o.appendChild(box);
            o.appendChild(lbl);
          }
          document.body.appendChild(o);
        }, marks);
        await page!.screenshot({ path: outPath, ...SCREENSHOT_DETERMINISM_OPTS });
        await page!.evaluate(() => document.getElementById('__som__')?.remove());
      }
      return { marks, path: outPath ?? null };
    }
    case 'browser.traceStop': {
      const path = params?.path as string | undefined;
      if (!path) throw new Error('traceStop: missing params.path');
      if (context && tracingStarted && !tracingStopped) {
        await context.tracing.stop({ path });
        tracingStopped = true;
      }
      return { path }; // no-op when tracing was never started (PW_NO_TRACE=1 auth run)
    }
    case 'browser.tabs': {
      // M9.4 (A6): list tracked browser tabs/pages (drop any that closed). Indices match switchTab.
      await ensureBrowser();
      pages = pages.filter((p) => !p.isClosed());
      const tabs = await Promise.all(
        pages.map(async (p, i) => ({
          index: i,
          url: p.url(),
          title: await p.title().catch(() => ''),
          active: p === page,
        })),
      );
      return { tabs };
    }
    case 'browser.switchTab': {
      // M9.4 (A6): make tab `index` the active page; every existing tool then operates on it unchanged.
      await ensureBrowser();
      pages = pages.filter((p) => !p.isClosed());
      const idx = Number(params?.index);
      if (!Number.isInteger(idx) || idx < 0 || idx >= pages.length)
        throw new Error(`switchTab: index ${String(params?.index)} out of range (0..${pages.length - 1})`);
      page = pages[idx];
      await page.bringToFront();
      return { index: idx, url: page.url(), title: await page.title().catch(() => '') };
    }
    case 'shutdown':
      return { ok: true };
    default:
      throw new Error(`unknown method: ${method}`);
  }
}

/** Tool names exposed over MCP (browser.* only; initialize/shutdown are lifecycle, not tools). */
const TOOL_METHODS = [
  'browser.navigate',
  'browser.snapshot',
  'browser.currentUrl',
  'browser.links',
  'browser.click',
  'browser.fill',
  'browser.type',
  'browser.press',
  'browser.select',
  'browser.expect',
  'browser.saveStorageState',
  'browser.probe',
  'browser.interactives',
  'browser.screenshotHash',
  'browser.setOfMarks',
  'browser.traceStop',
  'browser.tabs',
  'browser.switchTab',
];

// --- Transport 1: newline JSON-RPC 2.0 (default) ----------------------------
async function mainJsonRpc(): Promise<void> {
  await setupTracing();
  const rl = readline.createInterface({ input: process.stdin });
  for await (const line of rl) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let req: RpcRequest;
    try {
      req = JSON.parse(trimmed) as RpcRequest;
    } catch (e) {
      log('parse error', e);
      continue;
    }
    const res: RpcResponse = { jsonrpc: '2.0', id: req.id };
    try {
      res.result = await dispatch(req.method, req.params ?? {});
    } catch (e) {
      res.error = { code: -32000, message: e instanceof Error ? e.message : String(e) };
    }
    process.stdout.write(JSON.stringify(res) + '\n');
    if (req.method === 'shutdown') break;
  }
  try {
    if (context && tracingStarted && !tracingStopped) await context.tracing.stop();
    if (!attachedOverCDP) await browser?.close(); // M9.6: never close the user's CDP-attached browser
  } catch (e) {
    log('cleanup error', e);
  }
  log('exit');
  process.exit(0);
}

// --- Transport 2: MCP SDK (opt-in) ------------------------------------------
async function mainMcp(): Promise<void> {
  await setupTracing();
  const server = new McpServer({ name: 'pw-executor', version: '0.0.0' });
  const locatorShape = { locator: z.record(z.string(), z.any()) };
  const schemas: Record<string, Record<string, z.ZodTypeAny>> = {
    'browser.navigate': { url: z.string() },
    'browser.snapshot': {},
    'browser.currentUrl': {},
    'browser.links': {},
    'browser.click': locatorShape,
    'browser.fill': { locator: z.record(z.string(), z.any()), value: z.string().optional(), secretRef: z.string().optional() },
    'browser.type': { locator: z.record(z.string(), z.any()), text: z.string(), clear: z.boolean().optional() },
    'browser.press': { locator: z.record(z.string(), z.any()).optional(), key: z.string() },
    'browser.select': { locator: z.record(z.string(), z.any()), value: z.any() },
    'browser.expect': { locator: z.record(z.string(), z.any()).optional(), condition: z.string(), expected: z.any().optional() },
    'browser.saveStorageState': { path: z.string() },
    'browser.probe': locatorShape,
    'browser.interactives': {},
    'browser.screenshotHash': {},
    'browser.setOfMarks': { path: z.string() },
    'browser.traceStop': { path: z.string() },
  };
  for (const method of TOOL_METHODS) {
    const toolName = method.replace('browser.', 'browser_'); // MCP tool names avoid dots
    server.registerTool(
      toolName,
      { inputSchema: schemas[method] },
      async (args: Record<string, unknown>) => {
        const result = await dispatch(method, args ?? {});
        return { content: [{ type: 'text' as const, text: JSON.stringify(result) }] };
      },
    );
  }
  process.on('SIGTERM', () => {
    // M9.6: in CDP-attach we don't own the browser — just exit (drops the CDP connection), leave it open.
    if (attachedOverCDP) { process.exit(0); return; }
    void browser?.close().finally(() => process.exit(0));
  });
  await server.connect(new StdioServerTransport());
  log('MCP server connected (stdio)');
}

if (process.env.MCP_TRANSPORT === 'mcp') {
  mainMcp().catch((e) => {
    log('mcp fatal', e);
    process.exit(1);
  });
} else {
  mainJsonRpc().catch((e) => {
    log('fatal', e);
    process.exit(1);
  });
}
