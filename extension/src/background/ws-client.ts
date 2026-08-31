// Service-worker WebSocket client → control-api GET /v1/stream (#43, ADR-043). The browser WebSocket does
// the RFC6455 framing; our job is the subprotocol auth handshake, one-event-per-text-frame streaming,
// greeting/ack handling, and reconnect-with-backoff.
//
// AUTH: a browser WebSocket can't set Authorization, so the token rides as a second offered subprotocol
// `bearer.<token>`. The server validates it (constant-time) and echoes back ONLY `sentinel.recorder.v1`,
// so we assert ws.protocol === WS_SUBPROTOCOL after open (the token is never reflected).
//
// KEEPALIVE: intentionally none. Every text frame the server receives is ingested as a recorder event
// (ws.go streamRecord), so an app-level keepalive frame would corrupt events.ndjson, and the browser
// WebSocket API can't send raw RFC6455 ping frames. We rely on active recording (events reset the
// server's 5-min idle timer) and reconnect on close.
import {
  WS_SUBPROTOCOL,
  wsSubprotocols,
  type Connection,
  type RecorderLine,
  type ServerMessage,
  type TakeoverSignal,
} from '../shared/protocol.js';

export interface WsClientCallbacks {
  onConnection(state: Connection, detail?: { session?: string; error?: string }): void;
  onServerMessage(msg: ServerMessage): void;
}

export interface WsClient {
  connect(url: string, token: string): void;
  send(event: RecorderLine): boolean;
  sendSignal(signal: TakeoverSignal): boolean;
  close(): void;
  isOpen(): boolean;
}

const MAX_RETRIES = 6;
const BASE_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 15_000;
const STABLE_MS = 5_000; // a socket must stay open this long before we treat it as "stable" and reset backoff

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);

/** Normalise an http(s) control-api base into a ws(s) /v1/stream URL. */
function streamUrl(base: string): string {
  const u = new URL('/v1/stream', base);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  return u.toString();
}

export function createWsClient(cb: WsClientCallbacks): WsClient {
  let ws: WebSocket | null = null;
  let url = '';
  let token = '';
  let retries = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  function clearRetry() {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  }

  // Detach a socket we no longer care about so its async onclose/onerror can't drive our state machine.
  function detach(sock: WebSocket) {
    sock.onopen = sock.onmessage = sock.onerror = sock.onclose = null;
    if (ws === sock) ws = null;
  }

  function scheduleReconnect() {
    if (retries >= MAX_RETRIES) {
      cb.onConnection('error', { error: `giving up after ${MAX_RETRIES} attempts (check URL / token)` });
      return;
    }
    const delay = Math.min(BASE_BACKOFF_MS * 2 ** retries, MAX_BACKOFF_MS);
    retries++;
    clearRetry();
    retryTimer = setTimeout(open, delay);
  }

  function open() {
    clearRetry();
    const target = new URL(streamUrl(url));
    // Never send the bearer token in cleartext to a non-loopback host. ws:// to a remote control-api would
    // expose it on the wire; require https/wss there. Loopback (the default 127.0.0.1) is fine.
    if (target.protocol === 'ws:' && !LOOPBACK.has(target.hostname)) {
      cb.onConnection('error', { error: 'refusing plaintext ws:// to a non-loopback host — use https/wss' });
      return; // do NOT reconnect: this is a config error, not a transient failure
    }

    let sock: WebSocket;
    try {
      sock = new WebSocket(target.toString(), wsSubprotocols(token));
    } catch {
      // A token with chars invalid in a WS subprotocol (e.g. spaces) makes the constructor throw; the
      // exception message embeds the offending subprotocol — so NEVER surface it (it carries the token).
      cb.onConnection('error', { error: 'failed to open websocket (token has characters invalid for the WS subprotocol?)' });
      return; // do NOT reconnect: re-trying the same bad token loops forever
    }
    ws = sock;
    cb.onConnection('connecting');

    let stableTimer: ReturnType<typeof setTimeout> | null = null;
    sock.onopen = () => {
      // The server echoes ONLY the non-secret subprotocol. This guards against a benign server that doesn't
      // speak our protocol — it is NOT server authentication (a MITM can echo the constant); TLS (wss) is.
      if (sock.protocol !== WS_SUBPROTOCOL) {
        cb.onConnection('error', { error: `unexpected subprotocol "${sock.protocol}"` });
        detach(sock);
        sock.close();
        return;
      }
      // Reset backoff only after the socket proves stable — resetting in onopen lets a server that 101s
      // then immediately drops pin us in a tight reconnect loop that never reaches MAX_RETRIES.
      stableTimer = setTimeout(() => {
        retries = 0;
      }, STABLE_MS);
    };

    sock.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      let msg: ServerMessage;
      try {
        msg = JSON.parse(ev.data) as ServerMessage;
      } catch {
        return; // ignore non-JSON server frames
      }
      if (!msg || typeof msg !== 'object') return; // a literal null/primitive frame is not a message
      if (msg.type === 'session') cb.onConnection('connected', { session: msg.session });
      cb.onServerMessage(msg);
    };

    sock.onerror = () => {
      // The browser hides the HTTP status of a failed upgrade (403 looks like any close); surface generically.
      cb.onConnection('error', { error: 'websocket error (auth or network)' });
    };

    sock.onclose = () => {
      if (stableTimer) clearTimeout(stableTimer);
      if (ws !== sock) return; // superseded by a newer connect()/close() — ignore this straggler
      ws = null;
      cb.onConnection('disconnected');
      scheduleReconnect();
    };
  }

  function sendText(text: string): boolean {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(text);
      return true;
    }
    return false;
  }

  return {
    connect(nextUrl, nextToken) {
      url = nextUrl;
      token = nextToken;
      retries = 0;
      if (ws) {
        const old = ws; // drop the old socket cleanly — detach first so its async onclose won't fire our logic
        detach(old);
        old.close();
      }
      open();
    },
    send(event) {
      return sendText(JSON.stringify(event));
    },
    sendSignal(signal) {
      // Co-pilot signals share the duplex socket (ADR-039). Server-side demux is brain-side R3; until then
      // the bridge drops these frames as ungroundable, so they don't corrupt a recorded scenario.
      return sendText(JSON.stringify(signal));
    },
    close() {
      clearRetry();
      if (ws) {
        const old = ws;
        detach(old);
        old.close();
      }
      cb.onConnection('disconnected');
    },
    isOpen() {
      return !!ws && ws.readyState === WebSocket.OPEN;
    },
  };
}
