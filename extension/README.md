# Sentinel browser extension (MV3)

Live action **recorder** + co-pilot **takeover/return** for Sentinel (milestone M9.8). Records what you
click and type into a replayable scenario, and lets you hand control of a live session between the agent
and yourself.

Like `frontend/`, this is a **dev tool**: it needs `npm install`, it is **not air-gapped** and **not built
in CI**. The vanilla `docs/*` UI stays the primary, offline interface.

## Build

```sh
npm ci          # reproducible install (package-lock.json is committed)
npm run build   # → dist/  (load this as an unpacked extension)
npm run watch   # rebuild on change
npm test        # tsc --noEmit + jsdom unit tests (selector + redaction)
```

Load `dist/` via `chrome://extensions` → Developer mode → **Load unpacked**.

## Use

1. Open DevTools on the page you want to record → the **Sentinel** panel.
2. **Config**: set the control-API URL and bearer token (kept in `chrome.storage`, never committed).
3. **Start recording** → click/type on the page → **Stop**. Events stream over the WebSocket to
   `control-api` `/v1/stream` and land in `runs/record-<session>/events.ndjson`.
4. Turn that into a scenario: `python -m brain.record_bridge runs/record-<session>/events.ndjson out.json`.
5. **Take over / Return**: attach the debugger (Chrome shows a banner) to drive the tab yourself, then hand
   it back to the agent.

## How it talks to Sentinel

- **Transport** — the service worker opens a WebSocket to `${CONTROL_API_URL}/v1/stream` (ADR-043). A
  browser WebSocket can't set `Authorization`, so the token rides as a subprotocol: the client offers
  `["sentinel.recorder.v1", "bearer.<token>"]`; the server validates the bearer (constant-time) and echoes
  back only `sentinel.recorder.v1` — the token is never reflected.
- **One event per text frame** — the server does not reassemble fragments; each captured action is one
  JSON line.
- **Recorder → scenario** — `brain/record_bridge.py` grounds the events through the same emitter the
  goal/describe heads use (M9.2b reuse) — real selectors only, never fabricated.

## Redaction (mandatory)

Password (`type=password`, `autocomplete=current/new-password`), fields marked `data-sentinel-secret`, and
common secret fields (cvv/otp/…) are **never** recorded by value. The event carries a `secretRef` (env-var
name) instead, mirroring pw-executor's `secretRef`.

## Permissions

Minimal by design: `activeTab`, `storage`, `scripting`. `debugger` and per-origin host access are
**optional** and requested lazily, on the gesture that needs them (takeover / start recording) — never at
install.

**Caveat:** Chrome won't let two debuggers attach to one tab. If DevTools is already open on the tab you
take over, the attach fails and the panel surfaces the error (see `docs/THREAT_MODEL.md` ❾).

## Layout

```
manifest.json            MV3 manifest (minimal perms; debugger optional/lazy)
esbuild.mjs              build → dist/ (one IIFE per world + static assets)
public/                  devtools.html, panel.html (+ panel styles)
src/shared/protocol.ts   the contracts: event schema, WS handshake, messages, status
src/background/          service worker: index.ts (router) · ws-client.ts (#43) · takeover.ts (#47)
src/content/             recorder.ts (#44) · selectors.ts (candidates + redaction, unit-tested)
src/devtools/            devtools.ts (registers panel) · panel.ts (#45 UI)
```
