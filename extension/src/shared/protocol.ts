// Shared contracts for the Sentinel MV3 extension (M9.8). This is the single source of truth that the
// service worker, content script, and DevTools panel all import — keep it dependency-free (DOM types only,
// no chrome.* usage) so it stays trivially testable and bundlable into every world.
//
// Two external contracts are pinned here and MUST NOT drift:
//   1. RecorderEvent — the NDJSON line shape the recorder streams over /v1/stream. The server persists it
//      verbatim to runs/record-<session>/events.ndjson, and brain/record_bridge.py grounds it into a
//      scenario (M9.8 §2/§3, ADR-038). The field names below mirror that bridge's event schema exactly.
//   2. The WebSocket subprotocol handshake — control-api cmd/control-api/ws.go (ADR-043) validates the
//      bearer token from the Sec-WebSocket-Protocol list and echoes back only WS_SUBPROTOCOL.

// ---------------------------------------------------------------------------------------------------
// 1. Recorder event schema (the record_bridge.py contract)
// ---------------------------------------------------------------------------------------------------

/** A pw-executor locator (server.ts buildLocator): exactly one strategy key is meaningful per object,
 * except role+name which travel together. Mirrors LocatorSpec in pw-executor/src/server.ts. */
export interface Locator {
  testid?: string;
  role?: string;
  name?: string;
  label?: string;
  text?: string;
  css?: string;
  xpath?: string;
}

/** Locator strategy names, ranked by trust (brain/strategies.PRIORS). `role_name` is the role+name pair. */
export type Strategy = 'testid' | 'role_name' | 'label' | 'text' | 'css' | 'xpath';

/** One ranked candidate: a named strategy plus the locator that realises it. */
export interface SelectorCandidate {
  strategy: Strategy;
  locator: Locator;
}

/** DOM event types the recorder captures. `submit` is captured but dropped by the bridge (the submit
 * control's own click already fires) — we still emit it so the bridge, not the recorder, owns that policy. */
export type RecorderEventType = 'click' | 'input' | 'change' | 'submit';

/** One recorded action. Exactly the line shape brain/record_bridge.py consumes:
 *   { type, url, selectorCandidates: [{strategy, locator}, …], value?, secretRef?, verb? }
 * `value` is present ONLY for non-secret inputs. A redacted secret carries `secretRef` (the env-var
 * name), never the literal value — redaction happens here, at record time (M9.8 §2, mandatory). */
export interface RecorderEvent {
  type: RecorderEventType;
  url: string;
  selectorCandidates: SelectorCandidate[];
  value?: string;
  secretRef?: string;
  /** explicit verb override (e.g. 'select' for a <select>, 'press' for an Enter-submit); else the DOM
   * type maps to a verb in record_bridge. */
  verb?: string;
  /** key for a 'press' verb (e.g. 'Enter'); record_bridge routes it to the press step. */
  key?: string;
}

/** How the address changed. `pop` covers Back/Forward AND a bare `location.hash = …` — measured in
 * Chromium (ADR-135) and again here: both raise `popstate` first, so a separate `hashchange`
 * listener never wins. */
export type RouteHow = 'push' | 'replace' | 'pop';

/** A route change the page made WITHOUT a document load — the fact the recorder was blind to.
 *
 * It is NOT a `RecorderEvent`: there is no element and no verb, and `selectorCandidates` is required
 * on that type for a reason (`_resolve_locator` drops anything without one). A fifth member of
 * `RecorderEventType` would have forced the field optional for everybody and traded that guarantee
 * away for a value that is always empty here. */
export interface RecorderRouteEvent {
  type: 'route';
  url: string;
  how: RouteHow;
}

/** Everything the recorder puts on the wire, in OBSERVED order. `seq` is stamped in the recorder's
 * single funnel, so "file order == observation order" is a checkable claim rather than an
 * assumption — and a gap in it makes the already-silent drop at index.ts (`event dropped (socket not
 * open)`) visible instead of looking like a person who did nothing. */
export type RecorderLine = (RecorderEvent | RecorderRouteEvent) & { seq?: number };

// ---------------------------------------------------------------------------------------------------
// 2. WebSocket transport (control-api /v1/stream, ADR-043)
// ---------------------------------------------------------------------------------------------------

/** The non-secret subprotocol the server echoes back; assert ws.protocol === this after connect. */
export const WS_SUBPROTOCOL = 'sentinel.recorder.v1';

/** Token rides as a second offered subprotocol: `bearer.<token>` (a browser WebSocket can't set a header). */
export const WS_BEARER_PREFIX = 'bearer.';

/** Build the subprotocol list for `new WebSocket(url, …)`. The server validates the bearer entry
 * (constant-time) and echoes back only WS_SUBPROTOCOL — the token is never reflected. */
export function wsSubprotocols(token: string): [string, string] {
  return [WS_SUBPROTOCOL, WS_BEARER_PREFIX + token];
}

/** Server→client greeting sent once on connect: the recorder session id used for the runs/ dir. */
export interface ServerGreeting {
  type: 'session';
  session: string;
}

/** Server→client ack after each ingested event (n = running count). */
export interface ServerAck {
  type: 'ack';
  n: number;
}

/** Co-pilot signals (ADR-039) ride the same duplex socket. The recorder ingest path ignores any frame
 * that isn't a recorder event; these are the control frames for takeover/return/state-sync. */
export interface TakeoverSignal {
  type: 'takeover' | 'return' | 'state-sync';
  /** opaque state delta payload for state-sync; absent for takeover/return. */
  state?: unknown;
}

export type ServerMessage = ServerGreeting | ServerAck | TakeoverSignal;

// ---------------------------------------------------------------------------------------------------
// 3. chrome.runtime message protocol (panel ⇆ service worker ⇆ content script)
// ---------------------------------------------------------------------------------------------------

/** Connection lifecycle of the SW's WebSocket to the control-api. */
export type Connection = 'disconnected' | 'connecting' | 'connected' | 'error';

/** Who is driving the live session (ADR-039). `idle` = no agent run in progress. */
export type DriveState = 'idle' | 'agent' | 'human';

/** Snapshot the SW broadcasts to the panel after every state change. */
export interface Status {
  connection: Connection;
  session: string | null;
  recording: boolean;
  events: number;
  drive: DriveState;
  /** last human-readable error (auth failure, attach conflict, …); null when healthy. */
  error: string | null;
}

export function emptyStatus(): Status {
  return { connection: 'disconnected', session: null, recording: false, events: 0, drive: 'idle', error: null };
}

/** Persisted config (chrome.storage.local). Never hardcoded or committed — entered in the panel. */
export interface Config {
  controlApiUrl: string;
  bearerToken: string;
}

export const STORAGE_KEYS = { controlApiUrl: 'controlApiUrl', bearerToken: 'bearerToken' } as const;
export const DEFAULT_CONTROL_API_URL = 'http://127.0.0.1:8090';

/** Port name the panel uses for its long-lived connection to the SW (keeps the SW alive during a session). */
export const PANEL_PORT = 'sentinel.panel';

// Panel → service worker commands.
export type PanelCommand =
  | { kind: 'start-record'; tabId: number; origin: string }
  | { kind: 'stop-record' }
  | { kind: 'takeover'; tabId: number }
  | { kind: 'return' }
  | { kind: 'get-status' }
  | { kind: 'set-config'; config: Config };

// Service worker → panel messages (over the panel port).
export type PanelMessage = { kind: 'status'; status: Status };

// Content script → service worker (one-shot runtime messages).
export type ContentMessage =
  | { kind: 'recorder-event'; event: RecorderLine }
  | { kind: 'recorder-ready' }
  /** A degradation the person needs to see while recording (ADR-138) — surfaced as `status.error`
   * rather than swallowed, because the failure it reports produces no other symptom at all. */
  | { kind: 'recorder-warning'; text: string };

// ---------------------------------------------------------------------------------------------------
// 4. MAIN-world route journal → ISOLATED content script (window.postMessage)
// ---------------------------------------------------------------------------------------------------

/** Envelope name for the one message the page-world journal posts. Checked together with
 * `event.source === window` — measured true for a MAIN→ISOLATED post in Chromium; without both
 * checks any page script could inject routes. (A page can already forge a `click` via
 * `dispatchEvent`, which the capture-phase listener records, so this is not a new class of
 * exposure — but the guard is cheap and its absence would widen it.) */
export const ROUTE_MSG = 'sentinel.route.v1';

/** What the MAIN-world journal posts out. `main:false` means the journal landed in the ISOLATED
 * world by mistake, where the page's own `history.pushState` is invisible to it — the one failure
 * of this feature that would otherwise be perfectly silent (zero routes forever, every gate green). */
export interface RouteMessage {
  __sentinel: typeof ROUTE_MSG;
  url: string;
  how: RouteHow;
  main: boolean;
}

// Service worker → content script (response / push).
export type RecordControl = { kind: 'record-control'; recording: boolean };

/** Discriminated envelope for everything flowing through chrome.runtime.onMessage. */
export type RuntimeMessage = PanelCommand | ContentMessage;
