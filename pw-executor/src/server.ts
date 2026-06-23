/**
 * Sentinel M0 — pw-executor: minimal Playwright execution server.
 * Transport: newline-delimited JSON-RPC 2.0 over stdio (MCP-aligned; SDK migration @ M1).
 *
 * CRITICAL: stdout carries ONLY JSON-RPC responses. All logs MUST go to stderr.
 * We BUILD this server ourselves (ADR-001, build-only constraint).
 */
import { chromium, Browser, BrowserContext, Page } from 'playwright';
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
        capabilities: ['browser.navigate', 'browser.snapshot', 'browser.traceStop'],
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
