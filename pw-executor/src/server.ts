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
import { chromium, Browser, BrowserContext, Page, Locator, Frame } from 'playwright';
import * as readline from 'node:readline';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as dns from 'node:dns/promises';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { setupTracing, spanForTool, currentTraceparent } from './otel.js';
import { resolveLaunchPlan, cdpHostNeedsNumericAddress, withCdpHost, pickCdpAddress } from './launch.js';
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
/* ADR-072: the tally, per code. The lines themselves go to stderr, which the brain does NOT read —
 * `brain/executor.py` inherits the executor's stderr rather than piping it, so the only process that
 * ever parsed these lines was control-api's log sink, and by the time IT has counted them the run has
 * already exited with its verdict. Counting here, at the emitter, is what makes the number available
 * INSIDE the run: the brain asks for it at report time (`browser.appFaults`) and can therefore both
 * report it on the verdict and, if asked, fail on it. */
const appFaultCounts: Record<string, number> = {};
let appFaultsCapped = false;

function appLog(lvl: 'debug' | 'info' | 'warn' | 'error', code: string, fields: Record<string, unknown>): void {
  if (appLogCount > APP_LOG_CAP) return;
  appLogCount += 1;
  if (appLogCount > APP_LOG_CAP) {
    appFaultsCapped = true;
    console.error(`[warn|app] app.log_capped: ${render(APP_MESSAGES['app.log_capped'], { cap: APP_LOG_CAP })}`);
    return;
  }
  // Tallied BEFORE the template lookup so a code missing from APP_MESSAGES still counts — the fault
  // happened whether or not we can phrase it, and a silent drop would understate the application.
  appFaultCounts[code] = (appFaultCounts[code] ?? 0) + 1;
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

/** What the tool considers an interactive control. ADR-093: ONE definition.
 *
 * This literal used to be copy-pasted three times — `browser.interactives`, `browser.setOfMarks` and
 * `browser.perceptionAudit` each carried their own copy — and the copies are exactly how the audit
 * came to measure something other than the perception it reported on. Naming it once makes the three
 * surfaces disagree only if someone changes them on purpose.
 *
 * ⚠ `browser.links` deliberately keeps its OWN `a[href]` selector: it feeds the navigation frontier
 * (which URLs are reachable), not the control inventory. Merging the two would make every button on
 * the page look like somewhere to go. Leave it separate.
 *
 * Read through Playwright's selector engine (`page.locator` / `page.$$eval`), which PIERCES open
 * shadow roots — measured, not assumed: on `l5.html` it yields 23 controls where the raw DOM API
 * yields 15. Anything that measures this selector must go through the same engine or it is measuring
 * a different page (ADR-093).
 */
const PERCEPTION_SELECTOR = 'button, a[href], input, select, textarea, [role=button], [role=tab]';

/** The ARIA role an `<input>` carries by its `type`. ADR-094.
 *
 * MEASURED against Playwright's own role engine, not transcribed from the ARIA spec — and the two
 * disagree in three places that matter to us: `color`, `date` and `time` resolve as `textbox` here
 * although the spec assigns them no role, and `file`/`image` resolve as `button`. Playwright's engine
 * is what `getByRole` consults, so it is the only authority that predicts whether the locator we
 * build will find anything. A map copied from the spec would have been correct and useless.
 *
 * An absent entry means "no role" — a hidden input is not a control, and claiming one for it would
 * put an unclickable thing into the page model. */
const INPUT_ROLE: Record<string, string> = {
  text: 'textbox', password: 'textbox', email: 'textbox', tel: 'textbox', url: 'textbox',
  color: 'textbox', date: 'textbox', time: 'textbox', 'datetime-local': 'textbox', month: 'textbox',
  week: 'textbox',
  search: 'searchbox',
  checkbox: 'checkbox',
  radio: 'radio',
  submit: 'button', reset: 'button', button: 'button', file: 'button', image: 'button',
  number: 'spinbutton',
  range: 'slider',
  // `hidden` is deliberately absent.
};

/** The page-side source of `ariaRole`, inlined into every `$$eval` that needs it.
 *
 * It is a STRING because `$$eval` callbacks are serialised into the page and cannot close over
 * module scope. That is also why it is defined once here rather than written out at each call site —
 * two copies of a role table is the same defect ADR-093 removed from the selector, one layer down. */
const ARIA_ROLE_FN = `(e, INPUT_ROLE) => {
  // An explicit role attribute WINS over the tag. This is the ARIA rule, and getting it backwards is
  // what made every <button role="tab"> unreachable: the brain froze role "button" for a control the
  // accessibility tree calls a tab, and getByRole('button') can never match it. The attribute is a
  // token LIST — the first token that names a role we know is the effective one.
  const explicit = (e.getAttribute('role') || '').trim().toLowerCase();
  if (explicit) return explicit.split(/\\s+/)[0];
  const tag = e.tagName.toLowerCase();
  if (tag === 'button') return 'button';
  if (tag === 'a') return e.hasAttribute('href') ? 'link' : '';   // an anchor without href is not a link
  if (tag === 'textarea') return 'textbox';
  if (tag === 'select') return (e.multiple || e.size > 1) ? 'listbox' : 'combobox';
  if (tag === 'input') return INPUT_ROLE[(e.getAttribute('type') || 'text').toLowerCase()] || '';
  return '';
}`;

/** The page-side source of `accessibleName` — what the ACCESSIBILITY TREE calls this control, which
 * is what `getByRole(role, {name})` matches on. ADR-096.
 *
 * The old computation was `aria-label || textContent`, and an `<input>` has no text content. Its name
 * comes from the associated `<label>`, which was never read — so eleven form fields across this
 * repository's fixtures arrived with an empty name, failed the brain's "no anchor" guard and were
 * DROPPED from the page model without a word. On `l3.html`, the validation-form fixture, five of nine
 * controls reached the model while the audit reported a ratio of 1.00: we were not blind to them, we
 * threw them away after seeing them. Measured: every one of those is addressable —
 * `getByRole('textbox', {name: 'Username'})` resolves on `l2.html` where our name was ''.
 *
 * `<select>` was worse than empty: `textContent` on a select is the concatenation of its OPTIONS, so
 * `l5.html`'s theme picker reported the name "System default Light Dark" while the accessibility tree
 * calls it "Theme:". A wrong name is not a smaller version of a missing one — it produces a locator
 * that looks specific and matches nothing.
 *
 * Order follows the accessible-name computation for the sources that actually occur in applications.
 * It is deliberately NOT the full W3C algorithm: this is a claim, and the gate proves the claim by
 * asking the engine to resolve every name we report (the ADR-094 pattern). Measured at 102 of 102
 * visible controls resolving across the corpus. */
const ACCESSIBLE_NAME_FN = `(e) => {
  const t = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const al = e.getAttribute('aria-label');
  if (t(al)) return t(al);
  const lb = e.getAttribute('aria-labelledby');
  if (lb) {
    const s = lb.split(/\\s+/).map((id) => e.ownerDocument.getElementById(id))
                .filter(Boolean).map((n) => n.textContent).join(' ');
    if (t(s)) return t(s);
  }
  const tag = e.tagName.toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea') {
    // \`labels\` is the DOM's own answer and covers BOTH <label for=id> and a wrapping <label>.
    // Re-deriving it with querySelector would miss the wrapping form, which is what l5's checkboxes
    // use.
    //
    // The control's OWN subtree is removed first. A <select> wrapped in its label contributes its
    // options to that label's textContent, so the naive read gives "Theme: System defaultLight"
    // while the accessibility tree says "Theme:" — measured against \`ariaSnapshot\`, and the naive
    // name resolves to ZERO because getByRole's name match looks for the given string INSIDE the
    // real one, and a superset is not a substring. It changes nothing for a checkbox or a text
    // field, which contribute no text; it is the difference between working and not for a select.
    if (e.labels && e.labels.length) {
      const s = Array.from(e.labels).map((l) => {
        const c = l.cloneNode(true);
        c.querySelectorAll('input, select, textarea, button').forEach((n) => n.remove());
        return c.textContent;
      }).join(' ');
      if (t(s)) return t(s);
    }
    const ph = e.getAttribute('placeholder');
    if (t(ph)) return t(ph);
    if (tag === 'input' && ['submit', 'reset', 'button'].includes((e.getAttribute('type') || '').toLowerCase())) {
      const v = e.getAttribute('value');
      if (t(v)) return t(v);
    }
  }
  // Text content names a button or a link. It does NOT name a <select>: those children are options,
  // not a label, and treating them as one is how "System default Light Dark" happened.
  if (tag !== 'select') {
    const s = t(e.textContent);
    if (s) return s;
  }
  const ti = e.getAttribute('title');
  if (t(ti)) return t(ti);
  return '';
}`;

/** Anything a person could plausibly click that PERCEPTION_SELECTOR does NOT name. Deliberately
 * wider than what we perceive: the point is to measure what we MISS, and a generous denominator that
 * occasionally over-counts is more honest than a narrow one that flatters us. */
const CLICKABLE_SELECTOR =
  PERCEPTION_SELECTOR +
  ', [onclick], [tabindex]:not([tabindex="-1"]), [contenteditable=""], [contenteditable="true"]' +
  ', [role=link], [role=checkbox], [role=radio], [role=switch], [role=menuitem], [role=option]';

/** A locator is a dict with EXACTLY ONE of these shapes (M2 locator model), plus an OPTIONAL
 * `frame` (ADR-095).
 *
 * `frame` is not a seventh strategy — it is WHERE to look, orthogonal to HOW. That distinction is
 * the whole design: a frame-scoped role+name locator is still a `role_name` locator, so
 * `strategies.py`, the `PRIORS` table, `pick_confidence` and the locator-key vocabulary gate all
 * stay untouched. Adding a strategy would have meant a prior nobody measured, sitting next to six
 * others that are already admitted to be unmeasured (GAP-RISK-002). */
interface LocatorSpec {
  testid?: string;
  role?: string;
  name?: string;
  label?: string;
  text?: string;
  css?: string;
  xpath?: string;
  frame?: string;
}

/** What `buildLocator` can search in. `Page` and `FrameLocator` are unrelated types in Playwright's
 * .d.ts, but they carry the same five entry points — measured, not assumed: all six locator tiers
 * (`getByTestId`/`getByRole`/`getByLabel`/`getByText`/`locator(css)`/`locator('xpath=')`) resolve
 * and click through a `FrameLocator`, cross-origin included. That structural sameness is what makes
 * `frame` an axis rather than a rewrite. */
type LocatorRoot = Pick<Page, 'getByTestId' | 'getByRole' | 'getByLabel' | 'getByText' | 'locator'>;

/** Shared locator builder used by BOTH browser.click and browser.probe. */
function buildLocator(page: Page, locator: LocatorSpec): Locator {
  // ADR-095: resolve WHERE first, then HOW. Everything below is byte-identical to what it was —
  // only the root it runs against changes, which is why a plan without frames produces exactly the
  // locators it always did.
  const root: LocatorRoot = locator.frame !== undefined ? page.frameLocator(locator.frame) : page;
  if (locator.testid !== undefined) return root.getByTestId(locator.testid);
  if (locator.role !== undefined)
    return root.getByRole(locator.role as Parameters<Page['getByRole']>[0], { name: locator.name });
  if (locator.label !== undefined) return root.getByLabel(locator.label);
  if (locator.text !== undefined) return root.getByText(locator.text);
  if (locator.css !== undefined) return root.locator(locator.css);
  if (locator.xpath !== undefined) return root.locator('xpath=' + locator.xpath);
  throw new Error(
    'buildLocator: locator must provide one of {testid}, {role,name}, {label}, {text}, {css}, {xpath}' +
      ' — `frame` scopes those, it does not replace them',
  );
}

/** A selector for the `<iframe>` element that owns `f`, or null if it cannot be addressed (ADR-095).
 *
 * Preference order is stability, not convenience: a `name` is chosen by the author and survives
 * re-layout; an `id` usually does; an index survives neither, and is the honest last resort rather
 * than a silent one — a plan carrying `iframe >> nth=2` says out loud that it is positional.
 *
 * Depth is capped at 1 DELIBERATELY. `frameLocator` chains for deeper nesting, but a chain has to be
 * carried in the step, compared during identity checks and emitted by the exporter, and nested
 * iframes are rare enough that paying that everywhere buys very little. A deeper frame is counted in
 * `opaque.frames_nested` — a stated boundary, not silence. */
async function frameSelector(page: Page, f: Frame): Promise<string | null> {
  if (f === page.mainFrame() || f.parentFrame() !== page.mainFrame()) return null;
  try {
    const el = await f.frameElement();
    const own = await el.evaluate((node) => {
      const e = node as HTMLIFrameElement;
      const doc = e.ownerDocument;
      return {
        name: e.getAttribute('name'),
        id: e.id || null,
        nth: doc ? Array.from(doc.querySelectorAll('iframe')).indexOf(e) : -1,
      };
    });
    if (own.name) return `iframe[name="${own.name}"]`;
    if (own.id) return `iframe#${own.id}`;
    return own.nth >= 0 ? `iframe >> nth=${own.nth}` : null;
  } catch {
    // Detached or mid-navigation: it had a frame a moment ago and does not now. Reporting null lets
    // the caller count it as unreachable instead of inventing an address for it.
    return null;
  }
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

// ADR-108d: the live VIDEO mode — a CDP screencast, kept in memory and never on disk.
//
// A screencast delivers tens of frames a second. The per-step frames each become a file, which is
// right for them (one per step, kept with the run) and wrong for this: a two-minute run would leave
// thousands of files nobody asked for. Video of the live view is worth watching WHILE it happens, so
// only the most recent frame is kept and the rest are dropped as they arrive — bounded by
// construction rather than by a cleanup that has to be remembered.
let screencastSession: { detach: () => Promise<void> } | null = null;
let screencastLast: { data: string; ts: number } | null = null;
let tracingStarted = false;
let tracingStopped = false;
// M9.6/ADR-037: true when we attached to the user's browser over CDP — teardown must NOT close it.
let attachedOverCDP = false;

/**
 * ADR-110: turn a configured CDP endpoint into one Chrome will actually answer.
 *
 * Only DNS names are touched (see cdpHostNeedsNumericAddress). A lookup failure THROWS with the
 * name that could not be resolved: falling back to the unresolved endpoint would produce Chrome's
 * "Host header is specified and is not an IP address or localhost" — a message that describes a
 * header rather than the missing DNS record, and sends the reader to the wrong place entirely.
 */
async function resolveCdpEndpoint(endpoint: string): Promise<string> {
  if (!cdpHostNeedsNumericAddress(endpoint)) return endpoint;
  const host = new URL(endpoint).hostname;
  let addr: string;
  try {
    // IPv4 is PREFERRED, not merely accepted: cdp-service.ts binds its relay to 0.0.0.0, which is
    // the IPv4 wildcard, so an AAAA answer names an address nothing is listening on. A plain
    // dns.lookup() returns whatever the resolver puts first — on a GitHub runner `localhost` comes
    // back as ::1, and the connection was refused against a browser that was up and healthy. Fall
    // back to the first answer of any family so an IPv6-only deployment (CDP_LISTEN_ADDR=::) still
    // resolves; only the DEFAULT is opinionated.
    const answers = await dns.lookup(host, { all: true });
    const picked = pickCdpAddress(answers);
    if (!picked) throw new Error('resolved to no addresses');
    addr = picked;
  } catch (e) {
    throw new Error(
      `PW_CDP_ENDPOINT names host '${host}', which does not resolve: ${(e as Error).message}. ` +
      `Chrome's DevTools endpoint rejects a DNS-name Host header, so the name must resolve here ` +
      `to be rewritten to an address before connecting.`,
    );
  }
  const rewritten = withCdpHost(endpoint, addr);
  log(`CDP endpoint ${endpoint} -> ${rewritten} (Chrome rejects a DNS-name Host header)`);
  return rewritten;
}

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
    // ADR-110: Chrome's DevTools endpoint refuses a Host header that is a DNS name, so a
    // cross-container endpoint (`http://browser:9223`) has to be addressed numerically. The
    // substitution is announced — a connection made to an address other than the configured
    // one must never be something the operator has to infer from a failure later.
    const endpoint = await resolveCdpEndpoint(plan.cdpEndpoint!);
    browser = await chromium.connectOverCDP(endpoint);
    context = browser.contexts()[0] ?? (await browser.newContext());
    log('attached over CDP:', endpoint);
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
    // ADR-098: screenshots are a SEPARATE lever from redaction, because pixels are not redactable.
    // The text of a trace is cleaned (internal/redact); a screenshot of a filled login form is not —
    // that would mean OCR plus masking, which is unreliable and expensive, and a redactor that
    // half-works on images is worse than one that says it does not try. So whoever needs the frames
    // confidential turns them off; whoever needs the post-mortem keeps them. Default ON: the trace
    // exists to explain a failed run, and the failure is usually visible rather than textual.
    const wantShots = (process.env.SENTINEL_TRACE_SCREENSHOTS ?? '1') !== '0';
    await context.tracing.start({ screenshots: wantShots, snapshots: true });
    if (!wantShots) log('tracing: screenshots DISABLED (SENTINEL_TRACE_SCREENSHOTS=0)');
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
      // HEALTH-004: the application's OWN timing, read from the page it just loaded. The product had
      // no code about speed at all, and the tempting substitute — how long our step took — is a
      // surrogate: it includes locator resolution, healing and RPC, so a slow TOOL would be reported
      // as a slow application. PerformanceNavigationTiming is the browser's measurement of the
      // document exchange itself, which is the thing being claimed.
      //
      // `loadEventEnd` is deliberately absent: we wait for `domcontentloaded`, so the load event may
      // not have fired and would read as 0 — a fast page and an unfinished one look identical. Only
      // numbers that are settled at this point are reported.
      let timing: { response_ms: number; dom_ms: number } | null = null;
      try {
        timing = await page!.evaluate(() => {
          const n = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined;
          if (!n) return null;
          return {
            response_ms: Math.round(n.responseEnd),
            dom_ms: Math.round(n.domContentLoadedEventEnd || n.responseEnd),
          };
        });
      } catch {
        // A page that navigated away mid-read, or a context without the API. The navigation itself
        // succeeded, so this must not turn a working step into a failure — the caller sees null and
        // says nothing about speed rather than guessing.
        timing = null;
      }
      return { url: page!.url(), title: await page!.title(), status: resp?.status() ?? null, timing };
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
      // Its own selector ON PURPOSE (ADR-093): this feeds the navigation frontier — which URLs are
      // reachable from here — not the control inventory. Folding it into PERCEPTION_SELECTOR would
      // make every button on the page look like somewhere to go.
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
      // ADR-084: this file IS the session — cookies and localStorage of an authenticated user, in
      // cleartext. Playwright writes it with the process umask (0644 typically), so every local
      // account could read it. The run directory is chmod 0700 because it MIGHT hold PII; a file that
      // certainly holds live credentials had no protection at all, and `STORAGE_STATE_SAVE` is an
      // arbitrary path that usually lands next to the project rather than inside runs/.
      // Best-effort: a filesystem without POSIX modes (a Windows share) must not fail the run — the
      // state was still saved, and refusing to continue would trade a real capability for a mode bit
      // that platform never had.
      try {
        fs.chmodSync(path, 0o600);
      } catch (e) {
        log('saveStorageState: could not restrict permissions on', path, e);
      }
      return { path };
    }
    case 'browser.probe':
      await ensureBrowser();
      return { count: await buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).count() };
    case 'browser.interactives': {
      await ensureBrowser();
      // ADR-095: the same harvest, run once per addressable root. `$$eval` on the page never crosses
      // a frame boundary — that is a property of the selector engine, not an oversight — so a
      // control inside an iframe was invisible to planning, and `browser.perceptionAudit` has been
      // counting exactly those under `unseen.iframe` since ADR-093. Each element carries the frame
      // it was found in, and `frame` is a SCOPE on the locator rather than a new strategy, so
      // nothing about how a locator is chosen or scored changes.
      const roots: Array<{ frame?: string; scope: Page | Frame }> = [{ scope: page! }];
      for (const f of page!.frames()) {
        const sel = await frameSelector(page!, f);
        if (sel) roots.push({ frame: sel, scope: f });
      }
      const elements = (await Promise.all(roots.map(async ({ frame, scope }) => {
        const found = await scope.$$eval(
        PERCEPTION_SELECTOR,
        (els, { roleSrc, nameSrc, inputRole }) => {
          // eslint-disable-next-line no-new-func
          const ariaRole = new Function('return ' + roleSrc)() as (e: Element, m: Record<string, string>) => string;
          // eslint-disable-next-line no-new-func
          const accName = new Function('return ' + nameSrc)() as (e: Element) => string;
          return els.map((e) => ({
            // ADR-094: the ARIA ROLE, which is what `getByRole` consults — no longer
            // `role attribute || tagName`. That field was named `role` and was not one: for a plain
            // `<a>` it read "a", and `a` is not an ARIA role at all, so every locator built from it
            // resolved to nothing. Measured across this repo's fixtures: 42 of 48 broken locators
            // came from that one conflation, and they were ALL on the self-healing path, which is
            // the product's central promise.
            role: ariaRole(e, inputRole),
            // ADR-096: the ACCESSIBLE name — what `getByRole(role, {name})` matches. It was
            // `aria-label || textContent`, which is empty for every `<input>` and is the OPTION LIST
            // for a `<select>`. Eleven labelled form fields per corpus arrived nameless and were
            // dropped by the brain's no-anchor guard without a word.
            name: accName(e).slice(0, 200),
            testid: e.getAttribute('data-testid'),
            // Raw text stays separate and raw: it feeds the `text_role` strategy, which matches page
            // text rather than the accessibility tree. Conflating the two would make the fallback
            // strategy a duplicate of the primary one.
            text: (e.textContent || '').trim().slice(0, 200),
            // The tag stays, separately. It is not a role and never was, but it is real information
            // and the brain uses it to tell a link from a button when grouping.
            tag: e.tagName.toLowerCase(),
            // ADR-093: whether the control is RENDERED at all. Same contract as `disabled` below —
            // reported, never filtered here. It closes a disagreement between two perception
            // surfaces that neither of them knew about: `browser.setOfMarks` already drops
            // zero-box elements, so on `l5.html` the text tier saw 23 controls and the visual tier
            // 16, and nothing anywhere said which was right. Both are: they answer different
            // questions. Now they say so in the same vocabulary.
            // Deliberately Playwright's OWN definition of visible — a non-empty box, and not
            // `visibility:hidden` — because that is the definition that predicts whether `click()`
            // will work: it is what the actionability check waits for. Two things it therefore does
            // NOT call hidden, on purpose: `opacity:0` (Playwright will click it, so calling it
            // invisible would make us disagree with our own executor) and a control merely scrolled
            // out of view (Playwright scrolls to it).
            //
            // There is no `display === 'none'` test here and that is not an omission. An element
            // inside a `display:none` ancestor computes its OWN display as `block` — getComputedStyle
            // does not consult ancestors — so such a test would never fire for the case it appears to
            // handle. The box is what collapses, and the box is what is checked. (An earlier draft
            // carried the clause; a mutation that deleted it survived, which is how the dead branch
            // was found rather than reasoned about.)
            visible: (() => {
              const r = e.getBoundingClientRect();
              if (r.width <= 0 || r.height <= 0) return false;
              return getComputedStyle(e).visibility !== 'hidden';
            })(),
            // M9-LIVE: whether the control can be actuated AT THIS MOMENT. Reported, not filtered —
            // perception describes the page, the brain decides what to do about it. Both spellings
            // count: `disabled` is only valid on form controls, so a `<div role=button>` can only say
            // so through `aria-disabled`, and a tester's app uses whichever its framework emits.
            disabled:
              (e as HTMLButtonElement).disabled === true ||
              e.getAttribute('aria-disabled') === 'true',
          }));
        },
        { roleSrc: ARIA_ROLE_FN, nameSrc: ACCESSIBLE_NAME_FN, inputRole: INPUT_ROLE },
        );
        // Present ONLY when the control lives in a frame. An absent key rather than `null` keeps a
        // frameless page's descriptor byte-identical to what it was, which is what leaves the 106
        // stored `plan_hash`es alone — `canonical_plan_hash` hashes every field of every step.
        return frame === undefined ? found : found.map((e) => ({ ...e, frame }));
      }))).flat();
      return { elements };
    }
    case 'browser.perceptionAudit': {
      // How much of this page can we SEE? Reports a BREAKDOWN rather than one number: "we see 71%"
      // is unactionable, "1 control is in an iframe and 3 are outside our selector" tells a person
      // what to do about it.
      //
      // ADR-093 — THE NUMERATOR IS MEASURED THROUGH THE SAME ENGINE AS THE PERCEPTION IT DESCRIBES.
      // ADR-092 measured it with `document.querySelectorAll` inside `page.evaluate` while the
      // perception it claimed to describe uses `page.$$eval`, i.e. Playwright's selector engine,
      // which PIERCES open shadow roots. So the audit compared perception against a re-implementation
      // of perception, and the two disagreed exactly where it mattered: on `l5.html` it reported
      // `seen 15 / 23, shadow_dom: 8` while `browser.interactives` returned all 23 and
      // `browser.click{role,name}` actuated the very controls the audit called invisible. That false
      // `ratio < 1.0` raised a degradation, so the lie reached shipped artefacts.
      //
      // The rule this encodes: a measurement of a capability must invoke the capability, never
      // re-derive it. Everything below goes through `locator()` for that reason alone.
      await ensureBrowser();

      // Both counts, one engine — the same one `browser.interactives` uses, so `seen` is by
      // construction the size of the list that RPC returns, not an estimate of it. ADR-095 kept that
      // true when perception grew: `seen` now sums over the SAME roots the harvest walks, because a
      // numerator that stayed top-frame-only would under-report the moment we started reading frames
      // — the ADR-093 defect exactly, running the other way.
      let seen = await page!.locator(PERCEPTION_SELECTOR).count();
      const topClickable = await page!.locator(CLICKABLE_SELECTOR).count();
      const addressable = new Map<Frame, string>();
      let framesNested = 0;
      for (const f of page!.frames()) {
        if (f === page!.mainFrame()) continue;
        const sel = await frameSelector(page!, f);
        if (sel) addressable.set(f, sel);
        else framesNested++;   // deeper than one level, or its owner element vanished
      }
      for (const f of addressable.keys()) {
        try {
          seen += await f.locator(PERCEPTION_SELECTOR).count();
        } catch {
          /* detached mid-measure; it is counted as unreachable in the frames list below */
        }
      }

      // The shadow walk survives ONLY to describe boundaries, never to count controls: the engine
      // already crossed the open ones. A closed root cannot be entered at all, so counting the hosts
      // is the honest way to report a bound we cannot cross.
      const roots = await page!.evaluate(() => {
        let open = 0;
        let closed = 0;
        const walk = (root: ParentNode) => {
          root.querySelectorAll('*').forEach((el) => {
            const sr = (el as HTMLElement).shadowRoot;
            if (sr) {
              open++;
              walk(sr);
            } else if (el.tagName.includes('-')) {
              // a custom element with no reachable root: either closed, or not upgraded yet
              closed++;
            }
          });
        };
        walk(document);
        return { open, closed };
      });
      const topCanvas = await page!.locator('canvas').count();

      // Frames ARE a separate world for perception: `$$eval`/`locator` on the page never cross into
      // one, which is precisely why a control inside an iframe is invisible to planning today.
      // Measurement can cross, and does — Playwright injects into out-of-process frames too, so a
      // cross-origin child is counted rather than written off. (ADR-092 asserted the opposite here:
      // "cross-origin: the browser refuses, and that refusal IS the finding". Measured against two
      // local origins, nothing refuses — `frame.evaluate`, `frame.locator` and a click all succeed.
      // A frame that DOES throw is one that detached or is mid-navigation, which is a different
      // finding and is now named as one.)
      const frames: Array<Record<string, unknown>> = [];
      for (const f of page!.frames()) {
        if (f === page!.mainFrame()) continue;
        try {
          frames.push({
            url: f.url(),
            reachable: true,
            // ADR-095: whether PERCEPTION can enter, not merely whether measurement can. They were
            // the same thing until this change and are not any more, and reporting only the second
            // would tell an operator a frame is fine when nothing can plan against it.
            perceived: addressable.has(f),
            selector: addressable.get(f) ?? null,
            clickable: await f.locator(CLICKABLE_SELECTOR).count(),
            canvas: await f.locator('canvas').count(),
          });
        } catch (e) {
          frames.push({ url: f.url(), reachable: false, perceived: false, selector: null,
                        error: e instanceof Error ? e.message : String(e) });
        }
      }

      // Only what perception still cannot enter counts as unseen. A frame we now read contributes to
      // BOTH sides of the fraction, exactly as the top frame does.
      const inFrames = frames.reduce((n, f) => n + (Number(f.clickable) || 0), 0);
      const unseenInFrames = frames.reduce(
        (n, f) => n + (f.perceived ? 0 : Number(f.clickable) || 0), 0);
      const total = topClickable + inFrames;
      return {
        seen,
        total,
        // Guarded: a page with nothing clickable would otherwise report 0/0 as a failure of
        // perception rather than as an empty page.
        ratio: total > 0 ? Math.round((seen / total) * 1000) / 1000 : null,
        unseen: {
          // RENAMED, not re-meaninged (ADR-093). The old keys were `light_no_role` and `shadow_dom`,
          // and `shadow_dom` was the false one. A key whose meaning quietly changes lets every
          // consumer keep reading it and keep being wrong; a key that disappears forces each one to
          // be looked at. Where a missed control lives — light DOM or an open shadow root — is not
          // the operator's question anyway: our selector fails to name it either way, and widening
          // the selector would reach it either way.
          outside_selector: Math.max(0, total - seen - unseenInFrames),
          iframe: unseenInFrames,
        },
        opaque: {
          canvas: topCanvas + frames.reduce((n, f) => n + (Number(f.canvas) || 0), 0),
          shadow_roots_closed: roots.closed,
          frames_unreachable: frames.filter((f) => !f.reachable).length,
          // ADR-095: a frame nested deeper than one level. `frameLocator` chains and could reach it,
          // but the chain would have to be carried in the step, compared during identity checks and
          // emitted by the exporter — paid everywhere, for a shape that is rare. Named rather than
          // silently folded into `iframe`, because "we chose not to" and "we could not" are
          // different sentences to an operator.
          frames_nested: framesNested,
        },
        // Context, not a boundary: open roots are crossed. Reported because "we walked N shadow
        // roots and missed nothing in them" is the evidence for the claim, and ADR-092 computed this
        // number and then never returned it.
        shadow_roots_open: roots.open,
        frames,
      };
    }
    case 'browser.appFaults': {
      // Deliberately does NOT require a browser: the brain calls this at report time, and by then the
      // page may already be closed. Returning the tally of a run that never launched (all zeros) is a
      // correct answer, not an error.
      const total = Object.values(appFaultCounts).reduce((a, b) => a + b, 0);
      return { counts: { ...appFaultCounts }, total, capped: appFaultsCapped, cap: APP_LOG_CAP };
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
      //
      // ⚠ TOP FRAME ONLY, and stated rather than silently true (ADR-095). `browser.interactives` now
      // reads depth-1 frames; this does not, because a mark is a BOX and a box inside a frame is in
      // the frame's coordinate system — drawing it on the page's screenshot would put the number
      // somewhere the control is not. Offsetting by the frame's own box is possible and is not free
      // (inner scrolling), so the visual tier keeps the smaller reach until something needs it. The
      // consequence is real and belongs in the open: a heal that falls through to vision cannot
      // re-ground a control that lives in an iframe.
      await ensureBrowser();
      const outPath = params?.path as string | undefined;
      const marks = await page!.$$eval(
        PERCEPTION_SELECTOR,
        (els, { roleSrc, nameSrc, inputRole }) => {
          // eslint-disable-next-line no-new-func
          const ariaRole = new Function('return ' + roleSrc)() as (e: Element, m: Record<string, string>) => string;
          // eslint-disable-next-line no-new-func
          const accName = new Function('return ' + nameSrc)() as (e: Element) => string;
          return els
            .map((e, i) => {
              const r = e.getBoundingClientRect();
              return {
                mark: i,
                // ADR-094: the same true ARIA role `browser.interactives` reports. `healing.py`
                // maps marks and interactives through ONE function (`descriptor_to_locator`), so a
                // mark carrying a tag name where a role belongs breaks the visual tier in exactly
                // the way it broke the text tier.
                role: ariaRole(e, inputRole),
                // ADR-096: the same accessible name `browser.interactives` reports. Both feed
                // `descriptor_to_locator`, so two name computations would put the visual tier and
                // the text tier on different pages — the asymmetry ADR-093 removed for visibility
                // and ADR-094 for roles, one field further along.
                name: accName(e).slice(0, 120),
                testid: e.getAttribute('data-testid'),
                bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
                // ADR-093: the SAME definition `browser.interactives` reports as `visible`, so the
                // two perception surfaces cannot disagree about what is on screen. This filter used
                // to be `bbox > 0` alone, which let a `visibility:hidden` control through: its box
                // is full-size, so the vision tier was handed a numbered mark over a patch of
                // nothing and asked which element it was. Offering a model an element that is not in
                // the picture is worse than offering it nothing.
                onScreen: r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden',
              };
            })
            .filter((m) => m.onScreen);
        },
        { roleSrc: ARIA_ROLE_FN, nameSrc: ACCESSIBLE_NAME_FN, inputRole: INPUT_ROLE },
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
    case 'browser.screencastStart': {
      // Idempotent: asking twice keeps one session rather than stacking two on the same page, which
      // would double the frame rate and the ack traffic for no gain.
      if (screencastSession) return { started: true, already: true };
      await ensureBrowser();
      const cdp = await context!.newCDPSession(page!);
      cdp.on('Page.screencastFrame', async (f: any) => {
        screencastLast = { data: f.data, ts: Date.now() };
        // The ack is what keeps frames coming. Without it Chromium stops after the first one — so a
        // failure here is not cosmetic, it is the whole feature stopping silently.
        try { await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }); } catch { /* page gone */ }
      });
      await cdp.send('Page.startScreencast', {
        format: 'jpeg',
        quality: Number(params?.quality ?? 55),
        // Bounded on purpose: this is a view of the run, not a recording of it. A full-resolution
        // stream would cost bandwidth and memory to show something nobody can read at that size.
        maxWidth: Number(params?.maxWidth ?? 960),
        maxHeight: Number(params?.maxHeight ?? 720),
        everyNthFrame: Number(params?.everyNthFrame ?? 2),
      });
      screencastSession = { detach: async () => { try { await cdp.send('Page.stopScreencast'); } catch { /* gone */ } await cdp.detach(); } };
      return { started: true };
    }
    case 'browser.screencastStop': {
      if (!screencastSession) return { stopped: false };
      const sess = screencastSession;
      screencastSession = null;
      screencastLast = null;   // the buffer belongs to the session, not to the page
      await sess.detach();
      return { stopped: true };
    }
    case 'browser.screencastFrame': {
      // The LAST frame, as base64 — this one does travel as bytes, because it is answering a request
      // for it rather than riding the run's event stream. Null when nothing has arrived yet, which the
      // caller must be able to tell apart from an error.
      if (!screencastSession) return { frame: null, reason: 'no screencast session' };
      return screencastLast ? { frame: screencastLast.data, ts: screencastLast.ts } : { frame: null };
    }
    case 'browser.frame': {
      // ADR-108d: one FRAME of the live view, written to a file rather than returned as bytes.
      //
      // The AG-UI envelope is a stdout LINE (`@@AGUI {...}`). A base64 PNG in it would bloat the run
      // log past readability and break the very stream the UI reads to follow the run — so the frame
      // goes to the run's artifact directory and the event carries a NAME. The hub fetches it through
      // the artifact route that already exists, whitelist and all.
      //
      // Subject to the same lever as the trace's screenshots (ADR-098): pixels are not redactable, so
      // SENTINEL_TRACE_SCREENSHOTS=0 stops frames being taken at all rather than trying to clean them.
      if (process.env.SENTINEL_TRACE_SCREENSHOTS === '0') return { path: null, skipped: 'screenshots disabled' };
      const path = params?.path as string;
      if (!path) throw new Error('browser.frame needs a path');
      await ensureBrowser();
      await page!.screenshot({ path, fullPage: !!params?.fullPage });
      return { path };
    }
    case 'browser.traceStop': {
      // ADR-084: `path` is now OPTIONAL, and omitting it DISCARDS the trace instead of writing it.
      // Playwright's `tracing.stop()` without a path throws the buffered trace away, which is
      // strictly better than writing the file and deleting it: the bytes never reach the disk, so
      // there is no window in which a green run's live DOM sits in the filesystem.
      const path = params?.path as string | undefined;
      if (context && tracingStarted && !tracingStopped) {
        await context.tracing.stop(path ? { path } : undefined);
        tracingStopped = true;
        if (!path) log('trace discarded (run finished clean; set SENTINEL_TRACE_ALWAYS=1 to keep it)');
      }
      return { path: path ?? null }; // no-op when tracing was never started (PW_NO_TRACE=1 auth run)
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
  'browser.perceptionAudit',
  'browser.appFaults',
  'browser.screenshotHash',
  'browser.setOfMarks',
  'browser.frame',
  'browser.screencastStart',
  'browser.screencastStop',
  'browser.screencastFrame',
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
    'browser.perceptionAudit': {},
    'browser.appFaults': {},
    'browser.screenshotHash': {},
    'browser.setOfMarks': { path: z.string() },
    'browser.frame': { path: z.string(), fullPage: z.boolean().optional() },
    'browser.screencastStart': { quality: z.number().optional(), maxWidth: z.number().optional(),
                                 maxHeight: z.number().optional(), everyNthFrame: z.number().optional() },
    'browser.screencastStop': {},
    'browser.screencastFrame': {},
    'browser.traceStop': { path: z.string().optional() }, // ADR-084: omitted = discard the trace
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
