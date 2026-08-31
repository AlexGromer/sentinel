// MAIN-world route journal (ADR-138). Wraps `history.pushState`/`replaceState` and listens for
// `popstate`, then posts every address change out to the ISOLATED content script, which relays it to
// the service worker as a `{type:'route'}` line.
//
// WHY IT LIVES IN THE PAGE'S WORLD AND NOT IN THE CONTENT SCRIPT. Measured in Chromium: the worlds
// are isolated symmetrically, so a `history.pushState` patched from ISOLATED never sees the page's
// own call — the page keeps the native function. That is the whole reason a content-script-side
// patch cannot do this job, and the reason a relay is needed at all: MAIN has no `chrome.runtime`
// (measured `typeof chrome.runtime === 'undefined'` there), so the journal cannot talk to the
// service worker itself.
//
// WHY NOT `chrome.webNavigation`. Three measurements, each fatal on its own: it does NOT fire for a
// fragment change (that is `onReferenceFragmentUpdated`, a second subscription), it cannot tell
// `push` from `replace` (both arrive as `transitionType: 'link'`), and requesting the permission
// needs a user gesture. And it would arrive in the SERVICE WORKER while actions arrive from the
// CONTENT SCRIPT — two channels with nothing to order them against each other, when the whole value
// of the fact is its position relative to the click that caused it.
//
// WHY NOT "read location.href a tick later". `pw-executor/src/server.ts` already measured that an
// Angular router changes the route inside a promise — that is why clicking needs `waitForURL` with a
// budget rather than a tick. A deferred `pushState` and a bare `history.back()` (no DOM event at
// all) are invisible to that approach; both are visible here.
//
// This is a module, not the string-literal form `pw-executor/src/routes.ts` uses: that file feeds
// Playwright's `addInitScript({content})`, which takes a string, while `chrome.scripting` takes a
// FILE. Being a module means the offline driver and the unit test import the very same source
// instead of a second copy, and the page never has to eval anything (CSP-safe).
import { ROUTE_MSG, type RouteHow, type RouteMessage } from '../shared/protocol.js';

/** Latch on the page object: a second injection must not double-wrap `history` or double-report. */
const INSTALLED = '__sentinelRouteJournal';

/** Minimal surface of the window this journal needs — so the unit test and the jsdom driver can hand
 * it a jsdom window without pretending to be a browser. */
export interface JournalWindow {
  history: History;
  location: { href: string };
  addEventListener(type: string, cb: (e: unknown) => void): void;
  postMessage(msg: unknown, targetOrigin: string): void;
  [key: string]: unknown;
}

/** Install the journal on `win`. Idempotent. Returns true if this call installed it, false if it was
 * already there — the caller uses that only for diagnostics, never for control flow. */
export function installRouteJournal(win: JournalWindow): boolean {
  if (win[INSTALLED]) return false;
  win[INSTALLED] = true;

  // `chrome.runtime` exists in ISOLATED and does NOT in MAIN (measured). If we are not in MAIN the
  // page's own pushState is invisible to us and this feature records nothing at all — a failure with
  // no symptom, which is exactly the shape this PR exists to remove. So we SAY so, on every message.
  const anyWin = win as unknown as { chrome?: { runtime?: { id?: string } } };
  const inMain = !anyWin.chrome?.runtime?.id;

  // Consecutive-duplicate suppression. Syncing screen state to the address on every filter/sort/scroll
  // via `replaceState` is a common idiom; without this a single interaction can post the same address
  // many times. Compared against the LAST post only, never the whole history: `A → B → A` must stay
  // three routes, because a return to A is a real transition the scenario has to represent.
  let last: string | null = null;

  function note(how: RouteHow): void {
    let href: string;
    try {
      href = String(win.location.href);
    } catch {
      return; // an address we cannot even read is not a route we can report
    }
    if (href === last) return;
    last = href;
    const msg: RouteMessage = { __sentinel: ROUTE_MSG, url: href, how, main: inMain };
    try {
      win.postMessage(msg, '*');
    } catch {
      /* the journal has no right to break the page's navigation */
    }
  }

  function wrap(name: 'pushState' | 'replaceState', how: RouteHow): void {
    const orig = win.history[name];
    if (typeof orig !== 'function') return;
    win.history[name] = function (this: History, ...args: unknown[]) {
      // The original is called FIRST and its result returned as-is: the wrapper must be transparent
      // to the application. Swallowing a SecurityError here (pushState does throw one, e.g. for a
      // cross-origin path) would hide from the router a refusal it is written to expect, and send it
      // down a different branch because of us.
      const r = (orig as (...a: unknown[]) => unknown).apply(this, args);
      // The address is read AFTER the call, never from the argument: the argument is routinely
      // relative ('/orders', '?tab=x'), and the resolved href is what the scenario has to carry.
      try {
        note(how);
      } catch {
        /* ditto */
      }
      return r;
    } as unknown as History[typeof name];
  }

  wrap('pushState', 'push');
  wrap('replaceState', 'replace');
  // `popstate` covers Back/Forward AND a bare `location.hash = …`: measured in Chromium (ADR-135) and
  // again in jsdom, both raise popstate first, so a separate `hashchange` listener never wins.
  win.addEventListener('popstate', () => {
    try {
      note('pop');
    } catch {
      /* ditto */
    }
  });

  return true;
}
