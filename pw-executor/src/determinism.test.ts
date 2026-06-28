import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  DETERMINISM_VIEWPORT,
  DETERMINISM_DEVICE_SCALE_FACTOR,
  SCREENSHOT_DETERMINISM_OPTS,
} from './determinism.js';

// GAP-RISK-009: lock the screenshot determinism config so it can't silently regress. Byte-stable
// goldens depend on EXACTLY these anchors (fixed viewport + DSR=1 + animations/caret/scale). A change
// here is a determinism-contract change and must be deliberate (and re-baselined), not accidental.

test('viewport is pinned to 1280x720', () => {
  assert.deepEqual(DETERMINISM_VIEWPORT, { width: 1280, height: 720 });
});

test('device scale factor is 1 (no HiDPI pixel-count drift)', () => {
  assert.equal(DETERMINISM_DEVICE_SCALE_FACTOR, 1);
});

test('screenshot options freeze animations + caret + CSS scale', () => {
  assert.deepEqual(SCREENSHOT_DETERMINISM_OPTS, {
    animations: 'disabled',
    caret: 'hide',
    scale: 'css',
  });
});
