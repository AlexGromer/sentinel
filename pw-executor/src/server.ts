/**
 * Sentinel M0 — pw-executor: minimal Playwright execution server.
 * Transport: newline-delimited JSON-RPC 2.0 over stdio (MCP-aligned; SDK migration @ M1).
 *
 * CRITICAL: stdout carries ONLY JSON-RPC responses. All logs MUST go to stderr.
 * We BUILD this server ourselves (ADR-001, build-only constraint).
 */
import { chromium, Browser, BrowserContext, Page, Locator } from 'playwright';
import * as readline from 'node:readline';

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

/**
 * A locator is a dict with EXACTLY ONE of these shapes (M2 locator model).
 * Resolution order mirrors the L1–L6 rotation priors in M2_CONTRACT.md.
 */
interface LocatorSpec {
  testid?: string;
  role?: string;
  name?: string;
  label?: string;
  text?: string;
  css?: string;
  xpath?: string;
}

/**
 * Shared locator builder used by BOTH browser.click and browser.probe so the
 * resolution semantics stay identical across action and verify-before-accept.
 */
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

async function handle(req: RpcRequest): Promise<unknown> {
  switch (req.method) {
    case 'initialize':
      await ensureBrowser();
      return {
        name: 'pw-executor',
        version: '0.0.0-m0',
        capabilities: [
          'browser.navigate',
          'browser.snapshot',
          'browser.currentUrl',
          'browser.links',
          'browser.click',
          'browser.probe',
          'browser.interactives',
          'browser.traceStop',
        ],
      };
    case 'browser.navigate': {
      await ensureBrowser();
      const url = req.params?.url as string | undefined;
      if (!url) throw new Error('navigate: missing params.url');
      const resp = await page!.goto(url, { waitUntil: 'domcontentloaded' });
      return { url: page!.url(), title: await page!.title(), status: resp?.status() ?? null };
    }
    case 'browser.snapshot': {
      await ensureBrowser();
      // ariaSnapshot() is Playwright's current accessibility-tree representation (YAML-ish string).
      const ariaSnapshot = await page!.locator('body').ariaSnapshot();
      const nodeCount = ariaSnapshot
        .split('\n')
        .filter((l) => l.trim().startsWith('-')).length;
      return { ariaSnapshot, nodeCount };
    }
    case 'browser.currentUrl': {
      await ensureBrowser();
      return { url: page!.url(), title: await page!.title() };
    }
    case 'browser.links': {
      await ensureBrowser();
      const links = await page!.$$eval('a[href]', (els) =>
        els.map((a) => ({
          href: (a as HTMLAnchorElement).href,
          text: (a.textContent || '').trim(),
        })),
      );
      return { links };
    }
    case 'browser.click': {
      await ensureBrowser();
      const locator = (req.params?.locator ?? {}) as LocatorSpec;
      const loc = buildLocator(page!, locator).first();
      await loc.click({ timeout: 5000 });
      return { clicked: true, url: page!.url() };
    }
    case 'browser.probe': {
      await ensureBrowser();
      const locator = (req.params?.locator ?? {}) as LocatorSpec;
      return { count: await buildLocator(page!, locator).count() };
    }
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
    case 'browser.traceStop': {
      const path = req.params?.path as string | undefined;
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
      throw new Error(`unknown method: ${req.method}`);
  }
}

async function main(): Promise<void> {
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
      res.result = await handle(req);
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

main().catch((e) => {
  log('fatal', e);
  process.exit(1);
});
