/**
 * pw-executor — the human-readability layer (LIVE-HUMAN, ADR-120).
 *
 * WHAT WAS MEASURED before this file existed. Playwright draws NO cursor at all: the pointer it
 * moves is an internal coordinate, not a picture, so a screencast of a run shows a page in which
 * things happen with nothing performing them. Clicks are instantaneous, `slowMo` was set nowhere,
 * and no element was ever marked before it was acted on. `brain/observe.py` therefore listed `human`
 * among the modes and REFUSED it with the task named — the honest half of a feature. This file is
 * the other half; the refusal goes away because the machinery arrived, not because the sentence did.
 *
 * ONE SWITCH, `SENTINEL_DECORATE` (=1/0). It is WRITTEN only by `brain/observe.py::apply()`, out of
 * `plan.decorations`, beside the two frame switches and by the same `setdefault` rule; it is READ
 * only here, through `decorationsEnabled`. Both ends deliberately: a decoration that could be
 * switched on from either side is a mode with two authors, which is the exact defect ADR-120
 * consolidated away for the four switches that came before it.
 *
 * ⚠ THE CLEAN FRAME IS NOT AN OPTIMISATION (ADR-120, Alex 2026-08-11). A frame that goes to the
 * VISION MODEL or to a GOLDEN must carry no overlay. Not because the overlay is expensive, but
 * because a decorated reference is WRONG — and wrong in the way that only surfaces later, on
 * somebody else's replay, when the bytes fail to match and nothing on screen says why. So the
 * overlay is taken down AROUND such a capture (`withCleanFrame`) and put back, rather than the mode
 * being cancelled for the run. The person watching keeps their cursor; the machine gets the page.
 *
 * ⚠ THE PAGE-SIDE HALF IS INSTALLED WITH `addInitScript`, NOT `evaluate`. A one-shot evaluate is
 * wiped by the first navigation, and the mode would then work exactly until the run went somewhere —
 * a half-working cursor is worse than none, because the person reads its absence as "the tool
 * stopped" rather than "the injection was lost". `decorate.test.ts` navigates a second time on
 * purpose, so that shortcut cannot be taken back silently.
 */
import type { BrowserContext, Locator, Page } from 'playwright';

/** The one environment variable this layer reads. Named once so nothing can spell it differently. */
export const DECORATE_ENV = 'SENTINEL_DECORATE';

/** Whether this run draws for a person. Pure, so the launch plan can ask offline. */
export function decorationsEnabled(env: NodeJS.ProcessEnv): boolean {
  return env[DECORATE_ENV] === '1';
}

/**
 * `chromium.launch({slowMo})`, in ms — every Playwright operation is held back by this much.
 *
 * ⚠ It is a LAUNCH option, which is the whole reason the decision lives in `launch.ts`: by the time
 * a step runs, the browser exists and the value can no longer be applied. In CDP-attach the browser
 * was launched by somebody else, so it CANNOT be set at all — that case is named
 * (`slowMoUnavailable`) and paid for with a longer per-step pause instead of being pretended away.
 */
export const DECOR_SLOW_MO_MS = 100;

/** Held before each acting verb, so a person sees WHICH control is about to be used. */
export const DECOR_STEP_PAUSE_MS = 220;

/** The same pause where `slowMo` is impossible (CDP-attach): our side carries the whole pacing. */
export const DECOR_STEP_PAUSE_CDP_MS = 420;

/** How long the cursor takes to travel to its target. Long enough to be followed by an eye. */
export const DECOR_TRAVEL_MS = 320;

/** How long the target stays ringed before the action, and how long the echo lingers after it. */
export const DECOR_HIGHLIGHT_MS = 260;
export const DECOR_ECHO_MS = 280;

/** Per-keystroke delay when text is entered character by character instead of pasted. */
export const DECOR_TYPE_DELAY_MS = 45;

/** The overlay root and the cursor inside it. Exported because the gate addresses them by id. */
export const DECOR_ROOT_ID = '__sentinel_decor__';
export const DECOR_CURSOR_ID = '__sentinel_cursor__';

/** Where the cursor waits before it has ever been aimed — just outside the viewport, so its first
 *  travel reads as an entrance rather than as a jump out of the middle of the page. */
const DECOR_HOME = { x: -40, y: -40 };

export interface Point { x: number; y: number }
export interface Box { x: number; y: number; width: number; height: number }

/** Where warnings go. Decoration is an OBSERVATION concern and must never fail a step — but a
 *  decoration that quietly did nothing is the silence this arc exists to remove, so every give-up
 *  says so on stderr. */
export type DecorLog = (msg: string) => void;

export const sleep = (ms: number): Promise<void> =>
  new Promise((r) => setTimeout(r, Math.max(0, ms)));

/**
 * The page-side half, injected at document start into every page and every navigation.
 *
 * Plain ES5-ish source in a string, because that is what `addInitScript({content})` takes. The root
 * is created LAZILY, on first use: at document-start `document.body` does not exist yet, and an
 * install that depended on a ready event would race the first action on a fast page.
 *
 * The root is `position:fixed`, zero-sized and `pointer-events:none`, so it neither takes part in
 * layout nor intercepts what it is drawing attention to — an overlay that swallowed the click it
 * announces would be a defect that only shows up under decoration, i.e. the worst kind.
 */
export const DECOR_INIT_SCRIPT = `(function () {
  if (window.__sentinelDecor) return;
  var ROOT_ID = '${DECOR_ROOT_ID}';
  var CURSOR_ID = '${DECOR_CURSOR_ID}';
  var pos = { x: ${DECOR_HOME.x}, y: ${DECOR_HOME.y} };

  function root() {
    var el = document.getElementById(ROOT_ID);
    if (el) return el;
    if (!document.body) return null;
    el = document.createElement('div');
    el.id = ROOT_ID;
    el.setAttribute('aria-hidden', 'true');
    el.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;margin:0;padding:0;border:0;'
      + 'z-index:2147483647;pointer-events:none';
    var cur = document.createElement('div');
    cur.id = CURSOR_ID;
    cur.style.cssText = 'position:fixed;left:' + pos.x + 'px;top:' + pos.y + 'px;width:20px;'
      + 'height:26px;pointer-events:none;will-change:left,top';
    // The arrow's TIP is drawn at 0,0 of its own box, so the element's left/top IS the hotspot.
    // Anything else would make "the cursor is at the target" a claim with a fudge factor in it.
    cur.innerHTML = '<svg width="20" height="26" viewBox="0 0 20 26" xmlns="http://www.w3.org/2000/svg">'
      + '<path d="M0 0 L0 19 L5 14.5 L8.5 22.5 L12 21 L8.5 13.5 L15 13 Z" fill="#111"'
      + ' stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    el.appendChild(cur);
    document.body.appendChild(el);
    return el;
  }

  function cursor() { var r = root(); return r ? r.querySelector('#' + CURSOR_ID) : null; }

  function place(x, y) {
    pos.x = x; pos.y = y;
    var c = cursor();
    if (c) { c.style.left = x + 'px'; c.style.top = y + 'px'; }
  }

  // Timer-driven, NOT requestAnimationFrame: rAF is compositor-driven and a headless shell with no
  // frames to present can starve it, which would hang the promise this returns and stall the step it
  // belongs to. A step must never wait on a decoration.
  function moveTo(x, y, ms) {
    return new Promise(function (resolve) {
      if (!cursor()) { pos.x = x; pos.y = y; resolve(false); return; }
      var dur = Math.max(0, ms | 0);
      if (dur === 0) { place(x, y); resolve(true); return; }
      var sx = pos.x, sy = pos.y, t0 = Date.now();
      var timer = setInterval(function () {
        var k = Math.min(1, (Date.now() - t0) / dur);
        var e = k < 0.5 ? 2 * k * k : -1 + (4 - 2 * k) * k;   // easeInOutQuad
        place(sx + (x - sx) * e, sy + (y - sy) * e);
        if (k >= 1) { clearInterval(timer); place(x, y); resolve(true); }
      }, 16);
    });
  }

  function transient(el, ms) {
    return new Promise(function (resolve) {
      var r = root();
      if (!r) { resolve(false); return; }
      r.appendChild(el);
      setTimeout(function () {
        el.style.opacity = '0';
        setTimeout(function () {
          if (el.parentNode) el.parentNode.removeChild(el);
          resolve(true);
        }, 120);
      }, Math.max(0, ms - 120));
    });
  }

  // Both marks REMOVE themselves before their promise settles. A capture taken after the step must
  // find the page as the page is — the cursor is the only thing decoration leaves behind.
  function highlight(box, ms) {
    var d = document.createElement('div');
    d.className = 'sentinel-decor-highlight';
    d.style.cssText = 'position:fixed;left:' + (box.x - 3) + 'px;top:' + (box.y - 3) + 'px;width:'
      + (box.width + 6) + 'px;height:' + (box.height + 6) + 'px;border:3px solid rgba(255,138,0,.95);'
      + 'border-radius:5px;box-shadow:0 0 0 3px rgba(255,138,0,.25);pointer-events:none;'
      + 'transition:opacity 120ms linear';
    return transient(d, ms);
  }

  function echo(x, y, ms) {
    // The echo also PLACES the cursor, which is what repairs a click that navigated: the new
    // document re-ran this script but knows nothing of where the pointer had got to, and the echo is
    // by definition drawn where the action landed. Without it the cursor spends the whole of the
    // next page parked off-screen at its home, and the mode looks half-broken exactly on the steps
    // that matter most.
    place(x, y);
    var d = document.createElement('div');
    d.className = 'sentinel-decor-echo';
    d.style.cssText = 'position:fixed;left:' + (x - 21) + 'px;top:' + (y - 21) + 'px;width:42px;'
      + 'height:42px;border:3px solid rgba(0,150,255,.9);border-radius:50%;pointer-events:none;'
      + 'transition:opacity 120ms linear';
    return transient(d, ms);
  }

  // Taking the root DOWN rather than deleting it: the run is still decorated, this one capture is
  // not. Deleting would lose the cursor position and make the next step start from nowhere.
  //
  // ⚠ Looked up, never CREATED. Going through root() here would mean a capture taken before the
  // first action plants an overlay into a page nothing has touched yet — an element added to the
  // application under test in exchange for no picture at all, since the cursor would still be at its
  // off-screen home. Found by a mutation: with the restore removed, that phantom root was hidden for
  // the rest of the run and the cursor never appeared again.
  function setVisible(v) {
    var r = document.getElementById(ROOT_ID);
    if (r) r.style.display = v ? 'block' : 'none';
    return true;   // nothing to take down is a success, not a failure
  }

  window.__sentinelDecor = {
    moveTo: moveTo, highlight: highlight, echo: echo, setVisible: setVisible,
    at: function () { return { x: pos.x, y: pos.y }; },
  };
})();`;

/**
 * Register the page-side half on a CONTEXT, so it survives navigation and covers popups.
 *
 * Called once per context. In CDP-attach the context belongs to the user's browser — the script is
 * inert (zero-sized, pointer-events:none, one global) and comes off when the page navigates away
 * after the run, which is the least intrusive thing that still works on a browser we do not own.
 */
export async function installDecorations(context: BrowserContext): Promise<void> {
  await context.addInitScript({ content: DECOR_INIT_SCRIPT });
}

/** What the page-side half answered. `no-api` is the one that matters: it means the init script did
 *  not run on this document, which is exactly what a lost injection looks like from here. */
type DecorAck = 'ok' | 'no-api' | 'no-root';

function report(what: string, ack: DecorAck, log?: DecorLog): void {
  if (ack === 'no-api')
    log?.(`decorations: ${what} skipped — this page carries no overlay API (the init script did not run here)`);
  else if (ack === 'no-root')
    log?.(`decorations: ${what} skipped — the page has no <body> to draw into yet`);
}

/** Draw the cursor at a viewport point. Does NOT touch the real pointer — see `cursorTo`. */
async function drawCursorAt(
  page: Page, x: number, y: number, travelMs: number, log?: DecorLog,
): Promise<void> {
  try {
    const ack = (await page.evaluate(async ([px, py, ms]) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const d = (window as any).__sentinelDecor;
      if (!d) return 'no-api';
      return (await d.moveTo(px, py, ms)) ? 'ok' : 'no-root';
    }, [x, y, travelMs] as [number, number, number])) as DecorAck;
    report('cursor move', ack, log);
  } catch (e) {
    log?.(`decorations: cursor move failed, the step continues undecorated: ${(e as Error).message}`);
  }
}

/** Move the drawn cursor to a viewport point, and the REAL pointer with it.
 *
 * The real move is not cosmetic: hover states are part of what a person is watching for, and the
 * click that follows would move the pointer there anyway — doing it in the open, slowly, changes
 * nothing about what the run does and everything about whether it can be followed. */
export async function cursorTo(
  page: Page, x: number, y: number, travelMs: number, log?: DecorLog,
): Promise<void> {
  await drawCursorAt(page, x, y, travelMs, log);
  try {
    await page.mouse.move(x, y, { steps: 8 });
  } catch (e) {
    log?.(`decorations: pointer move failed: ${(e as Error).message}`);
  }
}

/** Ring the target before the action. */
export async function highlight(
  page: Page, box: Box, ms = DECOR_HIGHLIGHT_MS, log?: DecorLog,
): Promise<void> {
  try {
    const ack = (await page.evaluate(async ([b, m]) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const d = (window as any).__sentinelDecor;
      if (!d) return 'no-api';
      return (await d.highlight(b, m)) ? 'ok' : 'no-root';
    }, [box, ms] as [Box, number])) as DecorAck;
    report('highlight', ack, log);
  } catch (e) {
    log?.(`decorations: highlight failed: ${(e as Error).message}`);
  }
}

/** The echo after it — the answer to "did that land?", which a frozen frame otherwise cannot give. */
export async function echo(
  page: Page, x: number, y: number, ms = DECOR_ECHO_MS, log?: DecorLog,
): Promise<void> {
  try {
    const ack = (await page.evaluate(async ([px, py, m]) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const d = (window as any).__sentinelDecor;
      if (!d) return 'no-api';
      return (await d.echo(px, py, m)) ? 'ok' : 'no-root';
    }, [x, y, ms] as [number, number, number])) as DecorAck;
    report('echo', ack, log);
  } catch (e) {
    log?.(`decorations: echo failed: ${(e as Error).message}`);
  }
}

/** Take the overlay down / put it back. Reported, never thrown — see `withCleanFrame`. */
async function setOverlayVisible(page: Page, visible: boolean, log?: DecorLog): Promise<void> {
  try {
    const ack = (await page.evaluate((v) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const d = (window as any).__sentinelDecor;
      if (!d) return 'no-api';
      return d.setVisible(v) ? 'ok' : 'no-root';
    }, visible)) as DecorAck;
    report(visible ? 'overlay restore' : 'overlay takedown', ack, log);
  } catch (e) {
    log?.(`decorations: overlay ${visible ? 'restore' : 'takedown'} failed: ${(e as Error).message}`);
  }
}

/**
 * Run `fn` with the overlay taken down, and put it back afterwards — ADR-120's clean frame.
 *
 * `on === false` is a straight pass-through: an undecorated run must not pay two page round-trips
 * per screenshot, and more importantly must produce EXACTLY the bytes it produced before this file
 * existed. The restore is in a `finally`, because a capture that throws with the overlay hidden
 * would leave the person watching a run that lost its cursor for no stated reason.
 */
export async function withCleanFrame<T>(
  page: Page, on: boolean, fn: () => Promise<T>, log?: DecorLog,
): Promise<T> {
  if (!on) return fn();
  await setOverlayVisible(page, false, log);
  try {
    return await fn();
  } finally {
    await setOverlayVisible(page, true, log);
  }
}

/**
 * Announce what is about to happen to `loc`: pause, aim the cursor at it, ring it.
 *
 * Returns the point aimed at, so the caller can echo the same place afterwards, or null when there
 * was nothing to aim at (a locator that resolves to a box-less element — reported, never fatal).
 *
 * `scrollIntoViewIfNeeded` FIRST, and that ordering is the whole correctness of it: `click()` scrolls
 * on its own, so a box read before the scroll names coordinates the control is about to leave, and
 * the cursor would be shown pointing confidently at the wrong thing.
 */
export async function announce(
  page: Page, loc: Locator, pauseMs: number, log?: DecorLog,
): Promise<Point | null> {
  try {
    if (pauseMs > 0) await sleep(pauseMs);
    await loc.scrollIntoViewIfNeeded({ timeout: 2000 }).catch(() => {});
    const box = await loc.boundingBox({ timeout: 2000 });
    if (!box) {
      log?.('decorations: the target has no box (off-layout element) — acting without aiming');
      return null;
    }
    const centre = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
    await cursorTo(page, centre.x, centre.y, DECOR_TRAVEL_MS, log);
    await highlight(page, box, DECOR_HIGHLIGHT_MS, log);
    return centre;
  } catch (e) {
    log?.(`decorations: could not announce the target: ${(e as Error).message}`);
    return null;
  }
}

/**
 * Put the cursor back after a navigation, at the point it already occupied.
 *
 * The init script re-runs on the new document, so the API is there — but the DOM it drew into is
 * gone, and nothing would recreate it until the next action. Without this, every navigation would
 * blank the cursor for as long as the next step takes to start, which reads exactly like the mode
 * having stopped working.
 */
export async function restoreCursor(page: Page, at: Point | null, log?: DecorLog): Promise<void> {
  const p = at ?? DECOR_HOME;
  // The DRAWN cursor only. The physical pointer never moved — the page did — so pushing a mouse
  // event into a document that has just loaded would be an input the run never asked for, landing
  // wherever the previous page happened to leave it.
  await drawCursorAt(page, p.x, p.y, 0, log);
}
