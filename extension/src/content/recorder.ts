// content-script recorder (#44, M9.8 §2). Injected on demand into the active tab (chrome.scripting,
// activeTab + per-origin host permission — never a static <all_urls> content script). It listens for
// click / input / change / submit ONLY while recording, builds a RecorderEvent (real selector candidates,
// mandatory secret redaction — see selectors.ts), and forwards it to the service worker, which streams it
// over the WebSocket to /v1/stream.
import type { ContentMessage, RecorderEvent, RecordControl } from '../shared/protocol.js';
import { buildRecorderEvent, buildSelectorCandidates, controlKind } from './selectors.js';

// When a click lands on an inner node (e.g. <button><svg>…), climb to the nearest interactive ancestor so
// the candidates bind to the real control (role_name) instead of an svg-path css/xpath.
const INTERACTIVE = 'a,button,[role],input,select,textarea,summary,label,[onclick],[tabindex]';

function isSubmitControl(el: Element): boolean {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  return (tag === 'button' && (type === 'submit' || type === '')) || (tag === 'input' && type === 'submit');
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

  function emit(event: RecorderEvent): void {
    if (!recording) return;
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

  // Capture phase so we see the event before the page can stopPropagation it.
  document.addEventListener(
    'click',
    (e) => {
      let el = e.target as Element | null;
      if (!el || el.nodeType !== 1) return;
      el = (el.closest(INTERACTIVE) as Element | null) ?? el;
      if (isSubmitControl(el)) lastSubmitClickAt = Date.now();
      send('click', el);
    },
    true,
  );

  // Per-keystroke input is debounced (trailing) — the bridge collapses consecutive same-element fills,
  // but debouncing keeps us well under the server's per-session event cap during live typing.
  document.addEventListener(
    'input',
    (e) => {
      const el = e.target as Element | null;
      if (!el || el.nodeType !== 1 || controlKind(el) === 'toggle') return;
      const prev = inputTimers.get(el);
      if (prev) clearTimeout(prev);
      inputTimers.set(
        el,
        setTimeout(() => {
          inputTimers.delete(el);
          send('input', el, valueOf(el));
        }, 300),
      );
    },
    true,
  );

  // change fires on commit/blur — emit the final value immediately (flush any pending input timer first).
  // Skip checkbox/radio: their toggle is already captured by the click; a fill('on') would fail on replay.
  document.addEventListener(
    'change',
    (e) => {
      const el = e.target as Element | null;
      if (!el || el.nodeType !== 1 || controlKind(el) === 'toggle') return;
      const prev = inputTimers.get(el);
      if (prev) {
        clearTimeout(prev);
        inputTimers.delete(el);
      }
      send('change', el, valueOf(el));
    },
    true,
  );

  document.addEventListener(
    'submit',
    (e) => {
      const form = e.target as Element | null;
      if (!form || form.nodeType !== 1 || !recording) return;
      if (Date.now() - lastSubmitClickAt < 700) {
        // A submit-control click was just recorded; the bridge drops the submit (the click is the action).
        send('submit', form);
        return;
      }
      // Enter-driven submit (no button click) — emit a press Enter on the focused field so it survives
      // grounding (the bridge drops a bare submit, which would otherwise lose the form submission).
      const active = (document.activeElement && document.activeElement !== document.body
        ? document.activeElement
        : form) as Element;
      try {
        emit({ type: 'submit', url: location.href, selectorCandidates: buildSelectorCandidates(active),
          verb: 'press', key: 'Enter' });
      } catch {
        /* skip on a detached/odd target */
      }
    },
    true,
  );

  void chrome.runtime.sendMessage({ kind: 'recorder-ready' } satisfies ContentMessage);
}
