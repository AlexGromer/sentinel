// content-script recorder (#44, M9.8 §2). Injected on demand into the active tab (chrome.scripting,
// activeTab + per-origin host permission — never a static <all_urls> content script). It listens for
// click / input / change / submit ONLY while recording, builds a RecorderEvent (real selector candidates,
// mandatory secret redaction — see selectors.ts), and forwards it to the service worker, which streams it
// over the WebSocket to /v1/stream.
//
// Shadow DOM (PERCEPT-RECORDER-SHADOW): every target here goes through `deepTarget`, because the DOM
// retargets `e.target` to the shadow HOST on the way out of an open root — a document-level listener
// is told `<x-color-picker>` where the user clicked the button inside it. Closed roots are a stated
// boundary, and the non-composed events (change/submit) need a listener on the root itself; both are
// spelled out at their implementations below.
import {
  ROUTE_MSG,
  type ContentMessage,
  type RecorderLine,
  type RecordControl,
  type RouteMessage,
} from '../shared/protocol.js';
import {
  buildRecorderEvent,
  buildSelectorCandidates,
  controlKind,
  nameLooksSecret,
  shadowHostOf,
} from './selectors.js';

/** Replace the VALUE of any secret-looking query parameter, keeping the parameter name and the shape
 * of the route (ADR-138).
 *
 * Until now `page_identity` dropped the query entirely, so a token in the address could never reach
 * an artifact. Routes carry it, and `scenario.json` is a file people commit into their application's
 * repository — so the same mandatory redaction that guards field values (M9.8 §2) has to guard the
 * address. The cost is named: two routes differing only by a secret parameter become
 * indistinguishable, which is the correct trade against writing the secret down. */
function redactQuery(href: string): string {
  const q = href.indexOf('?');
  if (q < 0) return href;
  const end = href.indexOf('#', q);
  const tail = end < 0 ? '' : href.slice(end);
  const query = href.slice(q + 1, end < 0 ? undefined : end);
  if (!query) return href;
  const parts = query.split('&').map((pair) => {
    const eq = pair.indexOf('=');
    if (eq < 0) return pair;
    const name = pair.slice(0, eq);
    return nameLooksSecret(decodeURIComponent(name)) ? `${name}=REDACTED` : pair;
  });
  return `${href.slice(0, q)}?${parts.join('&')}${tail}`;
}

// When a click lands on an inner node (e.g. <button><svg>…), climb to the nearest interactive ancestor so
// the candidates bind to the real control (role_name) instead of an svg-path css/xpath.
const INTERACTIVE = 'a,button,[role],input,select,textarea,summary,label,[onclick],[tabindex]';

function isSubmitControl(el: Element): boolean {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  return (tag === 'button' && (type === 'submit' || type === '')) || (tag === 'input' && type === 'submit');
}

/** The element the user actually touched.
 *
 * `e.target` is RETARGETED to the shadow HOST the moment an event crosses an open shadow boundary
 * (DOM §2.10 "retargeting"), so a document-level listener is told `<x-color-picker>` where the user
 * clicked a button inside it. The recorded plan then names a component instead of a control — while
 * the executor, whose CSS and role engines pierce open roots, sees and can drive the control perfectly
 * well. `composedPath()[0]` is the un-retargeted deepest target and closes that gap.
 *
 * A CLOSED root is a BOUNDARY, not a debt: the browser deliberately keeps its nodes out of the composed
 * path (and `host.shadowRoot` is null for everybody), so `path[0]` is legitimately the host. We then
 * record the host — the same answer as before, and the only honest one available — rather than pretend
 * to have seen inside. */
function deepTarget(e: Event): Element | null {
  const path = typeof e.composedPath === 'function' ? e.composedPath() : [];
  for (const node of path) {
    if (node && (node as Node).nodeType === 1) return node as Element;
  }
  const target = e.target as Element | null;
  return target && target.nodeType === 1 ? target : null;
}

/** Nearest interactive ancestor, crossing open shadow boundaries only when the element's own tree
 * offers nothing. `closest()` stops at the root of the tree by design, so a component whose internals
 * are inert (<span>/<svg>) and whose HOST carries role/tabindex would otherwise bind to the inert
 * span. Climbing only after the inner tree comes up empty keeps the real control when there IS one. */
function closestInteractive(el: Element): Element {
  let node: Element | null = el;
  for (let hop = 0; node && hop < 4; hop++) {
    const hit = node.closest(INTERACTIVE) as Element | null;
    if (hit) return hit;
    node = shadowHostOf(node);
  }
  return el;
}

/** The focused element, following focus INTO open shadow roots. `document.activeElement` is the host
 * for anything focused inside a component — same retargeting, and here it would attribute an
 * Enter-driven submit to the whole component instead of the field the user typed in. */
function deepActiveElement(): Element | null {
  let node: Element | null = document.activeElement;
  for (let hop = 0; node && node.shadowRoot && hop < 4; hop++) {
    const inner = node.shadowRoot.activeElement;
    if (!inner) break;
    node = inner;
  }
  return node;
}

declare global {
  interface Window {
    __sentinelRecorderInstalled?: boolean;
  }
}

// Re-injection (panel re-presses Start) just re-syncs state instead of double-binding listeners.
if (window.__sentinelRecorderInstalled) {
  void chrome.runtime.sendMessage({ kind: 'recorder-ready' } satisfies ContentMessage);
} else {
  window.__sentinelRecorderInstalled = true;
  installRecorder();
}

function installRecorder(): void {
  let recording = false;
  let lastSubmitClickAt = 0;
  const inputTimers = new WeakMap<Element, ReturnType<typeof setTimeout>>();

  chrome.runtime.onMessage.addListener((msg: RecordControl) => {
    if (msg?.kind === 'record-control') recording = msg.recording;
  });

  // The ONE funnel every line goes through — both producers (buildRecorderEvent and the hand-written
  // Enter-submit literal below) and the route relay. `seq` is stamped here so that "file order ==
  // observation order" is a claim a gate can check rather than one the reader has to assume; a gap in
  // it also makes the already-silent drop in the service worker ("event dropped (socket not open)")
  // visible, instead of looking like a person who did nothing.
  let seq = 0;

  function emit(event: RecorderLine): void {
    if (!recording) return;
    event.seq = ++seq;
    // Redaction of the ADDRESS belongs here, in the funnel, and covers every line — not just routes.
    // Measured by the route gate: a click recorded while the address carried `?apiKey=…` wrote the
    // live token into `runs/record-<session>/events.ndjson` verbatim. That predates routes (the
    // bridge dropped the query, so it never reached `scenario.json`, and the leak stayed in the raw
    // stream where nobody looked); routes would have carried it the rest of the way.
    event.url = redactQuery(event.url);
    void chrome.runtime.sendMessage({ kind: 'recorder-event', event } satisfies ContentMessage);
  }

  function send(type: 'click' | 'input' | 'change' | 'submit', el: Element, rawValue?: string): void {
    if (!recording) return;
    try {
      emit(buildRecorderEvent(type, el, rawValue, location.href));
    } catch {
      // A detached node or odd target shouldn't kill the recorder — just skip this event.
    }
  }

  function valueOf(el: Element): string | undefined {
    const v = (el as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).value;
    return typeof v === 'string' ? v : undefined;
  }

  // `change` and `submit` are NOT composed (they carry composed:false), so unlike click/input they do
  // not cross a shadow boundary at all: a document-level listener never sees a <select> committed
  // inside a component. That action is not mis-recorded — it is MISSING, silently. So we also listen
  // on the shadow roots themselves, and the set of roots is DERIVED from the composed events we do
  // see (click/input arrive from inside the component), never maintained as a list.
  const hookedRoots = new WeakSet<Node>();
  function hookShadowRootOf(el: Element): void {
    if (!shadowHostOf(el)) return;                     // light DOM (or a closed root: nothing to hook)
    const root = el.getRootNode();
    if (hookedRoots.has(root)) return;
    hookedRoots.add(root);
    root.addEventListener('change', onChange, true);
    root.addEventListener('submit', onSubmit, true);
  }

  // One Event object can reach us twice — a component that re-dispatches a COMPOSED `change` from an
  // inner node is seen by both its shadow root's listener and the document's. Recording it twice would
  // double every such action, and the bridge's fill-collapsing would hide the duplicate for fills
  // while leaving it for everything else.
  const seen = new WeakSet<Event>();
  function first(e: Event): boolean {
    if (seen.has(e)) return false;
    seen.add(e);
    return true;
  }

  function onClick(e: Event): void {
    const target = deepTarget(e);
    if (!target || !first(e)) return;
    hookShadowRootOf(target);
    const el = closestInteractive(target);
    if (isSubmitControl(el)) lastSubmitClickAt = Date.now();
    send('click', el);
  }

  // Per-keystroke input is debounced (trailing) — the bridge collapses consecutive same-element fills,
  // but debouncing keeps us well under the server's per-session event cap during live typing.
  function onInput(e: Event): void {
    const el = deepTarget(e);
    if (!el || !first(e) || controlKind(el) === 'toggle') return;
    hookShadowRootOf(el);
    const prev = inputTimers.get(el);
    if (prev) clearTimeout(prev);
    inputTimers.set(
      el,
      setTimeout(() => {
        inputTimers.delete(el);
        send('input', el, valueOf(el));
      }, 300),
    );
  }

  // change fires on commit/blur — emit the final value immediately (flush any pending input timer first).
  // Skip checkbox/radio: their toggle is already captured by the click; a fill('on') would fail on replay.
  function onChange(e: Event): void {
    const el = deepTarget(e);
    if (!el || !first(e) || controlKind(el) === 'toggle') return;
    const prev = inputTimers.get(el);
    if (prev) {
      clearTimeout(prev);
      inputTimers.delete(el);
    }
    send('change', el, valueOf(el));
  }

  function onSubmit(e: Event): void {
    const form = deepTarget(e);
    if (!form || !recording || !first(e)) return;
    if (Date.now() - lastSubmitClickAt < 700) {
      // A submit-control click was just recorded; the bridge drops the submit (the click is the action).
      send('submit', form);
      return;
    }
    // Enter-driven submit (no button click) — emit a press Enter on the focused field so it survives
    // grounding (the bridge drops a bare submit, which would otherwise lose the form submission).
    const focused = deepActiveElement();
    const active = (focused && focused !== document.body ? focused : form) as Element;
    try {
      emit({ type: 'submit', url: location.href, selectorCandidates: buildSelectorCandidates(active),
        verb: 'press', key: 'Enter' });
    } catch {
      /* skip on a detached/odd target */
    }
  }

  // Capture phase so we see the event before the page can stopPropagation it.
  document.addEventListener('click', onClick, true);
  document.addEventListener('input', onInput, true);
  document.addEventListener('change', onChange, true);
  document.addEventListener('submit', onSubmit, true);
  // `focusin` (composed, unlike `focus`) is how a KEYBOARD user first touches a control. Without it a
  // component entered by Tab and committed with the keyboard — a <select> changed with the arrow keys,
  // say — would produce no click and no input, so its root would never be learned and its
  // non-composed `change` would be lost in silence. Nothing is RECORDED here: this only discovers roots.
  document.addEventListener('focusin', (e) => {
    const target = deepTarget(e);
    if (target) hookShadowRootOf(target);
  }, true);

  // ADR-138: the MAIN-world route journal reports every address change that happened WITHOUT a
  // document load — the class of transition this recorder was blind to. It cannot reach the service
  // worker itself (no `chrome.runtime` in the page's world, measured), so it posts and we relay.
  //
  // Both guards are required. `e.source === window` is measured true for a MAIN→ISOLATED post; the
  // envelope name pins it to ours. A page could forge the message — but a page can already forge a
  // `click` through `dispatchEvent`, which the capture-phase listener records, so this widens no
  // class of exposure while the guards keep it from being trivially open.
  let warnedWrongWorld = false;
  window.addEventListener('message', (e: MessageEvent) => {
    const data = e.data as RouteMessage | undefined;
    if (e.source !== window || !data || data.__sentinel !== ROUTE_MSG) return;
    if (!data.main && !warnedWrongWorld) {
      // The journal landed in the ISOLATED world, where the page's own pushState is invisible to it.
      // That failure records ZERO routes and leaves every gate green — say it once, loudly.
      warnedWrongWorld = true;
      void chrome.runtime.sendMessage({
        kind: 'recorder-warning',
        text: 'route journal is not in the page world — route changes will NOT be recorded',
      } satisfies ContentMessage);
    }
    if (typeof data.url !== 'string' || !data.url) return;
    emit({ type: 'route', url: data.url, how: data.how });   // redaction happens in emit(), for every line
  });

  void chrome.runtime.sendMessage({ kind: 'recorder-ready' } satisfies ContentMessage);
}
