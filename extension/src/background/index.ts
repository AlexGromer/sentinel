// Service-worker entry: the router that wires the recorder + co-pilot together (#42 wiring; uses #43
// ws-client and #47 takeover). It owns the single Status snapshot and pushes it to every connected panel.
//
// Topology:
//   panel  ──port(PANEL_PORT)──▶ SW : commands (start/stop record, takeover/return, config)
//   panel  ◀──port─────────────  SW : Status pushes
//   content ──runtime.sendMessage▶ SW : recorder events  → ws.send → /v1/stream
//   SW ──tabs.sendMessage────────▶ content : record-control (start/stop)
import {
  DEFAULT_CONTROL_API_URL,
  PANEL_PORT,
  STORAGE_KEYS,
  emptyStatus,
  type Config,
  type ContentMessage,
  type DriveState,
  type PanelCommand,
  type PanelMessage,
  type RecordControl,
  type Status,
} from '../shared/protocol.js';
import { createWsClient } from './ws-client.js';
import { createTakeover } from './takeover.js';
import { ensureRecorder } from './ensure-recorder.js';

const status: Status = emptyStatus();
const panels = new Set<chrome.runtime.Port>();
let recordingTabId: number | null = null;
let recordingSession: string | null = null; // the server session id for the current recording (fragmentation guard)

function broadcast(): void {
  const msg: PanelMessage = { kind: 'status', status };
  for (const port of panels) {
    try {
      port.postMessage(msg);
    } catch {
      panels.delete(port);
    }
  }
}

const ws = createWsClient({
  onConnection(state, detail) {
    status.connection = state;
    if (detail?.session !== undefined && detail.session !== null) {
      // A reconnect mid-recording mints a NEW server session (ws.go newRunID per connection): the earlier
      // events land in a separate runs/record-<session> dir. Surface that so the fragmentation isn't silent.
      if (status.recording && recordingSession && recordingSession !== detail.session) {
        status.error = 'reconnected as a NEW recording session — earlier events are in a separate file';
      }
      recordingSession = detail.session;
      status.session = detail.session;
    }
    if (detail?.error !== undefined) status.error = detail.error;
    if (state === 'disconnected') status.session = null;
    broadcast();
  },
  onServerMessage() {
    // Acks/greeting already update connection/session via onConnection. Future: react to state-sync (R3).
  },
});

const takeover = createTakeover({
  sendSignal(signal) {
    return ws.sendSignal(signal);
  },
  onDrive(drive: DriveState, error) {
    status.drive = drive;
    if (error !== undefined) status.error = error;
    broadcast();
  },
});

async function loadConfig(): Promise<Config> {
  const got = await chrome.storage.local.get([STORAGE_KEYS.controlApiUrl, STORAGE_KEYS.bearerToken]);
  return {
    controlApiUrl: (got[STORAGE_KEYS.controlApiUrl] as string) || DEFAULT_CONTROL_API_URL,
    bearerToken: (got[STORAGE_KEYS.bearerToken] as string) || '',
  };
}

function tellTab(tabId: number, msg: RecordControl): void {
  void chrome.tabs.sendMessage(tabId, msg).catch(() => {
    /* no recorder in that tab yet — recorder-ready will resync */
  });
}

/** Inject the recorder bundle into a tab (idempotent — the content guard re-syncs on re-injection).
 *
 * TWO worlds, and the split is not a preference (ADR-138). The recorder runs ISOLATED (the default),
 * which is what lets it use `chrome.runtime`. The route journal must run in the page's own world:
 * measured in Chromium, a `history.pushState` patched from ISOLATED never sees the page's own call —
 * the page keeps the native function — so a journal in this world would report zero routes forever
 * while every gate stayed green. Both scripts carry their own idempotency latch. */
async function injectRecorder(tabId: number): Promise<void> {
  await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
  await chrome.scripting.executeScript({ target: { tabId }, files: ['route-journal.js'], world: 'MAIN' });
}

async function startRecording(tabId: number): Promise<void> {
  const config = await loadConfig();
  if (!config.bearerToken) {
    status.error = 'no bearer token — set it in Config';
    broadcast();
    return;
  }
  // If another tab was recording, stop it first — one recording at a time (avoids its events leaking
  // into this session over a shared SW state).
  if (recordingTabId !== null && recordingTabId !== tabId) {
    tellTab(recordingTabId, { kind: 'record-control', recording: false });
  }
  status.events = 0;
  status.error = null;
  status.recording = true;
  recordingTabId = tabId;
  recordingSession = null;
  broadcast();

  ws.connect(config.controlApiUrl, config.bearerToken);

  try {
    await injectRecorder(tabId);
  } catch (e) {
    status.error = `inject failed: ${e instanceof Error ? e.message : String(e)}`;
    status.recording = false;
    recordingTabId = null;
    ws.close();
    broadcast();
    return;
  }
  tellTab(tabId, { kind: 'record-control', recording: true });
}

function stopRecording(): void {
  status.recording = false;
  if (recordingTabId !== null) tellTab(recordingTabId, { kind: 'record-control', recording: false });
  recordingTabId = null;
  recordingSession = null;
  ws.close();
  broadcast();
}

// A full-page navigation destroys the injected recorder (the page's JS context is replaced). Re-inject on
// the recording tab after each top-frame load so multi-page flows keep capturing (the bridge expects them).
//
// ⚠ WHAT THIS LISTENER DOES NOT DO, MEASURED (ADR-138). The registry entry
// [RECORDER-BLIND-TO-PUSHSTATE] read this filter as the cause of the recorder missing SPA route
// changes, on the theory that `pushState` never reaches `status: 'complete'`. It does. Measured on
// Chrome 151 and Chromium 150: EVERY route change without a load — pushState, replaceState, a bare
// `location.hash = …`, and history.back — raises a PAIR, `{status:'loading', url:<new>}` then
// `{status:'complete'}` (which carries no url), so this filter passes and injects on every one of
// them; a burst of three pushState calls injects three times. And the recorder does not need it to:
// pushState does not replace the JS context, so the content script survives and keeps recording.
// The real blindness was in the PROTOCOL — there was no line shape for "the address changed" — and
// that is what the route journal above fixes. Re-injection here remains correct for the case it was
// written for: a full document load, which really does destroy the recorder.
//
// ⚠ ADR-142 ACTS ON THAT MEASUREMENT. The paragraph above has described this waste since ADR-138 —
// "a burst of three pushState calls injects three times" — and the code kept doing it, because the
// comment was corrected and the behaviour was not. The event cannot tell the two cases apart
// (`{status:'complete'}` carries no url), so the tab is ASKED instead: a live content script answers
// `tabs.sendMessage`, a replaced document does not. See ensure-recorder.ts for why the ping message
// is the very `record-control` this listener used to send AFTER injecting.
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status !== 'complete' || !status.recording || tabId !== recordingTabId) return;
  void ensureRecorder(tabId, {
    ping: (id) => chrome.tabs.sendMessage(id, { kind: 'record-control', recording: true }),
    inject: (id) => injectRecorder(id),
  });
});

async function handlePanelCommand(cmd: PanelCommand): Promise<void> {
  switch (cmd.kind) {
    case 'start-record':
      await startRecording(cmd.tabId);
      break;
    case 'stop-record':
      stopRecording();
      break;
    case 'takeover':
      await takeover.takeover(cmd.tabId);
      break;
    case 'return':
      await takeover.return();
      break;
    case 'set-config':
      await chrome.storage.local.set({
        [STORAGE_KEYS.controlApiUrl]: cmd.config.controlApiUrl,
        [STORAGE_KEYS.bearerToken]: cmd.config.bearerToken,
      });
      break;
    case 'get-status':
      broadcast();
      break;
  }
}

// Panel port: long-lived, keeps the SW alive during a session and carries commands + status pushes.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== PANEL_PORT) return;
  panels.add(port);
  port.postMessage({ kind: 'status', status } satisfies PanelMessage);
  port.onMessage.addListener((cmd: PanelCommand) => void handlePanelCommand(cmd));
  port.onDisconnect.addListener(() => panels.delete(port));
});

// Content-script messages (one-shot).
chrome.runtime.onMessage.addListener((msg: ContentMessage, sender) => {
  if (msg?.kind === 'recorder-event') {
    // Only accept events from the tab we're recording — a stale recorder in another tab must not leak
    // its events into this session.
    if (!status.recording || sender.tab?.id !== recordingTabId) return;
    const ok = ws.send(msg.event);
    if (ok) status.events++;
    else status.error = 'event dropped (socket not open)';
    broadcast();
  } else if (msg?.kind === 'recorder-warning') {
    // ADR-138: a degradation with no other symptom (the route journal in the wrong world records
    // nothing, silently). Only from the tab being recorded, same rule as events.
    if (status.recording && sender.tab?.id === recordingTabId) {
      status.error = msg.text;
      broadcast();
    }
  } else if (msg?.kind === 'recorder-ready') {
    // (Re)injected recorder announces itself — resync its recording state.
    const tabId = sender.tab?.id;
    if (tabId !== undefined && tabId === recordingTabId) {
      tellTab(tabId, { kind: 'record-control', recording: status.recording });
    }
  }
});
