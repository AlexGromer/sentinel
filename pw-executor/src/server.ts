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
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

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

let browser: Browser | null = null;
let context: BrowserContext | null = null;
let page: Page | null = null;
let tracingStopped = false;

async function ensureBrowser(): Promise<void> {
  if (browser) return;
  browser = await chromium.launch({ headless: true });
  context = await browser.newContext();
  await context.tracing.start({ screenshots: true, snapshots: true });
  page = await context.newPage();
  log('browser launched, tracing started');
}

/** Transport-agnostic tool dispatch. `method` is the dotted name (e.g. "browser.navigate"). */
async function dispatch(method: string, params: Record<string, unknown>): Promise<unknown> {
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
      const buf = await page!.screenshot();
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
        await page!.screenshot({ path: outPath });
        await page!.evaluate(() => document.getElementById('__som__')?.remove());
      }
      return { marks, path: outPath ?? null };
    }
    case 'browser.traceStop': {
      const path = params?.path as string | undefined;
      if (!path) throw new Error('traceStop: missing params.path');
      if (context && !tracingStopped) {
        await context.tracing.stop({ path });
        tracingStopped = true;
      }
      return { path };
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
  'browser.probe',
  'browser.interactives',
  'browser.screenshotHash',
  'browser.setOfMarks',
  'browser.traceStop',
];

// --- Transport 1: newline JSON-RPC 2.0 (default) ----------------------------
async function mainJsonRpc(): Promise<void> {
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
    if (context && !tracingStopped) await context.tracing.stop();
    await browser?.close();
  } catch (e) {
    log('cleanup error', e);
  }
  log('exit');
  process.exit(0);
}

// --- Transport 2: MCP SDK (opt-in) ------------------------------------------
async function mainMcp(): Promise<void> {
  const server = new McpServer({ name: 'pw-executor', version: '0.0.0' });
  const locatorShape = { locator: z.record(z.string(), z.any()) };
  const schemas: Record<string, Record<string, z.ZodTypeAny>> = {
    'browser.navigate': { url: z.string() },
    'browser.snapshot': {},
    'browser.currentUrl': {},
    'browser.links': {},
    'browser.click': locatorShape,
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
