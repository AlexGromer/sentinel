// DevTools panel (#45): start/stop recording, takeover/return, live status, and config. It talks to the
// service worker over a long-lived port (PANEL_PORT) and drives the recorder for the inspected tab.
//
// Lazy permissions are requested HERE, from the button click (a user gesture), then handed to the SW:
//   • Start  → request the inspected origin's host permission (optional_host_permissions).
//   • Takeover → request the `debugger` optional permission (never held at install).
import {
  DEFAULT_CONTROL_API_URL,
  PANEL_PORT,
  STORAGE_KEYS,
  type Config,
  type PanelCommand,
  type PanelMessage,
  type Status,
} from '../shared/protocol.js';

const tabId = chrome.devtools.inspectedWindow.tabId;

// The SW can be evicted, which drops our port. Reconnect so the panel doesn't freeze (and send() doesn't
// throw on a dead port).
let port: chrome.runtime.Port | null = null;
function connectPort(): void {
  const p = chrome.runtime.connect({ name: PANEL_PORT });
  p.onMessage.addListener((msg: PanelMessage) => {
    if (msg.kind === 'status') render(msg.status);
  });
  p.onDisconnect.addListener(() => {
    port = null;
    setTimeout(connectPort, 500); // SW probably went idle — reconnect and re-pull status
  });
  port = p;
  send({ kind: 'get-status' });
}

function send(cmd: PanelCommand): void {
  try {
    port?.postMessage(cmd);
  } catch {
    /* dead port — onDisconnect will trigger a reconnect */
  }
}

function $(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el;
}

const els = {
  connDot: $('conn-dot'),
  connText: $('conn-text'),
  session: $('session'),
  drive: $('drive'),
  error: $('error'),
  events: $('events'),
  start: $('start') as HTMLButtonElement,
  stop: $('stop') as HTMLButtonElement,
  takeover: $('takeover') as HTMLButtonElement,
  return: $('return') as HTMLButtonElement,
  url: $('url') as HTMLInputElement,
  token: $('token') as HTMLInputElement,
  save: $('save') as HTMLButtonElement,
  saved: $('saved'),
};

// Cache the inspected origin OUT of band: chrome.permissions.request() must run inside the click's user
// gesture, but inspectedWindow.eval is async and would consume it. Keep the origin fresh here instead.
let inspectedOrigin = '';
function refreshOrigin(): void {
  chrome.devtools.inspectedWindow.eval('location.origin', (result: unknown, err) => {
    if (!err && typeof result === 'string') inspectedOrigin = result;
  });
}
refreshOrigin();
chrome.devtools.network.onNavigated.addListener(() => refreshOrigin());

function render(status: Status): void {
  els.connDot.className = 'dot ' + status.connection;
  els.connText.textContent = status.connection;
  els.session.textContent = status.session ?? '—';
  els.drive.textContent = status.drive;
  els.events.textContent = String(status.events);
  els.error.textContent = status.error ?? '';

  els.start.disabled = status.recording;
  els.stop.disabled = !status.recording;
  els.takeover.disabled = status.drive === 'human';
  els.return.disabled = status.drive !== 'human';
}

els.start.addEventListener('click', () => {
  const origin = inspectedOrigin;
  if (!origin || origin === 'null') {
    send({ kind: 'start-record', tabId, origin });
    return;
  }
  // Call chrome.permissions.request synchronously inside the gesture (no await before it), then start.
  chrome.permissions
    .request({ origins: [origin + '/*'] })
    .then((granted) => {
      if (granted) send({ kind: 'start-record', tabId, origin });
      else els.error.textContent = 'host permission denied — can’t inject the recorder';
    })
    .catch(() => {
      els.error.textContent = 'could not request host permission';
    });
});

els.stop.addEventListener('click', () => send({ kind: 'stop-record' }));

els.takeover.addEventListener('click', async () => {
  const granted = await chrome.permissions.request({ permissions: ['debugger'] });
  if (!granted) {
    els.error.textContent = 'debugger permission denied — takeover needs it';
    return;
  }
  send({ kind: 'takeover', tabId });
});

els.return.addEventListener('click', () => send({ kind: 'return' }));

els.save.addEventListener('click', () => {
  const config: Config = { controlApiUrl: els.url.value.trim() || DEFAULT_CONTROL_API_URL, bearerToken: els.token.value };
  send({ kind: 'set-config', config });
  els.saved.textContent = 'saved';
  setTimeout(() => (els.saved.textContent = ''), 1500);
});

// Initial paint: connect to the SW (which pushes status), then load persisted config into the inputs.
connectPort();
void (async () => {
  const got = await chrome.storage.local.get([STORAGE_KEYS.controlApiUrl, STORAGE_KEYS.bearerToken]);
  els.url.value = (got[STORAGE_KEYS.controlApiUrl] as string) || '';
  els.token.value = (got[STORAGE_KEYS.bearerToken] as string) || '';
})();
