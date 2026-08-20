import { test } from 'node:test';
import assert from 'node:assert/strict';
import { shouldTrackNewPage, shouldClosePagesOnTeardown } from './ownership.js';

// --- shouldTrackNewPage (ADR-128) -------------------------------------------------------------

test('launch mode: every page in our own context is ours, opener or not', () => {
  assert.equal(shouldTrackNewPage({ attachedOverCDP: false, openerIsOurs: true }), true);
  // The one that matters: `newContext` gives us a private context, so a page with no opener there
  // can only be one we made. Answering `false` would drop a legitimate tab in the deterministic path.
  assert.equal(shouldTrackNewPage({ attachedOverCDP: false, openerIsOurs: false }), true);
});

test('CDP-attach: a popup OUR page opened is the run\'s', () => {
  assert.equal(shouldTrackNewPage({ attachedOverCDP: true, openerIsOurs: true }), true);
});

test('CDP-attach: a tab the human opened beside us is NOT the run\'s', () => {
  // The whole point of ADR-128 applied to pages that appear later: adopting this one would let
  // browser.switchTab drive somebody's own tab and copy its console into runs/<id>/.
  assert.equal(shouldTrackNewPage({ attachedOverCDP: true, openerIsOurs: false }), false);
});

test('the two modes disagree on exactly one input, which is why the flag is a parameter', () => {
  const cdp = shouldTrackNewPage({ attachedOverCDP: true, openerIsOurs: false });
  const own = shouldTrackNewPage({ attachedOverCDP: false, openerIsOurs: false });
  assert.notEqual(cdp, own);
});

// --- shouldClosePagesOnTeardown (ADR-128) -----------------------------------------------------

test('CDP-attach with pages of ours: hand them back', () => {
  assert.equal(
    shouldClosePagesOnTeardown({ attachedOverCDP: true, havePages: true, contextClosed: false }), true);
});

test('launch mode: never — closing the browser takes every page with it', () => {
  // Not a preference: `browser.close()` on the following line already disposes everything, and
  // closing the pages first would only add a way for teardown to throw.
  assert.equal(
    shouldClosePagesOnTeardown({ attachedOverCDP: false, havePages: true, contextClosed: false }), false);
});

test('no pages (the run never started a browser): nothing to hand back', () => {
  assert.equal(
    shouldClosePagesOnTeardown({ attachedOverCDP: true, havePages: false, contextClosed: false }), false);
});

test('a closed context is not reopened to close pages inside it', () => {
  // Unreachable today — video is refused over CDP (ADR-125), and `videoStop` is the only thing that
  // closes the context — so this pins the guard rather than a behaviour anyone can currently produce.
  assert.equal(
    shouldClosePagesOnTeardown({ attachedOverCDP: true, havePages: true, contextClosed: true }), false);
});
