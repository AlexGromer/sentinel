# Sentinel browser extension (MV3)

Live action **recorder** + co-pilot **takeover/return** for Sentinel (milestone M9.8). Records what you
click and type into a replayable scenario, and lets you hand control of a live session between the agent
and yourself.

Like `frontend/`, this is a **dev tool**: it needs `npm install` and it is **not air-gapped**. The vanilla
`docs/*` UI stays the primary, offline interface.

It **is** gated in CI, since PERCEPT-RECORDER-SHADOW: the `build` job runs `npm test` here (`tsc --noEmit`
plus the jsdom unit tests), and `tests/test_recorder_shadow_offline.py` loads the real content-script
recorder into jsdom over `test/e2e/shadow-fixture.html` and grounds what it emits through the real
`brain/record_bridge.py`. Before that the extension was named by no workflow at all, so a change to
`src/content/recorder.ts` could not turn anything red — the only recorder check in CI replays a transcript
frozen inside the test file and passes a fix and a regression identically.

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

## Shadow DOM

A click inside a web component arrives at a document-level listener with `event.target` **retargeted to
the host** (DOM §2.10), so a recorder that trusts `e.target` writes down `<x-color-picker>` where you
pressed the button inside it — while the executor, whose CSS and role engines pierce open roots, could
have driven that button perfectly well. Three consequences, all handled in `src/content/`:

- **Targets come from `composedPath()`**, not `e.target`. The nearest-interactive climb crosses the
  boundary only when the component's own tree offers nothing, so a component with inert internals and a
  role on its host still records as its host.
- **`change` and `submit` are not composed** — they never reach the document from inside a root at all.
  The recorder also listens on the roots it learned about from the composed events it did see; the set is
  derived, never a list.
- **CSS candidates are host-prefixed** (`#picker > #swatch`; Playwright pierces both combinators), and
  **no xpath candidate is emitted** for a shadow node — Playwright's xpath engine is a bare
  `document.evaluate` with no shadow expansion, so such a locator could only resolve to nothing or to the
  wrong element.

**A closed root is a boundary, not a debt.** Its nodes are absent from `composedPath` and `host.shadowRoot`
is `null` for everybody. The recorder records the host — the only honest answer — rather than pretending
to have seen inside.

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
test/e2e/                recorder.e2e.mjs (live Chromium, dev-only) · login-fixture.html
                         shadow-fixture.html (open/closed roots; actions declared as data-record)
test/record-in-jsdom.mjs offline driver: the real recorder in jsdom → RecorderEvents as JSON
```
