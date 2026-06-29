// Co-pilot takeover/return over chrome.debugger (#47, ADR-039). The agent ↔ human handoff on ONE live
// tab. Takeover attaches chrome.debugger (Chrome shows the "started debugging this browser" banner — the
// takeover is never hidden) and signals the agent to pause; return detaches and signals resume.
//
// chrome.debugger is requested LAZILY (optional permission, granted at the panel's takeover gesture — see
// panel.ts), never at install. We attach from the service worker so the panel can close without dropping
// the session.
//
// CAVEAT (documented in README + THREAT_MODEL ❾): if DevTools is already open on the SAME tab, Chrome
// won't let a second debugger attach — attach fails and we surface the error rather than entering a half
// state. The agent's own drive is pw-executor over CDP (M9.6/ADR-037); this module is the human side.
import type { DriveState, TakeoverSignal } from '../shared/protocol.js';

const DEBUGGER_PROTOCOL = '1.3';

export interface TakeoverDeps {
  /** send a co-pilot signal over the WS; returns false if it couldn't be delivered (socket not open). */
  sendSignal(signal: TakeoverSignal): boolean;
  onDrive(drive: DriveState, error?: string | null): void;
}

export interface Takeover {
  takeover(tabId: number): Promise<void>;
  return(): Promise<void>;
  isActive(): boolean;
  attachedTab(): number | null;
}

export function createTakeover(deps: TakeoverDeps): Takeover {
  let attached: number | null = null;
  let transitioning = false;

  // A prior SW instance may have been evicted while a debugger was still attached (its in-memory `attached`
  // is gone, but Chrome keeps the attachment) — best-effort detach any leftover page attachment on startup
  // so the user isn't stranded in a half-state they can't Return from. detach() on a target we didn't
  // attach throws and is ignored.
  void chrome.debugger.getTargets().then((targets) => {
    for (const t of targets) {
      if (t.attached && t.tabId !== undefined) {
        void chrome.debugger.detach({ tabId: t.tabId }).catch(() => {});
      }
    }
  }).catch(() => {});

  // An external detach (user clicks "Cancel" on the banner, tab closes) must not leave us thinking we
  // still drive. Reconcile state and tell the agent to resume.
  chrome.debugger.onDetach.addListener((source) => {
    if (attached !== null && source.tabId === attached) {
      attached = null;
      deps.sendSignal({ type: 'return' });
      deps.onDrive('agent');
    }
  });

  return {
    async takeover(tabId) {
      if (transitioning) return;
      if (attached !== null) {
        deps.onDrive('human', 'already driving (take over is active)');
        return;
      }
      transitioning = true;
      try {
        await chrome.debugger.attach({ tabId }, DEBUGGER_PROTOCOL);
        attached = tabId;
        const notified = deps.sendSignal({ type: 'takeover' }); // tell the agent to pause
        // The human now drives regardless, but warn if the agent couldn't be told (it may keep driving).
        deps.onDrive('human', notified ? null : 'took over, but the agent was not notified (socket closed)');
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        // Most common: "Another debugger is already attached" (DevTools open on this tab).
        deps.onDrive('agent', `takeover failed: ${msg}`);
      } finally {
        transitioning = false;
      }
    },

    async return() {
      if (transitioning || attached === null) return;
      transitioning = true;
      const tabId = attached;
      try {
        await chrome.debugger.detach({ tabId });
      } catch {
        // Already detached (tab closed / external) — fall through to clean up state.
      } finally {
        attached = null;
        const notified = deps.sendSignal({ type: 'return' }); // tell the agent to resume
        deps.onDrive('agent', notified ? null : 'returned, but the agent was not notified (socket closed)');
        transitioning = false;
      }
    },

    isActive() {
      return attached !== null;
    },
    attachedTab() {
      return attached;
    },
  };
}
