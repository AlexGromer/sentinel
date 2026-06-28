import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resolveLaunchPlan } from './launch.js';

test('default (no env) -> headless launch', () => {
  const p = resolveLaunchPlan({});
  assert.equal(p.kind, 'launch');
  assert.equal(p.headless, true);
  assert.equal(p.cdpEndpoint, undefined);
});

test('PW_HEADLESS=0 -> headed launch', () => {
  const p = resolveLaunchPlan({ PW_HEADLESS: '0' });
  assert.equal(p.kind, 'launch');
  assert.equal(p.headless, false);
});

test('PW_HEADED=1 -> headed launch (alias)', () => {
  assert.equal(resolveLaunchPlan({ PW_HEADED: '1' }).headless, false);
});

test('PW_CDP_ENDPOINT -> cdp, takes precedence over headed', () => {
  const p = resolveLaunchPlan({ PW_CDP_ENDPOINT: 'http://localhost:9222', PW_HEADLESS: '0' });
  assert.equal(p.kind, 'cdp');
  assert.equal(p.cdpEndpoint, 'http://localhost:9222');
});

test('blank / non-"0" values fall back to headless launch', () => {
  assert.equal(resolveLaunchPlan({ PW_CDP_ENDPOINT: '   ' }).kind, 'launch'); // blank CDP ignored
  assert.equal(resolveLaunchPlan({ PW_HEADLESS: 'true' }).headless, true); // only "0" means headed
  assert.equal(resolveLaunchPlan({ PW_HEADLESS: '1' }).headless, true);
});
