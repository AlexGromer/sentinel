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
import { setupTracing, spanForTool } from './otel.js';

const log = (...a: unknown[]): void => console.error('[pw-executor]', ...a);

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
let tracingStarted = false;
let tracingStopped = false;

async function ensureBrowser(): Promise<void> {
  if (browser) return;
  browser = await chromium.launch({ headless: true });
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
  // M8/GAP-RISK-009: fixed viewport + DSR=1 so screenshot bytes are stable across browser processes.
  context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 1,
    ...(storageState ? { storageState } : {}),
  });
  if (storageState) log('storageState loaded from', statePath);
  // M9.1/ADR-026 + GAP-RISK-010: an auth run sets PW_NO_TRACE=1 so a typed password never lands in
  // trace.zip (the trace captures DOM input.value AND the submit POST body — Playwright has no mask API).
  if (process.env.PW_NO_TRACE !== '1') {
    await context.tracing.start({ screenshots: true, snapshots: true });
    tracingStarted = true;
    log('browser launched, tracing started');
  } else {
    log('browser launched, tracing DISABLED (PW_NO_TRACE=1)');
  }
  page = await context.newPage();
  page.setDefaultTimeout(5000); // bound browser.expect's pollUntil inner waits to the intended 5s budget
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
      const resp = await page!.goto(url, { waitUntil: 'domcontentloaded' });
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
        'button, a[href], input, select, textarea, [role=button]',
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
      // GAP-RISK-009: disable animations + hide the caret + CSS-scale so the hash is byte-stable.
      const buf = await page!.screenshot({ animations: 'disabled', caret: 'hide', scale: 'css' });
      return { hash: crypto.createHash('sha256').update(buf).digest('hex') };
    }
    case 'browser.setOfMarks': {
      // M5-2 visual heal: number every interactive element + (optionally) write an overlay
      // screenshot, returning the mark->element map so the vision LLM picks a mark, not a pixel.
      await ensureBrowser();
      const outPath = params?.path as string | undefined;
      const marks = await page!.$$eval(
        'button, a[href], input, select, textarea, [role=button]',
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
        await page!.screenshot({ path: outPath, animations: 'disabled', caret: 'hide', scale: 'css' });
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
    await browser?.close();
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
