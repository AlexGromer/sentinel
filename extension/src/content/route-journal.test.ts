// Unit tests for the MAIN-world route journal (ADR-138). Runs under jsdom via
// `node --import tsx --test`, alongside the recorder's own unit tests.
//
// These cover the journal's PURE obligations — the ones that hold regardless of which world it ends
// up in. The world question itself (a patch applied from ISOLATED never sees the page's own
// `history.pushState`) is unobservable in jsdom, which has one world; it is guarded at runtime by the
// `main` flag on every message and exercised end to end by tests/test_recorder_routes_offline.py.
import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { installRouteJournal, type JournalWindow } from './route-journal.js';
import { ROUTE_MSG, type RouteMessage } from '../shared/protocol.js';

/** A jsdom window plus a capture of everything the journal posts out of it. */
function page(url = 'https://spa.test/app') {
  const dom = new JSDOM('<!doctype html><body></body>', { url });
  const win = dom.window as unknown as JournalWindow;
  const posted: RouteMessage[] = [];
  // jsdom delivers postMessage asynchronously and without `source`; capture at the source instead,
  // which is what these tests are about — the listener side is covered by the offline suite.
  win.postMessage = (msg: unknown) => {
    posted.push(msg as RouteMessage);
  };
  return { win, posted, dom };
}

test('a pushState is reported with the RESOLVED address, not the argument', () => {
  const { win, posted } = page();
  installRouteJournal(win);
  // A router routinely passes a relative argument; the scenario needs the address it resolved to.
  win.history.pushState({}, '', '/app/orders');
  assert.equal(posted.length, 1);
  assert.equal(posted[0].__sentinel, ROUTE_MSG);
  assert.equal(posted[0].url, 'https://spa.test/app/orders');
  assert.equal(posted[0].how, 'push');
});

test('replaceState is told apart from pushState', () => {
  const { win, posted } = page();
  installRouteJournal(win);
  win.history.replaceState({}, '', '/app/cart');
  assert.equal(posted.length, 1);
  assert.equal(posted[0].how, 'replace');
});

test('the wrapper is transparent: the original runs and its result is returned', () => {
  const { win, posted } = page();
  const seen: unknown[][] = [];
  const original = win.history.pushState.bind(win.history);
  const sentinel = { marker: 'original-return' };
  win.history.pushState = function (...args: unknown[]) {
    seen.push(args);
    original(args[0], args[1] as string, args[2] as string);
    return sentinel as unknown as void;
  } as typeof win.history.pushState;

  installRouteJournal(win);
  const got = (win.history.pushState as unknown as (...a: unknown[]) => unknown)({ s: 1 }, '', '/app/x');

  assert.equal(got, sentinel, 'the wrapper must return what the original returned, unchanged');
  assert.equal(seen.length, 1, 'the original must be called exactly once');
  assert.deepEqual(seen[0], [{ s: 1 }, '', '/app/x'], 'arguments must reach it untouched');
  assert.equal(posted.length, 1);
});

test('a throwing original still throws — the journal must not swallow a refusal', () => {
  // `pushState` really does throw (SecurityError on a cross-origin path). A router is written to
  // expect that refusal; swallowing it here would send the application down a different branch
  // because of us.
  const { win, posted } = page();
  win.history.pushState = function () {
    throw new Error('SecurityError: refused');
  } as typeof win.history.pushState;
  installRouteJournal(win);
  assert.throws(() => win.history.pushState({}, '', '/nope'), /SecurityError/);
  assert.equal(posted.length, 0, 'a refused navigation is not a route');
});

test('a consecutive repeat of the same address is not reported twice', () => {
  // Syncing screen state to the address on every filter/sort/scroll via replaceState is a common
  // idiom; without this one interaction posts the same address many times.
  const { win, posted } = page();
  installRouteJournal(win);
  win.history.replaceState({}, '', '/app/list');
  win.history.replaceState({}, '', '/app/list');
  win.history.replaceState({}, '', '/app/list');
  assert.equal(posted.length, 1, posted.map((p) => p.url).join(','));
});

test('A -> B -> A stays three routes: a return is a real transition', () => {
  // Compared against the LAST post only, never the whole history — otherwise a scenario could not
  // represent going back to a screen it has already visited.
  const { win, posted } = page();
  installRouteJournal(win);
  win.history.pushState({}, '', '/a');
  win.history.pushState({}, '', '/b');
  win.history.pushState({}, '', '/a');
  assert.deepEqual(posted.map((p) => p.url), [
    'https://spa.test/a', 'https://spa.test/b', 'https://spa.test/a',
  ]);
});

test('popstate is reported, and covers a bare hash assignment', () => {
  // Measured in Chromium (ADR-135) and again in jsdom: `location.hash = …`, an anchor click and
  // history.back all raise popstate FIRST, so a separate hashchange listener never wins.
  const { win, posted, dom } = page();
  installRouteJournal(win);
  dom.window.location.hash = '#/deep';
  return new Promise<void>((resolve) => {
    setTimeout(() => {
      assert.equal(posted.length, 1, posted.map((p) => p.url).join(','));
      assert.equal(posted[0].how, 'pop');
      assert.ok(posted[0].url.endsWith('#/deep'), posted[0].url);
      resolve();
    }, 50);
  });
});

test('installing twice does not double-wrap or double-report', () => {
  const { win, posted } = page();
  assert.equal(installRouteJournal(win), true, 'first install reports that it installed');
  assert.equal(installRouteJournal(win), false, 'second install is a no-op');
  win.history.pushState({}, '', '/app/once');
  assert.equal(posted.length, 1, posted.map((p) => p.url).join(','));
});

test('every message says which world the journal is in', () => {
  // The one failure of this feature that is otherwise perfectly silent: in the ISOLATED world the
  // page's own pushState is invisible, so zero routes are recorded and every gate stays green.
  const { win, posted } = page();
  installRouteJournal(win);
  win.history.pushState({}, '', '/app/y');
  assert.equal(posted[0].main, true, 'no chrome.runtime.id here, so this IS the page world');

  const iso = page();
  (iso.win as unknown as { chrome: unknown }).chrome = { runtime: { id: 'abcdef' } };
  installRouteJournal(iso.win);
  iso.win.history.pushState({}, '', '/app/z');
  assert.equal(iso.posted[0].main, false, 'chrome.runtime.id present -> this is the ISOLATED world');
});
