# Sentinel Co-pilot (AG-UI / CopilotKit front) — `frontend/`

> 🧊 **FROZEN — non-maintained reference (ADR-055, M14).** The sovereign single UI is now the in-house
> **vanilla AG-UI co-pilot** in `docs/index.html` (live AG-UI timeline, `hitl_needed`
> takeover/return, scenario/test library, conversation management — all air-gapped, zero-dep, `file://`-safe).
> ADR-055 dropped CopilotKit from the delivery path: an npm/React + Node-runtime build-toolchain is
> structurally incompatible with the "download a release → run offline" sovereignty bar (ADR-049/053), and
> maintaining parity across two UIs is pure tax. This scaffold is kept as a **non-maintained reference** (not
> deleted, not updated) — it also removes GAP-SEC-002 (the npm supply-chain surface) from the delivery path.
> If you need the current co-pilot: open `docs/index.html` and pick a view from the rail on the left. The `Settings | Tests` tab pair described above was replaced by the rail in **ADR-066**; views are addressable by hash — `#v=chat` to author a run, `#v=live` for the live AG-UI timeline and `hitl_needed` takeover/return, `#v=library` for the scenario/test library and conversations. The rail is the list; this line names examples, not all of it.

A **rich co-pilot** front for Sentinel, built on **CopilotKit** + the **AG-UI** protocol, driving Sentinel through the OpenAI-compat shim (`POST /v1/chat/completions`, ADR-041). This is the "rich front on top" deferred from M12 (ADR-041) and scoped here as **ADR-044** — a runnable **skeleton**, not a finished product.

> ⚠ **DEV-ONLY.** This is the first npm-built front in the repo. It is **not air-gapped**, **not served by GitHub Pages**, and **not built or tested in CI** (unlike the vanilla `docs/index.html` / `docs/chat/` / `docs/setup/` consoles, which stay the air-gapped path). It needs `npm install` (network). The air-gapped vanilla consoles remain the offline fallback.

## Stack

| Piece | Package | Role |
|------|---------|------|
| Frontend | `@copilotkit/react-core`, `@copilotkit/react-ui` | `<CopilotKit>` provider + `<CopilotChat>` UI |
| Runtime | `@copilotkit/runtime` | self-hosted Copilot Runtime endpoint (`/api/copilotkit`) |
| Model bridge | `ai` (Vercel AI SDK) + `@ai-sdk/openai` | `createOpenAI({ baseURL })` → the Sentinel shim |
| App shell | `next` (App Router), `react` 19 | dev server + the runtime route |

Versions in `package.json` were verified against the npm registry on **2026-06-28**. CopilotKit's runtime is at **v2** and evolving — **re-confirm the exact exports** (`BuiltInAgent`, `copilotRuntimeNextJSAppRouterEndpoint`, `convertMessagesToVercelAISDKMessages`) against the current docs before installing: <https://docs.copilotkit.ai/backend/copilot-runtime> and <https://docs.copilotkit.ai/backend/custom-agent>.

## Run

```bash
cd frontend
cp .env.example .env.local          # set CONTROL_API_URL + CONTROL_API_TOKEN
npm install                         # NOT air-gapped — pulls from the npm registry
npm run dev                         # http://localhost:3000
```

You also need a running **control-api** with a token and this origin in its CORS allowlist:

```bash
CONTROL_API_TOKEN=secret \
CONTROL_API_CORS_ORIGINS=http://localhost:3000 \
  ./bin/control-api          # binds 127.0.0.1:8090 (ADR-032)
```

In the chat, include a `target:` URL and an instruction, e.g.:

```
describe: log in as a standard user and open the dashboard
target: https://app.example
```

One chat turn → one Sentinel run (`describe` / `goal` / `explore` via `SENTINEL_MODEL` or the `goal:`/`explore:` prefix). The bearer token stays server-side in the Copilot Runtime — it is never shipped to the browser.

## How it connects

```
CopilotChat (browser) ──▶ Copilot Runtime (/api/copilotkit, server)
                              └─ @ai-sdk/openai createOpenAI({ baseURL: $CONTROL_API_URL/v1 })
                                    └─▶ Sentinel shim  POST /v1/chat/completions  (ADR-041)
                                          └─▶ one agentctl run → verdict
```

## Roadmap (historical — superseded by ADR-055)

- **Was next (M9.8-impl):** richer AG-UI events (tool calls, run progress, state) over the control-API
  **WebSocket** `/v1/stream` (ADR-043) once the MV3 recorder + takeover/return landed — the duplex channel
  this scaffold was meant to graduate onto.
- **What actually happened (M14, ADR-055):** that live AG-UI timeline + `hitl_needed` takeover/return banner
  shipped in the vanilla `docs/index.html` co-pilot instead (Tests → Live), not here. This scaffold is frozen
  at the "Now" line above and will not receive the WS graduation described in the previous paragraph.
