// Unit tests for the recorder's pure logic (#44): selector-candidate ranking + MANDATORY redaction.
// Runs under jsdom (no browser) via `node --import tsx --test`. The redaction tests are the security
// acceptance for #44 — a password value must never reach the event.
import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { accessibleName, buildRecorderEvent, buildSelectorCandidates, classifyField, controlKind, cssPath, shadowHostOf, xpathOf } from './selectors.js';

const URL_ = 'https://example.test/login';

function el(html: string): Element {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`);
  const node = dom.window.document.body.firstElementChild;
  assert.ok(node, 'fixture produced an element');
  return node as unknown as Element;
}

test('testid ranks first and css+xpath are always present', () => {
  const c = buildSelectorCandidates(el('<button data-testid="submit-btn">Go</button>'));
  assert.equal(c[0].strategy, 'testid');
  assert.equal(c[0].locator.testid, 'submit-btn');
  const strategies = c.map((x) => x.strategy);
  assert.ok(strategies.includes('css'), 'has css fallback');
  assert.ok(strategies.includes('xpath'), 'has xpath fallback');
});

test('role_name candidate uses implicit role + accessible name', () => {
  const c = buildSelectorCandidates(el('<button>Sign in</button>'));
  const rn = c.find((x) => x.strategy === 'role_name');
  assert.ok(rn, 'has a role_name candidate');
  assert.equal(rn!.locator.role, 'button');
  assert.equal(rn!.locator.name, 'Sign in');
});

test('accessibleName prefers aria-label', () => {
  assert.equal(accessibleName(el('<button aria-label="Close dialog">x</button>')), 'Close dialog');
});

test('label candidate from associated <label for>', () => {
  const dom = new JSDOM('<!doctype html><body><label for="e">Email</label><input id="e" type="text"></body>');
  const input = dom.window.document.getElementById('e') as unknown as Element;
  const c = buildSelectorCandidates(input);
  const label = c.find((x) => x.strategy === 'label');
  assert.ok(label, 'has a label candidate');
  assert.equal(label!.locator.label, 'Email');
});

// --- redaction (security acceptance) ---

test('password field is secret; value is redacted to a secretRef', () => {
  const input = el('<input type="password" name="userPassword">');
  const cls = classifyField(input);
  assert.equal(cls.secret, true);
  assert.equal(cls.secretRef, 'USER_PASSWORD');

  const event = buildRecorderEvent('change', input, 'hunter2', URL_);
  assert.equal(event.value, undefined, 'literal password value must NOT be present');
  assert.equal(event.secretRef, 'USER_PASSWORD');
  assert.ok(!JSON.stringify(event).includes('hunter2'), 'the secret must not appear anywhere in the event');
});

test('autocomplete current-password marks a non-password input secret', () => {
  assert.equal(classifyField(el('<input type="text" autocomplete="current-password">')).secret, true);
});

test('data-sentinel-secret marks a field secret with its env name', () => {
  const cls = classifyField(el('<input type="text" data-sentinel-secret="API_TOKEN">'));
  assert.equal(cls.secret, true);
  assert.equal(cls.secretRef, 'API_TOKEN');
});

test('conservative name heuristic catches cvv / otp', () => {
  assert.equal(classifyField(el('<input type="text" name="cvv">')).secret, true);
  assert.equal(classifyField(el('<input type="tel" name="otp-code">')).secret, true);
});

test('name heuristic catches more credential names (pin / token / api_key)', () => {
  assert.equal(classifyField(el('<input type="text" name="pin">')).secret, true);
  assert.equal(classifyField(el('<input type="text" name="api_key">')).secret, true);
  assert.equal(classifyField(el('<input type="text" name="accessToken">')).secret, true);
});

test('secret detection uses sensitive autocomplete tokens (one-time-code / cc-csc)', () => {
  assert.equal(classifyField(el('<input type="text" autocomplete="one-time-code" name="code">')).secret, true);
  assert.equal(classifyField(el('<input type="text" autocomplete="cc-csc" name="csc">')).secret, true);
});

test('does NOT over-redact ordinary fields that merely contain a secret substring', () => {
  for (const name of ['passenger1', 'passport', 'className', 'compass', 'email', 'addressLine']) {
    assert.equal(classifyField(el(`<input type="text" name="${name}">`)).secret, false, name);
  }
});

test('controlKind classifies select / toggle / fillable', () => {
  assert.equal(controlKind(el('<select><option>a</option></select>')), 'select');
  assert.equal(controlKind(el('<input type="checkbox">')), 'toggle');
  assert.equal(controlKind(el('<input type="radio">')), 'toggle');
  assert.equal(controlKind(el('<input type="text">')), 'fillable');
  assert.equal(controlKind(el('<textarea></textarea>')), 'fillable');
});

test('a <select> change carries verb=select (replays as selectOption, not fill)', () => {
  const event = buildRecorderEvent('change', el('<select name="role"><option value="admin">Admin</option></select>'), 'admin', URL_);
  assert.equal(event.verb, 'select');
  assert.equal(event.value, 'admin');
});

test('contenteditable secret does not leak typed text into selector candidates', () => {
  const dom = new JSDOM('<!doctype html><body><div contenteditable role="textbox" data-sentinel-secret="SEED">correct horse battery staple</div></body>');
  const node = dom.window.document.body.firstElementChild as unknown as Element;
  const candidates = buildSelectorCandidates(node);
  const blob = JSON.stringify(candidates);
  assert.ok(!blob.includes('correct horse'), 'typed secret must not appear in any candidate');
  // a role_name candidate, if present, must not carry the typed text as its name
  const rn = candidates.find((c) => c.strategy === 'role_name');
  assert.ok(!rn || rn.locator.name !== 'correct horse battery staple', rn?.locator.name);
});

test('non-secret input keeps its value', () => {
  const event = buildRecorderEvent('change', el('<input type="text" name="email">'), 'a@b.com', URL_);
  assert.equal(event.value, 'a@b.com');
  assert.equal(event.secretRef, undefined);
});

test('click event carries no value', () => {
  const event = buildRecorderEvent('click', el('<a href="/next">Next</a>'), undefined, URL_);
  assert.equal(event.type, 'click');
  assert.equal(event.value, undefined);
  assert.ok(event.selectorCandidates.length >= 1);
});

// --- shadow DOM (PERCEPT-RECORDER-SHADOW) ---
//
// The end-to-end proof — the real recorder driven over a real fixture — is
// tests/test_recorder_shadow_offline.py. These are the unit-level properties of the locator builder
// it rests on, each of which would otherwise only be visible as "the whole scenario is wrong".

/** A host with an open shadow root containing `inner`. Returns [host, firstElementOfTheRoot]. */
function withShadow(hostHtml: string, inner: string, mode: 'open' | 'closed' = 'open'): [Element, Element] {
  const dom = new JSDOM(`<!doctype html><body>${hostHtml}</body>`);
  const host = dom.window.document.body.firstElementChild!;
  const root = host.attachShadow({ mode });
  root.innerHTML = inner;
  return [host as unknown as Element, root.firstElementChild as unknown as Element];
}

test('shadowHostOf answers the host inside a root and null in the light DOM', () => {
  const [host, inner] = withShadow('<x-picker id="picker"></x-picker>', '<button id="swatch">Red</button>');
  assert.equal(shadowHostOf(inner), host);
  assert.equal(shadowHostOf(host), null);
});

test('cssPath prefixes the host, so a pierced query cannot hit a same-id light-DOM element', () => {
  const [, inner] = withShadow('<x-picker id="picker"></x-picker>', '<button id="swatch">Red</button>');
  assert.equal(cssPath(inner), '#picker > #swatch');
});

test('cssPath crosses a boundary for an element with no id of its own', () => {
  const [, wrap] = withShadow('<x-panel id="panel"></x-panel>', '<div><button>Go</button></div>');
  assert.equal(cssPath(wrap.firstElementChild as unknown as Element), '#panel > div > button');
});

test('xpathOf refuses to invent a document path for a shadow node', () => {
  // Playwright's xpath engine is a bare document.evaluate with no shadow expansion (measured in
  // playwright-core/XPathEngine), so any path emitted here would resolve to nothing — or to a
  // DIFFERENT element that happens to sit at the same indices.
  const [host, inner] = withShadow('<x-picker id="picker"></x-picker>', '<button id="swatch">Red</button>');
  assert.equal(xpathOf(inner), '');
  assert.ok(xpathOf(host).startsWith('/'), 'the light-DOM host still has one');

  const candidates = buildSelectorCandidates(inner);
  assert.ok(!candidates.some((c) => c.strategy === 'xpath'), 'no xpath candidate for a shadow node');
  assert.ok(candidates.some((c) => c.strategy === 'css'), 'a css fallback is still always present');
});

test('a <label for> inside the root is found there — and a same-id document label is not used', () => {
  const dom = new JSDOM('<!doctype html><body><label for="alias">WRONG light-DOM label</label>' +
    '<x-settings id="settings"></x-settings></body>');
  const host = dom.window.document.getElementById('settings')!;
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = '<label for="alias">Alias</label><input id="alias" name="alias" type="text">';
  const input = root.getElementById('alias') as unknown as Element;

  assert.equal(accessibleName(input), 'Alias');
  const candidates = buildSelectorCandidates(input);
  assert.deepEqual(candidates.find((c) => c.strategy === 'label')?.locator, { label: 'Alias' });
  assert.deepEqual(candidates.find((c) => c.strategy === 'role_name')?.locator,
    { role: 'textbox', name: 'Alias' });
});

test('aria-labelledby resolves in the element own tree, not the document', () => {
  const dom = new JSDOM('<!doctype html><body><span id="cap">WRONG light-DOM caption</span>' +
    '<x-card id="card"></x-card></body>');
  const root = dom.window.document.getElementById('card')!.attachShadow({ mode: 'open' });
  root.innerHTML = '<span id="cap">Archive</span><button id="b" aria-labelledby="cap">x</button>';
  assert.equal(accessibleName(root.getElementById('b') as unknown as Element), 'Archive');
});

test('a CLOSED root is a boundary: its host is addressed normally and nothing leaks out', () => {
  const [host] = withShadow('<x-sealed id="sealed"></x-sealed>', '<button id="inner">Sealed</button>', 'closed');
  assert.equal(host.shadowRoot, null, 'the browser exposes nothing — for us either');
  const candidates = buildSelectorCandidates(host);
  assert.deepEqual(candidates.find((c) => c.strategy === 'css')?.locator, { css: '#sealed' });
  assert.ok(!JSON.stringify(candidates).includes('inner'), 'nothing from inside a closed root');
});
