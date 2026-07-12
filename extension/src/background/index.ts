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

/** Inject the recorder bundle into a tab (idempotent — the content guard re-syncs on re-injection). */
async function injectRecorder(tabId: number): Promise<void> {
  await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
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
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status !== 'complete' || !status.recording || tabId !== recordingTabId) return;
  injectRecorder(tabId)
    .then(() => tellTab(tabId, { kind: 'record-control', recording: true }))
    .catch(() => {
      /* tab closed or not scriptable — recorder-ready (if any) will resync */
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
  } else if (msg?.kind === 'recorder-ready') {
    // (Re)injected recorder announces itself — resync its recording state.
    const tabId = sender.tab?.id;
    if (tabId !== undefined && tabId === recordingTabId) {
      tellTab(tabId, { kind: 'record-control', recording: status.recording });
    }
  }
});
