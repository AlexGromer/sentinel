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
import {
  installDecorations, announce, echo, withCleanFrame, restoreCursor, sleep,
  DECOR_TYPE_DELAY_MS, type Point,
} from './decorate.js';
import { installRouteJournal, takeRoutes } from './routes.js';
import { makeVideoDir, dropVideoDir } from './record.js';
import { shouldTrackNewPage, shouldClosePagesOnTeardown } from './ownership.js';

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
      // ⚠ ТЕГ ВЫВОДИТСЯ ИЗ ЭЛЕМЕНТА, А НЕ ЗАШИТ. Все три ветки ниже раньше говорили `iframe`, а
      // индекс считался по `querySelectorAll('iframe')`. Для <frame> внутри <frameset> это давало
      // -1 → адрес null → корень МОЛЧА выпадал из обхода; а если у фрейма было имя, возвращался
      // `iframe[name="frame-top"]` — адрес выдан, адресуемое по нему не резолвится. Замерено на
      // `the-internet/nested_frames`, где оба фрейма несут name.
      const tag = e.tagName.toLowerCase() === 'frame' ? 'frame' : 'iframe';
      return {
        tag,
        name: e.getAttribute('name'),
        id: e.id || null,
        nth: doc ? Array.from(doc.querySelectorAll(tag)).indexOf(e) : -1,
      };
    });
    if (own.name) return `${own.tag}[name="${own.name}"]`;
    if (own.id) return `${own.tag}#${own.id}`;
    return own.nth >= 0 ? `${own.tag} >> nth=${own.nth}` : null;
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
// ⚠ ADR-128 narrowed what gets in here: in CDP-attach mode a tab the HUMAN opened during the run is
// not the run's, and is neither switchable nor captured from.
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

// LIVE-RECORD (ADR-125): the scratch directory Playwright drops raw videos into while the context
// lives, or null when this run does not record. See record.ts for why it is not the artifact dir.
let videoDir: string | null = null;
// ⚠ Finishing a video REQUIRES closing the context — that is the only moment Playwright guarantees
// the bytes are complete. So `browser.videoStop` is destructive in a way `browser.traceStop` is not,
// and this flag is what keeps the shutdown path from closing an already-closed context and turning a
// finished run into a crash in its last line.
// ADR-134: сколько ждать смены адреса после клика, прежде чем признать, что навигации не было.
// Потолок, а не задержка: клик, действительно сменивший маршрут, разрешает ожидание немедленно.
// Платит только клик, который навигацией не был.
const CLICK_NAV_SETTLE_MS = Number(process.env.SENTINEL_CLICK_NAV_SETTLE_MS || 250);

let contextClosed = false;

// LIVE-HUMAN (ADR-120): does this run draw for a person? Decided ONCE, by the launch plan, which is
// the only reader of SENTINEL_DECORATE (see launch.ts) — a second reader here would be a second
// author of the same mode, and two authors of one decision is what ADR-120 consolidated away.
let decorate = false;
// The pacing our own side owes. Zero in a plain run; larger under CDP-attach, where `slowMo` cannot
// be set on a browser we did not launch and this pause is the entire slowdown.
let decorPauseMs = 0;
// Where the drawn cursor is standing. Kept HERE because the page loses it on every navigation: the
// init script re-runs and re-creates the API, but nothing knows where the pointer had got to.
let decorAt: Point | null = null;

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

/** Take a page into the run: bound timeout, tracked for `browser.tabs`, captured from (ADR-067). */
function trackPage(p: Page): void {
  if (pages.includes(p)) return;
  p.setDefaultTimeout(5000);
  pages.push(p);
  attachAppCapture(p);
  log('new browser tab/page tracked: index', pages.length - 1);
}

/**
 * A page appeared in a context we ADOPTED — decide whether it is this run's (ADR-128).
 *
 * `opener()` is the only thing that can tell a popup our page raised from a tab the human opened
 * beside us: both arrive on the same `page` event, in the same context, in the same instant. It is
 * read from state Playwright already has (the target's opener id comes with the attach), not from a
 * round trip — but it is still a promise, which is why this path is async and the launch path is not.
 */
async function trackAdoptedPage(p: Page): Promise<void> {
  if (pages.includes(p)) return;
  let openerIsOurs = false;
  try {
    const o = await p.opener();
    openerIsOurs = !!o && pages.includes(o);
  } catch {
    // A page that closed before we could ask is not ours to adopt on a guess. Fail CLOSED here, and
    // that direction is deliberate: mistakenly adopting the human's tab copies their console into
    // our artifacts, while mistakenly skipping a popup costs one untracked tab and says so below.
    openerIsOurs = false;
  }
  if (!shouldTrackNewPage({ attachedOverCDP, openerIsOurs })) {
    log('a page appeared in the adopted browser that this run did not open — leaving it to its owner ' +
        '(ADR-128); it is not switchable and nothing is captured from it:', p.url());
    return;
  }
  trackPage(p);
}

async function ensureBrowser(): Promise<void> {
  if (browser) return;
  // M9.6/ADR-037: resolve launch mode (headless default / headed / CDP-attach) from env (pure, tested).
  // ADR-120 folded the decoration pacing into the same plan, because `slowMo` is a launch option and
  // has to be decided before the browser exists.
  const plan = resolveLaunchPlan(process.env);
  decorate = plan.decorate;
  decorPauseMs = plan.stepPauseMs;
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
    if (plan.videoUnavailable) {
      // ADR-125, second guard. `brain/observe.py` refuses observe=record + PW_CDP_ENDPOINT before the
      // run starts, so reaching this line means the switch arrived by some OTHER route — a hand-set
      // SENTINEL_RECORD, or a caller that is not our brain. Unlike slowMo there is nothing to
      // compensate with, so the only honest act left is to say it at the top of the log rather than
      // let the run end and leave somebody looking for a file that was never going to exist.
      log('⚠ SENTINEL_RECORD=1 but this run ATTACHED to an existing browser over CDP: `recordVideo` ' +
          'is an option of a context we would have to CREATE, and this context was adopted. NO VIDEO ' +
          'WILL BE PRODUCED by this run. Record against a browser the run launches itself.');
    }
  } else {
    // M8/GAP-RISK-009: fixed viewport + DSR=1 so screenshot bytes are stable across browser processes.
    // The anchors live in determinism.ts (single source of truth, asserted by determinism.test.ts).
    // ADR-120: `slowMo` holds back EVERY Playwright operation, which is what makes a decorated run
    // followable by an eye. It is passed only when it is non-zero so an ordinary run's launch
    // arguments are byte-for-byte what they were.
    browser = await chromium.launch({
      headless: plan.headless,
      ...(plan.slowMo > 0 ? { slowMo: plan.slowMo } : {}),
    });
    // ADR-125: the recording is decided HERE and can be decided nowhere else — `recordVideo` is an
    // option of the context at the moment of creation, and no later call can add one. The size is
    // pinned to the determinism viewport rather than left to Playwright's default so the video frames
    // what the run actually drove; a recording of a differently-sized window shows a layout the run
    // never saw.
    if (plan.video) videoDir = makeVideoDir();
    context = await browser.newContext({
      viewport: DETERMINISM_VIEWPORT,
      deviceScaleFactor: DETERMINISM_DEVICE_SCALE_FACTOR,
      // GAP-OPS-002: AUT TLS handling. Strict by DEFAULT (cert errors surface). Opt-in bypass only for
      // testing a self-signed/expired AUT cert — NEVER for prod auth runs. When strict, browser.navigate
      // re-throws cert failures as a classified, actionable diagnostic instead of an opaque error.
      ...(process.env.PW_IGNORE_HTTPS_ERRORS === '1' ? { ignoreHTTPSErrors: true } : {}),
      ...(storageState ? { storageState } : {}),
      ...(videoDir ? { recordVideo: { dir: videoDir, size: DETERMINISM_VIEWPORT } } : {}),
    });
    log(plan.headless ? 'browser launched (headless)' : 'browser launched (headed)');
    if (storageState) log('storageState loaded from', statePath);
    if (videoDir) {
      // ⚠ The window this names is real and cannot be closed from here — see record.ts. Unlike the
      // trace (ADR-084), which is buffered and discarded at the end without ever touching the disk,
      // the video is written as it happens; keeping or dropping it is a decision taken afterwards.
      log('recording video to a scratch dir; the file is written AS THE RUN GOES and kept or dropped ' +
          'afterwards — unlike the trace, there is no way to un-write it');
    }
  }

  // W8 PR-2 (ADR-135): журнал смен маршрута, тоже на КОНТЕКСТЕ — и БЕЗУСЛОВНО.
  //
  // ⚠ ОТСУТСТВИЕ `if` ЗДЕСЬ — САМО РЕШЕНИЕ, а не недостающая строка. Соседний init-скрипт стоит под
  // флагом, потому что украшение есть режим: человек его просит. Журнал маршрутов режимом не
  // является — от него зависит, что обход НАЙДЁТ. Поставленный под флагом, он дал бы продукт, у
  // которого полнота обхода тем выше, чем чаще на него смотрят.
  //
  // ⚠ И СТАВИТСЯ ДО `context.newPage()` НИЖЕ. `addInitScript` действует на будущие документы;
  // страница, созданная раньше регистрации, пошла бы без журнала, и первый же экран прогона — тот,
  // на котором роутер приложения обычно и делает свой первый редирект, — остался бы неучтённым.
  try {
    await installRouteJournal(context);
  } catch (e) {
    // Fail-OPEN и вслух, по образцу соседа: обход без журнала беднее, но работает — он всё ещё
    // видит якоря. Молчаливый отказ дал бы прогон, который просто ничего не находит.
    log('журнал маршрутов не установлен — смены маршрута SPA этот прогон не увидит:', e);
  }

  // LIVE-HUMAN (ADR-120): the page-side half of the decoration layer, registered on the CONTEXT.
  //
  // ⚠ `addInitScript`, NOT a one-shot `evaluate`, and that is the difference between a mode that
  // works and one that works until the run navigates. It also covers popups and new tabs for free,
  // which a per-page injection would have to remember to repeat on `context.on('page')`.
  if (decorate) {
    try {
      await installDecorations(context);
      if (plan.slowMoUnavailable) {
        // Said out loud rather than fudged: this browser was launched by somebody else, so `slowMo`
        // is not ours to set. The pacing is carried entirely by our per-step pause, and a person who
        // is told that can judge the run's timings; one who is not will read the difference as the
        // mode having half-failed.
        log(`decorations ON — slowMo cannot be applied to a browser we attached to over CDP; ` +
            `pacing with a ${plan.stepPauseMs}ms pause before each action instead`);
      } else {
        log(`decorations ON — cursor, highlight and per-character entry; slowMo ${plan.slowMo}ms, ` +
            `pause ${plan.stepPauseMs}ms per action. ⚠ this run's timings are not comparable to an ` +
            `undecorated one`);
      }
    } catch (e) {
      // Fail-OPEN, and loudly. Decoration is an observation concern: a run that cannot be drawn on
      // is still a run that must complete, and refusing to start would let a cosmetic layer kill
      // work that has nothing to do with it. `decorate` is cleared rather than left true so the
      // failure is reported ONCE here instead of once per verb for the rest of the run — and so the
      // capture verbs go back to being byte-for-byte what they are in an undecorated run.
      decorate = false;
      log('decorations were requested but could not be installed — this run continues UNDECORATED:',
          (e as Error).message);
    }
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
  // ADR-128 (was M9.4 A6): the run OPENS ITS OWN PAGE, always — in launch mode inside the context we
  // just created, in CDP-attach mode inside the context we adopted.
  //
  // WHAT THIS CHANGES AND WHY IT IS A DECISION, NOT A FIX. ADR-037 promised to reuse the user's
  // session AND their open tab; the tab half is withdrawn here, deliberately (Alex, 2026-08-16). The
  // session is untouched — same context, same cookies, same login — but the page is ours. Measured
  // reason: `pages()[0]` made two concurrent runs drive ONE tab and announce ONE `targetId` (both
  // `84DC6185`, measured live), so the live view could attribute the picture to neither and had to
  // refuse both (ADR-121). A label cannot separate what the browser did not separate. It also fixes
  // a leak nobody had asked about: the NEXT run inherited the previous run's tab, with its URL and
  // its cookies, and started work on somebody else's page.
  //
  // In launch mode this line is not a change at all: `browser.newContext()` returns a context with
  // no pages, so `existing.length` was always 0 there and the old expression already created one.
  // The determinism path is therefore byte-for-byte what it was — which matters, because every
  // golden ever captured was captured through it.
  page = await context.newPage();
  page.setDefaultTimeout(5000); // bound browser.expect's pollUntil inner waits to the intended 5s budget
  pages = [page];
  attachAppCapture(page); // ADR-067: the site's own console/errors/failed requests
  // The 'page' event fires only for pages created AFTER this handler is attached.
  context.on('page', (p) => {
    // Launch mode keeps the SYNCHRONOUS path it has always had: our context, so every page in it is
    // ours by construction, and `attachAppCapture` must not be deferred by even a microtask — a
    // popup can log to the console in the same tick it is created.
    if (!attachedOverCDP) { trackPage(p); return; }
    void trackAdoptedPage(p);
  });

  // LIVE-PER-RUN: tell the browser service WHICH page is this run's, so the live view can be asked
  // about a run instead of about the service. Deliberately after the page exists and deliberately
  // fail-open — see claimLivePage.
  void claimLivePage(page);
}

/**
 * Announce this run's page to the browser service (ADR-110), so /live/* can be scoped to a run.
 *
 * WHY A TARGET ID AND NOT SOMETHING SIMPLER. Page order, URL and creation time were all available
 * and all three are wrong the moment two runs overlap — which is the only case this exists for.
 * `Target.getTargetInfo` returns an id that every CDP client sees identically, INCLUDING a client
 * that adopted a page it did not create. That was measured before this was written, because the
 * whole design turns on it: in CDP-attach mode (ADR-037) the executor owns nothing, so a label it
 * could only write as an owner would leave exactly the deployment that has a browser service
 * unscoped.
 *
 * ⚠ FAIL-OPEN, AND THAT IS A DECISION. This is an OBSERVATION concern: a run whose picture cannot
 * be scoped is still a run that must complete. Making the announcement fatal would let a live-view
 * detail kill work that has nothing to do with it. It is not silent either — every failure is
 * logged with its reason, because "the live view shows the wrong run" with no explanation is the
 * defect this task exists to remove.
 *
 * Unset endpoint = nothing to announce to: a standalone executor launches its own browser, and
 * there is no service holding a live view. Correct by construction rather than by special case.
 */
async function claimLivePage(p: Page): Promise<void> {
  const runId = (process.env.RUN_ID ?? '').trim();
  if (!runId) return;
  const claimUrl = liveClaimUrl();
  if (!claimUrl) return;
  try {
    const cdp = await p.context().newCDPSession(p);
    const info = (await cdp.send('Target.getTargetInfo')) as { targetInfo?: { targetId?: string } };
    await cdp.detach().catch(() => {});
    const targetId = info?.targetInfo?.targetId;
    if (!targetId) { log('live claim skipped: Chromium reported no target id for this page'); return; }
    const res = await fetch(claimUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ run_id: runId, target_id: targetId }),
      signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) { log(`live claim refused by the browser service: HTTP ${res.status}`); return; }
    log(`live claim sent: run ${runId} -> target ${targetId.slice(0, 8)}`);
  } catch (e) {
    log('live claim failed (the run continues; the live view stays unscoped):', (e as Error).message);
  }
}

/**
 * Where to announce. Explicit `PW_LIVE_CLAIM` wins; otherwise it is DERIVED from the CDP endpoint the
 * executor was already given, because a second address in compose is a second thing to get wrong —
 * and the measured way it goes wrong here is a YAML merge key REPLACING an `environment:` block
 * rather than deepening it, which has already swallowed PW_CDP_ENDPOINT once.
 *
 * The derivation is ANNOUNCED, never inferred silently: an address reached other than the one
 * configured must not be something an operator reconstructs from a failure later.
 */
function liveClaimUrl(): string | null {
  const explicit = (process.env.PW_LIVE_CLAIM ?? '').trim();
  if (explicit) return explicit;
  const cdpEndpoint = (process.env.PW_CDP_ENDPOINT ?? '').trim();
  if (!cdpEndpoint) return null;
  try {
    const u = new URL(cdpEndpoint);
    u.port = (process.env.CDP_LIVE_PORT ?? '9224');
    u.pathname = '/live/claim';
    u.search = '';
    log(`live claim endpoint derived from PW_CDP_ENDPOINT: ${u.toString()} (set PW_LIVE_CLAIM to override)`);
    return u.toString();
  } catch {
    return null;
  }
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
      // ADR-120: the new document re-ran the init script, so the API is back — but the DOM it drew
      // into is gone. Put the cursor back where it was standing, instantly (no travel: it did not
      // move, the page did), or the person watches it vanish for as long as the next step takes to
      // begin and reads that as the mode having stopped.
      //
      // ⚠ Only once there IS a cursor. Before the first aimed action there is nothing to restore,
      // and materialising the overlay anyway would add an element to a page nobody has touched yet —
      // a change to the application under test bought with no picture, since the arrow would be
      // parked off-screen.
      if (decorate && decorAt) await restoreCursor(page!, decorAt, log);
      return { url: page!.url(), title: await page!.title(), status: resp?.status() ?? null, timing };
    }
    case 'browser.snapshot': {
      await ensureBrowser();
      // ⚠ КОРЕНЬ — ТОТ, ЧТО НА СТРАНИЦЕ ЕСТЬ, А НЕ ТОЛЬКО `body`. Прежняя строка ждала `body`
      // безусловно и роняла ВЕСЬ прогон на странице, у которой его нет по стандарту.
      //
      // Замерено 2026-08-23 на `the-internet/nested_frames`: обход прошёл 45 шагов и умер здесь с
      // `locator.ariaSnapshot: Timeout 5000ms exceeded — waiting for locator('body')`, exit 4, и
      // `plan.json` не появился вовсе. Страница отдаёт чистый `<frameset>` с двумя `<frame>`.
      //
      // Тонкость, из-за которой это не бросалось в глаза: по спецификации HTML `document.body` на
      // frameset-странице возвращает НЕ null, а сам `<frameset>` — «первый ребёнок html, который либо
      // body, либо frameset». То есть JS, читающий `document.body`, тут работает, а CSS-селектор
      // `body` не матчит: он сравнивает имя тега. Код и спецификация расходились молча.
      //
      // Таймаут задан ЯВНО и он короткий: дефолт страницы — 5 с (`page.setDefaultTimeout`), и он
      // выбран под бюджет `browser.expect`, а не под снимок. Снимок либо есть сразу, либо его нет.
      //
      // ⚠ ОТСУТСТВИЕ КОРНЯ — ДЕГРАДАЦИЯ С ПРИЧИНОЙ, А НЕ ОТКАЗ. В этом же процессе `decorate.ts`
      // давно обращается с отсутствующим `<body>` как со штатным поводом промолчать («the page has
      // no <body> to draw into yet»), а снимок считал это поводом уронить прогон: одна подсистема
      // называла нормой то, что другая называла катастрофой.
      const SNAPSHOT_ROOT = 'body, frameset';
      const root = page!.locator(SNAPSHOT_ROOT).first();
      let ariaSnapshot = '';
      let rootless: string | null = null;
      let snapshotError: string | null = null;
      try {
        ariaSnapshot = await root.ariaSnapshot({ timeout: 1500 });
      } catch (e) {
        // ⚠ ДВА РАЗНЫХ ФАКТА, И ОДИН ИЗ НИХ НЕЛЬЗЯ УТВЕРЖДАТЬ, НЕ СПРОСИВ. Голый `catch`, который
        // здесь стоял, приписывал ЛЮБОЙ отказ отсутствию корня — а с таймаутом в 1500 мс самый
        // частый отказ совсем другой: `act` кликает по ссылке и НЕ ждёт навигацию, следующий узел
        // сразу просит снимок, и на удалённой цели новый документ доходит до DOMContentLoaded
        // дольше полутора секунд. Страница с корнем объявлялась бескорневой, текст настоящей ошибки
        // выбрасывался, а вызывающий получал пустой снимок с уверенной ложной причиной.
        //
        // Вопрос «есть ли корень» задаётся отдельно и дёшево. `count()` не ждёт появления — он
        // отвечает о том, что в документе есть СЕЙЧАС, — поэтому ноль здесь означает именно то, что
        // объявляется, а не «не дождались».
        const n = await page!.locator(SNAPSHOT_ROOT).count().catch(() => -1);
        const detail = String((e as Error)?.message ?? e).split('\n')[0].slice(0, 200);
        if (n === 0) {
          rootless =
            `this document has no ${SNAPSHOT_ROOT} to snapshot — the page carries no rendered root ` +
            `(a frameset whose frames failed, a document still mid-parse, or a non-HTML body)`;
        } else {
          snapshotError =
            `the ${SNAPSHOT_ROOT} root is present but the snapshot did not complete: ${detail}`;
        }
      }
      const nodeCount = ariaSnapshot.split('\n').filter((l) => l.trim().startsWith('-')).length;
      // `rootless` присутствует, только когда снимка нет: пустая строка и «снимок пуст, потому что
      // корня нет» — разные новости, и вторая обязана быть произнесённой, а не выведенной читателем
      // из нуля узлов.
      if (rootless) return { ariaSnapshot, nodeCount, rootless };
      if (snapshotError) return { ariaSnapshot, nodeCount, snapshotError };
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
      //
      // ⚠ ФРЕЙМЫ ОБХОДЯТСЯ, КАК У `browser.interactives`. `$$eval` не пересекает границу фрейма —
      // свойство селекторного движка, а не недосмотр, — поэтому ссылки внутри фрейма не попадали во
      // фронтир ВООБЩЕ, и это молчало: инструмент возвращал `{links: []}`, что неотличимо от
      // «ссылок нет». Замерено на `the-internet/nested_frames`: верхний документ — чистый frameset,
      // весь его контент во фреймах, и обход считал страницу тупиком.
      //
      // Корни те же, что у инвентаря контролов, и берутся тем же `frameSelector`: два перечня одного
      // сайта, построенные по-разному, разъехались бы на первой же странице с фреймом.
      const roots: Array<Page | Frame> = [page!];
      for (const f of page!.frames()) {
        if (await frameSelector(page!, f)) roots.push(f);
      }
      const perRoot = await Promise.all(roots.map((scope) =>
        scope.$$eval('a[href]', (els) =>
          els.map((a) => ({ href: (a as HTMLAnchorElement).href, text: (a.textContent || '').trim() })),
        ).catch(() => [] as Array<{ href: string; text: string }>),
      ));
      // Дедуп по href: один и тот же адрес может встретиться и в верхнем документе, и во фрейме, а
      // фронтир — это множество адресов, а не список вхождений.
      const seen = new Set<string>();
      const links: Array<{ href: string; text: string }> = [];
      for (const l of perRoot.flat()) {
        if (seen.has(l.href)) continue;
        seen.add(l.href);
        links.push(l);
      }
      return { links };
    }
    case 'browser.click': {
      await ensureBrowser();
      const loc = buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).first();
      // ADR-120: aim, ring, act, echo. `aim` is null when decoration is off OR when the target has
      // no box to aim at — either way the click below is the one that always ran.
      const aim = decorate ? await announce(page!, loc, decorPauseMs, log) : null;
      if (aim) decorAt = aim;
      // ⚠ АДРЕС СНИМАЕТСЯ ДО КЛИКА, И ПОСЛЕ КЛИКА ЕГО ЖДУТ — ADR-134.
      //
      // Прежняя строка читала `page.url()` ТЕМ ЖЕ ТАКТОМ, что и клик, и возвращала мгновенный
      // снимок. Для документа это верно — навигация документа синхронна для Playwright. Для SPA
      // неверно вдвойне: Playwright обновляет адрес фрейма по протокольному событию
      // (`Page.navigatedWithinDocument`), поэтому даже СИНХРОННЫЙ `history.pushState` в обработчике
      // может не успеть отразиться, а роутер Angular меняет маршрут в промисе — и тогда возвращался
      // СТАРЫЙ адрес. Обход, для которого клик и есть способ найти маршрут (ADR-134), тихо не
      // замечал находку: маршрут открыт, а во фронтир он не попадал.
      //
      // Ждём СОСТОЯНИЯ, а не спим: `waitForURL` с предикатом разрешается немедленно, если адрес уже
      // сменился, и это единственное API в этом файле, которое штатно срабатывает на навигацию
      // ВНУТРИ документа. Потолок платит только клик, который НЕ был навигацией, — цена замерена и
      // названа в ADR-134; переменная существует затем, чтобы её можно было перезамерить, а не
      // затем, что число выбрано на глаз.
      const urlBefore = page!.url();
      await loc.click({ timeout: 5000 });
      if (aim) await echo(page!, aim.x, aim.y, undefined, log);
      // ⚠ `> 0` — НЕ ЗАЩИТНАЯ ПРИВЫЧКА, А ЗАМЕРЕННЫЙ ДЕФЕКТ. В Playwright `timeout: 0` означает «ждать
      // ВЕЧНО», а не «не ждать»: человек, поставивший `SENTINEL_CLICK_NAV_SETTLE_MS=0`, чтобы
      // выключить ожидание, получал прогон, повисший на ПЕРВОМ же клике без навигации. Замерено —
      // прогон встал на шаге 2 и не двинулся. Ноль здесь обязан значить то, что он значит для
      // человека: не ждать вовсе.
      if (CLICK_NAV_SETTLE_MS > 0 && page!.url() === urlBefore) {
        await page!
          .waitForURL((u) => u.href !== urlBefore, { timeout: CLICK_NAV_SETTLE_MS })
          .catch(() => { /* не навигация — это самый частый исход, и он не отказ */ });
      }
      const urlAfter = page!.url();
      // `navigated` — ОТДЕЛЬНЫЙ факт, а не вывод вызывающего. Сравнивая адреса сам, brain не мог
      // отличить «не двигались» от «не успели увидеть»: обе ситуации выглядели как равные строки.
      return { clicked: true, url: urlAfter, navigated: urlAfter !== urlBefore };
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
        // Decoration for a secret field is the CURSOR AND THE RING ONLY — both are computed from the
        // element's box and know nothing about its contents. The per-character entry below is
        // deliberately NOT applied here: it would turn one value into N keystroke events, and every
        // one of them is something a page listener, a screencast frame or a future trace could pick
        // up. A person still sees which field is being filled, which is all the mode promises.
        const aim = decorate ? await announce(page!, loc, decorPauseMs, log) : null;
        if (aim) decorAt = aim;
        const v = process.env[secretRef];
        if (v === undefined) throw new Error(`secret '${secretRef}' not set`);
        log('fill', params?.locator, '= <redacted>');
        try {
          await loc.fill(v, { timeout: 5000 });
        } catch {
          throw new Error('browser.fill failed (secret redacted)');
        }
        if (aim) await echo(page!, aim.x, aim.y, undefined, log);
      } else {
        const value = (params?.value as string) ?? '';
        const aim = decorate ? await announce(page!, loc, decorPauseMs, log) : null;
        if (aim) decorAt = aim;
        if (decorate) {
          // ADR-120: a value that appears all at once shows nothing about what was entered where.
          // `fill('')` first because `fill` REPLACES and `pressSequentially` APPENDS — dropping the
          // clear would quietly change the verb's meaning under decoration, which is exactly the
          // kind of mode-dependent behaviour that makes a decorated run untrustworthy.
          await loc.fill('', { timeout: 5000 });
          await loc.pressSequentially(value, { delay: DECOR_TYPE_DELAY_MS, timeout: 5000 });
        } else {
          await loc.fill(value, { timeout: 5000 });
        }
        if (aim) await echo(page!, aim.x, aim.y, undefined, log);
      }
      return { filled: true };
    }
    case 'browser.type': {
      // Keystroke-by-keystroke entry (pressSequentially; locator.type() is deprecated since PW 1.38).
      // Does NOT clear by default (append); pass clear:true to fill('') first.
      await ensureBrowser();
      const loc = buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).first();
      const aim = decorate ? await announce(page!, loc, decorPauseMs, log) : null;
      if (aim) decorAt = aim;
      if (params?.clear) await loc.fill('', { timeout: 5000 });
      // Already keystroke-by-keystroke; decoration only gives the keystrokes a pace a person can read.
      await loc.pressSequentially((params?.text as string) ?? '',
        { timeout: 5000, ...(decorate ? { delay: DECOR_TYPE_DELAY_MS } : {}) });
      if (aim) await echo(page!, aim.x, aim.y, undefined, log);
      return { typed: true };
    }
    case 'browser.press': {
      await ensureBrowser();
      const key = params?.key as string | undefined;
      if (!key) throw new Error('press: missing params.key');
      if (params?.locator) {
        const loc = buildLocator(page!, params.locator as LocatorSpec).first();
        const aim = decorate ? await announce(page!, loc, decorPauseMs, log) : null;
        if (aim) decorAt = aim;
        await loc.press(key, { timeout: 5000 });
        if (aim) await echo(page!, aim.x, aim.y, undefined, log);
      } else {
        // A page-level key has no target to aim at, so there is nothing to ring — the pause alone
        // keeps it from happening in the same instant as the step before it.
        if (decorate && decorPauseMs > 0) await sleep(decorPauseMs);
        await page!.keyboard.press(key); // page-level key needs prior focus
      }
      return { pressed: key };
    }
    case 'browser.select': {
      await ensureBrowser();
      const loc = buildLocator(page!, (params?.locator ?? {}) as LocatorSpec).first();
      const aim = decorate ? await announce(page!, loc, decorPauseMs, log) : null;
      if (aim) decorAt = aim;
      const selected = await loc.selectOption(
        params?.value as Parameters<Locator['selectOption']>[0], { timeout: 5000 });
      if (aim) await echo(page!, aim.x, aim.y, undefined, log);
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
      //
      // ADR-120: and the overlay comes OFF for the duration. This is a GOLDEN — a reference other
      // runs are compared against — so a cursor drawn into it does not degrade the reference, it
      // makes it wrong, and wrong in the way that surfaces on somebody else's replay as a hash
      // mismatch with nothing on screen to explain it. The mode is not cancelled; this one capture
      // is taken with the overlay down and it goes straight back up.
      const buf = await withCleanFrame(page!, decorate,
        () => page!.screenshot(SCREENSHOT_DETERMINISM_OPTS), log);
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
        // ADR-120: this picture is for the VISION MODEL, and it is asked to name one of OUR numbered
        // marks. A second, unexplained pointer drawn over the page is one more thing in the frame
        // that looks deliberate and is not — it can only cost us a wrong mark. So the whole overlay
        // comes down around the capture, and the person's cursor returns immediately after.
        await withCleanFrame(page!, decorate, async () => {
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
        }, log);
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
      // ⚠ NOT wrapped in withCleanFrame, and that is the decision rather than an oversight (ADR-120):
      // this frame is what the hub shows a PERSON. Stripping the cursor out of it would remove the
      // only evidence of who is acting — which is the entire reason the decorated mode exists. The
      // clean frame is owed to the vision model and to the golden, both of which are captured by
      // other verbs; a human frame is owed the opposite.
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
    case 'browser.videoStop': {
      // ADR-125. Deliberately shaped like `browser.traceStop` — optional `path`, omitting it drops
      // the recording — so a caller learns one rule for both artifacts. The MECHANISM underneath is
      // the opposite, and that difference is the whole reason this is a separate verb:
      //
      //   trace: buffered in memory, `tracing.stop()` with no path throws it away UNWRITTEN.
      //   video: written to disk AS THE RUN GOES; the only choice left is to delete it afterwards.
      //
      // ⚠ AND IT MUST CLOSE THE CONTEXT. `video.saveAs()` waits for the page to close before the file
      // is complete, so finishing the recording ends the browser session. That makes this verb
      // TERMINAL in a way traceStop is not — every call site invokes it last, after traceStop, and
      // `contextClosed` stops the shutdown path from closing it a second time.
      const vpath = params?.path as string | undefined;
      if (!videoDir || !context || contextClosed) return { path: null, kept: false };
      // Captured BEFORE the close: `page.video()` on a closed page still resolves, but reading the
      // list afterwards would race with Playwright tearing the pages down.
      const videos = pages.map((p) => p.video()).filter((v): v is NonNullable<typeof v> => !!v);
      const main = page?.video() ?? videos[0] ?? null;
      await context.close();
      contextClosed = true;
      if (videos.length > 1) {
        // Named rather than dropped in silence: a run with popups produced several recordings and
        // only the main page's is the artifact. Somebody looking for what happened in a popup should
        // learn from the log that it was recorded and discarded, not conclude it was never captured.
        log(`video: ${videos.length} recordings existed (popups/new tabs); only the main page's is ` +
            `kept as the artifact, the rest are dropped with the scratch dir`);
      }
      if (!vpath || !main) {
        dropVideoDir(videoDir);
        videoDir = null;
        log('video discarded (run finished clean; set SENTINEL_VIDEO_ALWAYS=1 to keep it). ⚠ unlike ' +
            'the trace, the bytes HAD been on disk for the duration of the run');
        return { path: null, kept: false };
      }
      await main.saveAs(vpath);
      dropVideoDir(videoDir);
      videoDir = null;
      log('video saved:', vpath);
      return { path: vpath, kept: true };
    }
    case 'browser.routes': {
      // W8 PR-2 (ADR-135): снять журнал смен маршрута, который вела сама страница, и очистить его.
      //
      // ⚠ ВТОРОЙ ИСТОЧНИК ФРОНТИРА, А НЕ ДИАГНОСТИКА. `browser.links` отвечает «куда отсюда ведут
      // ссылки», этот верб — «какие адреса эта страница у себя уже открывала». Второй вопрос
      // отвечает на то, чего первый не видит вовсе: маршрут, открытый `pushState` и покинутый
      // раньше, чем адрес успели прочитать снаружи.
      //
      // `journal: false` произносится вслух, потому что молчаливо пустой журнал неотличим от
      // «страница никуда не ходила», а означает противоположное — что инъекция не отработала.
      await ensureBrowser();
      const taken = await takeRoutes(page!);
      if (!taken.journal)
        log('browser.routes: на этом документе нет журнала маршрутов (init-скрипт здесь не ' +
            'отработал) — смены маршрута этой страницы во фронтир не попадут');
      return taken;
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
  'browser.videoStop',
  'browser.routes',
  'browser.tabs',
  'browser.switchTab',
];

// --- Transport 1: newline JSON-RPC 2.0 (default) ----------------------------
/**
 * Teardown on a signal — ADR-128.
 *
 * ⚠ THE JSON-RPC TRANSPORT HAD NO SIGNAL HANDLER AT ALL, and that was harmless only while the run
 * owned nothing: in launch mode Playwright kills the browser it started when this process dies, and
 * in CDP-attach mode there was nothing of ours to clean up. Now there is. A run killed mid-flight —
 * budget stop (ADR-021), `agentctl` cancel, `docker compose down` — reaches the browser service the
 * same way a finished one does, so the tab has to go the same way too, or "no leak" holds only for
 * runs that end politely.
 *
 * Bounded on purpose: a browser that will not answer must never be the reason a killed process
 * refuses to die. The timer is `unref`'d so it is never itself what keeps the process alive.
 */
function installSignalTeardown(): void {
  const bail = (sig: string): void => {
    log(`${sig} — tearing down`);
    const t = setTimeout(() => process.exit(0), 2_000);
    t.unref?.();
    const finish = (): void => process.exit(0);
    const mine = pages.filter((p) => !p.isClosed());
    if (shouldClosePagesOnTeardown({ attachedOverCDP, havePages: mine.length > 0, contextClosed })) {
      void Promise.all(mine.map((p) => p.close().catch(() => {}))).finally(finish);
      return;
    }
    // M9.6: never close the user's CDP-attached browser — just drop the connection by exiting.
    if (!attachedOverCDP) { void browser?.close().finally(finish); return; }
    finish();
  };
  for (const sig of ['SIGTERM', 'SIGINT'] as const) process.on(sig, () => bail(sig));
}

async function mainJsonRpc(): Promise<void> {
  await setupTracing();
  installSignalTeardown();
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
    if (context && tracingStarted && !tracingStopped && !contextClosed) await context.tracing.stop();
    // ADR-128: we opened these pages, so we close them — and ONLY them. The browser and the context
    // stay exactly as they were (ADR-037 is unchanged in that half). Leaving them would not be
    // politeness: the browser service outlives every run, so abandoned tabs grow without bound, and
    // the newest of them would keep answering unnamed live requests on behalf of a finished run.
    // AFTER the trace stop, because stopping the trace reads from the pages.
    const mine = pages.filter((p) => !p.isClosed());
    if (shouldClosePagesOnTeardown({ attachedOverCDP, havePages: mine.length > 0, contextClosed })) {
      for (const p of mine) await p.close().catch(() => {});
      log(`closed the ${mine.length} page(s) this run opened in the adopted browser (ADR-128); the ` +
          'browser, the context and every tab that was already there are untouched');
    }
    if (!attachedOverCDP) await browser?.close(); // M9.6: never close the user's CDP-attached browser
  } catch (e) {
    log('cleanup error', e);
  }
  // ADR-125: a run that ended without reaching `browser.videoStop` — crash, abort, budget kill — still
  // has raw video in a scratch dir. Dropping it here is not tidiness: those frames are a recording of
  // somebody's application, and leaving them in /tmp for the next process to find is a disclosure,
  // not a leftover. Runs AFTER the browser close above, so the files Playwright is still finalising
  // are complete before they are removed.
  try {
    if (videoDir) {
      dropVideoDir(videoDir);
      log('video scratch dir dropped on shutdown (the run never reached browser.videoStop)');
      videoDir = null;
    }
  } catch (e) {
    log('video cleanup error', e);
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
    'browser.videoStop': { path: z.string().optional() }, // ADR-125: omitted = drop the recording
    'browser.routes': {},                                 // ADR-135: журнал берётся целиком, без параметров
    'browser.tabs': {},
    'browser.switchTab': { index: z.number() },
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
  // One teardown for both transports (ADR-128). It used to be written out here and nowhere else,
  // which is why the transport that actually ships had none — two statements of one rule, with the
  // second one missing, is the same shape as the two live URLs that drifted in ADR-121.
  installSignalTeardown();
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
