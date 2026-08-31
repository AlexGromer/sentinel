#!/usr/bin/env node
// Drives the REAL content-script recorder (src/content/recorder.ts, TypeScript, loaded through tsx)
// over a fixture inside jsdom and prints the RecorderEvents it emitted as JSON on stdout. No browser,
// no network, no server — this is the offline half of the recorder's proof, consumed by
// tests/test_recorder_shadow_offline.py, which then grounds the very same events through
// brain/record_bridge.py.
//
// It exists because the only CI-visible recorder check was tests/test_record_bridge_recorder_e2e.py,
// which replays a VERBATIM transcript frozen in the test file: it cannot go red for any change to
// recorder.ts, so a fix or a regression there would both pass. The live Chromium e2e
// (test/e2e/recorder.e2e.mjs) does run the real code, but needs a full Chromium and a running
// control-api and is dev-only by design.
//
// jsdom is faithful for exactly the properties under test — measured before this was written:
//   · a click from inside an OPEN shadow root arrives at a document listener with e.target retargeted
//     to the host, while composedPath()[0] is the real inner element;
//   · a CLOSED root contributes only its host to composedPath, and host.shadowRoot is null;
//   · `change` / `submit` carry composed:false and never reach the document from inside a root.
//
// Usage:  node --import tsx test/record-in-jsdom.mjs test/e2e/shadow-fixture.html [page-url]
//
// The action list is DERIVED from the fixture's own `data-record` attributes (verb[:arg], several
// separated by `;`), never carried here — a control added to the fixture is driven by the fact that it
// is in the fixture. `--floor=N` fails when fewer than N [data-record] ELEMENTS were found (an element
// may declare several actions), so a walk that stops finding anything cannot report success over an
// empty set.
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const floorArg = process.argv.slice(2).find((a) => a.startsWith('--floor='));
const FLOOR = floorArg ? Number(floorArg.split('=')[1]) : 1;
const fixture = resolve(here, '..', argv[0] || 'test/e2e/shadow-fixture.html');
const pageUrl = argv[1] || 'https://shadow.test/settings';

const dom = new JSDOM(readFileSync(fixture, 'utf8'), { url: pageUrl, runScripts: 'dangerously' });
const { window } = dom;

// The content script is written against the page's globals; give it exactly those.
globalThis.window = window;
globalThis.document = window.document;
globalThis.location = window.location;
globalThis.Event = window.Event;
globalThis.MouseEvent = window.MouseEvent;

const emitted = [];
const controlListeners = [];
globalThis.chrome = {
  runtime: {
    sendMessage: (m) => {
      if (m && m.kind === 'recorder-event') emitted.push(m.event);
      return Promise.resolve();
    },
    onMessage: { addListener: (f) => controlListeners.push(f) },
  },
};

// ADR-138: the MAIN-world route journal, installed on the SAME window. In production it lives in the
// page's world and reaches the recorder through window.postMessage; here there is only one world, so
// the transport is real and the isolation is not — that limit is stated in
// tests/test_recorder_routes_offline.py rather than papered over.
//
// ⚠ ONE jsdom limitation is compensated, and only one: jsdom delivers `window.postMessage` with
// `event.source === null`, while a real Chromium delivers `=== window` (both measured). The recorder
// checks `e.source === window` as a guard, so without this the offline run would drop every route and
// the gate would be red for a reason that has nothing to do with the product. The message itself is
// still the real one the real journal posts — only the `source` field is restored.
const { installRouteJournal } = await import('../src/content/route-journal.ts');
const nativePostMessage = window.postMessage.bind(window);
window.postMessage = (data) => {
  window.dispatchEvent(new window.MessageEvent('message', {
    data, source: window, origin: window.location.origin,
  }));
};
void nativePostMessage;
installRouteJournal(window);

// Importing the module installs the recorder on the globals above — that is its production shape.
await import('../src/content/recorder.ts');
if (!controlListeners.length) {
  console.error('the recorder registered no chrome.runtime.onMessage listener — it did not install');
  process.exit(2);
}
for (const f of controlListeners) f({ kind: 'record-control', recording: true });

// ---------------------------------------------------------------------------------------------------
// Derive the actions from the fixture, then perform them.
// ---------------------------------------------------------------------------------------------------

/** Every [data-record] element in document order, descending into OPEN shadow roots, plus any roots
 * the page published for itself (a closed root is unreachable by walking — that IS the boundary). */
function collect(rootNode, out) {
  for (const el of rootNode.querySelectorAll('*')) {
    if (el.hasAttribute && el.hasAttribute('data-record')) out.push(el);
    if (el.shadowRoot) collect(el.shadowRoot, out);
  }
  return out;
}

const targets = collect(window.document, []);
for (const extra of window.__closedRoots || []) collect(extra, targets);

if (targets.length < FLOOR) {
  console.error(`only ${targets.length} [data-record] element(s) found in ${fixture} (floor ${FLOOR}) — ` +
    'the walk is not finding the fixture, and every assertion over these events would pass over nothing');
  process.exit(2);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fire = (el, type, init) => el.dispatchEvent(new window.Event(type, init));

const performed = [];
for (const el of targets) {
  for (const spec of el.getAttribute('data-record').split(';')) {
    const [verb, ...rest] = spec.trim().split(':');
    const arg = rest.join(':');
    performed.push({ verb, arg, tag: el.tagName.toLowerCase(), id: el.id });
    if (verb === 'click') {
      // composed:true is what a real user click carries — that is how it escapes the shadow root.
      el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, composed: true }));
    } else if (verb === 'fill') {
      el.value = arg;
      fire(el, 'input', { bubbles: true, composed: true });
      await sleep(350);                                  // the recorder debounces input by 300 ms
      fire(el, 'change', { bubbles: true });             // composed:false, exactly as the DOM emits it
    } else if (verb === 'focus') {
      el.focus();                                        // focusin is composed; `focus` is not
    } else if (verb === 'select') {
      el.value = arg;
      fire(el, 'change', { bubbles: true });
    } else if (verb === 'press-enter') {
      el.focus();
      const form = el.closest('form');
      if (!form) {
        console.error(`press-enter on #${el.id} but it is in no <form>`);
        process.exit(2);
      }
      fire(form, 'submit', { bubbles: true, cancelable: true });
    } else {
      console.error(`unknown data-record verb ${JSON.stringify(verb)} on #${el.id}`);
      process.exit(2);
    }
    // Drain microtasks between actions (ADR-138). A real browser gives each click its own task, so a
    // router that lands its route in a promise has resolved before the next click; this loop is
    // otherwise synchronous across clicks, which would deliver a deferred route AFTER the actions
    // that followed it and make the observed order an artefact of the driver.
    await sleep(0);
  }
}
await sleep(400);                                        // let any trailing debounce land

process.stdout.write(JSON.stringify({ url: pageUrl, fixture, actions: performed, events: emitted }, null, 2));
process.stdout.write('\n');
